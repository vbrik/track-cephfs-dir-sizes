import gzip
import os
import sys
from collections import Counter
from dataclasses import replace

import pytest
from conftest import SUBTREE, collapse, must_not_walk

from trackdir import live, sample
from trackdir.capture import load_capture
from trackdir.cli import main
from trackdir.db import (
    DbError,
    SamplesExist,
    SchemaVersionMismatch,
    TreeKey,
    tree_key_of,
)
from trackdir.messages import FatalError
from trackdir.model import error_counts
from trackdir.sample import (
    SampleOptions,
    SampleResult,
    describe_tree,
    render_sample_summary,
    replay_walk,
    run_sample,
)

UNREACHABLE = "postgresql://trackdir@127.0.0.1:1/x_test"


@pytest.fixture(autouse=True)
def no_dsn_in_environment(monkeypatch):
    """Keep the developer's TRACKDIR_DB out of the tests."""
    monkeypatch.delenv(sample.DSN_ENV, raising=False)


@pytest.fixture(scope="module")
def subtree():
    return load_capture(SUBTREE)


@pytest.fixture
def dsn(db):
    """The DSN of the db fixture's database, password included."""
    return os.environ["TRACKDIR_TEST_DSN"]


def without_children(probes):
    return [replace(p, children=None) for p in probes]


def test_replay_walk_at_capture_depth(subtree):
    header, probes = replay_walk(subtree, subtree.header.max_depth, max_dirs=10**6)
    assert without_children(probes) == without_children(subtree.probes.values())
    assert header == subtree.header


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

    def fake_run_sample(opts, warn):
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
    def interrupted(opts, warn):
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
        "tree": TreeKey("fsid", None, b"/x"),
        "max_depth": 2,
        "dirs": 5,
        "errors": Counter({("open", "EACCES"): 2}),
        "walk_seconds": 1.25,
        "store_seconds": 0.5,
        "replayed_from": None,
    }
    return SampleResult(**(fields | changes))


def test_render_sample_summary():
    assert collapse(render_sample_summary(result())) == (
        "Stored sample 7: 5 directories under /mnt/cephfs/x to depth 2, tree "
        "(cluster fsid, default filesystem, path /x); 2 had errors: open EACCES "
        "2. Walk 1.2 s, store 0.5 s."
    )
    replayed = result(
        tree=TreeKey("fsid", "cephfs", b"/x"),
        errors=Counter(),
        replayed_from="/c.gz",
    )
    assert collapse(render_sample_summary(replayed)) == (
        "Stored sample 7: 5 directories under /mnt/cephfs/x to depth 2, tree "
        "(cluster fsid, filesystem cephfs, path /x); none had errors. Walk 1.2 s, "
        "store 0.5 s. Replayed from /c.gz."
    )


@pytest.mark.parametrize("fs_name", [None, "cephfs"])
def test_samples_exist_names_the_tree_as_the_summary_does(
    subtree, monkeypatch, fs_name
):
    key = replace(tree_key_of(subtree.header), fs_name=fs_name)

    def samples_exist(dsn):
        raise SamplesExist(key)

    monkeypatch.setattr(sample, "connect", samples_exist)
    with pytest.raises(FatalError) as exc:
        run_sample(SampleOptions(dsn="x", replay=SUBTREE), print)
    tree = describe_tree(key)
    assert f"the {tree} already has samples" in collapse(str(exc.value))
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
    monkeypatch.setattr(sample, "connect", fail_to_connect(DbError("boom")))
    assert main(args) == 1
    assert (
        collapse(capsys.readouterr().err) == f"ERROR: database error: boom. {outcome}"
    )


@pytest.mark.parametrize(
    ("found", "next_step"),
    [(2, "migrate it, or drop schema trackdir"), (None, "drop schema trackdir")],
)
def test_schema_version_mismatch_says_what_to_do(monkeypatch, capsys, found, next_step):
    error = SchemaVersionMismatch(found)
    monkeypatch.setattr(sample, "connect", fail_to_connect(error))
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
    res = run_sample(SampleOptions(dsn=dsn, replay=SUBTREE), print)
    assert res.dirs == subtree.header.dirs
    assert res.errors.total() == subtree.header.errors
    assert res.max_depth == subtree.header.max_depth
    assert res.replayed_from == str(SUBTREE.resolve())
    assert os.path.isabs(res.replayed_from)
    count = db.execute("SELECT count(*) FROM trackdir.dir_state").fetchone()[0]
    assert count == res.dirs
    row = db.execute(
        "SELECT replayed_from, max_depth FROM trackdir.sample WHERE id = %s",
        (res.sample_id,),
    ).fetchone()
    assert row == (res.replayed_from, subtree.header.max_depth)


def test_second_replay_fails_before_walking(db, dsn, monkeypatch):
    sample.run_db_init(dsn)
    opts = SampleOptions(dsn=dsn, replay=SUBTREE, max_depth=1)
    run_sample(opts, print)
    monkeypatch.setattr(sample, "replay_walk", must_not_walk)
    with pytest.raises(FatalError) as exc:
        run_sample(opts, print)
    message = collapse(str(exc.value))
    assert "already has samples" in message
    assert "incremental" in message


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
