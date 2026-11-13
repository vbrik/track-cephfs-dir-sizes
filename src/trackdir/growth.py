"""Own growth of directories between two states of a tree; pure: no database."""

import heapq
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

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
