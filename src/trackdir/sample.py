"""trackdir sample and trackdir db init: store samples of a tree in PostgreSQL.

A tree's first sample (initial) stores every directory; later ones
(incremental) store only the directories whose state changed since the
baseline, the tree's current state, and tombstones for deleted ones. Live
samples and replays run the same steps, each with its own database connection
or none: a pre-check (schema current, baseline loaded, a replay newer than
the latest sample) so a doomed run fails before a walk that can take hours;
the walk, pruned by the baseline, with no connection held; the diff against
the baseline; the store, in one transaction that fails if another sample was
stored meanwhile.
"""

import contextlib
import os
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
    ConcurrentSample,
    DbError,
    OutOfOrderSample,
    PsycopgMissing,
    SchemaMissing,
    SchemaVersionMismatch,
    TreeKey,
    check_order,
    check_schema,
    connect,
    init_schema,
    load_baseline,
    store_sample,
    tree_key_of,
)
from trackdir.live import (
    DEFAULT_JOBS,
    DEFAULT_MAX_DIRS,
    open_live_root,
    walk_live,
    walk_within,
)
from trackdir.messages import FatalError, describe_errors, paragraph
from trackdir.model import DirProbe, ShouldList, error_counts

if TYPE_CHECKING:
    import psycopg

DSN_ENV = "TRACKDIR_DB"  # environment variable with the DSN when --db is absent
# What a failed or interrupted command leaves, as a lowercase clause.
NOTHING_STORED = "nothing was stored"
DB_UNCHANGED = "the database was not changed"
# Next step for a schema this trackdir cannot use; ends the message.
_DROP_AND_INIT = (
    "drop schema trackdir, which deletes all stored samples, and run: trackdir db init"
)
# Ends the message of a walk over --max-dirs: raising it splits the history.
SAMPLE_LIMIT_ADVICE = (
    "--max-dirs and --max-depth are part of a tree's identity: a higher "
    "--max-dirs or another --max-depth starts a new tree with a full initial "
    "sample."
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
    errors: Counter[tuple[str, str]]  # (stage, code) -> number of directories
    walk_seconds: float
    store_seconds: float
    replayed_from: str | None  # absolute capture path; None for a live sample
    kind: Literal["initial", "incremental"]  # initial: the tree had no sample


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
) -> tuple[CaptureHeader, list[DirProbe]]:
    """Walk CAPTURE down to MAX_DEPTH; return its header and the probes.

    SHOULD_LIST is as in walk_within (None: list everything above MAX_DEPTH).
    The header keeps everything that describes the run that read the
    filesystem (times, host, trackdir_version, anonymized); max_depth, dirs
    and errors describe this walk. Raises FatalError if MAX_DEPTH exceeds the
    capture's or more than MAX_DIRS directories are probed.
    """
    probes = walk_within(
        _replay_backend(capture, max_depth),
        max_depth=max_depth,
        max_dirs=max_dirs,
        jobs=1,
        should_list=should_list,
        limit_advice=SAMPLE_LIMIT_ADVICE,
    )
    header = replace(
        capture.header,
        max_depth=max_depth,
        dirs=len(probes),
        errors=error_counts(probes).total(),
    )
    return header, probes


def run_sample(opts: SampleOptions, warn: Callable[[str], None]) -> SampleResult:
    """Store a sample of the tree at opts.root or in opts.replay.

    The sample is initial if the tree has none yet, else incremental. Calls
    WARN once per degraded mode of a live ROOT, before walking. Raises
    FatalError, before walking if the database cannot take the sample;
    KeyboardInterrupt propagates. A sample is stored only if this returns.
    """
    if opts.replay is None:
        walked = _walk_live(opts, warn)
        replayed_from = None
    else:
        walked = _walk_replay(opts)
        replayed_from = str(opts.replay.resolve())
    header, baseline = walked.header, walked.baseline
    sample_diff = diff(baseline, walked.probes, header.max_depth)
    clock = time.monotonic()
    with _database(opts.dsn, NOTHING_STORED) as conn:
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
        errors=error_counts(walked.probes),
        walk_seconds=walked.seconds,
        store_seconds=time.monotonic() - clock,
        replayed_from=replayed_from,
        kind="initial" if baseline.sample_id is None else "incremental",
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
    with _database(dsn, DB_UNCHANGED) as conn:
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


def _walk_live(opts: SampleOptions, warn: Callable[[str], None]) -> _Walk:
    """Pre-check, then walk opts.root pruned by the baseline."""
    assert opts.root is not None and opts.max_depth is not None
    with open_live_root(opts.root, warn) as live:
        key = TreeKey.of(
            live.cluster_fsid,
            live.fs_name,
            live.fs_path,
            opts.max_depth,
            opts.max_dirs,
        )
        # The real start is later than now, so this order check never refuses
        # falsely; it fails before the walk if the tree's latest sample is
        # dated in the future (clock skew, or another host sampled the tree).
        baseline = _precheck(opts.dsn, key, datetime.now(UTC))
        walked = walk_live(
            live,
            max_depth=opts.max_depth,
            max_dirs=opts.max_dirs,
            jobs=opts.jobs,
            should_list=should_list_changed(baseline, opts.max_depth),
            limit_advice=SAMPLE_LIMIT_ADVICE,
        )
    return _Walk(walked.header, key, baseline, walked.probes, walked.elapsed)


def _walk_replay(opts: SampleOptions) -> _Walk:
    """Pre-check, then walk opts.replay pruned by the baseline."""
    assert opts.replay is not None
    capture = _load(opts.replay)
    max_depth = capture.header.max_depth if opts.max_depth is None else opts.max_depth
    _replay_backend(capture, max_depth)  # fails on the depth before the pre-check
    key = tree_key_of(replace(capture.header, max_depth=max_depth), opts.max_dirs)
    started = datetime.fromisoformat(capture.header.started)
    baseline = _precheck(opts.dsn, key, started)
    clock = time.monotonic()
    header, probes = replay_walk(
        capture, max_depth, opts.max_dirs, should_list_changed(baseline, max_depth)
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


def _precheck(dsn: str, key: TreeKey, started: datetime | None = None) -> Baseline:
    """Return tree KEY's baseline; raise FatalError unless a sample can be stored.

    The schema must be current and, if STARTED (a replayed capture's, or for a
    live sample the current time) is given, later than the tree's latest
    sample. The connection is closed on return.
    """
    with _database(dsn, NOTHING_STORED) as conn:
        check_schema(conn)
        baseline = load_baseline(conn, key)
        if started is not None:
            check_order(baseline, key, started)
    return baseline


@contextlib.contextmanager
def _database(dsn: str, unchanged: str) -> Iterator["psycopg.Connection"]:
    """Connect to DSN, yield the connection and close it; DbError becomes FatalError.

    UNCHANGED is what a database error leaves, e.g. NOTHING_STORED.
    """
    try:
        with connect(dsn) as conn:
            yield conn
    except DbError as e:
        raise FatalError(_db_message(e, unchanged)) from e


def _db_message(e: DbError, unchanged: str) -> str:
    """Return the FatalError message for E: what failed and what to do.

    UNCHANGED ends the message of an unexpected database error.
    """
    match e:
        case PsycopgMissing():
            return (
                "the PostgreSQL driver psycopg is not installed. Install it with: "
                "pip install 'trackdir[postgres]'"
            )
        case SchemaMissing():
            return (
                "the database has no trackdir schema. Create it with: trackdir db init"
            )
        case SchemaVersionMismatch(found=None):
            return (
                "the database's trackdir schema is incomplete (no version "
                f"recorded), but this trackdir needs version {SCHEMA_VERSION}. "
                f"An administrator must {_DROP_AND_INIT}"
            )
        case SchemaVersionMismatch(found=found):
            return (
                f"the database's trackdir schema is version {found}, from "
                f"another trackdir version, but this trackdir needs version "
                f"{SCHEMA_VERSION}. An administrator must migrate it, or "
                f"{_DROP_AND_INIT}"
            )
        case ConcurrentSample(key=key):
            return (
                f"another sample of the {describe_tree(key)} was stored while this "
                f"one ran; {NOTHING_STORED}. Run trackdir sample again."
            )
        case OutOfOrderSample(key=key, started=started, latest_started=latest):
            return (
                f"this sample started at {_utc(started)}, which is not later than "
                f"the latest sample of the {describe_tree(key)} ({_utc(latest)}); "
                f"{NOTHING_STORED}. A sample must start later than the tree's "
                "latest sample: check the clocks of the hosts that sample this "
                "tree, and for a replay use a newer capture."
            )
        case _:
            # psycopg's text can span lines and end in punctuation of its own.
            detail = " ".join(str(e).split())
            if not detail.endswith((".", "?", "!")):
                detail += "."
            return f"database error: {detail} {unchanged[0].upper()}{unchanged[1:]}."


def describe_tree(key: TreeKey) -> str:
    """Return "tree (cluster FSID, filesystem NAME, path PATH, depth N, max dirs M)".

    "default filesystem" stands in for a None fs_name.
    """
    fs = "default filesystem" if key.fs_name is None else f"filesystem {key.fs_name}"
    return (
        f"tree (cluster {key.cluster_fsid}, {fs}, path {os.fsdecode(key.fs_path)}, "
        f"depth {key.max_depth}, max dirs {key.max_dirs})"
    )


def _utc(moment: datetime) -> str:
    """Return MOMENT in ISO 8601 and UTC, as capture headers give it."""
    return moment.astimezone(UTC).isoformat()
