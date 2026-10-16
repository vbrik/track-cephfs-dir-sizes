"""The baseline of a tree and the diff of a walk against it.

The baseline is the tree's current state: per path, the latest non-deleted
dir_state row. should_list_changed() prunes a walk to the directories whose row
differs from the baseline, and diff() turns the probes of such a walk into the
rows to store and the paths to tombstone. An initial sample is the same thing
with an empty baseline. The module is pure: no database, no filesystem.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Self

from trackdir.model import (
    ROOT,
    DirProbe,
    ProbeError,
    ShouldList,
    Stage,
    join,
    split,
    tree_key,
)

# The dir_state columns after sample_id and path, in the order row_of() gives them.
ROW_COLUMNS = (
    "ino",
    "mode",
    "uid",
    "gid",
    "nlink",
    "rctime",
    "rbytes",
    "rfiles",
    "rsubdirs",
    "error_stage",
    "error_code",
)

type Row = tuple[int | str | None, ...]

_ERROR_STAGE = ROW_COLUMNS.index("error_stage")
_OPEN_ENOENT = ProbeError(Stage.OPEN, "ENOENT")  # the directory is gone


def row_of(probe: DirProbe) -> Row:
    """Return PROBE's values for ROW_COLUMNS; unknown values are None."""
    error = probe.error
    return (
        probe.ino,
        probe.mode,
        probe.uid,
        probe.gid,
        probe.nlink,
        probe.rctime,
        probe.rbytes,
        probe.rfiles,
        probe.rsubdirs,
        str(error.stage) if error else None,
        error.code if error else None,
    )


@dataclass(frozen=True, slots=True)
class Baseline:
    """A tree's current state, as of its latest sample.

    sample_id and started are the latest sample's (None: the tree has no
    samples). rows maps each non-deleted path to its row. retry holds the paths
    to list even if unchanged: those with an error row, and their ancestors.
    children maps a path to the full paths of its subdirectories in rows.
    The rows must be parent-closed (every row's parent directory also has a
    row), as the current-state view guarantees.
    """

    sample_id: int | None
    started: datetime | None
    rows: Mapping[bytes, Row]
    retry: frozenset[bytes]
    children: Mapping[bytes, tuple[bytes, ...]]

    @classmethod
    def empty(cls) -> Self:
        """Return the baseline of a tree with no samples."""
        return cls(None, None, {}, frozenset(), {})

    @classmethod
    def build(
        cls, sample_id: int, started: datetime, rows: Mapping[bytes, Row]
    ) -> Self:
        """Return the baseline of sample SAMPLE_ID, whose current state is ROWS."""
        retry: set[bytes] = set()
        for start, row in rows.items():
            if row[_ERROR_STAGE] is None:
                continue
            # Walk up until an already-collected path, whose ancestors are too.
            path = start
            while path not in retry:
                retry.add(path)
                if path == ROOT:
                    break
                path = split(path)[0]
        children: dict[bytes, list[bytes]] = {}
        for path in rows:
            if path != ROOT:
                children.setdefault(split(path)[0], []).append(path)
        return cls(
            sample_id,
            started,
            rows,
            frozenset(retry),
            {parent: tuple(paths) for parent, paths in children.items()},
        )


def should_list_changed(baseline: Baseline) -> ShouldList:
    """Return a should_list that lists a directory only if needed.

    A directory is listed if its row differs from its baseline row (a directory
    without one is new) or if it is in baseline.retry. Pruning is sound because
    the rstats describe the whole subtree. The depth limit is the walk's to
    enforce, as walk_within does. The result only reads BASELINE, so it is safe
    to call from several threads.
    """

    def should_list(probe: DirProbe) -> bool:
        return (
            row_of(probe) != baseline.rows.get(probe.path)
            or probe.path in baseline.retry
        )

    return should_list


@dataclass(frozen=True, slots=True)
class SampleDiff:
    """What a walk adds to the baseline.

    changed: probes to store as rows (changed, new or newly failed), in tree
    order. deleted: paths to tombstone, in tree order. skipped: directories the
    walk pruned.
    """

    changed: list[DirProbe]
    deleted: list[bytes]
    skipped: int


def diff(baseline: Baseline, probes: Sequence[DirProbe], max_depth: int) -> SampleDiff:
    """Compare the PROBES of a walk (in tree order) with BASELINE.

    changed holds every probe whose row differs from its baseline row (a
    missing baseline row differs), except "open ENOENT" probes, which mean
    deletion. deleted holds the baseline children missing from the listing of a
    successfully listed parent, and the paths of "open ENOENT" probes that have
    a baseline row, plus all baseline descendants of those. Nothing is deleted
    below a directory that was not listed. skipped counts the unlisted
    directories without an error above MAX_DEPTH: those the walk pruned.
    """
    changed: list[DirProbe] = []
    gone: set[bytes] = set()
    skipped = 0
    for probe in probes:
        if probe.children is not None:
            present = {join(probe.path, name) for name in probe.children}
            gone.update(set(baseline.children.get(probe.path, ())) - present)
        elif probe.error is None and probe.depth < max_depth:
            skipped += 1
        if probe.error == _OPEN_ENOENT:
            if probe.path in baseline.rows:
                gone.add(probe.path)
        elif row_of(probe) != baseline.rows.get(probe.path):
            changed.append(probe)
    deleted: set[bytes] = set()
    pending = list(gone)
    while pending:
        path = pending.pop()
        if path not in deleted:
            deleted.add(path)
            pending.extend(baseline.children.get(path, ()))
    return SampleDiff(changed, sorted(deleted, key=tree_key), skipped)
