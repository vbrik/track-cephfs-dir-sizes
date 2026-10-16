"""trackdir sample and trackdir db init: store a tree's first sample in PostgreSQL.

A sample runs in three steps, each with its own database connection or none:
a pre-check (schema current, tree has no samples) so a doomed run fails
before a walk that can take hours; the walk, with no connection held; the
store, in one transaction that re-checks for samples stored meanwhile.
"""

import contextlib
import os
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from trackdir.capture import (
    Capture,
    CaptureFormatError,
    CaptureHeader,
    ReplayBackend,
    load_capture,
)
from trackdir.db import (
    SCHEMA_VERSION,
    DbError,
    PsycopgMissing,
    SamplesExist,
    SchemaMissing,
    SchemaVersionMismatch,
    TreeKey,
    check_schema,
    connect,
    has_samples,
    init_schema,
    store_initial_sample,
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
from trackdir.model import DirProbe, error_counts

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
    dirs: int
    errors: Counter[tuple[str, str]]  # (stage, code) -> number of directories
    walk_seconds: float
    store_seconds: float
    replayed_from: str | None  # absolute capture path; None for a live sample


def replay_walk(
    capture: Capture, max_depth: int, max_dirs: int
) -> tuple[CaptureHeader, list[DirProbe]]:
    """Walk CAPTURE down to MAX_DEPTH; return its header and the probes.

    The header keeps everything that describes the run that read the
    filesystem (times, host, trackdir_version, anonymized); max_depth, dirs
    and errors describe this walk. Raises FatalError if MAX_DEPTH exceeds the
    capture's or there are more than MAX_DIRS directories.
    """
    probes = walk_within(
        _replay_backend(capture, max_depth),
        max_depth=max_depth,
        max_dirs=max_dirs,
        jobs=1,
    )
    header = replace(
        capture.header,
        max_depth=max_depth,
        dirs=len(probes),
        errors=error_counts(probes).total(),
    )
    return header, probes


def run_sample(opts: SampleOptions, warn: Callable[[str], None]) -> SampleResult:
    """Store the first sample of the tree at opts.root or in opts.replay.

    Calls WARN once per degraded mode of a live ROOT, before walking. Raises
    FatalError, before walking if the database cannot take the sample;
    KeyboardInterrupt propagates. A sample is stored only if this returns.
    """
    if opts.replay is None:
        header, probes, walk_seconds = _walk_live(opts, warn)
        replayed_from = None
    else:
        header, probes, walk_seconds = _walk_replay(opts)
        replayed_from = str(opts.replay.resolve())
    clock = time.monotonic()
    with _database(opts.dsn, NOTHING_STORED) as conn:
        sample_id = store_initial_sample(conn, header, probes, replayed_from)
    return SampleResult(
        sample_id=sample_id,
        root=header.root,
        tree=tree_key_of(header),
        max_depth=header.max_depth,
        dirs=header.dirs,
        errors=error_counts(probes),
        walk_seconds=walk_seconds,
        store_seconds=time.monotonic() - clock,
        replayed_from=replayed_from,
    )


def render_sample_summary(result: SampleResult) -> str:
    """Return the success message for RESULT as one wrapped paragraph."""
    text = (
        f"Stored sample {result.sample_id}: {result.dirs} directories under "
        f"{result.root} to depth {result.max_depth}, {describe_tree(result.tree)}; "
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


def _walk_live(
    opts: SampleOptions, warn: Callable[[str], None]
) -> tuple[CaptureHeader, list[DirProbe], float]:
    """Pre-check, then walk opts.root; return the header, probes and walk seconds."""
    assert opts.root is not None and opts.max_depth is not None
    with open_live_root(opts.root, warn) as live:
        _precheck(opts.dsn, TreeKey.of(live.cluster_fsid, live.fs_name, live.fs_path))
        walked = walk_live(
            live, max_depth=opts.max_depth, max_dirs=opts.max_dirs, jobs=opts.jobs
        )
    return walked.header, walked.probes, walked.elapsed


def _walk_replay(opts: SampleOptions) -> tuple[CaptureHeader, list[DirProbe], float]:
    """Pre-check, then walk opts.replay; return the header, probes and walk seconds."""
    assert opts.replay is not None
    capture = _load(opts.replay)
    max_depth = capture.header.max_depth if opts.max_depth is None else opts.max_depth
    _replay_backend(capture, max_depth)  # fails on the depth before the pre-check
    _precheck(opts.dsn, tree_key_of(capture.header))
    clock = time.monotonic()
    header, probes = replay_walk(capture, max_depth, opts.max_dirs)
    return header, probes, time.monotonic() - clock


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


def _precheck(dsn: str, key: TreeKey) -> None:
    """Raise FatalError unless the schema is current and tree KEY has no samples."""
    with _database(dsn, NOTHING_STORED) as conn:
        check_schema(conn)
        if has_samples(conn, key):
            raise SamplesExist(key)


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
        case SamplesExist(key=key):
            return (
                f"the {describe_tree(key)} already has samples. This version of "
                "trackdir stores only a tree's first sample; incremental "
                "sampling is not implemented yet."
            )
        case _:
            # psycopg's text can span lines and end in punctuation of its own.
            detail = " ".join(str(e).split())
            if not detail.endswith((".", "?", "!")):
                detail += "."
            return f"database error: {detail} {unchanged[0].upper()}{unchanged[1:]}."


def describe_tree(key: TreeKey) -> str:
    """Return "tree (cluster FSID, filesystem NAME, path PATH)" for KEY.

    "default filesystem" stands in for a None fs_name.
    """
    fs = "default filesystem" if key.fs_name is None else f"filesystem {key.fs_name}"
    return f"tree (cluster {key.cluster_fsid}, {fs}, path {os.fsdecode(key.fs_path)})"
