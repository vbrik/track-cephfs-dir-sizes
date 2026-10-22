"""trackdir sample and trackdir db init: store samples of a tree in PostgreSQL.

A sample against an empty baseline (initial: the tree's first, or the first
after every directory was deleted) stores every directory; any other
(incremental) stores only the directories whose state changed since the
baseline, the tree's current state, and tombstones for deleted ones. Live
samples and replays run the same steps, each with its own database connection
or none: a pre-check (schema current, baseline loaded, a replay newer than
the latest sample) so a doomed run fails before a walk that can take hours;
the walk, pruned by the baseline, with no connection held; the diff against
the baseline; the store, in one transaction that fails if another sample was
stored meanwhile.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from trackdir.baseline import Baseline, diff, should_list_changed
from trackdir.capture import (
    Capture,
    CaptureFormatError,
    CaptureHeader,
    ReplayBackend,
    load_capture,
)
from trackdir.db import (
    SCHEMA_VERSION,
    TreeKey,
    check_order,
    check_schema,
    init_schema,
    load_baseline,
    store_sample,
    tree_key_of,
)
from trackdir.dbcli import DB_UNCHANGED, NOTHING_STORED, database, describe_tree
from trackdir.live import (
    DEFAULT_JOBS,
    DEFAULT_MAX_DIRS,
    open_live_root,
    walk_live,
    walk_within,
)
from trackdir.messages import FatalError, describe_errors, paragraph
from trackdir.model import DirProbe, ShouldList, error_counts
from trackdir.progress import NO_PROGRESS, Progress

# Ends the message of a walk over --max-dirs. Unlike capture's advice it does not
# suggest a lower --max-depth: the depth is part of the tree's identity, so that
# would start a new tree and leave this tree's history behind.
SAMPLE_LIMIT_ADVICE = (
    "Raise --max-dirs if memory allows; the tree stays the same. Another "
    "--max-depth would start a new tree with a full initial sample."
)


@dataclass(frozen=True, slots=True)
class SampleOptions:
    """What to sample and where to store it; see `trackdir sample --help`.

    Exactly one of root and replay is set. max_depth is required with root;
    with replay, None means the capture's.
    """

    dsn: str
    root: Path | None = None
    replay: Path | None = None
    max_depth: int | None = None
    max_dirs: int = DEFAULT_MAX_DIRS
    jobs: int = DEFAULT_JOBS  # live only; replay walks with one job


@dataclass(frozen=True, slots=True)
class SampleResult:
    """Outcome of a stored sample."""

    sample_id: int
    root: str  # resolved ROOT of the run that read the filesystem
    tree: TreeKey
    max_depth: int
    dirs: int  # probed
    changed: int  # rows stored, tombstones excluded
    deleted: int  # tombstones stored
    skipped: int  # unchanged directories not listed: pruned subtrees
    skipped_pinned: int  # of them, those with a pinned rctime
    errors: Counter[tuple[str, str]]  # (stage, code) -> number of directories
    walk_seconds: float
    store_seconds: float
    replayed_from: str | None  # absolute capture path; None for a live sample
    kind: Literal["initial", "incremental"]  # initial: nothing to compare with


@dataclass(frozen=True, slots=True)
class _Walk:
    """A pre-checked walk, ready to diff and store."""

    header: CaptureHeader
    key: TreeKey
    baseline: Baseline
    probes: list[DirProbe]
    seconds: float


def replay_walk(
    capture: Capture,
    max_depth: int,
    max_dirs: int,
    should_list: ShouldList | None = None,
    progress: Progress = NO_PROGRESS,
) -> tuple[CaptureHeader, list[DirProbe]]:
    """Walk CAPTURE down to MAX_DEPTH; return its header and the probes.

    SHOULD_LIST and PROGRESS are as in walk_within (SHOULD_LIST None: list
    everything above MAX_DEPTH). The header keeps everything that describes the
    run that read the filesystem (times, host, trackdir_version, anonymized);
    max_depth, dirs and errors describe this walk. Raises FatalError if
    MAX_DEPTH exceeds the capture's or more than MAX_DIRS directories are
    probed.
    """
    probes = walk_within(
        _replay_backend(capture, max_depth),
        max_depth=max_depth,
        max_dirs=max_dirs,
        jobs=1,
        should_list=should_list,
        limit_advice=SAMPLE_LIMIT_ADVICE,
        progress=progress,
    )
    header = replace(
        capture.header,
        max_depth=max_depth,
        dirs=len(probes),
        errors=error_counts(probes).total(),
    )
    return header, probes


def run_sample(
    opts: SampleOptions,
    warn: Callable[[str], None],
    *,
    progress: Progress = NO_PROGRESS,
) -> SampleResult:
    """Store a sample of the tree at opts.root or in opts.replay.

    The sample is initial if the tree's baseline is empty, else incremental. Calls
    WARN once per degraded mode of a live ROOT, before walking. Shows each step
    and the walk's counts on PROGRESS. Raises FatalError, before walking if the
    database cannot take the sample; KeyboardInterrupt propagates. A sample is
    stored only if this returns.
    """
    if opts.replay is None:
        walked = _walk_live(opts, warn, progress)
        replayed_from = None
    else:
        walked = _walk_replay(opts, progress)
        replayed_from = str(opts.replay.resolve())
    header, baseline = walked.header, walked.baseline
    progress.phase("comparing with the baseline")
    started = datetime.fromisoformat(header.started)
    sample_diff = diff(baseline, walked.probes, header.max_depth, started)
    progress.phase(
        f"storing {len(sample_diff.changed):,} directories and "
        f"{len(sample_diff.deleted):,} deletions"
    )
    clock = time.monotonic()
    with database(opts.dsn, NOTHING_STORED) as conn:
        sample_id = store_sample(
            conn, header, walked.key, baseline, sample_diff, replayed_from
        )
    return SampleResult(
        sample_id=sample_id,
        root=header.root,
        tree=walked.key,
        max_depth=header.max_depth,
        dirs=header.dirs,
        changed=len(sample_diff.changed),
        deleted=len(sample_diff.deleted),
        skipped=sample_diff.skipped,
        skipped_pinned=sample_diff.skipped_pinned,
        errors=error_counts(walked.probes),
        walk_seconds=walked.seconds,
        store_seconds=time.monotonic() - clock,
        replayed_from=replayed_from,
        kind="incremental" if baseline.rows else "initial",
    )


def render_sample_summary(result: SampleResult) -> str:
    """Return the success message for RESULT as one wrapped paragraph."""
    where = (
        f"under {result.root} to depth {result.max_depth}, {describe_tree(result.tree)}"
    )
    if result.kind == "initial":
        counts = f"{result.dirs} directories {where}"
    else:
        counts = (
            f"probed {result.dirs} directories {where}; {result.changed} changed, "
            f"{result.deleted} deleted, {result.skipped} unchanged subtrees skipped"
        )
        if result.skipped_pinned:
            counts += (
                f" ({result.skipped_pinned} with a pinned rctime, under which a "
                "same-size rewrite goes unseen)"
            )
    text = (
        f"Stored sample {result.sample_id} ({result.kind}): {counts}; "
        f"{describe_errors(result.errors)}. Walk {result.walk_seconds:.1f} s, "
        f"store {result.store_seconds:.1f} s."
    )
    if result.replayed_from is not None:
        text += f" Replayed from {result.replayed_from}."
    return paragraph(text)


def run_db_init(dsn: str) -> bool:
    """Create the trackdir schema in DSN's database; return whether it was created.

    Returns False if it already exists at SCHEMA_VERSION. Raises FatalError.
    """
    with database(dsn, DB_UNCHANGED) as conn:
        return init_schema(conn)


def render_db_init(created: bool) -> str:
    """Return the success message of db init as one wrapped paragraph."""
    if created:
        return paragraph(
            f"Created the trackdir schema (version {SCHEMA_VERSION}) in the database."
        )
    return paragraph(
        f"The trackdir schema (version {SCHEMA_VERSION}) already exists; "
        "nothing changed."
    )


def _walk_live(
    opts: SampleOptions, warn: Callable[[str], None], progress: Progress
) -> _Walk:
    """Pre-check, then walk opts.root pruned by the baseline."""
    assert opts.root is not None and opts.max_depth is not None
    with open_live_root(opts.root, warn) as live:
        key = TreeKey.of(
            live.cluster_fsid,
            live.fs_name,
            live.fs_path,
            opts.max_depth,
        )
        # The real start is later than now, so this order check never refuses
        # falsely; it fails before the walk if the tree's latest sample is
        # dated in the future (clock skew, or another host sampled the tree).
        progress.phase("loading the baseline")
        baseline = _precheck(opts.dsn, key, datetime.now(UTC))
        walked = walk_live(
            live,
            max_depth=opts.max_depth,
            max_dirs=opts.max_dirs,
            jobs=opts.jobs,
            should_list=should_list_changed(baseline),
            limit_advice=SAMPLE_LIMIT_ADVICE,
            progress=progress,
        )
    return _Walk(walked.header, key, baseline, walked.probes, walked.elapsed)


def _walk_replay(opts: SampleOptions, progress: Progress) -> _Walk:
    """Pre-check, then walk opts.replay pruned by the baseline."""
    assert opts.replay is not None
    progress.phase("loading the capture")
    capture = _load(opts.replay)
    max_depth = capture.header.max_depth if opts.max_depth is None else opts.max_depth
    _replay_backend(capture, max_depth)  # fails on the depth before the pre-check
    key = tree_key_of(replace(capture.header, max_depth=max_depth))
    started = datetime.fromisoformat(capture.header.started)
    progress.phase("loading the baseline")
    baseline = _precheck(opts.dsn, key, started)
    clock = time.monotonic()
    header, probes = replay_walk(
        capture, max_depth, opts.max_dirs, should_list_changed(baseline), progress
    )
    return _Walk(header, key, baseline, probes, time.monotonic() - clock)


def _load(path: Path) -> Capture:
    try:
        return load_capture(path)
    except CaptureFormatError as e:
        raise FatalError(f"cannot read the capture {path}: {e}.") from e
    except OSError as e:
        raise FatalError(f"cannot read the capture {path}: {e.strerror}.") from e


def _replay_backend(capture: Capture, max_depth: int) -> ReplayBackend:
    try:
        return ReplayBackend(capture, max_depth)
    except ValueError as e:
        raise FatalError(f"{e}.") from e


def _precheck(dsn: str, key: TreeKey, started: datetime) -> Baseline:
    """Return tree KEY's baseline; raise FatalError unless a sample can be stored.

    The schema must be current and STARTED (a replayed capture's, or for a live
    sample the current time) later than the tree's latest sample. The
    connection is closed on return.
    """
    with database(dsn, NOTHING_STORED) as conn:
        check_schema(conn)
        baseline = load_baseline(conn, key)
        check_order(baseline, key, started)
    return baseline
