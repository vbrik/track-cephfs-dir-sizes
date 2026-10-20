"""trackdir db summary: the statistics query, its rendering and the command.

The database tests replay SUBTREE and edited copies of it at depth 2, which
keeps them fast; they need TRACKDIR_TEST_DSN (the db fixture) and psycopg.
"""

import json
import os
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import SUBTREE, collapse, edited_capture, later, replay

from trackdir import sample
from trackdir.capture import Capture, load_capture, write_capture
from trackdir.cli import main
from trackdir.db import (
    DbStats,
    DirGrowth,
    Growth,
    RootTotals,
    SampleStats,
    SchemaMissing,
    TreeKey,
    TreeStats,
    summarize,
)
from trackdir.dbcli import display_path, tree_identity
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, split
from trackdir.summary import format_bytes, render_json, render_text

DEPTH = 2  # of most replays; SUBTREE has 380 directories down to it


def edit(base: Capture, tmp_path: Path, name: str, hours: float, **edits) -> Path:
    """Write BASE with EDITS, started HOURS after BASE, to a new capture."""
    out = tmp_path / f"{name}.jsonl.gz"
    return edited_capture(base, out, started=later(base, hours), **edits)


def only_root(base: Capture, tmp_path: Path, name: str, error: ProbeError) -> Path:
    """Write a capture, an hour after BASE, whose ROOT failed with ERROR."""
    out = tmp_path / f"{name}.jsonl.gz"
    header = replace(base.header, started=later(base), finished=later(base))
    write_capture(out, replace(header, dirs=1, errors=1), [DirProbe(ROOT, error=error)])
    return out


def with_rstats_errors(path: Path, *dirs: bytes) -> Path:
    """Write a copy of capture PATH with an rstats EIO error at each of DIRS.

    Returns the copy's path: PATH's with "eio-" before the name.
    """
    capture = load_capture(path)
    error = ProbeError(Stage.RSTATS, "EIO")
    gone = dict(rctime=None, rbytes=None, rfiles=None, rsubdirs=None, error=error)
    probes = {**capture.probes, **{d: replace(capture.probes[d], **gone) for d in dirs}}
    header = replace(capture.header, errors=capture.header.errors + len(dirs))
    out = path.with_name(f"eio-{path.name}")
    write_capture(out, header, probes.values())
    return out


def at_depth(capture: Capture, depth: int) -> list[bytes]:
    """CAPTURE's paths at DEPTH without errors, in tree order."""
    return [p.path for p in capture.probes.values() if p.depth == depth and not p.error]


def current(db) -> tuple[int, Counter[tuple[str, str]]]:
    """Directories in the current-state view, and its errors by (stage, code)."""
    rows = db.execute(
        "SELECT error_stage, error_code FROM trackdir.current_dir_state"
    ).fetchall()
    return len(rows), Counter((s, c) for s, c in rows if s is not None)


@pytest.fixture
def initial(db, dsn) -> sample.SampleResult:
    sample.run_db_init(dsn)
    return replay(dsn, SUBTREE, max_depth=DEPTH)


def test_missing_schema_is_refused(db):
    with pytest.raises(SchemaMissing):
        summarize(db)


def test_empty_database(db, dsn):
    sample.run_db_init(dsn)
    stats = summarize(db)
    assert stats.schema_version == 4
    assert (stats.trees, stats.samples, stats.dir_states) == (0, 0, 0)
    assert stats.bytes_on_disk > 0
    assert stats.tree_stats == ()


def test_initial_sample(db, subtree, initial):
    stats = summarize(db)
    assert (stats.trees, stats.samples) == (1, 1)
    assert stats.dir_states == initial.dirs
    (tree,) = stats.tree_stats
    assert tree.key == initial.tree
    assert tree.samples == 1
    (only,) = tree.recent
    assert only.id == initial.sample_id
    assert only.kind == "initial"
    assert (only.new, only.modified, only.deleted) == (initial.dirs, 0, 0)
    assert (only.probed, only.errors) == (initial.dirs, initial.errors.total())
    assert only.replayed_from == str(SUBTREE.resolve())
    assert tree.first_started == tree.last_started == only.started
    assert (tree.current_dirs, tree.current_errors) == current(db)
    root = subtree.probes[ROOT]
    assert tree.root_now == tree.root_first
    assert tree.root_now.sample_id == initial.sample_id
    assert (tree.root_now.rbytes, tree.root_now.rfiles, tree.root_now.rsubdirs) == (
        root.rbytes,
        root.rfiles,
        root.rsubdirs,
    )


def test_incremental_sample_splits_new_from_modified(
    db, dsn, subtree, initial, tmp_path
):
    touched, removed = at_depth(subtree, 2)[:2]
    added = split(touched)[0] + b"/new"
    path = edit(
        subtree, tmp_path, "second", 1, touch=[touched], remove=[removed], add=[added]
    )
    second = replay(dsn, path, max_depth=DEPTH)
    stats = summarize(db)
    (tree,) = stats.tree_stats
    newest, oldest = tree.recent
    assert (newest.id, oldest.id) == (second.sample_id, initial.sample_id)
    assert (newest.kind, oldest.kind) == ("incremental", "initial")
    assert newest.new == 1
    assert newest.new + newest.modified == second.changed
    assert newest.deleted == second.deleted == 1
    assert (tree.current_dirs, tree.current_errors) == current(db)
    assert tree.current_dirs == initial.dirs
    root = load_capture(path).probes[ROOT]
    assert tree.root_now.sample_id == second.sample_id
    assert tree.root_now.rbytes == root.rbytes
    assert tree.root_first.sample_id == initial.sample_id


def test_sample_after_whole_tree_was_deleted_is_initial_and_path_reborn_is_new(
    db, dsn, subtree, initial, tmp_path
):
    gone = ProbeError(Stage.OPEN, "ENOENT")
    deleted = replay(dsn, only_root(subtree, tmp_path, "gone", gone), max_depth=DEPTH)
    back = replay(dsn, edit(subtree, tmp_path, "back", 2), max_depth=DEPTH)
    (tree,) = summarize(db).tree_stats
    kinds = [(s.id, s.kind, s.new, s.deleted) for s in tree.recent]
    assert kinds == [
        (back.sample_id, "initial", back.changed, 0),
        (deleted.sample_id, "incremental", 0, initial.dirs),
        (initial.sample_id, "initial", initial.dirs, 0),
    ]
    assert tree.current_dirs == back.dirs == current(db)[0]
    assert tree.root_first.sample_id == initial.sample_id
    assert tree.root_now.sample_id == back.sample_id


def test_root_without_rstats_has_no_current_totals(db, dsn, subtree, initial, tmp_path):
    replay(
        dsn,
        only_root(subtree, tmp_path, "eio", ProbeError(Stage.RSTATS, "EIO")),
        max_depth=DEPTH,
    )
    (tree,) = summarize(db).tree_stats
    assert tree.root_now is None
    assert tree.root_first.sample_id == initial.sample_id
    assert tree.current_errors[("rstats", "EIO")] == 1


def test_tree_filter_and_recent_limit(db, dsn, subtree, initial, tmp_path):
    replay(
        dsn,
        edit(subtree, tmp_path, "second", 1, touch=at_depth(subtree, 2)[:1]),
        max_depth=DEPTH,
    )
    other = replay(dsn, SUBTREE, max_depth=1)
    everything = summarize(db)
    assert [t.id for t in everything.tree_stats] == sorted(
        t.id for t in everything.tree_stats
    )
    assert everything.trees == 2
    one = summarize(db, tree_id=everything.tree_stats[1].id, recent=0)
    assert (one.trees, one.samples, one.dir_states) == (
        everything.trees,
        everything.samples,
        everything.dir_states,
    )
    (tree,) = one.tree_stats
    assert tree.key == other.tree
    assert tree.recent == ()
    first = summarize(db, recent=1).tree_stats[0]
    assert first.samples == 2
    assert [s.kind for s in first.recent] == ["incremental"]
    assert summarize(db, tree_id=10**9).tree_stats == ()


def test_growth_needs_two_samples(db, initial):
    (tree,) = summarize(db).tree_stats
    assert tree.growth == ()


def test_growth_since_first_and_previous_sample(db, dsn, subtree, initial, tmp_path):
    a, b = at_depth(subtree, 1)[:2]
    size = {p: subtree.probes[p].rbytes for p in (a, b)}
    new = b"0000new"
    path = edit(subtree, tmp_path, "second", 1, touch=[a, a, a, b, new, new], add=[new])
    second = replay(dsn, path, max_depth=DEPTH)
    (tree,) = summarize(db).tree_stats
    assert tree.growth == (
        Growth(
            since=initial.sample_id,
            depth=1,
            dirs=(
                DirGrowth(a, size[a], size[a] + 3),
                DirGrowth(new, None, 2),
                DirGrowth(b, size[b], size[b] + 1),
            ),
        ),
    )
    third = edit(load_capture(path), tmp_path, "third", 1, touch=[b] * 4)
    replay(dsn, third, max_depth=DEPTH)
    (tree,) = summarize(db).tree_stats
    since_first, since_previous = tree.growth
    assert since_first.since == initial.sample_id
    assert since_first.dirs == (
        DirGrowth(b, size[b], size[b] + 5),
        DirGrowth(a, size[a], size[a] + 3),
        DirGrowth(new, None, 2),
    )
    assert since_previous == Growth(
        since=second.sample_id,
        depth=1,
        dirs=(DirGrowth(b, size[b] + 1, size[b] + 5),),
    )


def test_growth_honors_depth_top_and_ties_by_path(db, dsn, subtree, initial, tmp_path):
    touched = sorted(at_depth(subtree, 2)[:3], reverse=True)
    replay(dsn, edit(subtree, tmp_path, "second", 1, touch=touched), max_depth=DEPTH)
    (growth,) = summarize(db, depth=2, top=2).tree_stats[0].growth
    assert growth.depth == 2
    assert [d.path for d in growth.dirs] == sorted(touched)[:2]
    assert summarize(db, top=0).tree_stats[0].growth == ()
    (deeper,) = summarize(db, depth=DEPTH + 1).tree_stats[0].growth
    assert deeper.dirs == ()


def test_growth_skips_deleted_dirs_and_rows_without_rstats(db, dsn, subtree, tmp_path):
    sample.run_db_init(dsn)
    broken_before, broken_after, grown, removed = at_depth(subtree, 1)[:4]
    first = edit(subtree, tmp_path, "first", 0)
    replay(dsn, with_rstats_errors(first, broken_before), max_depth=DEPTH)
    touch = [broken_before, broken_after, grown, grown]
    second = edit(subtree, tmp_path, "second", 1, touch=touch, remove=[removed])
    replay(dsn, with_rstats_errors(second, broken_after), max_depth=DEPTH)
    (growth,) = summarize(db).tree_stats[0].growth
    rbytes = subtree.probes[grown].rbytes
    assert growth.dirs == (DirGrowth(grown, rbytes, rbytes + 2),)


T1 = datetime(2026, 10, 16, 20, 48, 11, 500_000, tzinfo=UTC)
KEY = TreeKey("11111111-2222-4333-8444-555555555555", None, b"/proj/sub", 4)


def stats_of(**changes) -> DbStats:
    """Two samples of KEY's tree, the newest shown; CHANGES apply to the tree."""
    newest = SampleStats(
        id=7,
        started=T1,
        finished=T1 + timedelta(seconds=13.25),
        probed=962,
        errors=41,
        new=1,
        modified=17,
        deleted=2,
        euid=1000,
        host="h",
        root=b"/mnt/cephfs/proj/sub",
        anonymized=False,
        replayed_from=None,
        kind="incremental",
    )
    tree = TreeStats(
        id=3,
        key=KEY,
        samples=2,
        first_started=T1 - timedelta(days=1),
        last_started=T1,
        current_dirs=72017,
        current_errors=Counter({("open", "EACCES"): 41}),
        root_now=RootTotals(7, 3 * 2**40 + 2**39, 4567890, 72017),
        root_first=RootTotals(5, 3 * 2**40, 4567000, 72018),
        recent=(newest,),
        growth=(
            Growth(
                since=5,
                depth=1,
                dirs=(
                    DirGrowth(b"alpha", 2**40, 2**40 + 2**39),
                    DirGrowth(b"beta", None, 3 * 2**30),
                ),
            ),
            Growth(since=6, depth=1, dirs=()),
        ),
    )
    return DbStats(4, 1, 2, 144434, 39542784, (replace(tree, **changes),))


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (39542784, "37.7 MiB"),
        (-3 * 2**30, "-3.0 GiB"),
        (2**70, "1024.0 EiB"),
    ],
)
def test_format_bytes(size, text):
    assert format_bytes(size) == text


def test_render_text():
    assert render_text(stats_of()) == (
        "Database: schema version 4; 1 tree, 2 samples, 144434 dir_state rows;"
        " 37.7 MiB on disk\n"
        "\n"
        "Tree 3 (cluster 11111111-2222-4333-8444-555555555555, default filesystem,"
        " path /proj/sub, depth 4)\n"
        "  samples: 2, 2026-10-15T20:48:11Z to 2026-10-16T20:48:11Z\n"
        "  current: 72017 directories; 41 had errors: open EACCES 41\n"
        "  ROOT:    3.5 TiB, 4567890 files, 72017 directories, last changed in"
        " sample 7\n"
        "  change:  +512.0 GiB, +890 files, -1 directory since sample 5\n"
        "  sample  kind         started (UTC)           took  probed  new  modified"
        "  deleted  errors  euid  source\n"
        "       7  incremental  2026-10-16T20:48:11Z  13.2 s     962    1        17"
        "        2      41  1000  live\n"
        "  1 older sample not shown\n"
        "  grew most at depth 1 since sample 5:\n"
        "    +512.0 GiB  alpha\n"
        "      +3.0 GiB  beta (new)\n"
        "  grew most at depth 1 since sample 6: none\n"
    )


def test_render_text_says_when_depth_is_past_the_tree():
    growth = Growth(since=5, depth=KEY.max_depth + 1, dirs=())
    text = render_text(stats_of(growth=(growth,)))
    assert text.endswith(
        "  grew most at depth 5 since sample 5: none; the tree is sampled to depth 4\n"
    )


def test_render_text_marks_hidden_samples_and_missing_root():
    text = render_text(stats_of(samples=12, root_now=None))
    assert "  ROOT:    no rstats in its current row\n" in text
    assert "change:" not in text
    assert "\n  11 older samples not shown\n" in text


def test_render_text_of_a_tree_without_samples():
    text = render_text(
        stats_of(
            samples=0,
            first_started=None,
            last_started=None,
            current_dirs=0,
            current_errors=Counter(),
            root_now=None,
            root_first=None,
            recent=(),
            growth=(),
        )
    )
    assert "  samples: 0\n" in text
    assert "  current: 0 directories; none had errors\n" in text
    assert "sample  kind" not in text


def test_render_text_without_trees():
    text = render_text(replace(stats_of(), tree_stats=()))
    assert text.count("\n") == 1


def test_render_text_escapes_undecodable_paths():
    growth = Growth(since=5, depth=1, dirs=(DirGrowth(b"caf\xe9", 1, 2),))
    stats = stats_of(key=replace(KEY, fs_path=b"/caf\xe9"), growth=(growth,))
    text = render_text(stats)
    text.encode()
    assert "path /caf\\xe9, depth 4)\n" in text
    assert "    +1 B  caf\\xe9\n" in text


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        (b"plain dir", "plain dir"),
        (b"caf\xc3\xa9", "caf\u00e9"),
        (b"caf\xe9", "caf\\xe9"),
        (b"a\nb", "a\\nb"),
        (b"\x1b[2Jx", "\\x1b[2Jx"),
        (b"zero\xe2\x80\x8bwidth", "zero\\u200bwidth"),
    ],
)
def test_display_path_is_printable(path, shown):
    assert display_path(path) == shown
    assert shown.isprintable()


def test_render_json():
    text = render_json(stats_of(root_first=None))
    assert text.endswith("}\n")
    data = json.loads(text)
    assert data["database"] == {
        "schema_version": 4,
        "trees": 1,
        "samples": 2,
        "dir_states": 144434,
        "bytes_on_disk": 39542784,
    }
    (tree,) = data["trees"]
    assert tree["key"]["fs_path"] == "/proj/sub"
    assert tree["key"]["fs_name"] is None
    assert tree["current_errors"] == [{"stage": "open", "code": "EACCES", "dirs": 41}]
    assert tree["root_first"] is None
    assert tree["root_now"]["rbytes"] == 3 * 2**40 + 2**39
    (newest,) = tree["recent"]
    assert newest["started"] == "2026-10-16T20:48:11.500000+00:00"
    assert newest["root"] == "/mnt/cephfs/proj/sub"
    assert newest["kind"] == "incremental"
    since_first, since_previous = tree["growth"]
    assert since_first["since"] == 5
    assert since_first["dirs"][1] == {
        "path": "beta",
        "rbytes_before": None,
        "rbytes_after": 3 * 2**30,
    }
    assert since_previous == {"since": 6, "depth": 1, "dirs": []}


def test_render_json_keeps_undecodable_paths_ascii():
    text = render_json(stats_of(key=replace(KEY, fs_path=b"/caf\xe9")))
    assert text.isascii()
    assert json.loads(text)["trees"][0]["key"]["fs_path"] == os.fsdecode(b"/caf\xe9")


def test_cli_prints_the_report_on_stdout(initial, dsn, capsys):
    assert main(["db", "summary", "--db", dsn]) == 0
    stdout, stderr = capsys.readouterr()
    assert stderr == ""
    assert stdout.startswith("Database: schema version 4; 1 tree, 1 sample,")
    assert f"\nTree 1 ({tree_identity(initial.tree)})\n" in stdout


def test_cli_json_honors_tree_and_samples(initial, dsn, capsys):
    replay(dsn, SUBTREE, max_depth=1)
    assert main(["db", "summary", "--db", dsn, "--json", "--tree", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [t["id"] for t in data["trees"]] == [2]
    assert main(["db", "summary", "--db", dsn, "--samples", "0"]) == 0
    stdout = capsys.readouterr().out
    assert stdout.count("\nTree ") == 2
    assert "sample  kind" not in stdout
    assert "\n  1 sample not shown\n" in stdout


def test_cli_unknown_tree_fails(initial, dsn, capsys):
    assert main(["db", "summary", "--db", dsn, "--tree", "9"]) == 1
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("ERROR: the database has no tree 9.")


def test_cli_without_schema_says_to_init(dsn, capsys):
    assert main(["db", "summary", "--db", dsn]) == 1
    assert "trackdir db init" in collapse(capsys.readouterr().err)


def test_cli_top_and_depth_select_growth(initial, dsn, subtree, tmp_path, capsys):
    touched = at_depth(subtree, 2)[:4]
    replay(dsn, edit(subtree, tmp_path, "second", 1, touch=touched), max_depth=DEPTH)
    assert main(["db", "summary", "--db", dsn, "--depth", "2", "--top", "4"]) == 0
    stdout = capsys.readouterr().out
    assert "  grew most at depth 2 since sample 1:\n" in stdout
    for path in touched:
        assert f"    +1 B  {os.fsdecode(path)}\n" in stdout
    assert main(["db", "summary", "--db", dsn, "--top", "0"]) == 0
    assert "grew most" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [["--samples", "-1"], ["--tree", "0"], ["--top", "-1"], ["--depth", "0"]],
)
def test_cli_usage_errors(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["db", "summary", "--db", "x", *args])
    assert exc.value.code == 2


def test_render_text_has_no_change_while_root_has_one_row():
    root = RootTotals(7, 1, 2, 3)
    assert "change:" not in render_text(stats_of(root_now=root, root_first=root))
