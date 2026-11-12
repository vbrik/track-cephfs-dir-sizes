"""What the database commands share: the DSN, connecting, and error messages."""

import contextlib
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
    TreeMismatch,
    TreeNamedOtherwise,
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
        raise FatalError(db_message(e, unchanged)) from e


def db_message(e: DbError, unchanged: str) -> str:
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
        case SchemaVersionMismatch(found=found) if found > SCHEMA_VERSION:
            # Dropping would delete what newer trackdirs stored; upgrading loses
            # nothing.
            return (
                f"the database's trackdir schema is version {found}, from a "
                f"newer trackdir, but this trackdir needs version "
                f"{SCHEMA_VERSION}. Upgrade trackdir on this host."
            )
        case SchemaVersionMismatch(found=found):
            return (
                f"the database's trackdir schema is version {found}, from an "
                f"older trackdir, but this trackdir needs version "
                f"{SCHEMA_VERSION}. There is no migration: keep using the older "
                f"trackdir with this database, or have an administrator "
                f"{_DROP_AND_INIT}"
            )
        case TreeMismatch(name=name, recorded=recorded, found=found):
            return (
                f"tree {name} is recorded in the database as "
                f"({tree_identity(recorded)}), but what was found is "
                f"({tree_identity(found)}): {_differences(recorded, found)}; "
                f"{unchanged}. If ROOT, its mount or the replayed capture is the "
                "wrong one, fix that. If this is another directory or depth, "
                "give it another tree name. If it is "
                'the same directory mounted differently, see "Changed mount" '
                "in docs/schema.md."
            )
        case TreeNamedOtherwise(name=name, other=other, key=key):
            return (
                f"the {describe_tree(key)} is already recorded as tree {other}, "
                f"not {name}; {unchanged}. A directory sampled to one depth is "
                f"one tree: use the name {other}."
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


def _differences(recorded: TreeKey, found: TreeKey) -> str:
    """Return how FOUND differs from RECORDED, e.g. "fs name differs
    (recorded the default filesystem, found cephfs)"; several differences are
    joined by ", ".

    Fields are compared as stored, not as shown, so two values that would
    print alike still count: a filesystem named "none" is not the default
    one, and paths with the same printable form are shown as bytes literals.
    """

    def fs_name(name: str | None) -> str:
        return "the default filesystem" if name is None else name

    def fs_paths(was: bytes, now: bytes) -> tuple[str, str]:
        shown = display_path(was), display_path(now)
        return shown if shown[0] != shown[1] else (repr(was), repr(now))

    fields = (
        (
            "cluster",
            recorded.cluster_fsid != found.cluster_fsid,
            (recorded.cluster_fsid, found.cluster_fsid),
        ),
        (
            "fs name",
            recorded.fs_name != found.fs_name,
            (fs_name(recorded.fs_name), fs_name(found.fs_name)),
        ),
        (
            "fs path",
            recorded.fs_path != found.fs_path,
            fs_paths(recorded.fs_path, found.fs_path),
        ),
        (
            "depth",
            recorded.max_depth != found.max_depth,
            (recorded.max_depth, found.max_depth),
        ),
    )
    return ", ".join(
        f"{label} differs (recorded {was}, found {now})"
        for label, differs, (was, now) in fields
        if differs
    )


def describe_tree(key: TreeKey) -> str:
    """Return "tree (cluster FSID, filesystem NAME, path PATH, depth N)"."""
    return f"tree ({tree_identity(key)})"


def tree_identity(key: TreeKey) -> str:
    """Return "cluster FSID, filesystem NAME, path PATH, depth N".

    "default filesystem" stands in for a None fs_name.
    """
    fs = "default filesystem" if key.fs_name is None else f"filesystem {key.fs_name}"
    return (
        f"cluster {key.cluster_fsid}, {fs}, path {display_path(key.fs_path)}, "
        f"depth {key.max_depth}"
    )


def display_path(path: bytes) -> str:
    r"""Return PATH as printable UTF-8 text for a terminal.

    Bytes that do not decode become \xNN, and unprintable characters (newlines,
    terminal escapes) their repr escapes, as directory names are anyone's to
    choose. Unlike fsdecode's surrogates, the result suits any UTF-8 stream.
    """
    text = path.decode(errors="backslashreplace")
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


def utc(moment: datetime) -> str:
    """Return MOMENT in ISO 8601 and UTC, as capture headers give it."""
    return moment.astimezone(UTC).isoformat()
