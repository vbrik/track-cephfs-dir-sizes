"""PostgreSQL storage of samples: the schema, the baseline, the sample store and
the statistics of what is stored.

psycopg is imported only when a function needs it, so the rest of trackdir,
capture included, works without it. Errors raised by psycopg inside this
module's functions become DbError; KeyboardInterrupt propagates as is.
Writes run in one transaction each, so a failure or interrupt stores nothing.
"""

import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Literal, Self

from trackdir.baseline import ROW_COLUMNS, Baseline, Row, SampleDiff, row_of
from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirProbe

if TYPE_CHECKING:
    import psycopg

SCHEMA = "trackdir"  # the DDL and queries fix this name; it is not a setting
SCHEMA_VERSION = 4  # of DDL; no migrations exist, a mismatch is fatal

# The initial-sample spec's section 5 as amended by section 4 of the
# incremental-sample spec. dir_state holds the rows a sample stored because they
# changed (tombstones included), not a full listing; current_dir_state is each
# path's latest row of a tree, hiding deleted paths. Because a sample's rows are
# not a listing, a sample cannot be deleted on its own: dir_state's foreign key
# to sample does not cascade, so deleting a sample that has rows fails. A tree is
# removed by deleting its dir_state rows, then its samples, then the tree row.
# What a sample stored is counted from its dir_state rows, not kept redundantly.
DDL = """\
CREATE SCHEMA trackdir;

CREATE TABLE trackdir.meta (
    schema_version integer NOT NULL
);  -- exactly one row

CREATE TABLE trackdir.tree (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    stored           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trackdir.dir_state (
    sample_id   bigint NOT NULL REFERENCES trackdir.sample,
    path        bytea  NOT NULL,  -- relative to ROOT; '.' is ROOT
    ino         bigint,
    mode        integer,
    uid         bigint,
    gid         bigint,
    nlink       bigint,
    rctime      bigint,           -- ns since the epoch
    rbytes      bigint,
    rfiles      bigint,
    rsubdirs    bigint,
    error_stage text CHECK (error_stage IN ('open', 'stat', 'rstats', 'readdir')),
    error_code  text,
    deleted     boolean NOT NULL DEFAULT false,  -- a tombstone: the path is gone
    PRIMARY KEY (sample_id, path),
    CHECK ((error_stage IS NULL) = (error_code IS NULL)),
    CHECK (NOT deleted OR (ino IS NULL AND mode IS NULL AND uid IS NULL
        AND gid IS NULL AND nlink IS NULL AND rctime IS NULL AND rbytes IS NULL
        AND rfiles IS NULL AND rsubdirs IS NULL AND error_stage IS NULL))
);

CREATE VIEW trackdir.current_dir_state AS
SELECT * FROM (
    SELECT DISTINCT ON (s.tree_id, d.path) s.tree_id, d.*
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    ORDER BY s.tree_id, d.path, s.started DESC, s.id DESC
) latest
WHERE NOT deleted;
"""  # noqa: E501

# dir_state columns in COPY order; dir_state_row() gives those between
# sample_id and deleted.
DIR_STATE_COLUMNS = ("sample_id", "path", *ROW_COLUMNS, "deleted")

# Matches a tree by key, in _key_params() order. fs_name is NULL for the default
# filesystem, which "=" would never match.
_TREE_MATCH = (
    "cluster_fsid = %s AND fs_name IS NOT DISTINCT FROM %s AND fs_path = %s"
    " AND max_depth = %s"
)

# The latest sample of tree %s: the greatest (started, id), as in the view.
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
    """Identifies a tree: a directory of a CephFS filesystem, on any host or mount,
    sampled to one depth.

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
class TreeStats:
    """One tree: its samples and its current state."""

    id: int
    key: TreeKey
    samples: int
    first_started: datetime | None  # None: the tree has no samples
    last_started: datetime | None
    current_dirs: int
    current_errors: Counter[tuple[str, str]]  # (stage, code) -> directories
    root_now: RootTotals | None  # None if ROOT's current row has no rstats
    root_first: RootTotals | None  # earliest ROOT row with rstats
    recent: tuple[SampleStats, ...]  # newest first


@dataclass(frozen=True, slots=True)
class DbStats:
    """The whole database's counts, and the statistics of some of its trees."""

    schema_version: int
    trees: int
    samples: int
    dir_states: int
    bytes_on_disk: int  # tables with their indexes and TOAST
    tree_stats: tuple[TreeStats, ...]  # by id


def tree_key_of(header: CaptureHeader) -> TreeKey:
    """Return the key of the tree HEADER describes."""
    return TreeKey.of(
        header.cluster_fsid, header.fs_name, header.fs_path, header.max_depth
    )


def dir_state_row(probe: DirProbe) -> tuple:
    """Return PROBE's values for DIR_STATE_COLUMNS[1:-1]; unknown values are None."""
    return (probe.path, *row_of(probe))


def check_order(baseline: Baseline, key: TreeKey, started: datetime) -> None:
    """Raise OutOfOrderSample unless STARTED is later than BASELINE's sample.

    The current-state view orders a tree's samples by started, so a sample
    that started earlier than the latest would not supersede it. BASELINE is
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


def load_baseline(conn: "psycopg.Connection", key: TreeKey) -> Baseline:
    """Return the baseline of tree KEY: its current state as of its latest sample.

    An unknown tree, or one without samples, has an empty baseline. One
    REPEATABLE READ transaction keeps the latest sample and the rows consistent
    with each other; the rows come through a server-side cursor, so they are
    held in memory once, in the baseline.
    """
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        tree = cur.execute(
            f"SELECT id FROM trackdir.tree WHERE {_TREE_MATCH}", _key_params(key)
        ).fetchone()
        if tree is None:
            return Baseline.empty()
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
                rows[path] = tuple(values)
    sample_id, started = latest
    return Baseline.build(sample_id, started, rows)


def store_sample(
    conn: "psycopg.Connection",
    header: CaptureHeader,
    key: TreeKey,
    baseline: Baseline,
    diff: SampleDiff,
    replayed_from: str | None,
) -> int:
    """Store a sample of tree KEY, in one transaction; return its id.

    The sample row comes from HEADER (dirs and errors count the probed
    directories; its max_depth is the tree's, so not stored again); its
    dir_state rows, loaded with COPY, are DIFF's changed probes and a
    tombstone per deleted path. KEY must be HEADER's tree, else
    ValueError. BASELINE is what DIFF was computed against.
    REPLAYED_FROM is the capture's absolute path, None for a live sample.

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
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        tree_id = _lock_tree(cur, key)
        latest = cur.execute(_LATEST_SAMPLE, (tree_id,)).fetchone()
        if (latest[0] if latest else None) != baseline.sample_id:
            raise ConcurrentSample(key)
        check_order(baseline, key, started)
        cur.execute(
            "INSERT INTO trackdir.sample (tree_id, root, rstat_mode, openat2,"
            " jobs, started, finished, euid, host, kernel, trackdir_version,"
            " dirs, errors, anonymized, replayed_from) VALUES (%s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
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
            ),
        )
        (sample_id,) = cur.fetchone()
        columns = ", ".join(DIR_STATE_COLUMNS)
        gone = (None,) * len(ROW_COLUMNS)
        with cur.copy(f"COPY trackdir.dir_state ({columns}) FROM STDIN") as copy:
            for probe in diff.changed:
                copy.write_row((sample_id, *dir_state_row(probe), False))
            for path in diff.deleted:
                copy.write_row((sample_id, path, *gone, True))
    return sample_id


def summarize(
    conn: "psycopg.Connection", tree_id: int | None = None, recent: int = 10
) -> DbStats:
    """Return the database's statistics, with those of tree TREE_ID or all trees.

    An unknown TREE_ID gives no tree statistics. Each tree lists its RECENT
    newest samples. Telling new rows from modified ones, and so a sample's kind,
    sorts every dir_state row of the trees; one READ ONLY REPEATABLE READ
    transaction keeps all numbers consistent. Raises SchemaMissing or
    SchemaVersionMismatch unless the schema is current.
    """
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        _check_version(conn)
        counts = cur.execute(_COUNTS).fetchone()
        params = {"tree": tree_id}
        trees = cur.execute(_TREES, params).fetchall()
        samples = _group(cur.execute(_SAMPLES, params))
        roots = _group(cur.execute(_ROOT_ROWS, {**params, "root": ROOT}))
        errors = _group(cur.execute(_CURRENT_ERRORS, params))
    tree_stats = tuple(
        _tree_stats(
            tree_id,
            TreeKey(*key),
            samples.get(tree_id, []),
            roots.get(tree_id, []),
            errors.get(tree_id, []),
            recent,
        )
        for tree_id, *key in trees
    )
    return DbStats(SCHEMA_VERSION, *counts, tree_stats)


# Queries of summarize(); %(tree)s is a tree id or NULL for all trees.
_COUNTS = """SELECT (SELECT count(*) FROM trackdir.tree),
       (SELECT count(*) FROM trackdir.sample),
       (SELECT count(*) FROM trackdir.dir_state),
       (SELECT coalesce(sum(pg_total_relation_size(oid)), 0)::bigint
        FROM pg_class WHERE relnamespace = 'trackdir'::regnamespace AND relkind = 'r')
"""
_IN_TREE = "(%(tree)s::bigint IS NULL OR {column} = %(tree)s::bigint)"
_TREES = (
    "SELECT id, cluster_fsid, fs_name, fs_path, max_depth"
    f" FROM trackdir.tree WHERE {_IN_TREE.format(column='id')} ORDER BY id"
)
# Each tree's samples, oldest first, with what they stored. A row is new unless
# the path's previous row (in the view's order) exists and is no tombstone.
# Columns after tree_id are SampleStats's fields in order, but for kind.
_SAMPLES = f"""WITH history AS (
    SELECT d.sample_id, d.deleted,
           lag(d.deleted) OVER (PARTITION BY s.tree_id, d.path
                                ORDER BY s.started, s.id) AS was_deleted
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    WHERE {_IN_TREE.format(column="s.tree_id")}
), stored AS (
    SELECT sample_id,
           count(*) FILTER (WHERE NOT deleted AND coalesce(was_deleted, true)) AS new,
           count(*) FILTER (WHERE NOT deleted AND NOT was_deleted) AS modified,
           count(*) FILTER (WHERE deleted) AS deleted
    FROM history GROUP BY sample_id
)
SELECT s.tree_id, s.id, s.started, s.finished, s.dirs, s.errors,
       coalesce(st.new, 0), coalesce(st.modified, 0), coalesce(st.deleted, 0),
       s.euid, s.host, s.root, s.anonymized, s.replayed_from
FROM trackdir.sample s LEFT JOIN stored st ON st.sample_id = s.id
WHERE {_IN_TREE.format(column="s.tree_id")}
ORDER BY s.tree_id, s.started, s.id
"""
# ROOT's rows, oldest first; the primary key finds each from its sample.
_ROOT_ROWS = f"""SELECT s.tree_id, s.id, d.rbytes, d.rfiles, d.rsubdirs
FROM trackdir.sample s
JOIN trackdir.dir_state d ON d.sample_id = s.id AND d.path = %(root)s
WHERE {_IN_TREE.format(column="s.tree_id")}
ORDER BY s.tree_id, s.started, s.id
"""
_CURRENT_ERRORS = f"""SELECT tree_id, error_stage, error_code, count(*)
FROM trackdir.current_dir_state
WHERE error_stage IS NOT NULL AND {_IN_TREE.format(column="tree_id")}
GROUP BY tree_id, error_stage, error_code
"""


def _group(rows: Iterator[tuple]) -> dict[int, list[tuple]]:
    """Return ROWS by their first column, without it, in order."""
    groups: dict[int, list[tuple]] = {}
    for first, *rest in rows:
        groups.setdefault(first, []).append(tuple(rest))
    return groups


def _tree_stats(
    tree_id: int,
    key: TreeKey,
    samples: list[tuple],
    roots: list[tuple],
    errors: list[tuple],
    recent: int,
) -> TreeStats:
    """Return the statistics of a tree from the rows of summarize's queries.

    A sample is initial if no directory was current before it: the running
    count of new minus deleted rows is zero, as the baseline was empty.
    """
    current = 0
    stats: list[SampleStats] = []
    for row in samples:
        stat = SampleStats(*row, kind="incremental" if current else "initial")
        current += stat.new - stat.deleted
        stats.append(stat)
    with_rstats = [RootTotals(*row) for row in roots if row[1] is not None]
    root_now = None
    if roots and roots[-1][1] is not None:
        root_now = RootTotals(*roots[-1])
    return TreeStats(
        id=tree_id,
        key=key,
        samples=len(stats),
        first_started=stats[0].started if stats else None,
        last_started=stats[-1].started if stats else None,
        current_dirs=current,
        current_errors=Counter({(stage, code): n for stage, code, n in errors}),
        root_now=root_now,
        root_first=with_rstats[0] if with_rstats else None,
        recent=tuple(reversed(stats[-recent:])) if recent else (),
    )


def _lock_tree(cur: "psycopg.Cursor", key: TreeKey) -> int:
    """Return the id of tree KEY with its row locked, creating the tree if new.

    Selects first so that only a tree's first sample inserts. If a concurrent
    first sample inserts the row in between, ON CONFLICT waits for it to
    commit and inserts nothing, and the second select finds the row.
    """
    params = _key_params(key)
    select = f"SELECT id FROM trackdir.tree WHERE {_TREE_MATCH} FOR UPDATE"
    row = cur.execute(select, params).fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO trackdir.tree (cluster_fsid, fs_name, fs_path, max_depth)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            params,
        )
        row = cur.execute(select, params).fetchone()
    return row[0]


def _key_params(key: TreeKey) -> tuple[str, str | None, bytes, int]:
    """Return KEY's fields in _TREE_MATCH order."""
    return key.cluster_fsid, key.fs_name, key.fs_path, key.max_depth
