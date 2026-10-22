from datetime import UTC, datetime, timedelta

import pytest

from trackdir.baseline import (
    ROW_COLUMNS,
    Baseline,
    SampleDiff,
    diff,
    row_of,
    should_list_changed,
)
from trackdir.model import ROOT, DirProbe, ProbeError, Stage

T = datetime(2026, 1, 1, tzinfo=UTC)
MAX_DEPTH = 5


def probe(path: bytes, size: int = 1, **kwargs) -> DirProbe:
    """Return a healthy, unlisted probe of PATH; SIZE feeds the rstats."""
    values = dict(
        ino=sum(path),
        mode=0o40755,
        uid=1,
        gid=1,
        nlink=2,
        rctime=1000,
        rbytes=size,
        rfiles=size,
        rsubdirs=1,
    )
    return DirProbe(path, **{**values, **kwargs})


def error_probe(path: bytes, stage: Stage = Stage.OPEN, code: str = "EACCES"):
    return DirProbe(path, error=ProbeError(stage, code))


def baseline_of(*probes: DirProbe) -> Baseline:
    return Baseline.build(1, T, {p.path: row_of(p) for p in probes})


def test_row_of_matches_columns_and_splits_error():
    assert len(row_of(probe(b"a"))) == len(ROW_COLUMNS)
    row = row_of(error_probe(b"a"))
    assert row[-2:] == ("open", "EACCES")
    assert type(row[-2]) is str
    assert row_of(probe(b"a"))[-2:] == (None, None)


def test_build_retry_set_is_error_paths_and_ancestors():
    b = baseline_of(
        probe(ROOT), probe(b"a"), probe(b"a/b"), error_probe(b"a/b/c"), probe(b"z")
    )
    assert b.retry == {b"a/b/c", b"a/b", b"a", ROOT}
    assert baseline_of(probe(ROOT), probe(b"a")).retry == frozenset()


def test_build_children_maps_parents_to_full_child_paths():
    b = baseline_of(probe(ROOT), probe(b"a"), probe(b"a/b"), probe(b"z"))
    assert sorted(b.children[ROOT]) == [b"a", b"z"]
    assert b.children[b"a"] == (b"a/b",)
    assert b"z" not in b.children
    assert (b.sample_id, b.started) == (1, T)


def test_empty_baseline_has_no_sample():
    b = Baseline.empty()
    assert b.sample_id is None
    assert b.started is None
    assert not b.rows and not b.retry and not b.children


def test_empty_baseline_lists_everything():
    should_list = should_list_changed(Baseline.empty())
    assert should_list(probe(ROOT))
    assert should_list(probe(b"a/b"))


def test_unchanged_directory_is_pruned():
    b = baseline_of(probe(ROOT), probe(b"a"))
    assert not should_list_changed(b)(probe(b"a"))


@pytest.mark.parametrize(
    "change",
    [
        {"rctime": 2000},
        {"rbytes": 99},
        {"rfiles": 99},
        {"rsubdirs": 99},
        {"ino": 12345678},
        {"uid": 2},
    ],
)
def test_changed_rstat_is_listed(change):
    b = baseline_of(probe(b"a"))
    changed = probe(b"a", **change)
    assert should_list_changed(b)(changed)


def test_new_directory_is_listed():
    b = baseline_of(probe(ROOT))
    assert should_list_changed(b)(probe(b"new"))


def test_error_baseline_forces_listing_of_ancestors():
    b = baseline_of(
        probe(ROOT), probe(b"a"), error_probe(b"a/b"), probe(b"z"), error_probe(b"q")
    )
    should_list = should_list_changed(b)
    assert should_list(probe(ROOT))
    assert should_list(probe(b"a"))
    assert should_list(error_probe(b"a/b"))
    assert not should_list(probe(b"z"))


def test_diff_unchanged_stores_nothing():
    b = baseline_of(probe(ROOT), probe(b"a"))
    result = diff(b, [probe(ROOT, children=(b"a",)), probe(b"a")], MAX_DEPTH, T)
    # The unchanged "a" was pruned, hence not listed.
    assert result == SampleDiff(changed=[], deleted=[], skipped=1, skipped_pinned=0)


def test_diff_changed_and_new_are_stored_in_tree_order():
    b = baseline_of(probe(ROOT), probe(b"a"), probe(b"b"))
    probes = [
        probe(ROOT, rbytes=9, children=(b"a", b"b", b"n")),
        probe(b"a", rbytes=9),
        probe(b"b"),
        probe(b"n"),
    ]
    result = diff(b, probes, MAX_DEPTH, T)
    assert [p.path for p in result.changed] == [ROOT, b"a", b"n"]
    assert result.deleted == []


def test_diff_stores_error_rows():
    b = baseline_of(probe(ROOT), probe(b"a"))
    broken = error_probe(b"a")
    result = diff(b, [probe(ROOT, children=(b"a",)), broken], MAX_DEPTH, T)
    assert result.changed == [broken]
    assert result.deleted == []


def test_listed_parent_missing_a_child_tombstones_it_and_its_descendants():
    b = baseline_of(probe(b"a"), probe(b"a/b"), probe(b"a/b/c"), probe(b"a/z"))
    result = diff(b, [probe(b"a", children=(b"z",)), probe(b"a/z")], MAX_DEPTH, T)
    assert result.deleted == [b"a/b", b"a/b/c"]
    assert result.changed == []


def test_deleted_is_in_tree_order():
    b = baseline_of(probe(ROOT), probe(b"a-c"), probe(b"a"), probe(b"a/b"), probe(b"b"))
    result = diff(b, [probe(ROOT, rbytes=9, children=())], MAX_DEPTH, T)
    assert result.deleted == [b"a", b"a/b", b"a-c", b"b"]


def test_open_enoent_tombstones_without_error_row():
    b = baseline_of(probe(b"a"), probe(b"a/b"))
    result = diff(b, [error_probe(b"a", code="ENOENT")], MAX_DEPTH, T)
    assert result.deleted == [b"a", b"a/b"]
    assert result.changed == []


def test_enoent_and_missing_child_report_a_path_once():
    b = baseline_of(probe(ROOT), probe(b"a"), probe(b"a/b"))
    probes = [probe(ROOT, rbytes=9, children=()), error_probe(b"a", code="ENOENT")]
    assert diff(b, probes, MAX_DEPTH, T).deleted == [b"a", b"a/b"]


def test_enoent_without_baseline_stores_nothing():
    b = baseline_of(probe(ROOT))
    result = diff(b, [error_probe(b"a", code="ENOENT")], MAX_DEPTH, T)
    assert result.changed == []
    assert result.deleted == []


def test_non_open_enoent_is_an_ordinary_error_row():
    b = baseline_of(probe(b"a"))
    stat_gone = error_probe(b"a", Stage.STAT, "ENOENT")
    result = diff(b, [stat_gone], MAX_DEPTH, T)
    assert result.changed == [stat_gone]
    assert result.deleted == []


@pytest.mark.parametrize(
    "unlisted",
    [
        DirProbe(b"a"),  # pruned
        DirProbe(b"a", error=ProbeError(Stage.READDIR, "EIO")),
        DirProbe(b"a", error=ProbeError(Stage.OPEN, "EACCES")),
    ],
)
def test_nothing_is_tombstoned_below_unlisted_directories(unlisted):
    b = baseline_of(probe(b"a"), probe(b"a/b"), probe(b"a/b/c"))
    assert diff(b, [unlisted], MAX_DEPTH, T).deleted == []


def test_nothing_is_tombstoned_below_a_directory_at_max_depth():
    b = baseline_of(probe(b"a"), probe(b"a/b"))
    assert diff(b, [DirProbe(b"a")], 1, T).deleted == []


def test_listing_with_an_rstats_error_still_tombstones():
    b = baseline_of(probe(b"a"), probe(b"a/b"))
    listed = DirProbe(b"a", children=(), error=ProbeError(Stage.RSTATS, "EIO"))
    result = diff(b, [listed], MAX_DEPTH, T)
    assert result.deleted == [b"a/b"]
    assert result.changed == [listed]


def test_recreated_path_after_tombstone_is_stored():
    # The baseline is the view, which hides tombstoned paths.
    b = baseline_of(probe(ROOT))
    back = probe(b"a")
    result = diff(b, [probe(ROOT, rbytes=9, children=(b"a",)), back], MAX_DEPTH, T)
    assert back in result.changed


def test_skipped_counts_pruned_directories_only():
    b = baseline_of(probe(ROOT))
    probes = [
        DirProbe(b"pruned"),
        DirProbe(b"pruned2"),
        DirProbe(b"deep/er", rbytes=1),  # at max depth 2
        error_probe(b"errored"),
        probe(b"listed", children=()),
    ]
    assert diff(b, probes, 2, T).skipped == 2


def test_skipped_pinned_counts_pruned_directories_with_a_future_rctime():
    at_start = (
        (T - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000
    )
    b = baseline_of(probe(ROOT))
    probes = [
        DirProbe(b"pinned", rctime=at_start + 1),
        DirProbe(b"at-start", rctime=at_start),
        DirProbe(b"past", rctime=1000),
        DirProbe(b"no-rstats"),
        DirProbe(b"deep/pinned", rctime=at_start + 1),  # at max depth 2: not pruned
        probe(b"listed", rctime=at_start + 1, children=()),
    ]
    result = diff(b, probes, 2, T)
    assert (result.skipped, result.skipped_pinned) == (4, 1)
