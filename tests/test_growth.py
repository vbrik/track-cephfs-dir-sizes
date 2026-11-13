"""Tests for trackdir.growth: own growth between two states of a tree, without
a database."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from trackdir.growth import (
    DEFAULT_WINDOWS,
    DirGrowth,
    End,
    Window,
    kept_paths,
    own_growth,
    window_ends,
    window_start,
    witness_dirs,
)
from trackdir.model import ROOT, path_depth

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


# Windows: which sample a window starts at


LATEST = datetime(2030, 1, 1, tzinfo=UTC)


def test_default_windows_are_a_day_and_a_week():
    assert (Window("1d", 86_400), Window("7d", 604_800)) == DEFAULT_WINDOWS


@pytest.mark.parametrize(
    "hours_ago, seconds, expected",
    [
        ((48, 24, 1, 0), 3600, (2, False)),  # exactly 1 h before: that sample
        ((48, 24, 1, 0), 86_400, (1, False)),
        ((48, 24, 1, 0), 7 * 86_400, (0, True)),  # clipped to the first
        ((48, 24, 1, 0), 48 * 3600, (0, False)),  # the first, exactly
        ((48, 24, 1, 0), 1, (2, False)),
        ((1, 0), 3600, (0, False)),
        ((0,), 86_400, None),
        ((), 86_400, None),
    ],
)
def test_window_start(hours_ago, seconds, expected):
    started = [LATEST - timedelta(hours=h) for h in hours_ago]
    assert window_start(started, seconds) == expected


@pytest.mark.parametrize("seconds", [800_000 * 86_400, 99_999_999_999_999 * 86_400])
def test_window_longer_than_the_calendar_is_all_history(seconds):
    started = [LATEST - timedelta(hours=1), LATEST]
    assert window_start(started, seconds) == (0, True)


# The paths a window's growth depends on


def test_witness_dirs():
    changed = {
        b"needs": ((3, "readdir"), ok(5)),
        b"both": ((3, "readdir"), (5, "readdir")),
        b"end_only": (ok(3), (5, "readdir")),
        b"no_rbytes_start": ((None, "open"), ok(5)),
        b"no_rbytes_end": ((3, "readdir"), (None, "rstats")),
        b"new": (None, ok(5)),
        b"gone": ((3, "readdir"), None),
        b"rstats": ((None, "rstats"), ok(5)),
        b"stat": ((4, "stat"), ok(5)),
    }
    assert witness_dirs(changed) == [b"needs", b"stat"]
    assert witness_dirs({ROOT: ((3, "readdir"), ok(5))}) == [ROOT]


def test_kept_paths_adds_every_ancestor_once():
    assert kept_paths([b"a/b/c", b"a/d"]) == {ROOT, b"a", b"a/b", b"a/b/c", b"a/d"}
    assert kept_paths([ROOT]) == {ROOT}
    assert kept_paths([]) == set()


def test_window_ends():
    changed = {b"a": (ok(1), ok(2)), b"ghost": (None, None)}
    current = {ROOT: ok(9), b"a": ok(2), b"b": ok(3)}
    kept = {ROOT, b"a", b"b", b"ghost", b"unknown"}
    assert window_ends(changed, current, kept) == {
        ROOT: (ok(9), ok(9)),
        b"a": (ok(1), ok(2)),
        b"b": (ok(3), ok(3)),
    }


# The restricted set gives the full result


NAMES = (b"a", b"b", b"c")


def child(parent: bytes, name: bytes) -> bytes:
    return name if parent == ROOT else parent + b"/" + name


def below(path: bytes, ancestor: bytes) -> bool:
    return path != ROOT and (ancestor == ROOT or path.startswith(ancestor + b"/"))


def random_state(rng: random.Random) -> dict[bytes, End]:
    """Return a parent-closed tree of 1 to 40 paths, depth at most 4, some with
    errors."""
    state: dict[bytes, End] = {ROOT: ok(rng.randrange(100))}
    for _ in range(rng.randrange(40)):
        parent = rng.choice([p for p in state if path_depth(p) < 4])
        state[child(parent, rng.choice(NAMES))] = ok(rng.randrange(100))
    for path in state:
        roll = rng.random()
        if roll < 0.05:
            state[path] = (None, rng.choice(("open", "stat", "rstats")))
        elif roll < 0.25:  # unlisted, mostly with rstats
            rbytes = rng.randrange(100)
            state[path] = (rng.choice((None, rbytes, rbytes)), "readdir")
    return state


def unlisting(end: End) -> bool:
    return end is not None and end[1] in ("open", "stat", "readdir")


def random_end(rng: random.Random, start: dict[bytes, End]) -> dict[bytes, End]:
    """Return a state that START can change into within a window."""
    end = dict(start)
    # Some directories unlisted at the start are listed again and grow, and
    # everything below them stays as it was: only a witness tells growth
    # below them is unknown.
    relisted = [
        p
        for p, then in start.items()
        if then[1] == "readdir"
        and then[0] is not None
        and any(below(q, p) for q in start)
        and rng.random() < 0.7
    ]
    frozen = {q for q in start for p in relisted if below(q, p)}
    for path in relisted:
        end[path] = ok(start[path][0] + rng.randrange(1, 50))
    for path in start:
        if path in frozen or path in relisted:
            continue
        roll = rng.random()
        if roll < 0.2 and end[path][0] is not None:
            end[path] = (end[path][0] + rng.randrange(1, 50), end[path][1])
        elif roll < 0.3:  # fixed, or listed again with its stale children
            end[path] = ok(rng.randrange(100))
        elif roll < 0.35:  # unlisted, keeping its children as stale rows
            end[path] = (rng.choice((None, rng.randrange(100))), "readdir")
    for path in rng.sample(sorted(start), min(len(start), 2)):
        if path != ROOT and path not in frozen and rng.random() < 0.3:
            for gone in [p for p in end if p == path or below(p, path)]:
                del end[gone]
    listed = [p for p in end if p in start and not unlisting(start[p])]
    for _ in range(rng.randrange(4) if listed else 0):
        parent = rng.choice(listed)
        if path_depth(parent) < 4:
            end.setdefault(child(parent, rng.choice(NAMES)), ok(rng.randrange(100)))
    return end


def restricted(
    start: dict[bytes, End], end: dict[bytes, End], rng: random.Random, witness: bool
) -> dict[bytes, tuple[End, End]]:
    """Return own_growth's ENDS as the database would read them."""
    changed = {
        p: (start.get(p), end.get(p))
        for p in sorted(start.keys() | end.keys())  # an order the seed fixes
        if start.get(p) != end.get(p) or rng.random() < 0.1
    }
    for _ in range(rng.randrange(3)):  # created and deleted in the window
        changed.setdefault(child(rng.choice(sorted(end)), b"ghost"), (None, None))
    unknown = {p for p, now in end.items() if now[0] is None and p not in changed}
    witnesses = set()
    if witness:
        for u in witness_dirs(changed):
            first = min((p for p in end if below(p, u)), default=None)
            if first is not None:
                witnesses.add(first)
    return window_ends(changed, end, kept_paths(changed.keys() | unknown | witnesses))


def full(start: dict[bytes, End], end: dict[bytes, End]) -> dict:
    return {p: (start.get(p), end.get(p)) for p in start.keys() | end.keys()}


def test_restricted_ends_give_the_full_result():
    witnessed = 0
    for seed in range(300):
        rng = random.Random(seed)
        start = random_state(rng)
        end = random_end(rng, start)
        state = rng.getstate()
        ends = restricted(start, end, rng, witness=True)
        for top in (3, 1000):
            assert own_growth(ends, 4, top) == own_growth(full(start, end), 4, top), (
                seed
            )
        rng.setstate(state)
        if own_growth(restricted(start, end, rng, witness=False), 4, 1000) != (
            own_growth(full(start, end), 4, 1000)
        ):
            witnessed += 1
    # the witnesses mattered often enough to be tested
    assert witnessed >= 30


def test_restricted_root_unlisted_only_at_the_start():
    start = {ROOT: (10, "readdir"), b"a": ok(4), b"a/b": ok(1)}
    end = {ROOT: ok(15), b"a": ok(4), b"a/b": ok(1)}
    changed = {ROOT: (start[ROOT], end[ROOT])}
    assert witness_dirs(changed) == [ROOT]
    ends = window_ends(changed, end, kept_paths([ROOT, b"a"]))
    assert own_growth(ends, 4, 10) == own_growth(full(start, end), 4, 10)
    assert own_growth(ends, 4, 10)[1] == (DirGrowth(ROOT, 10, 15, 5, True),)
