"""What the database commands share: the DSN, connecting, and error messages."""

import contextlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from trackdir.db import (
    SCHEMA_VERSION,
    ConcurrentSample,
    DbError,
    OutOfOrderSample,
    PsycopgMissing,
    SchemaMissing,
    SchemaVersionMismatch,
    TreeKey,
    connect,
)
from trackdir.messages import FatalError

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


@contextlib.contextmanager
def database(dsn: str, unchanged: str) -> Iterator["psycopg.Connection"]:
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
                f"this sample started at {utc(started)}, which is not later than "
                f"the latest sample of the {describe_tree(key)} ({utc(latest)}); "
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
    """Return "tree (cluster FSID, filesystem NAME, path PATH, depth N)"."""
    return f"tree ({tree_identity(key)})"


def tree_identity(key: TreeKey) -> str:
    """Return "cluster FSID, filesystem NAME, path PATH, depth N".

    "default filesystem" stands in for a None fs_name.
    """
    fs = "default filesystem" if key.fs_name is None else f"filesystem {key.fs_name}"
    return (
        f"cluster {key.cluster_fsid}, {fs}, path {os.fsdecode(key.fs_path)}, "
        f"depth {key.max_depth}"
    )


def utc(moment: datetime) -> str:
    """Return MOMENT in ISO 8601 and UTC, as capture headers give it."""
    return moment.astimezone(UTC).isoformat()
