"""Own growth of directories between two states of a tree; pure: no database.

A growth window runs from one sample of a tree to its latest. Its growth
depends on few of the tree's paths: those stored in the window, those without
rstats now, a witness below each directory listed again in the window, and
their ancestors (see docs/superpowers/plans/2026-11-12-growth-windows.md,
"Design"). The functions below choose them; the database reads them.
"""

import heapq
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trackdir.model import ROOT, UNLISTING_STAGES, path_depth, split

# A path's (rbytes, error stage) at one end of a growth window; None: absent.
type End = tuple[int | None, str | None] | None


@dataclass(frozen=True, slots=True)
class DirGrowth:
    """A directory's rbytes at the start and the end of a growth window.

    Its own growth is its rbytes growth minus that of the directories below it
    that get their own: the growth of its files, not its subdirectories'.
    """

    path: bytes
    rbytes_before: int | None  # None: the directory is new in the window
    rbytes_after: int
    own: int
    # own may hold subdirectories' growth: those below max depth, below an
    # unlisted directory, or without a known growth of their own
    subtree: bool


def own_growth(
    ends: Mapping[bytes, tuple[End, End]], max_depth: int, top: int
) -> tuple[int, tuple[DirGrowth, ...]]:
    """Return the sum of all positive own growths, and the TOP directories with
    the most own growth, most first, ties by path.

    ENDS maps each path present at either end of a window to its states at the
    start and at the end; the tree is sampled to MAX_DEPTH. A present path
    grows by its rbytes at the end minus those at the start, from 0 if new; a
    path gone by the end by minus its rbytes at the start. Growth is unknown
    without rstats, and below a directory unlisted at either end: its subtree
    was not read, so a row there may predate the window's start or end, or a
    new path may have existed unseen. A path's own growth is its growth minus
    that of each path whose nearest ancestor with a known growth it is; such a
    path with an unknown growth makes it a subtree, as does being unlisted at
    the end or at MAX_DEPTH. Directories gone by the end, with an unknown
    growth or with an own growth of 0 or less are left out.
    """

    def unlisting(end: End) -> bool:
        return end is not None and end[1] in UNLISTING_STAGES

    unread: dict[bytes, bool] = {}
    for path in sorted(ends, key=path_depth):
        if path != ROOT:
            parent = split(path)[0]
            unread[path] = unread.get(parent, False) or any(
                map(unlisting, ends.get(parent, ()))
            )
    grew: dict[bytes, int | None] = {}
    for path, (then, now) in ends.items():
        if unread.get(path):
            grew[path] = None
        elif now is None:
            grew[path] = None if then is None or then[0] is None else -then[0]
        elif now[0] is None or (then is not None and then[0] is None):
            grew[path] = None
        else:
            grew[path] = now[0] - (0 if then is None else then[0])
    below: Counter[bytes] = Counter()
    hidden: set[bytes] = set()
    for path, growth in grew.items():
        owner = path
        while owner != ROOT:
            owner = split(owner)[0]
            if grew.get(owner) is not None:
                if growth is None:
                    hidden.add(owner)
                else:
                    below[owner] += growth
                break
    owned = []
    for path, growth in grew.items():
        then, now = ends[path]
        if now is None or growth is None or growth - below[path] <= 0:
            continue
        subtree = path in hidden or unlisting(now) or path_depth(path) >= max_depth
        before = None if then is None else then[0]
        owned.append(DirGrowth(path, before, now[0], growth - below[path], subtree))
    positive = sum(d.own for d in owned)
    return positive, tuple(heapq.nsmallest(top, owned, key=lambda d: (-d.own, d.path)))


@dataclass(frozen=True, slots=True)
class Window:
    """A growth window's length: the text it was given as, and its seconds."""

    text: str  # e.g. "7d"
    seconds: int


DEFAULT_WINDOWS = (Window("1d", 86_400), Window("7d", 604_800))


def window_start(started: Sequence[datetime], seconds: int) -> tuple[int, bool] | None:
    """Return where a window of SECONDS starts, and whether it is clipped.

    STARTED are a tree's samples' start times, oldest first. The window ends at
    the last and starts at the latest sample that started at least SECONDS
    before it; if none did, it starts at the first and is clipped: the tree's
    history is shorter. None with fewer than 2 samples.
    """
    if len(started) < 2:
        return None
    try:
        start = started[-1] - timedelta(seconds=seconds)
    except OverflowError:  # before the year 1: before the first sample too
        return 0, True
    index = bisect_right(started, start) - 1
    return (0, True) if index < 0 else (index, False)


def witness_dirs(changed: Mapping[bytes, tuple[End, End]]) -> list[bytes]:
    """Return the paths of CHANGED that need a witness, sorted.

    CHANGED maps the paths stored in a window to their two ends. A directory
    unlisted at the start but not at the end, with rbytes at both, owns the
    unknown growth of the paths below it, which need not be stored in the
    window: one current path below it, any, gives it that mark.
    """
    return sorted(
        path
        for path, (start, end) in changed.items()
        if start is not None
        and end is not None
        and start[1] in UNLISTING_STAGES
        and end[1] not in UNLISTING_STAGES
        and start[0] is not None
        and end[0] is not None
    )


def kept_paths(seeds: Iterable[bytes]) -> set[bytes]:
    """Return SEEDS and all their ancestors, ROOT included."""
    kept: set[bytes] = set()
    for path in seeds:
        while path not in kept:
            kept.add(path)
            if path == ROOT:
                break
            path = split(path)[0]
    return kept


def window_ends(
    changed: Mapping[bytes, tuple[End, End]],
    current: Mapping[bytes, End],
    kept: Iterable[bytes],
) -> dict[bytes, tuple[End, End]]:
    """Return own_growth's ENDS for the KEPT paths.

    A path in CHANGED has its ends there; any other is unchanged, at its state
    in CURRENT at both. A path absent at both ends is left out, as is one in
    neither mapping.
    """
    ends: dict[bytes, tuple[End, End]] = {}
    for path in kept:
        if path in changed:
            if changed[path] != (None, None):
                ends[path] = changed[path]
        elif path in current:
            ends[path] = (current[path], current[path])
    return ends
