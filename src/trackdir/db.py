"""PostgreSQL storage of samples: the schema and the initial-sample store.

psycopg is imported only when a function needs it, so the rest of trackdir,
capture included, works without it. Errors raised by psycopg inside this
module's functions become DbError; KeyboardInterrupt propagates as is.
Writes run in one transaction each, so a failure or interrupt stores nothing.
"""

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Self

from trackdir.capture import CaptureHeader
from trackdir.model import DirProbe

if TYPE_CHECKING:
    import psycopg

SCHEMA = "trackdir"  # the DDL and queries fix this name; it is not a setting
SCHEMA_VERSION = 1  # of DDL; no migrations exist, a mismatch is fatal

# The design spec's section 5, verbatim.
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
    UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path)
);

CREATE TABLE trackdir.sample (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id          bigint      NOT NULL REFERENCES trackdir.tree,
    root             bytea       NOT NULL,  -- ROOT's resolved local path
    max_depth        integer     NOT NULL CHECK (max_depth >= 0),
    rstat_mode       text        NOT NULL CHECK (rstat_mode IN ('dirstat', 'getxattr')),
    openat2          boolean     NOT NULL,
    jobs             integer     NOT NULL,
    started          timestamptz NOT NULL,
    finished         timestamptz NOT NULL,
    euid             bigint      NOT NULL,
    host             text,
    kernel           text        NOT NULL,
    trackdir_version text        NOT NULL,  -- of the program that read the filesystem
    dirs             bigint      NOT NULL,
    errors           bigint      NOT NULL,  -- dirs with an error
    anonymized       boolean     NOT NULL,
    replayed_from    text,                  -- absolute capture path; NULL for live samples
    stored           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trackdir.dir_state (
    sample_id   bigint NOT NULL REFERENCES trackdir.sample ON DELETE CASCADE,
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
    PRIMARY KEY (sample_id, path),
    CHECK ((error_stage IS NULL) = (error_code IS NULL))
);

CREATE VIEW trackdir.current_dir_state AS
SELECT DISTINCT ON (s.tree_id, d.path) s.tree_id, d.*
FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
ORDER BY s.tree_id, d.path, s.started DESC, s.id DESC;
"""  # noqa: E501

# dir_state columns in COPY order; dir_state_row() gives all but sample_id.
DIR_STATE_COLUMNS = (
    "sample_id",
    "path",
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

# Matches a tree by key. fs_name is NULL for the default filesystem, which "="
# would never match.
_TREE_MATCH = "cluster_fsid = %s AND fs_name IS NOT DISTINCT FROM %s AND fs_path = %s"


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
    """Identifies a tree: a directory of a CephFS filesystem, on any host or mount."""

    cluster_fsid: str
    fs_name: str | None  # None: the cluster's default filesystem
    fs_path: bytes  # the directory's absolute path in the filesystem

    @classmethod
    def of(cls, cluster_fsid: str, fs_name: str | None, fs_path: str) -> Self:
        """Return the key for a tree given as header strings; fs_path is fsencoded."""
        return cls(cluster_fsid, fs_name, os.fsencode(fs_path))


class SamplesExist(DbError):
    """The tree already has samples, so an initial sample is refused."""

    def __init__(self, key: TreeKey) -> None:
        super().__init__(f"tree {key} already has samples")
        self.key = key


def tree_key_of(header: CaptureHeader) -> TreeKey:
    """Return the key of the tree HEADER describes."""
    return TreeKey.of(header.cluster_fsid, header.fs_name, header.fs_path)


def dir_state_row(probe: DirProbe) -> tuple:
    """Return PROBE's values for DIR_STATE_COLUMNS[1:]; unknown values are None."""
    error = probe.error
    return (
        probe.path,
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


def has_samples(conn: "psycopg.Connection", key: TreeKey) -> bool:
    """Return whether the tree KEY has samples; False if the tree is unknown."""
    query = (
        "SELECT EXISTS (SELECT FROM trackdir.sample WHERE tree_id ="
        f" (SELECT id FROM trackdir.tree WHERE {_TREE_MATCH}))"
    )
    with _db_errors():
        return conn.execute(query, _key_params(key)).fetchone()[0]


def store_initial_sample(
    conn: "psycopg.Connection",
    header: CaptureHeader,
    probes: Iterable[DirProbe],
    replayed_from: str | None,
) -> int:
    """Store the first sample of HEADER's tree, in one transaction; return its id.

    The sample row comes from HEADER (including dirs and errors, which PROBES
    must match); PROBES become its dir_state rows, loaded with COPY.
    REPLAYED_FROM is the capture's absolute path, None for a live sample.
    Raises SamplesExist if the tree already has a sample; nothing is stored.

    Locking the tree row serializes concurrent initial samples of one tree
    under READ COMMITTED: the second one waits for the first to commit, then
    sees its sample.
    """
    key = tree_key_of(header)
    params = _key_params(key)
    with _db_errors(), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trackdir.tree (cluster_fsid, fs_name, fs_path)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            params,
        )
        cur.execute(
            f"SELECT id FROM trackdir.tree WHERE {_TREE_MATCH} FOR UPDATE", params
        )
        (tree_id,) = cur.fetchone()
        cur.execute(
            "SELECT EXISTS (SELECT FROM trackdir.sample WHERE tree_id = %s)",
            (tree_id,),
        )
        if cur.fetchone()[0]:
            raise SamplesExist(key)
        cur.execute(
            "INSERT INTO trackdir.sample (tree_id, root, max_depth, rstat_mode,"
            " openat2, jobs, started, finished, euid, host, kernel,"
            " trackdir_version, dirs, errors, anonymized, replayed_from)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (
                tree_id,
                os.fsencode(header.root),
                header.max_depth,
                header.rstat_mode,
                header.openat2,
                header.jobs,
                datetime.fromisoformat(header.started),
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
        with cur.copy(f"COPY trackdir.dir_state ({columns}) FROM STDIN") as copy:
            for probe in probes:
                copy.write_row((sample_id, *dir_state_row(probe)))
    return sample_id


def _key_params(key: TreeKey) -> tuple[str, str | None, bytes]:
    return key.cluster_fsid, key.fs_name, key.fs_path
