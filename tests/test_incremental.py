"""End-to-end incremental samples: replays of edited copies of the real fixture.

Every test stores an initial sample of SUBTREE, then replays captures that
edited_capture() derived from it and dated later, and checks what reached the
database. All need TRACKDIR_TEST_DSN (the db fixture) and psycopg.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import SUBTREE, collapse, edited_capture, later, must_not_walk, replay

from trackdir import sample
from trackdir.baseline import ROW_COLUMNS, Row, row_of
from trackdir.capture import Capture, load_capture, write_capture
from trackdir.cli import main
from trackdir.messages import FatalError
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, path_depth, split
from trackdir.sample import SampleResult


@pytest.fixture
def initial(db, dsn) -> SampleResult:
    """Sample 1: the initial sample of SUBTREE."""
    sample.run_db_init(dsn)
    return replay(dsn, SUBTREE)


@pytest.fixture
def walks(monkeypatch) -> list[set[bytes]]:
    """Record the paths each replay walk probed, oldest first."""
    seen: list[set[bytes]] = []
    real = sample.replay_walk

    def spy(*args, **kwargs):
        header, probes = real(*args, **kwargs)
        seen.append({p.path for p in probes})
        return header, probes

    monkeypatch.setattr(sample, "replay_walk", spy)
    return seen


def sample_edit(
    dsn: str, base: Capture, tmp_path: Path, name: str, **edits
) -> tuple[SampleResult, Capture]:
    """Replay BASE with EDITS applied, dated an hour later; return it as loaded."""
    path = edited_capture(
        base, tmp_path / f"{name}.jsonl.gz", started=later(base), **edits
    )
    return replay(dsn, path), load_capture(path)


def rows_of(capture: Capture) -> dict[bytes, Row]:
    return {path: row_of(probe) for path, probe in capture.probes.items()}


def view(db) -> dict[bytes, Row]:
    """The current state of all trees, from the view."""
    columns = ", ".join(ROW_COLUMNS)
    cursor = db.execute(f"SELECT path, {columns} FROM trackdir.current_dir_state")
    return {path: tuple(values) for path, *values in cursor}


def stored(db, sample_id: int) -> dict[bytes, Row]:
    """The rows (not tombstones) that sample SAMPLE_ID stored."""
    columns = ", ".join(ROW_COLUMNS)
    cursor = db.execute(
        f"SELECT path, {columns} FROM trackdir.dir_state"
        " WHERE sample_id = %s AND NOT deleted",
        (sample_id,),
    )
    return {path: tuple(values) for path, *values in cursor}


def count(db, table: str, where: str = "true", *params) -> int:
    sql = f"SELECT count(*) FROM trackdir.{table} WHERE {where}"
    return db.execute(sql, params).fetchone()[0]


def chain(path: bytes) -> list[bytes]:
    """PATH and its ancestors up to ROOT."""
    paths = [path]
    while path != ROOT:
        path = split(path)[0]
        paths.append(path)
    return paths


def below(capture: Capture, path: bytes) -> list[bytes]:
    return [p for p in capture.probes if p.startswith(path + b"/")]


def deep_dir(capture: Capture) -> bytes:
    """A depth-4 directory without an error."""
    return next(
        p.path for p in capture.probes.values() if p.depth == 4 and p.error is None
    )


def mid_dir(capture: Capture) -> bytes:
    """A depth-2 directory with at least 3 descendants."""
    return next(
        p.path
        for p in capture.probes.values()
        if p.depth == 2 and len(below(capture, p.path)) >= 3
    )


def error_dir(capture: Capture) -> bytes:
    """An error directory whose parent has no other error below it.

    Fixing it takes its parent out of the retry set, so a later sample prunes
    the parent and does not probe the directory again.
    """
    errors = [p.path for p in capture.probes.values() if p.error]
    for path in errors:
        parent = split(path)[0]
        if all(not e.startswith(parent + b"/") or e == path for e in errors):
            return path
    pytest.fail("no error directory is alone below its parent")


def test_unchanged_later_capture_stores_nothing(db, dsn, subtree, initial, tmp_path):
    before = view(db)
    res, second = sample_edit(dsn, subtree, tmp_path, "second")
    assert res.kind == "incremental"
    assert (res.changed, res.deleted) == (0, 0)
    assert res.dirs < second.header.dirs
    assert res.skipped >= 1
    assert count(db, "dir_state", "sample_id = %s", res.sample_id) == 0
    assert view(db) == before


def test_changed_chain_is_stored_and_nothing_else(
    db, dsn, subtree, initial, tmp_path, walks
):
    deep = deep_dir(subtree)
    res, second = sample_edit(dsn, subtree, tmp_path, "second", touch=[deep])
    changed = set(chain(deep))
    assert res.changed == path_depth(deep) + 1 == len(changed)
    assert res.deleted == 0
    assert stored(db, res.sample_id) == {p: row_of(second.probes[p]) for p in changed}
    current = view(db)
    assert current == rows_of(second)
    untouched = rows_of(subtree)
    assert {p: r for p, r in current.items() if p not in changed} == {
        p: r for p, r in untouched.items() if p not in changed
    }
    assert current[deep] != untouched[deep]
    assert deep in walks[-1]
    assert len(walks[-1]) < second.header.dirs


def test_deleted_subtree_is_tombstoned_and_hidden(db, dsn, subtree, initial, tmp_path):
    mid = mid_dir(subtree)
    gone = [mid, *below(subtree, mid)]
    res, second = sample_edit(dsn, subtree, tmp_path, "second", remove=[mid])
    assert res.deleted == len(gone)
    assert res.changed == path_depth(mid)  # the parent chain, mid's parent to ROOT
    assert view(db) == rows_of(second)
    assert not [p for p in view(db) if p == mid or p.startswith(mid + b"/")]
    for path in (mid, gone[-1]):
        assert count(db, "dir_state", "path = %s", path) == 2  # state, tombstone
    assert count(db, "dir_state", "sample_id = %s AND deleted", res.sample_id) == len(
        gone
    )


def test_sample_after_the_whole_tree_was_deleted_is_initial(
    db, dsn, subtree, initial, tmp_path
):
    gone = DirProbe(ROOT, error=ProbeError(Stage.OPEN, "ENOENT"))
    path = tmp_path / "gone.jsonl.gz"
    header = replace(subtree.header, started=later(subtree), finished=later(subtree))
    write_capture(path, replace(header, dirs=1, errors=1), [gone])
    deleted = replay(dsn, path)
    assert (deleted.kind, deleted.deleted) == ("incremental", subtree.header.dirs)
    assert view(db) == {}
    back = edited_capture(
        subtree, tmp_path / "back.jsonl.gz", started=later(subtree, 2)
    )
    recreated, second = replay(dsn, back), load_capture(back)
    assert recreated.kind == "initial"
    assert recreated.changed == second.header.dirs
    assert view(db) == rows_of(second)


def test_recreated_path_gets_an_ordinary_row(db, dsn, subtree, initial, tmp_path):
    mid = mid_dir(subtree)
    _, second = sample_edit(dsn, subtree, tmp_path, "second", remove=[mid])
    res, third = sample_edit(dsn, second, tmp_path, "third", add=[mid])
    assert (res.kind, res.deleted) == ("incremental", 0)
    assert res.changed == path_depth(mid) + 1
    current = view(db)
    assert current[mid] == row_of(third.probes[mid])
    assert count(db, "dir_state", "path = %s", mid) == 3  # state, tombstone, new
    assert current == rows_of(third)
    assert not [p for p in current if p.startswith(mid + b"/")]


def test_error_directory_is_retried_when_readable(
    db, dsn, subtree, initial, tmp_path, walks
):
    err = error_dir(subtree)
    assert view(db)[err][-2:] == ("open", "EACCES")
    res, second = sample_edit(dsn, subtree, tmp_path, "second", fix=[err])
    assert res.changed == 1
    assert res.deleted == 0
    assert err in walks[-1]  # found through the retry set, ancestors untouched
    assert stored(db, res.sample_id) == {err: row_of(second.probes[err])}
    assert view(db)[err][-2:] == (None, None)
    res, third = sample_edit(dsn, second, tmp_path, "third")
    assert (res.changed, res.deleted) == (0, 0)
    assert err not in walks[-1]  # the retry set shrank
    assert len(walks[-1]) < len(walks[-2]) < third.header.dirs


def test_new_directory_is_stored(db, dsn, subtree, initial, tmp_path):
    parent = next(
        p.path
        for p in subtree.probes.values()
        if p.depth == 3 and p.error is None and p.children is not None
    )
    new = parent + b"/new"
    res, second = sample_edit(dsn, subtree, tmp_path, "second", add=[new])
    assert res.changed == path_depth(new) + 1  # the new directory and its ancestors
    assert res.deleted == 0
    assert stored(db, res.sample_id)[new] == row_of(second.probes[new])
    assert view(db) == rows_of(second)
    assert new not in rows_of(subtree)


def test_concurrent_sample_is_refused_at_store(
    db, dsn, subtree, initial, tmp_path, monkeypatch
):
    deep = deep_dir(subtree)
    outer = edited_capture(
        subtree, tmp_path / "outer.jsonl.gz", started=later(subtree, 1), touch=[deep]
    )
    inner = edited_capture(
        subtree, tmp_path / "inner.jsonl.gz", started=later(subtree, 2), touch=[deep]
    )
    real = sample.replay_walk

    def racing(*args, **kwargs):
        walked = real(*args, **kwargs)
        monkeypatch.setattr(sample, "replay_walk", real)  # the inner run walks as is
        replay(dsn, inner)
        return walked

    monkeypatch.setattr(sample, "replay_walk", racing)
    with pytest.raises(FatalError) as exc:
        replay(dsn, outer)
    message = collapse(str(exc.value))
    assert "another sample" in message
    assert "nothing was stored" in message
    assert count(db, "sample") == 2  # the initial one and the inner one
    assert count(db, "sample", "replayed_from LIKE %s", "%/outer.jsonl.gz") == 0


def test_replay_dated_before_latest_sample_is_refused_before_walking(
    db, dsn, subtree, initial, tmp_path, monkeypatch
):
    sample_edit(dsn, subtree, tmp_path, "second")  # started an hour after SUBTREE
    older = edited_capture(
        subtree, tmp_path / "older.jsonl.gz", started=later(subtree, 0.5)
    )
    monkeypatch.setattr(sample, "replay_walk", must_not_walk)
    with pytest.raises(FatalError) as exc:
        replay(dsn, older)
    message = collapse(str(exc.value))
    assert "not later than" in message
    assert "nothing was stored" in message
    assert count(db, "sample") == 2


def test_depth_or_max_dirs_starts_a_new_tree(db, dsn, subtree, initial):
    shallow = replay(dsn, SUBTREE, max_depth=2)
    roomy = replay(dsn, SUBTREE, max_dirs=10**6 + 1)
    assert (shallow.kind, roomy.kind) == ("initial", "initial")
    assert count(db, "tree") == 3
    assert len({initial.tree, shallow.tree, roomy.tree}) == 3
    assert shallow.changed == shallow.dirs < subtree.header.dirs
    assert roomy.changed == subtree.header.dirs


def test_failed_store_leaves_nothing(db, dsn, subtree, initial, tmp_path):
    deep = deep_dir(subtree)
    beyond_bigint = replace(subtree.probes[deep], rbytes=2**63)
    broken = Capture(subtree.header, {**subtree.probes, deep: beyond_bigint})
    # The parent chain is touched so that the walk reaches deep.
    path = edited_capture(
        broken,
        tmp_path / "bad.jsonl.gz",
        started=later(subtree),
        touch=[split(deep)[0]],
    )
    before = view(db)
    with pytest.raises(FatalError) as exc:
        replay(dsn, path)
    message = collapse(str(exc.value))
    assert message.startswith("database error")
    assert message.endswith("Nothing was stored.")
    assert count(db, "sample") == 1
    assert count(db, "dir_state") == subtree.header.dirs
    assert view(db) == before


def test_second_sample_prints_incremental_summary(
    db, dsn, subtree, initial, tmp_path, capsys
):
    deep = deep_dir(subtree)
    path = edited_capture(
        subtree, tmp_path / "second.jsonl.gz", started=later(subtree), touch=[deep]
    )
    assert main(["sample", "--replay", str(path), "--db", dsn]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("Stored sample 2 (incremental): probed")
    assert "5 changed, 0 deleted" in collapse(stderr)
