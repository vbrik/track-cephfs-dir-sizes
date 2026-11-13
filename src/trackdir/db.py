"""PostgreSQL storage of samples: the schema, the baseline, the sample store and
the statistics of what is stored.

psycopg is imported only when a function needs it, so the rest of trackdir,
capture included, works without it. Errors raised by psycopg inside this
module's functions become DbError; KeyboardInterrupt propagates as is.
Writes run in one transaction each, so a failure or interrupt stores nothing.
"""

import heapq
import os
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Literal, Self

from trackdir.baseline import ROW_COLUMNS, Baseline, Row, SampleDiff, row_of
from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirLimitExceeded, DirProbe, path_depth, split

if TYPE_CHECKING:
    import psycopg

SCHEMA = "trackdir"  # the DDL and queries fix this name; it is not a setting
# A tree's name: 1 to 64 of these characters, the first a letter or digit. The
# DDL's CHECK on tree.name is built from the same pattern.
TREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TREE_NAME_RULE = (
    "1 to 64 characters of A-Z, a-z, 0-9, '.', '_' and '-', the first a letter or digit"
)

# A sample that stores at least this many rows refreshes the planner's
# statistics of path, current_dir_state and dir_state (see store_sample), and
# autovacuum refreshes them after this many changed rows (see the DDL). A join
# planned from stale statistics costs about the sample's rows times the rows
# the statistics do not know of: this bounds the first factor in the store,
# and autovacuum the second between samples.
ANALYZE_AFTER = 1000

SCHEMA_VERSION = 5  # of DDL; version 4 cannot be migrated, a mismatch is fatal

# The schema-v5 spec's section 4. path names every directory a tree ever held,
# once; a path created again keeps its id. dir_state holds the rows a sample
# stored because they changed (tombstones included), not a full listing. Because
# a sample's rows are not a listing, a sample cannot be deleted on its own:
# dir_state's foreign key to sample does not cascade, so deleting a sample that
# has rows fails. A tree is removed by deleting its dir_state rows, its
# current_dir_state rows, its samples, its paths, then the tree row.
#
# Three things repeat what dir_state's history says, so that no reader has to
# sort it: current_dir_state is each live path's latest row (a tombstone removes
# the row); a row's prev_* are the rstats and error stage of its path's row
# before the sample (NULL without one, or without them there); created says
# there was none;
# a sample's new_dirs, modified_dirs and deleted_dirs count its rows.
# store_sample alone derives them, in the transaction that stores the rows.
DDL = """\
CREATE SCHEMA trackdir;

CREATE TABLE trackdir.meta (
    schema_version integer NOT NULL
);  -- exactly one row

CREATE TABLE trackdir.tree (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text  NOT NULL UNIQUE  -- the user's; the next four are what it stands for
                 CHECK (name ~ '^TREE_NAME$'),
    cluster_fsid text  NOT NULL,
    fs_name      text,            -- NULL: the cluster's default filesystem
    fs_path      bytea NOT NULL,  -- ROOT inside the filesystem; b'/' is its root
    max_depth    integer NOT NULL CHECK (max_depth >= 0),
    UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path, max_depth)
);

CREATE TABLE trackdir.sample (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id          bigint      NOT NULL REFERENCES trackdir.tree,
    root             bytea       NOT NULL,  -- ROOT's resolved local path
    rstat_mode       text        NOT NULL CHECK (rstat_mode IN ('dirstat', 'getxattr')),
    openat2          boolean     NOT NULL,
    jobs             integer     NOT NULL,
    started          timestamptz NOT NULL,
    finished         timestamptz NOT NULL,
    euid             bigint      NOT NULL,
    host             text,
    kernel           text        NOT NULL,
    trackdir_version text        NOT NULL,  -- of the program that read the filesystem
    dirs             bigint      NOT NULL,  -- probed directories
    errors           bigint      NOT NULL,  -- dirs with an error
    anonymized       boolean     NOT NULL,
    replayed_from    text,                  -- absolute capture path; NULL for live samples
    stored           timestamptz NOT NULL DEFAULT now(),
    new_dirs         bigint      NOT NULL DEFAULT 0,  -- rows stored with created
    modified_dirs    bigint      NOT NULL DEFAULT 0,  -- rows neither created nor deleted
    deleted_dirs     bigint      NOT NULL DEFAULT 0,  -- tombstones stored
    baseline_seconds double precision NOT NULL,       -- loading the baseline
    store_seconds    double precision NOT NULL DEFAULT 0  -- the store, up to its commit
);
-- Finds a tree's latest sample, which every baseline load and store asks for.
CREATE INDEX sample_tree_started ON trackdir.sample (tree_id, started, id);

CREATE TABLE trackdir.path (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id bigint NOT NULL REFERENCES trackdir.tree,
    path    bytea  NOT NULL,  -- relative to ROOT; '.' is ROOT
    UNIQUE (tree_id, path)
);

CREATE TABLE trackdir.dir_state (
    path_id       bigint  NOT NULL REFERENCES trackdir.path,
    sample_id     bigint  NOT NULL REFERENCES trackdir.sample,
    ino           bigint,
    mode          integer,
    uid           bigint,
    gid           bigint,
    nlink         bigint,
    rctime        bigint,           -- ns since the epoch
    rbytes        bigint,
    rfiles        bigint,
    rsubdirs      bigint,
    error_stage   text CHECK (error_stage IN ('open', 'stat', 'rstats', 'readdir')),
    error_code    text,
    created       boolean NOT NULL,  -- the path had no current row before the sample
    deleted       boolean NOT NULL,  -- a tombstone: the path is gone
    prev_rbytes   bigint,            -- the path's rstats before the sample
    prev_rfiles   bigint,
    prev_rsubdirs bigint,
    prev_error_stage text CHECK (prev_error_stage IN ('open', 'stat', 'rstats', 'readdir')),
    PRIMARY KEY (path_id, sample_id),
    CHECK ((error_stage IS NULL) = (error_code IS NULL)),
    CHECK (NOT (created AND deleted)),
    CHECK (NOT deleted OR (ino IS NULL AND mode IS NULL AND uid IS NULL
        AND gid IS NULL AND nlink IS NULL AND rctime IS NULL AND rbytes IS NULL
        AND rfiles IS NULL AND rsubdirs IS NULL AND error_stage IS NULL))
);
-- Rows are only appended, so they lie in sample order: a small index finds a
-- window of samples.
CREATE INDEX dir_state_sample_id ON trackdir.dir_state USING brin (sample_id);

CREATE TABLE trackdir.current_dir_state (
    tree_id     bigint NOT NULL REFERENCES trackdir.tree,
    path_id     bigint NOT NULL REFERENCES trackdir.path,
    path        bytea  NOT NULL,  -- a copy of path.path: a baseline reads no other table
    sample_id   bigint NOT NULL REFERENCES trackdir.sample,  -- stored this row
    ino         bigint,
    mode        integer,
    uid         bigint,
    gid         bigint,
    nlink       bigint,
    rctime      bigint,
    rbytes      bigint,
    rfiles      bigint,
    rsubdirs    bigint,
    error_stage text,
    error_code  text,
    PRIMARY KEY (tree_id, path_id)
);
-- Finds a tree's current errors, and its directories without rstats, whose
-- growth is unknown.
CREATE INDEX current_dir_state_errors ON trackdir.current_dir_state (tree_id)
    WHERE error_stage IS NOT NULL OR rbytes IS NULL;

-- store_sample joins path and current_dir_state by tree, and summarize joins
-- dir_state by sample. By default autovacuum refreshes a table's statistics
-- after changes to a tenth of it, which on a large table leaves a new or
-- growing tree, or recent samples, unknown to the planner for a long time,
-- and queries on them planned as if they had no rows. Refresh after a fixed
-- number of changed rows instead.
ALTER TABLE trackdir.path SET
    (autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = ANALYZE_AFTER);
ALTER TABLE trackdir.current_dir_state SET
    (autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = ANALYZE_AFTER);
ALTER TABLE trackdir.dir_state SET
    (autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = ANALYZE_AFTER);
""".replace("TREE_NAME", TREE_NAME.pattern).replace(  # noqa: E501
    "ANALYZE_AFTER", str(ANALYZE_AFTER)
)

# The staging table's columns in COPY order; dir_state_row() gives all but
# deleted.
STAGE_COLUMNS = ("path", *ROW_COLUMNS, "deleted")

# Matches a tree by key, in _key_params() order. fs_name is NULL for the default
# filesystem, which "=" would never match.
_TREE_MATCH = (
    "cluster_fsid = %s AND fs_name IS NOT DISTINCT FROM %s AND fs_path = %s"
    " AND max_depth = %s"
)

# The latest sample of tree %s: the greatest (started, id).
_LATEST_SAMPLE = (
    "SELECT id, started FROM trackdir.sample WHERE tree_id = %s"
    " ORDER BY started DESC, id DESC LIMIT 1"
)

_BASELINE_FETCH = 10_000  # rows per round trip of load_baseline's cursor


class DbError(Exception):
    """A database operation failed; also wraps any psycopg.Error."""


class PsycopgMissing(DbError):
    """psycopg is not installed."""


class SchemaMissing(DbError):
    """The database has no trackdir schema."""


class SchemaVersionMismatch(DbError):
    """The trackdir schema is not at SCHEMA_VERSION.

    found is the version recorded in trackdir.meta, or None if the schema has
    no meta table or not exactly one row in it.
    """

    def __init__(self, found: int | None) -> None:
        super().__init__(
            f"schema {SCHEMA} is at version {found}, expected {SCHEMA_VERSION}"
        )
        self.found = found


@dataclass(frozen=True, slots=True)
class TreeKey:
    """A tree's mount identity: a directory of a CephFS filesystem, on any host
    or mount, sampled to one depth. A tree's name stands for one.

    A run with another max_depth belongs to another tree, which has its own
    samples and baseline. max_dirs is not part of the key: a run over it stores
    nothing, so it never shapes what a tree holds.
    """

    cluster_fsid: str
    fs_name: str | None  # None: the cluster's default filesystem
    fs_path: bytes  # the directory's absolute path in the filesystem
    max_depth: int

    @classmethod
    def of(
        cls,
        cluster_fsid: str,
        fs_name: str | None,
        fs_path: str,
        max_depth: int,
    ) -> Self:
        """Return the key for a tree given as header strings; fs_path is fsencoded."""
        return cls(cluster_fsid, fs_name, os.fsencode(fs_path), max_depth)


class TreeMismatch(DbError):
    """Tree NAME stands for another mount identity than the one found.

    recorded is what the database holds for the name, found what is being
    sampled.
    """

    def __init__(self, name: str, recorded: TreeKey, found: TreeKey) -> None:
        super().__init__(f"tree {name} is recorded as {recorded}, not {found}")
        self.name = name
        self.recorded = recorded
        self.found = found


class TreeNamedOtherwise(DbError):
    """The mount identity KEY is recorded as tree OTHER, not as NAME."""

    def __init__(self, name: str, other: str, key: TreeKey) -> None:
        super().__init__(f"{key} is tree {other}, not {name}")
        self.name = name
        self.other = other
        self.key = key


class SampleConflict(DbError):
    """The sample conflicts with the samples stored for tree KEY; nothing is stored."""

    def __init__(self, key: TreeKey, message: str) -> None:
        super().__init__(message)
        self.key = key


class ConcurrentSample(SampleConflict):
    """Another sample of the tree was stored after the baseline was loaded."""

    def __init__(self, key: TreeKey) -> None:
        super().__init__(key, f"another sample of tree {key} was stored meanwhile")


class OutOfOrderSample(SampleConflict):
    """The sample started no later than the tree's latest sample (latest_started)."""

    def __init__(
        self, key: TreeKey, started: datetime, latest_started: datetime
    ) -> None:
        super().__init__(
            key,
            f"sample started at {started}, not after the latest sample of tree "
            f"{key} ({latest_started})",
        )
        self.started = started
        self.latest_started = latest_started


@dataclass(frozen=True, slots=True)
class RootTotals:
    """ROOT's rstats as one sample stored them: the size of the whole tree."""

    sample_id: int
    rbytes: int
    rfiles: int
    rsubdirs: int


@dataclass(frozen=True, slots=True)
class SampleStats:
    """One sample: its run, and what it stored.

    A stored row is new if its path had no current row before the sample,
    else modified; deleted counts tombstones.
    """

    id: int
    started: datetime
    finished: datetime
    probed: int
    errors: int  # probed directories with an error
    new: int
    modified: int
    deleted: int
    euid: int
    host: str | None
    root: bytes  # ROOT's local path on the host that read the filesystem
    anonymized: bool
    replayed_from: str | None  # absolute capture path; None for a live sample
    kind: Literal["initial", "incremental"]  # as run_sample decided it


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


@dataclass(frozen=True, slots=True)
class Growth:
    """The directories whose own growth was the greatest since a sample.

    The window runs from sample SINCE to the tree's latest sample. Directories
    gone by the end, with an unknown growth or with an own growth of 0 or less
    are left out.
    """

    since: int
    positive_own: int  # the sum of all positive own growths, listed or not
    dirs: tuple[DirGrowth, ...]  # most own growth first, ties by path


@dataclass(frozen=True, slots=True)
class TreeStats:
    """One tree: its samples and its current state."""

    id: int
    name: str
    key: TreeKey  # the mount identity the name stands for
    samples: int
    first_started: datetime | None  # None: the tree has no samples
    last_started: datetime | None
    current_dirs: int
    current_errors: Counter[tuple[str, str]]  # (stage, code) -> directories
    root_now: RootTotals | None  # None if ROOT's current row has no rstats
    root_first: RootTotals | None  # earliest ROOT row with rstats
    recent: tuple[SampleStats, ...]  # newest first
    growth: tuple[Growth, ...]  # since the first sample, then the second-to-last


@dataclass(frozen=True, slots=True)
class DbStats:
    """The whole database's counts, and the statistics of some of its trees."""

    schema_version: int
    trees: int
    samples: int
    dir_states: int
    bytes_on_disk: int  # tables with their indexes and TOAST
    tree_stats: tuple[TreeStats, ...]  # by id


def valid_tree_name(name: str) -> bool:
    """Return whether NAME can name a tree (TREE_NAME_RULE)."""
    return TREE_NAME.fullmatch(name) is not None


def tree_key_of(header: CaptureHeader) -> TreeKey:
    """Return the key of the tree HEADER describes."""
    return TreeKey.of(
        header.cluster_fsid, header.fs_name, header.fs_path, header.max_depth
    )


def dir_state_row(probe: DirProbe) -> tuple:
    """Return PROBE's values for STAGE_COLUMNS[:-1]; unknown values are None."""
    return (probe.path, *row_of(probe))


def check_order(baseline: Baseline, key: TreeKey, started: datetime) -> None:
    """Raise OutOfOrderSample unless STARTED is later than BASELINE's sample.

    A tree's samples are ordered by started, so a sample that started earlier
    than the latest must not replace its rows in the current state. BASELINE is
    tree KEY's; an empty baseline accepts any time.
    """
    if baseline.started is not None and started <= baseline.started:
        raise OutOfOrderSample(key, started, baseline.started)


def _psycopg() -> ModuleType:
    """Import psycopg on every call, so tests can hide it; raises PsycopgMissing."""
    try:
        import psycopg
    except ImportError as e:
        raise PsycopgMissing("psycopg is not installed") from e
    return psycopg


@contextmanager
def _db_errors() -> Iterator[None]:
    """Re-raise psycopg.Error as DbError."""
    psycopg = _psycopg()
    try:
        yield
    except psycopg.Error as e:
        raise DbError(str(e)) from e


def connect(dsn: str) -> "psycopg.Connection":
    """Connect to DSN in autocommit mode; this module's functions open transactions.

    Raises PsycopgMissing, or DbError if the connection fails.
    """
    psycopg = _psycopg()
    with _db_errors():
        return psycopg.connect(dsn, autocommit=True)


def _schema_version(conn: "psycopg.Connection") -> int | None:
    """Return the trackdir schema's version, None if it has no single meta row.

    Raises SchemaMissing if the schema does not exist.
    """
    if conn.execute("SELECT to_regnamespace(%s)", (SCHEMA,)).fetchone()[0] is None:
        raise SchemaMissing(f"schema {SCHEMA} does not exist")
    meta = f"{SCHEMA}.meta"
    if conn.execute("SELECT to_regclass(%s)", (meta,)).fetchone()[0] is None:
        return None
    rows = conn.execute("SELECT schema_version FROM trackdir.meta").fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _check_version(conn: "psycopg.Connection") -> None:
    """Raise SchemaMissing or SchemaVersionMismatch unless the schema is current."""
    found = _schema_version(conn)
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(found)


def init_schema(conn: "psycopg.Connection") -> bool:
    """Create the trackdir schema at SCHEMA_VERSION, in one transaction.

    Returns True if it was created, False if it already exists at that version.
    Raises SchemaVersionMismatch if it exists otherwise.
    """
    with _db_errors(), conn.transaction():
        try:
            _check_version(conn)
        except SchemaMissing:
            conn.execute(DDL)
            conn.execute(
                "INSERT INTO trackdir.meta (schema_version) VALUES (%s)",
                (SCHEMA_VERSION,),
            )
            return True
        return False


def check_schema(conn: "psycopg.Connection") -> None:
    """Raise SchemaMissing or SchemaVersionMismatch unless the schema is current."""
    with _db_errors(), conn.transaction():
        _check_version(conn)


def load_baseline(
    conn: "psycopg.Connection",
    name: str,
    key: TreeKey,
    *,
    max_dirs: int | None = None,
) -> Baseline:
    """Return the baseline of tree NAME: its current state as of its latest sample.

    KEY is the mount identity of what is about to be sampled; raises
    TreeMismatch or TreeNamedOtherwise if NAME and KEY do not belong together
    (see _resolve_tree).

    It reads the tree's current_dir_state rows, so it costs in proportion to
    the tree's live directories, whatever history is stored. An unknown tree,
    or one without samples, has an empty baseline. One
    REPEATABLE READ transaction keeps the latest sample and the rows consistent
    with each other; the rows come through a server-side cursor, so they are
    held in memory once, in the baseline. Raises DirLimitExceeded as soon as
    more than MAX_DIRS rows are read (None: no limit), so the limit bounds the
    memory of loading too.
    """
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        tree_id = _resolve_tree(cur, name, key, lock=False, create=False)
        if tree_id is None:
            return Baseline.empty()
        tree = (tree_id,)
        latest = cur.execute(_LATEST_SAMPLE, tree).fetchone()
        if latest is None:
            return Baseline.empty()
        rows: dict[bytes, Row] = {}
        columns = ", ".join(ROW_COLUMNS)
        with conn.cursor(name="trackdir_baseline") as named:
            named.itersize = _BASELINE_FETCH
            named.execute(
                f"SELECT path, {columns} FROM trackdir.current_dir_state"
                " WHERE tree_id = %s",
                tree,
            )
            for path, *values in named:
                if max_dirs is not None and len(rows) == max_dirs:
                    raise DirLimitExceeded(max_dirs)
                rows[path] = tuple(values)
    sample_id, started = latest
    return Baseline.build(sample_id, started, rows)


def latest_started(
    conn: "psycopg.Connection", name: str, key: TreeKey
) -> datetime | None:
    """Return when tree NAME's latest sample started; None without samples.

    KEY is the mount identity of what would be sampled; raises TreeMismatch or
    TreeNamedOtherwise as load_baseline does.

    The latest sample is the one with the greatest (started, id), as in the
    baseline. Unlike load_baseline, this reads no directory rows.
    """
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        tree_id = _resolve_tree(cur, name, key, lock=False, create=False)
        if tree_id is None:
            return None
        latest = cur.execute(_LATEST_SAMPLE, (tree_id,)).fetchone()
    return None if latest is None else latest[1]


def store_sample(
    conn: "psycopg.Connection",
    name: str,
    header: CaptureHeader,
    key: TreeKey,
    baseline: Baseline,
    diff: SampleDiff,
    replayed_from: str | None,
    baseline_seconds: float,
) -> int:
    """Store a sample of tree NAME, in one transaction; return its id.

    KEY is the tree's mount identity. A NAME and KEY that are both new create
    the tree; raises TreeMismatch or TreeNamedOtherwise if they do not belong
    together (see _resolve_tree), and nothing is stored.

    The sample row comes from HEADER (dirs and errors count the probed
    directories; its max_depth is the tree's, so not stored again); its
    dir_state rows are DIFF's changed probes and a tombstone per deleted path.
    They are loaded with COPY into a temporary table, from which SQL gives
    new paths an id, adds each row's previous rstats and updates the tree's
    current state: all of it in proportion to the sample's rows, except that
    a sample of very many rows is joined to path by reading all of path (see
    "Measured on version 5" in the schema-v5 spec). KEY must be
    HEADER's tree, else ValueError. BASELINE is what DIFF was computed against.
    REPLAYED_FROM is the capture's absolute path, None for a live sample.
    BASELINE_SECONDS, the time loading BASELINE took, is stored with the
    sample, as are the time this transaction takes up to its commit and the
    counts of new, modified and deleted rows.

    Raises ConcurrentSample if the tree's latest sample is no longer
    BASELINE's, and OutOfOrderSample (see check_order); nothing is stored.
    Locking the tree row serializes samples of one tree: a second one waits
    for the first to commit, then sees its sample. That needs READ COMMITTED,
    which the transaction sets whatever the session default: under REPEATABLE
    READ the snapshot, taken before the wait, would hide the first sample.
    """
    if key != tree_key_of(header):
        raise ValueError(f"tree {key} is not the tree of the header")
    started = datetime.fromisoformat(header.started)
    clock = time.monotonic()
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        tree_id = _resolve_tree(cur, name, key, lock=True, create=True)
        latest = cur.execute(_LATEST_SAMPLE, (tree_id,)).fetchone()
        if (latest[0] if latest else None) != baseline.sample_id:
            raise ConcurrentSample(key)
        check_order(baseline, key, started)
        cur.execute(
            "INSERT INTO trackdir.sample (tree_id, root, rstat_mode, openat2,"
            " jobs, started, finished, euid, host, kernel, trackdir_version,"
            " dirs, errors, anonymized, replayed_from, baseline_seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s) RETURNING id",
            (
                tree_id,
                os.fsencode(header.root),
                header.rstat_mode,
                header.openat2,
                header.jobs,
                started,
                datetime.fromisoformat(header.finished),
                header.euid,
                header.host,
                header.kernel,
                header.trackdir_version,
                header.dirs,
                header.errors,
                header.anonymized,
                replayed_from,
                baseline_seconds,
            ),
        )
        (sample_id,) = cur.fetchone()
        cur.execute(_CREATE_STAGE)
        columns = ", ".join(STAGE_COLUMNS)
        gone = (None,) * len(ROW_COLUMNS)
        staged = 0
        with cur.copy(f"COPY trackdir_stage ({columns}) FROM STDIN") as copy:
            for probe in diff.changed:
                copy.write_row((*dir_state_row(probe), False))
                staged += 1
            for path in diff.deleted:
                copy.write_row((path, *gone, True))
                staged += 1
        params = {"tree": tree_id, "sample": sample_id}
        # autovacuum never analyzes a temporary table, and a large sample
        # needs the planner to know its size
        cur.execute("ANALYZE trackdir_stage")
        # Statistics gathered before this tree existed, or before it grew,
        # say it has few rows or none, and the joins below are then planned
        # for one: a nested loop that compares every staged row with every
        # row of the tree, quadratic in a large sample. So a large sample
        # refreshes the statistics first, whether its rows are new paths,
        # changes or tombstones. ANALYZE reads a sample of the table: its
        # cost is bounded, not proportional to the table.
        bulk = staged >= ANALYZE_AFTER
        cur.execute(_INSERT_PATHS, params)
        if bulk:
            cur.execute("ANALYZE trackdir.path")
            cur.execute("ANALYZE trackdir.current_dir_state")
        # Reads the current state as it was before the sample, so it runs
        # before the statements that change it.
        new, modified, deleted = cur.execute(_INSERT_STATES, params).fetchone()
        cur.execute(_UPSERT_CURRENT, params)
        # Without tombstones the delete has nothing to do: skip it rather
        # than plan it. With them it is planned from the statistics taken
        # above, which count the rows it deletes.
        if diff.deleted:
            cur.execute(_DELETE_CURRENT, params)
        if bulk:  # for db summary and the tree's next sample
            cur.execute("ANALYZE trackdir.current_dir_state")
            cur.execute("ANALYZE trackdir.dir_state")
        cur.execute(
            "UPDATE trackdir.sample SET new_dirs = %s, modified_dirs = %s,"
            " deleted_dirs = %s, store_seconds = %s WHERE id = %s",
            (new, modified, deleted, time.monotonic() - clock, sample_id),
        )
    return sample_id


# What a sample stores, in a temporary table that its transaction drops.
_CREATE_STAGE = """CREATE TEMPORARY TABLE trackdir_stage (
    path bytea NOT NULL, ino bigint, mode integer, uid bigint, gid bigint,
    nlink bigint, rctime bigint, rbytes bigint, rfiles bigint, rsubdirs bigint,
    error_stage text, error_code text, deleted boolean NOT NULL
) ON COMMIT DROP"""
_STAGED = ", ".join(f"s.{column}" for column in ROW_COLUMNS)
_STAGE_PATHS = (
    "trackdir_stage s JOIN trackdir.path p ON p.tree_id = %(tree)s AND p.path = s.path"
)
# The statements below move the staged rows of sample %(sample)s of tree
# %(tree)s into the tables, under the tree row's lock.
#
# An anti-join, not ON CONFLICT DO NOTHING, which would use up an id for every
# path that exists; the lock keeps other inserts of the tree away.
_INSERT_PATHS = """INSERT INTO trackdir.path (tree_id, path)
SELECT %(tree)s, s.path FROM trackdir_stage s
WHERE NOT EXISTS (
    SELECT FROM trackdir.path p WHERE p.tree_id = %(tree)s AND p.path = s.path
)"""
# Returns the sample's counts of new, modified and deleted rows.
_INSERT_STATES = f"""WITH stored AS (
    INSERT INTO trackdir.dir_state (path_id, sample_id, {", ".join(ROW_COLUMNS)},
        created, deleted, prev_rbytes, prev_rfiles, prev_rsubdirs,
        prev_error_stage)
    SELECT p.id, %(sample)s, {_STAGED}, c.path_id IS NULL, s.deleted,
           c.rbytes, c.rfiles, c.rsubdirs, c.error_stage
    FROM {_STAGE_PATHS}
    LEFT JOIN trackdir.current_dir_state c
           ON c.tree_id = %(tree)s AND c.path_id = p.id
    RETURNING created, deleted
)
SELECT count(*) FILTER (WHERE created),
       count(*) FILTER (WHERE NOT created AND NOT deleted),
       count(*) FILTER (WHERE deleted)
FROM stored"""
_UPSERT_CURRENT = f"""INSERT INTO trackdir.current_dir_state
    (tree_id, path_id, path, sample_id, {", ".join(ROW_COLUMNS)})
SELECT %(tree)s, p.id, s.path, %(sample)s, {_STAGED}
FROM {_STAGE_PATHS}
WHERE NOT s.deleted
ON CONFLICT (tree_id, path_id) DO UPDATE SET sample_id = EXCLUDED.sample_id,
    {", ".join(f"{c} = EXCLUDED.{c}" for c in ROW_COLUMNS)}"""
_DELETE_CURRENT = """DELETE FROM trackdir.current_dir_state c
USING trackdir_stage s, trackdir.path p
WHERE s.deleted AND p.tree_id = %(tree)s AND p.path = s.path
  AND c.tree_id = %(tree)s AND c.path_id = p.id"""


def summarize(
    conn: "psycopg.Connection",
    tree: str | None = None,
    recent: int = 10,
    top: int = 3,
) -> DbStats:
    """Return the database's statistics, with those of the tree named TREE or
    of all trees.

    An unknown TREE gives no tree statistics. Each tree lists its RECENT
    newest samples, and the TOP directories with the most own growth since its
    first sample, given 2 samples, and since its second-to-last, given 3; TOP 0
    skips growth. Without growth nothing depends on how much history is
    stored: counts come from the sample rows, totals from the current state
    and from ROOT's first row with rstats. Growth reads each tree's current
    state, and for each window the earlier rows of the paths changed since. One
    READ ONLY REPEATABLE READ transaction keeps all numbers consistent. Raises
    SchemaMissing or SchemaVersionMismatch unless the schema is current.
    """
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        _check_version(conn)
        counts = cur.execute(_COUNTS).fetchone()
        tree_id = None
        if tree is not None:
            found = cur.execute(
                "SELECT id FROM trackdir.tree WHERE name = %s", (tree,)
            ).fetchone()
            if found is None:
                return DbStats(SCHEMA_VERSION, *counts, ())
            (tree_id,) = found
        params = {"tree": tree_id}
        trees = cur.execute(_TREES, params).fetchall()
        samples = _group(cur.execute(_SAMPLES, params))
        root = {**params, "root": ROOT}
        roots_now = {row[0]: row[1:] for row in cur.execute(_ROOT_NOW, root)}
        roots_first = {
            tree_id: cur.execute(
                _ROOT_FIRST, {"tree": tree_id, "root": ROOT}
            ).fetchone()
            for tree_id, *_ in trees
        }
        errors = _group(cur.execute(_CURRENT_ERRORS, params))
        growth = {
            tree_id: _growth(cur, tree_id, max_depth, samples.get(tree_id, []), top)
            for tree_id, *_, max_depth in trees
        }
    tree_stats = tuple(
        _tree_stats(
            tree_id,
            name,
            TreeKey(*key),
            samples.get(tree_id, []),
            roots_now.get(tree_id),
            roots_first[tree_id],
            errors.get(tree_id, []),
            recent,
            growth[tree_id],
        )
        for tree_id, name, *key in trees
    )
    return DbStats(SCHEMA_VERSION, *counts, tree_stats)


# Queries of summarize(); %(tree)s is a tree id or NULL for all trees.
_COUNTS = """SELECT (SELECT count(*) FROM trackdir.tree),
       (SELECT count(*) FROM trackdir.sample),
       (SELECT coalesce(sum(new_dirs + modified_dirs + deleted_dirs), 0)::bigint
        FROM trackdir.sample),
       (SELECT coalesce(sum(pg_total_relation_size(oid)), 0)::bigint
        FROM pg_class WHERE relnamespace = 'trackdir'::regnamespace AND relkind = 'r')
"""
_IN_TREE = "(%(tree)s::bigint IS NULL OR {column} = %(tree)s::bigint)"
_TREES = (
    "SELECT id, name, cluster_fsid, fs_name, fs_path, max_depth"
    f" FROM trackdir.tree WHERE {_IN_TREE.format(column='id')} ORDER BY id"
)
# Each tree's samples, oldest first, with what they stored. Columns after
# tree_id are SampleStats's fields in order, but for kind.
_SAMPLES = f"""SELECT tree_id, id, started, finished, dirs, errors,
       new_dirs, modified_dirs, deleted_dirs,
       euid, host, root, anonymized, replayed_from
FROM trackdir.sample
WHERE {_IN_TREE.format(column="tree_id")}
ORDER BY tree_id, started, id
"""
# ROOT's current row with rstats, per tree: RootTotals's fields after tree_id.
# ROOT's id comes from path's (tree_id, path) index, one lookup per tree, and
# finds the row by primary key; current_dir_state has no index on its path.
_ROOT_NOW = f"""SELECT t.id, c.sample_id, c.rbytes, c.rfiles, c.rsubdirs
FROM trackdir.tree t
JOIN trackdir.path p ON p.tree_id = t.id AND p.path = %(root)s
JOIN trackdir.current_dir_state c ON c.tree_id = t.id AND c.path_id = p.id
WHERE c.rbytes IS NOT NULL AND {_IN_TREE.format(column="t.id")}
"""
# ROOT's earliest row with rstats in tree %(tree)s. A tree's sample ids are in
# (started, id) order, so the primary key gives it without a sort.
_ROOT_FIRST = """SELECT d.sample_id, d.rbytes, d.rfiles, d.rsubdirs
FROM trackdir.dir_state d
WHERE d.path_id = (SELECT id FROM trackdir.path
                   WHERE tree_id = %(tree)s AND path = %(root)s)
  AND d.rbytes IS NOT NULL
ORDER BY d.sample_id LIMIT 1
"""
_CURRENT_ERRORS = f"""SELECT tree_id, error_stage, error_code, count(*)
FROM trackdir.current_dir_state
WHERE error_stage IS NOT NULL AND {_IN_TREE.format(column="tree_id")}
GROUP BY tree_id, error_stage, error_code
"""

# Each current path of tree %(tree)s, with its rbytes and error stage.
_CURRENT_ENDS = """SELECT path, rbytes, error_stage FROM trackdir.current_dir_state
WHERE tree_id = %(tree)s
"""
# Each path with a row in samples %(later)s, with its latest row in samples
# %(earlier)s, oldest first: rbytes, error stage and deleted, or NULLs if none.
_START_ENDS = """WITH later AS (
    SELECT DISTINCT path_id FROM trackdir.dir_state WHERE sample_id = ANY(%(later)s)
)
SELECT p.path, b.rbytes, b.error_stage, b.deleted
FROM later l JOIN trackdir.path p ON p.id = l.path_id LEFT JOIN (
    SELECT DISTINCT ON (d.path_id) d.path_id, d.rbytes, d.error_stage, d.deleted
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    WHERE d.sample_id = ANY(%(earlier)s)
      AND d.path_id IN (SELECT path_id FROM later)
    ORDER BY d.path_id, s.started DESC, s.id DESC
) b ON b.path_id = l.path_id
"""
# Error stages that leave a directory's subdirectories unread (capture.is_listed)
_UNLISTING = frozenset({"open", "stat", "readdir"})

# A path's (rbytes, error stage) at one end of a growth window; None: absent.
End = tuple[int | None, str | None] | None


def _growth(
    cur: "psycopg.Cursor",
    tree_id: int,
    max_depth: int,
    samples: list[tuple],
    top: int,
) -> tuple[Growth, ...]:
    """Return tree TREE_ID's growth windows; see summarize.

    SAMPLES are the tree's _SAMPLES rows, oldest first, without tree_id. A
    path's state at a window's start is its current one unless it has rows in
    later samples, so only those paths' earlier rows are read.
    """
    if not top or len(samples) < 2:
        return ()
    ids = [sample_id for sample_id, *_ in samples]
    current = {
        path: (rbytes, stage)
        for path, rbytes, stage in cur.execute(_CURRENT_ENDS, {"tree": tree_id})
    }
    starts = [0] if len(samples) < 3 else [0, len(samples) - 2]
    windows = []
    for start in starts:
        params = {"earlier": ids[: start + 1], "later": ids[start + 1 :]}
        changed = {
            path: None if deleted in (None, True) else (rbytes, stage)
            for path, rbytes, stage, deleted in cur.execute(_START_ENDS, params)
        }
        ends = {path: (changed.get(path, now), now) for path, now in current.items()}
        for path, then in changed.items():
            if path not in current and then is not None:
                ends[path] = (then, None)
        positive, dirs = own_growth(ends, max_depth, top)
        windows.append(Growth(ids[start], positive, dirs))
    return tuple(windows)


def own_growth(
    ends: dict[bytes, tuple[End, End]], max_depth: int, top: int
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
        return end is not None and end[1] in _UNLISTING

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


def _group(rows: Iterator[tuple]) -> dict[int, list[tuple]]:
    """Return ROWS by their first column, without it, in order."""
    groups: dict[int, list[tuple]] = {}
    for first, *rest in rows:
        groups.setdefault(first, []).append(tuple(rest))
    return groups


def _tree_stats(
    tree_id: int,
    name: str,
    key: TreeKey,
    samples: list[tuple],
    root_now: tuple | None,
    root_first: tuple | None,
    errors: list[tuple],
    recent: int,
    growth: tuple[Growth, ...],
) -> TreeStats:
    """Return the statistics of a tree from the rows of summarize's queries.

    ROOT_NOW and ROOT_FIRST are RootTotals's fields, None without such a row.

    A sample is initial if no directory was current before it: the running
    count of new minus deleted rows is zero, as the baseline was empty.
    """
    current = 0
    stats: list[SampleStats] = []
    for row in samples:
        stat = SampleStats(*row, kind="incremental" if current else "initial")
        current += stat.new - stat.deleted
        stats.append(stat)
    return TreeStats(
        id=tree_id,
        name=name,
        key=key,
        samples=len(stats),
        first_started=stats[0].started if stats else None,
        last_started=stats[-1].started if stats else None,
        current_dirs=current,
        current_errors=Counter({(stage, code): n for stage, code, n in errors}),
        root_now=RootTotals(*root_now) if root_now else None,
        root_first=RootTotals(*root_first) if root_first else None,
        recent=tuple(reversed(stats[-recent:])) if recent else (),
        growth=growth,
    )


def _resolve_tree(
    cur: "psycopg.Cursor", name: str, key: TreeKey, *, lock: bool, create: bool
) -> int | None:
    """Return the id of tree NAME, checked against the mount identity KEY.

    The rules, the only place a tree is told from another:
    - NAME is recorded with KEY: its id, the row locked if LOCK.
    - NAME is recorded with another identity: TreeMismatch.
    - NAME is unknown and KEY is recorded under another name:
      TreeNamedOtherwise.
    - neither is recorded: the tree is created if CREATE, else None.

    Selects first so that only a tree's first sample inserts. A concurrent
    first sample that inserts the same name or the same identity makes ON
    CONFLICT wait for it and insert nothing; the second pass then judges that
    row by the rules above, so a race ends in the same refusals, never in a
    unique violation. That needs READ COMMITTED, as the store sets.
    """
    by_name = (
        "SELECT id, cluster_fsid, fs_name, fs_path, max_depth FROM trackdir.tree"
        " WHERE name = %s" + (" FOR UPDATE" if lock else "")
    )
    for _ in range(2):
        row = cur.execute(by_name, (name,)).fetchone()
        if row is not None:
            recorded = TreeKey(row[1], row[2], bytes(row[3]), row[4])
            if recorded != key:
                raise TreeMismatch(name, recorded, key)
            return row[0]
        other = cur.execute(
            f"SELECT name FROM trackdir.tree WHERE {_TREE_MATCH}", _key_params(key)
        ).fetchone()
        if other is not None:
            if other[0] == name:
                # This tree was committed between the two lookups, which see
                # different snapshots under READ COMMITTED: look again.
                continue
            raise TreeNamedOtherwise(name, other[0], key)
        if not create:
            return None
        cur.execute(
            "INSERT INTO trackdir.tree"
            " (name, cluster_fsid, fs_name, fs_path, max_depth)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (name, *_key_params(key)),
        )
    raise DbError(f"tree {name} changed while it was looked up; try again")


def _key_params(key: TreeKey) -> tuple[str, str | None, bytes, int]:
    """Return KEY's fields in _TREE_MATCH order."""
    return key.cluster_fsid, key.fs_name, key.fs_path, key.max_depth
