"""Tests for trackdir.growth: own growth between two states of a tree, without
a database."""

from trackdir.growth import DirGrowth, own_growth
from trackdir.model import ROOT

DEPTH = 5  # deeper than every tree below, unless a test says otherwise


def ok(rbytes: int) -> tuple[int, None]:
    """A readable, listed directory of RBYTES."""
    return (rbytes, None)


def test_new_path_grows_from_zero():
    ends = {ROOT: (ok(10), ok(13)), b"a": (None, ok(3))}
    positive, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(b"a", None, 3, 3, False),)
    assert positive == 3


def test_deleted_path_lowers_its_parent_and_is_not_shown():
    ends = {ROOT: (ok(10), ok(4)), b"a": (ok(8), None), b"b": (ok(2), ok(4))}
    positive, dirs = own_growth(ends, DEPTH, 10)
    # ROOT's own growth: -6 - (-8 + 2) = 0
    assert dirs == (DirGrowth(b"b", 2, 4, 2, False),)
    assert positive == 2


def test_growth_is_unknown_without_rstats_at_the_start():
    ends = {ROOT: (ok(10), ok(15)), b"a": ((None, "rstats"), ok(5))}
    _, dirs = own_growth(ends, DEPTH, 10)
    # a's growth is unknown, so ROOT owns it, and holds a subdirectory's
    assert dirs == (DirGrowth(ROOT, 10, 15, 5, True),)


def test_growth_is_unknown_without_rstats_at_the_end():
    ends = {ROOT: (ok(10), ok(15)), b"a": (ok(5), (None, "rstats"))}
    _, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(ROOT, 10, 15, 5, True),)


def test_growth_is_unknown_below_a_dir_unlisted_at_the_start():
    ends = {
        ROOT: (ok(10), ok(20)),
        b"a": ((10, "readdir"), ok(20)),
        b"a/b": (ok(1), ok(9)),
    }
    _, dirs = own_growth(ends, DEPTH, 10)
    # a/b's row at the start may predate it; a owns its growth
    assert dirs == (DirGrowth(b"a", 10, 20, 10, True),)


def test_growth_is_unknown_below_a_dir_unlisted_at_the_end():
    ends = {
        ROOT: (ok(10), ok(20)),
        b"a": (ok(10), (20, "readdir")),
        b"a/b": (ok(1), ok(9)),
    }
    _, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(b"a", 10, 20, 10, True),)


def test_nearest_known_ancestor_owns_an_unknown_growth():
    ends = {
        ROOT: (ok(10), ok(20)),
        b"a": (ok(10), ok(20)),
        b"a/b": ((None, "open"), (None, "open")),
        b"a/b/c": (ok(1), ok(5)),
    }
    _, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(b"a", 10, 20, 10, True),)


def test_dir_at_max_depth_owns_its_subtree():
    ends = {ROOT: (ok(10), ok(12)), b"a": (ok(10), ok(12))}
    _, dirs = own_growth(ends, 1, 10)
    assert dirs == (DirGrowth(b"a", 10, 12, 2, True),)


def test_dir_unlisted_at_the_end_owns_its_subtree():
    ends = {ROOT: (ok(10), ok(12)), b"a": (ok(10), (12, "readdir"))}
    _, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(b"a", 10, 12, 2, True),)


def test_root_with_an_unknown_growth_owns_nothing():
    ends = {ROOT: ((None, "rstats"), ok(20)), b"a": (ok(1), ok(5))}
    positive, dirs = own_growth(ends, DEPTH, 10)
    assert dirs == (DirGrowth(b"a", 1, 5, 4, False),)
    assert positive == 4


def test_positive_own_counts_dirs_beyond_top():
    ends = {
        ROOT: (ok(10), ok(16)),
        b"a": (ok(1), ok(4)),
        b"b": (ok(1), ok(3)),
        b"c": (ok(8), ok(9)),
    }
    positive, dirs = own_growth(ends, DEPTH, 1)
    assert dirs == (DirGrowth(b"a", 1, 4, 3, False),)
    assert positive == 6


def test_top_breaks_ties_by_path_and_leaves_out_non_positive_own_growth():
    ends = {
        ROOT: (ok(10), ok(12)),
        b"b": (ok(1), ok(3)),
        b"a": (ok(1), ok(3)),
        b"c": (ok(8), ok(6)),
    }
    _, dirs = own_growth(ends, DEPTH, 10)
    # ROOT: 2 - (2 + 2 - 2) = 0, c shrank: neither is shown
    assert [d.path for d in dirs] == [b"a", b"b"]
