import gzip
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import SUBTREE, Terminal, collapse, must_not_walk, status_phases

from trackdir import dbcli, live, sample
from trackdir.cli import main
from trackdir.db import (
    ConcurrentSample,
    DbError,
    OutOfOrderSample,
    SchemaVersionMismatch,
    TreeKey,
    tree_key_of,
)
from trackdir.dbcli import describe_tree
from trackdir.messages import FatalError
from trackdir.model import ROOT, error_counts
from trackdir.progress import Progress
from trackdir.sample import (
    SampleOptions,
    SampleResult,
    render_sample_summary,
    replay_walk,
    run_sample,
)

UNREACHABLE = "postgresql://trackdir@127.0.0.1:1/x_test"


@pytest.fixture(autouse=True)
def no_dsn_in_environment(monkeypatch):
    """Keep the developer's TRACKDIR_DB out of the tests."""
    monkeypatch.delenv(dbcli.DSN_ENV, raising=False)


def without_children(probes):
    return [replace(p, children=None) for p in probes]


def test_replay_walk_at_capture_depth(subtree):
    header, probes = replay_walk(subtree, subtree.header.max_depth, max_dirs=10**6)
    assert without_children(probes) == without_children(subtree.probes.values())
    assert header == subtree.header


def test_replay_walk_with_a_should_list_reads_only_what_it_accepts(subtree):
    header, probes = replay_walk(
        subtree, 3, max_dirs=10**6, should_list=lambda probe: probe.path == ROOT
    )
    root = subtree.probes[ROOT]
    assert [p.path for p in probes] == [ROOT, *sorted(root.children)]
    assert probes[0] == root
    assert all(p.children is None for p in probes[1:])
    assert header.max_depth == 3
    assert header.dirs == len(probes)
    assert header.errors == error_counts(probes).total()


def test_dir_limit_message_says_the_tree_stays_the_same(subtree):
    with pytest.raises(FatalError) as exc:
        replay_walk(subtree, 2, max_dirs=1)
    message = collapse(str(exc.value))
    assert "Raise --max-dirs" in message and "the tree stays the same" in message


def test_replay_walk_at_smaller_depth(subtree):
    header, probes = replay_walk(subtree, 2, max_dirs=10**6)
    assert probes
    assert all(p.depth <= 2 for p in probes)
    assert header.max_depth == 2
    assert header.dirs == len(probes) < subtree.header.dirs
    assert header.errors == error_counts(probes).total()
    assert replace(header, max_depth=4, dirs=0, errors=0) == replace(
        subtree.header, dirs=0, errors=0
    )


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (["sample", "r", "--replay", "c", "--db", "x"], "ROOT or --replay"),
        (["sample", "--db", "x"], "ROOT or --replay"),
        (["sample", "--replay", "c", "--jobs", "2", "--db", "x"], "--jobs applies"),
        (["sample", "r", "--db", "x"], "--max-depth is required"),
        (["sample", "r", "--max-depth", "1"], "no database given"),
        (["db", "init"], "no database given"),
        (["db"], "required"),
    ],
)
def test_usage_errors(args, problem, capsys):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    # After "error:", since the usage line names every option.
    assert problem in collapse(capsys.readouterr().err.split("error:", 1)[1])


def test_dsn_from_environment(monkeypatch, tree):
    monkeypatch.setenv("TRACKDIR_DB", "x")
    seen = []

    def fake_run_sample(opts, warn, progress):
        seen.append(opts)
        raise FatalError("stop")

    monkeypatch.setattr(sample, "run_sample", fake_run_sample)
    assert main(["sample", str(tree), "--max-depth", "1"]) == 1
    assert [o.dsn for o in seen] == ["x"]
    assert seen[0].jobs == live.DEFAULT_JOBS


def test_db_option_overrides_environment(monkeypatch):
    monkeypatch.setenv("TRACKDIR_DB", "x")
    seen = []
    monkeypatch.setattr(sample, "run_db_init", lambda dsn: seen.append(dsn) or True)
    assert main(["db", "init", "--db", "y"]) == 0
    assert seen == ["y"]


def test_interrupt_says_nothing_was_stored(monkeypatch, capsys):
    def interrupted(opts, warn, progress):
        raise KeyboardInterrupt

    monkeypatch.setattr(sample, "run_sample", interrupted)
    assert main(["sample", "--replay", "c", "--db", "x"]) == 130
    assert collapse(capsys.readouterr().err) == (
        "ERROR: interrupted; nothing was stored."
    )


def test_psycopg_missing_message(monkeypatch, tree, fake_ceph, capsys):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setattr(live, "walk", must_not_walk)
    assert main(["sample", str(tree), "--max-depth", "1", "--db", "x"]) == 1
    assert "trackdir[postgres]" in collapse(capsys.readouterr().err)


def test_replay_deeper_than_capture_fails(capsys):
    assert (
        main(["sample", "--replay", str(SUBTREE), "--max-depth", "5", "--db", "x"]) == 1
    )
    err = collapse(capsys.readouterr().err)
    assert err.startswith("ERROR:")
    assert "depth" in err


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (gzip.compress(b'{"format":"x"}\n'), "not a trackdir capture"),
        (gzip.compress(b'{"format":"x"}\n')[:12], "Compressed file ended"),
        (b"plain text", "Not a gzipped file"),
        (None, "No such file"),
    ],
    ids=["not-a-capture", "truncated-gzip", "not-gzip", "missing"],
)
def test_replay_of_bad_capture_fails(tmp_path, capsys, content, detail):
    path = tmp_path / "bad.jsonl.gz"
    if content is not None:
        path.write_bytes(content)
    assert main(["sample", "--replay", str(path), "--db", "x"]) == 1
    err = collapse(capsys.readouterr().err)
    assert err.startswith(f"ERROR: cannot read the capture {path}: {detail}")
    assert err.count(str(path)) == 1


def result(**changes):
    fields = {
        "sample_id": 7,
        "root": "/mnt/cephfs/x",
        "tree": TreeKey("fsid", None, b"/x", 2),
        "max_depth": 2,
        "dirs": 5,
        "changed": 5,
        "deleted": 0,
        "skipped": 0,
        "errors": Counter({("open", "EACCES"): 2}),
        "walk_seconds": 1.25,
        "store_seconds": 0.5,
        "replayed_from": None,
        "kind": "initial",
    }
    return SampleResult(**(fields | changes))


def test_render_sample_summary():
    assert collapse(render_sample_summary(result())) == (
        "Stored sample 7 (initial): 5 directories under /mnt/cephfs/x to depth 2, "
        "tree (cluster fsid, default filesystem, path /x, depth 2); "
        "2 had errors: open EACCES 2. Walk 1.2 s, store 0.5 s."
    )
    incremental = result(
        tree=TreeKey("fsid", "cephfs", b"/x", 4),
        max_depth=4,
        dirs=1204,
        changed=18,
        deleted=2,
        skipped=31,
        errors=Counter(),
        walk_seconds=3.2,
        store_seconds=0.1,
        replayed_from="/c.gz",
        kind="incremental",
    )
    assert collapse(render_sample_summary(incremental)) == (
        "Stored sample 7 (incremental): probed 1204 directories under "
        "/mnt/cephfs/x to depth 4, tree (cluster fsid, filesystem cephfs, path /x, "
        "depth 4); 18 changed, 2 deleted, 31 unchanged subtrees "
        "skipped; none had errors. Walk 3.2 s, store 0.1 s. Replayed from /c.gz."
    )


def test_describe_tree_includes_depth():
    key = TreeKey("fsid", None, b"/caf\xe9", 3)
    assert describe_tree(key) == (
        "tree (cluster fsid, default filesystem, path /caf\\xe9, depth 3)"
    )


STARTED = datetime.fromisoformat("2026-10-15T00:00:00+00:00")
LATEST = datetime.fromisoformat("2026-10-16T00:00:00+00:00")


@pytest.mark.parametrize("fs_name", [None, "cephfs"])
@pytest.mark.parametrize(
    ("conflict", "text"),
    [
        (
            lambda key: ConcurrentSample(key),
            "another sample of the {tree} was stored while this one ran; nothing "
            "was stored. Run trackdir sample again.",
        ),
        (
            lambda key: OutOfOrderSample(key, STARTED, LATEST),
            "this sample started at 2026-10-15T00:00:00+00:00, which is not later "
            "than the latest sample of the {tree} (2026-10-16T00:00:00+00:00); "
            "nothing was stored. A sample must start later than the tree's latest "
            "sample: check the clocks of the hosts that sample this tree, and for a "
            "replay use a newer capture.",
        ),
    ],
    ids=["concurrent", "out-of-order"],
)
def test_conflicts_name_the_tree_as_the_summary_does(
    subtree, monkeypatch, fs_name, conflict, text
):
    key = replace(tree_key_of(subtree.header), fs_name=fs_name)
    monkeypatch.setattr(dbcli, "connect", fail_to_connect(conflict(key)))
    with pytest.raises(FatalError) as exc:
        run_sample(SampleOptions(dsn="x", replay=SUBTREE), print)
    tree = describe_tree(key)
    assert collapse(str(exc.value)) == text.format(tree=tree)
    assert tree in collapse(render_sample_summary(result(tree=key)))


def fail_to_connect(error):
    def connect(dsn):
        raise error

    return connect


@pytest.mark.parametrize(
    ("args", "outcome"),
    [
        (["db", "init", "--db", "x"], "The database was not changed."),
        (["sample", "--replay", str(SUBTREE), "--db", "x"], "Nothing was stored."),
    ],
)
def test_database_error_says_what_was_left(monkeypatch, capsys, args, outcome):
    monkeypatch.setattr(dbcli, "connect", fail_to_connect(DbError("boom")))
    assert main(args) == 1
    assert (
        collapse(capsys.readouterr().err) == f"ERROR: database error: boom. {outcome}"
    )


@pytest.mark.parametrize(
    ("found", "next_step"),
    [(1, "migrate it, or drop schema trackdir"), (None, "drop schema trackdir")],
)
def test_schema_version_mismatch_says_what_to_do(monkeypatch, capsys, found, next_step):
    error = SchemaVersionMismatch(found)
    monkeypatch.setattr(dbcli, "connect", fail_to_connect(error))
    assert main(["db", "init", "--db", "x"]) == 1
    err = collapse(capsys.readouterr().err)
    assert err.count("ERROR:") == 1
    assert next_step in err
    assert "deletes" in err
    assert err.endswith("trackdir db init")


def test_unreachable_database_fails_before_walking(
    tree, fake_ceph, monkeypatch, capsys
):
    pytest.importorskip("psycopg")
    monkeypatch.setattr(live, "walk", must_not_walk)
    status = main(["sample", str(tree), "--max-depth", "1", "--db", UNREACHABLE])
    assert status == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: database error")
    assert err.count("ERROR:") == 1
    assert "Traceback" not in err
    assert collapse(err).endswith("Nothing was stored.")


def test_db_init_cli(dsn, capsys):
    assert main(["db", "init", "--db", dsn]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("Created the trackdir schema")
    assert main(["db", "init", "--db", dsn]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert "already exists" in collapse(stderr)


def test_sample_without_schema_fails(dsn, capsys):
    assert main(["sample", "--replay", str(SUBTREE), "--db", dsn]) == 1
    assert "trackdir db init" in collapse(capsys.readouterr().err)


def test_replay_stores_initial_sample(db, dsn, subtree):
    sample.run_db_init(dsn)
    res = run_sample(SampleOptions(dsn=dsn, replay=SUBTREE, max_dirs=10**5), print)
    assert res.kind == "initial"
    assert res.dirs == subtree.header.dirs
    assert (res.changed, res.deleted, res.skipped) == (res.dirs, 0, 0)
    assert res.tree == tree_key_of(subtree.header)
    assert res.errors.total() == subtree.header.errors
    assert res.max_depth == subtree.header.max_depth
    assert res.replayed_from == str(SUBTREE.resolve())
    assert os.path.isabs(res.replayed_from)
    count = db.execute("SELECT count(*) FROM trackdir.dir_state").fetchone()[0]
    assert count == res.dirs
    row = db.execute(
        "SELECT s.replayed_from, t.max_depth FROM trackdir.sample s"
        " JOIN trackdir.tree t ON t.id = s.tree_id WHERE s.id = %s",
        (res.sample_id,),
    ).fetchone()
    assert row == (res.replayed_from, subtree.header.max_depth)


def test_older_replay_is_refused_before_walking(db, dsn, monkeypatch):
    sample.run_db_init(dsn)
    opts = SampleOptions(dsn=dsn, replay=SUBTREE, max_depth=1)
    run_sample(opts, print)
    monkeypatch.setattr(sample, "replay_walk", must_not_walk)
    with pytest.raises(FatalError) as exc:
        run_sample(opts, print)
    message = collapse(str(exc.value))
    assert "not later than" in message
    assert "nothing was stored" in message


def test_live_sample_dated_before_latest_is_refused_before_walking(
    db, dsn, tree, fake_ceph, monkeypatch, capsys
):
    sample.run_db_init(dsn)
    args = ["sample", str(tree), "--max-depth", "3", "--db", dsn]
    assert main(args) == 0
    db.execute("UPDATE trackdir.sample SET started = '2999-01-01T00:00:00+00:00'")
    capsys.readouterr()
    monkeypatch.setattr(live, "walk", must_not_walk)
    assert main(args) == 1
    err = collapse(capsys.readouterr().err)
    assert "not later than" in err
    assert "nothing was stored" in err
    assert "check the clocks" in err
    assert db.execute("SELECT count(*) FROM trackdir.sample").fetchone() == (1,)


def test_live_sample_with_fakes(db, dsn, tree, fake_ceph, capsys):
    sample.run_db_init(dsn)
    assert main(["sample", str(tree), "--max-depth", "2", "--db", dsn]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("Stored sample")
    row = db.execute(
        "SELECT t.fs_path, t.fs_name, s.replayed_from, s.dirs, s.root"
        " FROM trackdir.sample s JOIN trackdir.tree t ON t.id = s.tree_id"
    ).fetchall()
    assert row == [(b"/x", None, None, 4, os.fsencode(tree.resolve()))]


def test_live_second_sample_is_pruned_by_the_baseline(db, dsn, tree, fake_ceph, capsys):
    sample.run_db_init(dsn)
    args = ["sample", str(tree), "--max-depth", "3", "--db", dsn]
    assert main(args) == 0
    assert main(args) == 0  # the fake rstats are constant: nothing changed
    first, second = collapse(capsys.readouterr().err).split(" Stored sample ")
    assert first.startswith("Stored sample 1 (initial): 5 directories")
    assert second.startswith("2 (incremental): probed 1 directories")
    assert db.execute(
        "SELECT s.id, s.dirs, count(d.*) FILTER (WHERE NOT d.deleted),"
        " count(d.*) FILTER (WHERE d.deleted) FROM trackdir.sample s"
        " LEFT JOIN trackdir.dir_state d ON d.sample_id = s.id"
        " GROUP BY s.id ORDER BY s.id"
    ).fetchall() == [(1, 5, 5, 0), (2, 1, 0, 0)]


def test_replay_reports_each_phase(db, dsn):
    sample.run_db_init(dsn)
    stream = Terminal()
    opts = SampleOptions(dsn=dsn, replay=SUBTREE)
    result = run_sample(opts, print, progress=Progress(stream))
    assert status_phases(stream.getvalue()) == [
        "loading the capture",
        "loading the baseline",
        "probed",
        "comparing with the baseline",
        f"storing {result.changed:,} directories and 0 deletions",
    ]


def test_live_sample_erases_its_progress_before_the_summary(
    db, dsn, tree, fake_ceph, monkeypatch
):
    sample.run_db_init(dsn)
    stderr = Terminal()
    monkeypatch.setattr(sys, "stderr", stderr)
    assert main(["sample", str(tree), "--max-depth", "2", "--db", dsn]) == 0
    progress, summary = stderr.getvalue().rsplit("\r\x1b[K", 1)
    assert status_phases(progress) == [
        "loading the baseline",
        "probed",
        "comparing with the baseline",
        "storing 4 directories and 0 deletions",
    ]
    assert summary.startswith("Stored sample 1")
