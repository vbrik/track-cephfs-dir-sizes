import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import SUBTREE, assert_invariants

from trackdir import db as db_module
from trackdir.baseline import ROW_COLUMNS, Baseline, diff
from trackdir.capture import CaptureHeader, load_capture
from trackdir.db import (
    STAGE_COLUMNS,
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
    dir_state_row,
    init_schema,
    latest_started,
    load_baseline,
    store_sample,
    tree_key_of,
)
from trackdir.model import (
    ROOT,
    DirLimitExceeded,
    DirProbe,
    ProbeError,
    Stage,
    tree_key,
)

SRC = Path(__file__).parent.parent / "src"

HEADER = CaptureHeader(
    root="/mnt/cephfs/x",
    max_depth=1,
    cluster_fsid="00000000-0000-0000-0000-000000000000",
    fs_name=None,
    fs_path="/x",
    rstat_mode="dirstat",
    openat2=True,
    jobs=4,
    started="2026-10-15T00:00:00+00:00",
    finished="2026-10-15T00:00:01+00:00",
    euid=0,
    host="host",
    kernel="7.2.10",
    trackdir_version="0.1.0",
    dirs=1,
    errors=0,
    anonymized=False,
)
FULL = DirProbe(
    path=b"a/b",
    ino=1,
    mode=0o40755,
    uid=2,
    gid=3,
    nlink=4,
    rctime=5,
    rbytes=6,
    rfiles=7,
    rsubdirs=8,
    children=(b"c",),
)
T0 = datetime.fromisoformat(HEADER.started)


def later(header: CaptureHeader, seconds: int, **changes) -> CaptureHeader:
    """Return HEADER started SECONDS after HEADER (the module's), with CHANGES."""
    started = (T0 + timedelta(seconds=seconds)).isoformat()
    return replace(header, started=started, finished=started, **changes)


def store(
    conn,
    header: CaptureHeader = HEADER,
    probes: Sequence[DirProbe] = (),
    *,
    baseline: Baseline | None = None,
    replayed_from: str | None = None,
    baseline_seconds: float = 0.0,
) -> int:
    """Store PROBES as a sample of HEADER's tree against BASELINE (default: loaded)."""
    key = tree_key_of(header)
    if baseline is None:
        baseline = load_baseline(conn, key)
    started = datetime.fromisoformat(header.started)
    sample_diff = diff(baseline, probes, header.max_depth, started)
    return store_sample(
        conn, header, key, baseline, sample_diff, replayed_from, baseline_seconds
    )


def read_sample(conn, sample_id) -> list[DirProbe]:
    """Return the non-tombstone rows of SAMPLE_ID as probes in tree order."""
    columns = ", ".join(("p.path", *(f"d.{c}" for c in ROW_COLUMNS)))
    rows = conn.execute(
        f"SELECT {columns} FROM {NAMED} WHERE d.sample_id = %s AND NOT d.deleted",
        (sample_id,),
    ).fetchall()
    probes = [
        DirProbe(
            *values,
            error=ProbeError(Stage(stage), code) if stage is not None else None,
        )
        for *values, stage, code in rows
    ]
    return sorted(probes, key=lambda p: tree_key(p.path))


def counts(conn) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM trackdir.{table}").fetchone()[0]
        for table in TABLES
    }


TABLES = ("tree", "sample", "path", "dir_state", "current_dir_state")
NOTHING_STORED = dict.fromkeys(TABLES, 0)
# dir_state rows with their paths
NAMED = "trackdir.dir_state d JOIN trackdir.path p ON p.id = d.path_id"


def history(conn, path: bytes, *columns: str) -> list[tuple]:
    """Return COLUMNS of PATH's dir_state rows in all trees, oldest first."""
    return conn.execute(
        f"SELECT {', '.join(f'd.{c}' for c in columns)} FROM {NAMED}"
        " WHERE p.path = %s ORDER BY d.sample_id",
        (path,),
    ).fetchall()


# Without a database


def test_dir_state_row_full():
    row = dir_state_row(FULL)
    assert len(row) == len(STAGE_COLUMNS) - 1  # not deleted
    assert row == (b"a/b", 1, 0o40755, 2, 3, 4, 5, 6, 7, 8, None, None)


def test_dir_state_row_missing_fields_are_none():
    assert dir_state_row(DirProbe(ROOT)) == (ROOT,) + (None,) * 11


def test_dir_state_row_splits_error():
    probe = DirProbe(b"a", uid=0, error=ProbeError(Stage.OPEN, "EACCES"))
    row = dict(zip(STAGE_COLUMNS[:-1], dir_state_row(probe), strict=True))
    assert row["error_stage"] == "open"
    assert type(row["error_stage"]) is str
    assert row["error_code"] == "EACCES"
    assert row["uid"] == 0


def test_dir_state_row_keeps_non_utf8_path():
    assert dir_state_row(DirProbe(b"caf\xe9"))[0] == b"caf\xe9"


def test_stage_columns_are_path_row_and_deleted():
    assert ("path", *ROW_COLUMNS, "deleted") == STAGE_COLUMNS


def test_schema_doc_covers_the_ddl():
    doc = (SRC.parent / "docs" / "schema.md").read_text()
    objects = re.findall(r"CREATE TABLE trackdir\.(\w+)", db_module.DDL)
    tables = re.findall(r"^CREATE TABLE .*? \((.*?)^\);", db_module.DDL, re.M | re.S)
    columns = re.findall(r"^    (\w+) ", "".join(tables), re.MULTILINE)
    columns = [c for c in columns if c not in ("PRIMARY", "UNIQUE", "CHECK")]
    assert "dir_state" in objects and "rsubdirs" in columns
    assert [n for n in objects + columns if f"`{n}`" not in doc] == []
    assert f"(currently {db_module.SCHEMA_VERSION})" in doc


def test_tree_key_of_encodes_fs_path():
    header = replace(HEADER, fs_name="cephfs", fs_path="/caf\udce9", max_depth=3)
    assert tree_key_of(header) == TreeKey(
        cluster_fsid=HEADER.cluster_fsid,
        fs_name="cephfs",
        fs_path=b"/caf\xe9",
        max_depth=3,
    )


def test_check_order():
    key = tree_key_of(HEADER)
    check_order(Baseline.empty(), key, T0)
    baseline = Baseline.build(1, T0, {})
    for started in (T0, T0 - timedelta(microseconds=1)):
        with pytest.raises(OutOfOrderSample) as exc:
            check_order(baseline, key, started)
        assert exc.value.key == key
        assert exc.value.started == started
        assert exc.value.latest_started == T0
    check_order(baseline, key, T0 + timedelta(microseconds=1))


def test_store_sample_refuses_a_key_of_another_tree():
    key = tree_key_of(HEADER)
    empty = diff(Baseline.empty(), (), HEADER.max_depth, T0)
    with pytest.raises(ValueError, match="not the tree of the header"):
        store_sample(
            None, HEADER, replace(key, max_depth=2), Baseline.empty(), empty, None, 0.0
        )


def test_connect_without_psycopg(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(PsycopgMissing):
        connect("x")


def test_import_without_psycopg():
    code = "import sys; sys.modules['psycopg'] = None; import trackdir.db, trackdir.cli"
    env = os.environ | {"PYTHONPATH": str(SRC)}
    subprocess.run([sys.executable, "-c", code], env=env, check=True)


# With the db fixture


def test_init_creates_and_is_idempotent(db):
    assert init_schema(db) is True
    assert init_schema(db) is False
    assert db.execute("SELECT schema_version FROM trackdir.meta").fetchall() == [(5,)]
    check_schema(db)


def test_init_rejects_other_version(db):
    init_schema(db)
    db.execute("UPDATE trackdir.meta SET schema_version = 99")
    with pytest.raises(SchemaVersionMismatch) as exc:
        init_schema(db)
    assert exc.value.found == 99
    with pytest.raises(SchemaVersionMismatch):
        check_schema(db)


def test_init_rejects_schema_without_meta(db):
    db.execute("CREATE SCHEMA trackdir")
    with pytest.raises(SchemaVersionMismatch) as exc:
        init_schema(db)
    assert exc.value.found is None


def test_check_schema_missing(db):
    with pytest.raises(SchemaMissing):
        check_schema(db)


def test_store_round_trip(db):
    init_schema(db)
    c = load_capture(SUBTREE)
    probes = list(c.probes.values())
    sample_id = store(db, c.header, probes, replayed_from="/abs/sub")
    assert read_sample(db, sample_id) == [replace(p, children=None) for p in probes]
    row = db.execute(
        "SELECT started, finished, dirs, errors, replayed_from"
        " FROM trackdir.sample WHERE id = %s",
        (sample_id,),
    ).fetchone()
    assert row == (
        datetime.fromisoformat(c.header.started),
        datetime.fromisoformat(c.header.finished),
        c.header.dirs,
        c.header.errors,
        "/abs/sub",
    )
    current = db.execute("SELECT count(*) FROM trackdir.current_dir_state")
    assert current.fetchone()[0] == len(c.probes)


def test_load_baseline_empty_for_unknown_tree_and_for_tree_without_samples(db):
    init_schema(db)
    key = tree_key_of(HEADER)
    assert load_baseline(db, key) == Baseline.empty()
    db.execute(
        "INSERT INTO trackdir.tree (cluster_fsid, fs_name, fs_path, max_depth)"
        " VALUES (%s, %s, %s, %s)",
        (key.cluster_fsid, key.fs_name, key.fs_path, key.max_depth),
    )
    assert load_baseline(db, key) == Baseline.empty()


def test_latest_started(db):
    init_schema(db)
    key = tree_key_of(HEADER)
    assert latest_started(db, key) is None
    db.execute(
        "INSERT INTO trackdir.tree (cluster_fsid, fs_name, fs_path, max_depth)"
        " VALUES (%s, %s, %s, %s)",
        (key.cluster_fsid, key.fs_name, key.fs_path, key.max_depth),
    )
    assert latest_started(db, key) is None
    store(db, HEADER)
    store(db, later(HEADER, 60))
    assert latest_started(db, key) == T0 + timedelta(seconds=60)
    assert latest_started(db, tree_key_of(replace(HEADER, max_depth=3))) is None


# A tree to depth 2 (ROOT, a, a/b) and its second sample, which changes a and
# finds a/b gone.
DEEP = replace(HEADER, max_depth=2)
INITIAL = [
    DirProbe(ROOT, ino=1, rctime=10, children=(b"a",)),
    DirProbe(b"a", ino=2, rctime=10, children=(b"b",)),
    DirProbe(b"a/b", ino=3, rctime=10),
]
SECOND = [
    DirProbe(ROOT, ino=1, rctime=20, children=(b"a",)),
    DirProbe(b"a", ino=2, rctime=20, children=()),
]


def test_load_baseline_returns_latest_state_per_path(db):
    init_schema(db)
    store(db, DEEP, INITIAL)
    second = later(DEEP, 60)
    second_id = store(db, second, SECOND)
    baseline = load_baseline(db, tree_key_of(DEEP))
    assert baseline.sample_id == second_id
    assert baseline.started == datetime.fromisoformat(second.started)
    assert baseline.rows == {
        ROOT: (1, None, None, None, None, 20, None, None, None, None, None),
        b"a": (2, None, None, None, None, 20, None, None, None, None, None),
    }


def test_load_baseline_stops_past_max_dirs(db):
    init_schema(db)
    store(db, DEEP, INITIAL)
    key = tree_key_of(DEEP)
    assert len(load_baseline(db, key, max_dirs=len(INITIAL)).rows) == len(INITIAL)
    with pytest.raises(DirLimitExceeded) as exc:
        load_baseline(db, key, max_dirs=len(INITIAL) - 1)
    assert exc.value.limit == len(INITIAL) - 1


def test_tombstone_rows_satisfy_the_check_and_leave_the_current_state(db):
    import psycopg

    init_schema(db)
    store(db, DEEP, INITIAL)
    second_id = store(db, later(DEEP, 60), SECOND)
    columns = ", ".join(("deleted", *ROW_COLUMNS))
    tombstone = db.execute(
        f"SELECT {columns} FROM {NAMED} WHERE d.sample_id = %s AND p.path = %s",
        (second_id, b"a/b"),
    ).fetchone()
    assert tombstone == (True,) + (None,) * len(ROW_COLUMNS)
    assert db.execute(
        "SELECT count(*) FILTER (WHERE NOT deleted), count(*) FILTER (WHERE deleted)"
        " FROM trackdir.dir_state WHERE sample_id = %s",
        (second_id,),
    ).fetchone() == (2, 1)
    current = db.execute("SELECT path FROM trackdir.current_dir_state").fetchall()
    assert sorted(current) == [(ROOT,), (b"a",)]  # a/b's row is gone
    (root_id,) = db.execute(
        "SELECT id FROM trackdir.path WHERE path = %s", (ROOT,)
    ).fetchone()
    third_id = store(db, later(DEEP, 120))
    for values in ("1, false, true", "NULL, true, true"):  # values; created
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "INSERT INTO trackdir.dir_state"
                " (path_id, sample_id, ino, created, deleted)"
                f" VALUES (%s, %s, {values})",
                (root_id, third_id),
            )


def test_deleting_a_sample_with_rows_is_refused(db):
    import psycopg

    init_schema(db)
    sample_id = store(db, DEEP, INITIAL)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM trackdir.sample WHERE id = %s", (sample_id,))
    assert counts(db) == {
        "tree": 1,
        "sample": 1,
        "path": len(INITIAL),
        "dir_state": len(INITIAL),
        "current_dir_state": len(INITIAL),
    }


def test_a_tree_is_removed_rows_first(db):
    init_schema(db)
    store(db, DEEP, INITIAL)
    for table in ("dir_state", "current_dir_state", "sample", "path", "tree"):
        db.execute(f"DELETE FROM trackdir.{table}")
    assert counts(db) == NOTHING_STORED


def test_samples_of_a_tree_do_not_consume_tree_ids(db):
    init_schema(db)
    store(db, HEADER, [DirProbe(ROOT, rctime=1)])
    store(db, later(HEADER, 60), [DirProbe(ROOT, rctime=2)])
    store(db, later(HEADER, 120, fs_path="/y"), [DirProbe(ROOT)])
    assert db.execute("SELECT id FROM trackdir.tree ORDER BY id").fetchall() == [
        (1,),
        (2,),
    ]


def test_stale_baseline_is_refused(db):
    init_schema(db)
    key = tree_key_of(HEADER)
    store(db, HEADER, [DirProbe(ROOT, rctime=1)])
    stale = load_baseline(db, key)
    store(db, later(HEADER, 60), [DirProbe(ROOT, rctime=2)])
    after = counts(db)
    with pytest.raises(ConcurrentSample) as exc:
        store(db, later(HEADER, 120), [DirProbe(ROOT, rctime=3)], baseline=stale)
    assert exc.value.key == key
    assert counts(db) == after


def _wait_for_lock_wait(conn, pid: int) -> None:
    """Wait until backend PID waits on a lock; fail after 10 s."""
    deadline = time.monotonic() + 10
    query = "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s"
    while conn.execute(query, (pid,)).fetchone()[0] != "Lock":
        if time.monotonic() > deadline:
            pytest.fail(f"backend {pid} never waited on the tree row lock")
        time.sleep(0.01)


def test_stale_baseline_is_refused_under_a_repeatable_read_default(db):
    """The race check must see a sample committed while the store waited on
    the tree row lock, even if the session default is REPEATABLE READ (whose
    snapshot, taken before the wait, would hide that sample)."""
    init_schema(db)
    key = tree_key_of(HEADER)
    first_id = store(db, HEADER, [DirProbe(ROOT, rctime=1)])
    baseline = load_baseline(db, key)
    loser_header = later(HEADER, 120)
    loser_diff = diff(baseline, [DirProbe(ROOT, rctime=3)], HEADER.max_depth, T0)
    dsn = os.environ["TRACKDIR_TEST_DSN"]
    with connect(dsn) as rival, connect(dsn) as loser:
        loser.execute("SET default_transaction_isolation TO 'repeatable read'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with rival.transaction():
                # The rival holds the tree row and stores a later sample.
                rival.execute(
                    f"SELECT id FROM trackdir.tree WHERE {db_module._TREE_MATCH}"
                    " FOR UPDATE",
                    db_module._key_params(key),
                )
                rival.execute(
                    "INSERT INTO trackdir.sample (tree_id, root, rstat_mode,"
                    " openat2, jobs, started, finished, euid, host, kernel,"
                    " trackdir_version, dirs, errors, anonymized,"
                    " baseline_seconds)"
                    " SELECT tree_id, root, rstat_mode, openat2, jobs,"
                    " started + interval '1 minute', finished, euid, host,"
                    " kernel, trackdir_version, dirs, errors, anonymized, 0"
                    " FROM trackdir.sample WHERE id = %s",
                    (first_id,),
                )
                pending = pool.submit(
                    store_sample,
                    loser,
                    loser_header,
                    key,
                    baseline,
                    loser_diff,
                    None,
                    0.0,
                )
                _wait_for_lock_wait(db, loser.info.backend_pid)
            # The rival committed; the loser now gets the lock.
            with pytest.raises(ConcurrentSample):
                pending.result(timeout=10)
    assert counts(db) == {
        "tree": 1,
        "sample": 2,
        "path": 1,
        "dir_state": 1,
        "current_dir_state": 1,
    }


def test_initial_sample_racing_an_initial_sample_is_refused(db):
    init_schema(db)
    key = tree_key_of(HEADER)
    assert key.fs_name is None
    store(db, HEADER, [DirProbe(ROOT)], baseline=Baseline.empty())
    with pytest.raises(ConcurrentSample) as exc:
        store(db, HEADER, [DirProbe(ROOT)], baseline=Baseline.empty())
    assert exc.value.key == key
    assert counts(db) == {
        "tree": 1,
        "sample": 1,
        "path": 1,
        "dir_state": 1,
        "current_dir_state": 1,
    }


@pytest.mark.parametrize("seconds", [0, -1])
def test_older_started_is_refused(db, seconds):
    init_schema(db)
    store(db, HEADER, [DirProbe(ROOT, rctime=1)])
    after = counts(db)
    second = later(HEADER, seconds)
    with pytest.raises(OutOfOrderSample) as exc:
        store(db, second, [DirProbe(ROOT, rctime=2)])
    assert exc.value.started == datetime.fromisoformat(second.started)
    assert exc.value.latest_started == T0
    assert counts(db) == after


def test_trees_differ_by_depth_fs_name_and_fs_path(db):
    init_schema(db)
    trees = [
        HEADER,
        replace(HEADER, max_depth=2),
        replace(HEADER, fs_name="a"),
        replace(HEADER, fs_path="/other"),
    ]
    for ino, header in enumerate(trees):
        store(db, header, [DirProbe(ROOT, ino=ino)])
    assert counts(db)["tree"] == len(trees)
    for ino, header in enumerate(trees):
        baseline = load_baseline(db, tree_key_of(header))
        assert baseline.rows == {ROOT: (ino,) + (None,) * (len(ROW_COLUMNS) - 1)}
    # Two NULL fs_names with equal other fields are one tree.
    store(db, later(HEADER, 60), [DirProbe(ROOT, ino=99)])
    assert counts(db)["tree"] == len(trees)
    assert counts(db)["sample"] == len(trees) + 1


def test_awkward_values_round_trip(db):
    init_schema(db)
    probes = [
        DirProbe(ROOT, ino=2**63 - 1, rctime=4294967295 * 10**9),
        DirProbe(b"caf\xe9", error=ProbeError(Stage.READDIR, "EIO")),
    ]
    sample_id = store(db, HEADER, probes)
    assert read_sample(db, sample_id) == probes
    baseline = load_baseline(db, tree_key_of(HEADER))
    assert diff(baseline, probes, HEADER.max_depth, T0).changed == []


def test_out_of_range_value_stores_nothing(db):
    init_schema(db)
    with pytest.raises(DbError):
        store(db, HEADER, [DirProbe(ROOT, rbytes=2**63)])
    assert counts(db) == NOTHING_STORED


def test_interrupt_during_copy_stores_nothing(db, monkeypatch):
    init_schema(db)
    probes = [DirProbe(ROOT), *(DirProbe(f"d{i}".encode()) for i in range(1, 10))]
    rows = []

    def interrupting_row(probe):
        if len(rows) == 5:
            raise KeyboardInterrupt
        rows.append(probe)
        return (probe.path,) + (None,) * len(ROW_COLUMNS)

    monkeypatch.setattr(db_module, "dir_state_row", interrupting_row)
    with pytest.raises(KeyboardInterrupt):
        store(db, HEADER, probes)
    assert len(rows) == 5  # the interrupt came during COPY
    assert counts(db) == NOTHING_STORED


# What store_sample derives: path ids, previous values, the current state


def dirs(**rbytes: int | None) -> list[DirProbe]:
    """Return ROOT listing one directory per name, each with its RBYTES.

    None leaves the directory out. ROOT's rbytes is their sum, so it changes
    whenever one of them does.
    """
    present = {name.encode(): size for name, size in rbytes.items() if size is not None}
    root = DirProbe(ROOT, rbytes=sum(present.values()), children=tuple(present))
    return [root, *(DirProbe(p, rbytes=size) for p, size in present.items())]


def test_current_state_and_previous_values_follow_the_history(db):
    init_schema(db)
    failing = DirProbe(b"e", error=ProbeError(Stage.RSTATS, "EIO"))
    steps = [
        dirs(a=1, b=2),  # new
        dirs(a=5, b=2),  # modified
        dirs(a=5),  # deleted
        dirs(a=5, b=7),  # created again
        [*dirs(a=5, b=7, e=0)[:-1], failing],  # an error row
        dirs(a=5, b=7, e=3),  # the error recovered
    ]
    for minute, probes in enumerate(steps):
        store(db, later(HEADER, 60 * minute), probes)
        assert_invariants(db)
    assert counts(db)["path"] == 4  # ROOT, a, b, e
    assert counts(db)["current_dir_state"] == 4


def test_recreated_path_keeps_its_id_and_is_created(db):
    init_schema(db)
    for minute, probes in enumerate([dirs(a=1), dirs(), dirs(a=9)]):
        store(db, later(HEADER, 60 * minute), probes)
    rows = history(db, b"a", "path_id", "created", "deleted", "rbytes", "prev_rbytes")
    assert len({path_id for path_id, *_ in rows}) == 1
    assert [row[1:] for row in rows] == [
        (True, False, 1, None),
        (False, True, None, 1),
        (True, False, 9, None),
    ]


def test_tombstone_carries_the_last_known_values(db):
    init_schema(db)
    gone = DirProbe(b"a", rbytes=6, rfiles=7, rsubdirs=8)
    store(db, HEADER, [DirProbe(ROOT, rbytes=6, children=(b"a",)), gone])
    store(db, later(HEADER, 60), [DirProbe(ROOT, rbytes=0, children=())])
    columns = ("rbytes", "rfiles", "rsubdirs", "prev_rbytes", "prev_rfiles")
    assert history(db, b"a", *columns, "prev_rsubdirs")[-1] == (
        None,
        None,
        None,
        6,
        7,
        8,
    )


def test_error_row_gives_unknown_previous_values_but_not_created(db):
    init_schema(db)
    failing = DirProbe(b"a", error=ProbeError(Stage.RSTATS, "EIO"))
    store(db, HEADER, [DirProbe(ROOT, rbytes=0, children=(b"a",)), failing])
    store(db, later(HEADER, 60), dirs(a=4))
    assert history(db, b"a", "created", "rbytes", "prev_rbytes") == [
        (True, None, None),
        (False, 4, None),
    ]


def test_trees_with_the_same_relative_path_are_separate(db):
    init_schema(db)
    other = replace(HEADER, fs_path="/y")
    store(db, HEADER, dirs(a=1))
    store(db, other, dirs(a=2))
    paths = db.execute(
        "SELECT tree_id, id FROM trackdir.path WHERE path = %s", (b"a",)
    ).fetchall()
    assert len({tree for tree, _ in paths}) == len({i for _, i in paths}) == 2
    store(db, later(HEADER, 60), dirs())  # a is deleted in the first tree only
    assert_invariants(db)
    assert load_baseline(db, tree_key_of(HEADER)).rows.keys() == {ROOT}
    assert load_baseline(db, tree_key_of(other)).rows.keys() == {ROOT, b"a"}


def test_two_samples_on_one_connection_and_an_empty_one(db):
    init_schema(db)
    store(db, HEADER, dirs(a=1))
    empty_id = store(db, later(HEADER, 60), dirs(a=1))
    store(db, later(HEADER, 120), dirs(a=2))
    assert history(db, b"a", "rbytes") == [(1,), (2,)]
    assert db.execute(
        "SELECT count(*) FROM trackdir.dir_state WHERE sample_id = %s", (empty_id,)
    ).fetchone() == (0,)
    stage = db.execute("SELECT to_regclass('pg_temp.trackdir_stage')").fetchone()
    assert stage == (None,)
    assert_invariants(db)


def test_existing_paths_use_no_new_path_ids(db):
    init_schema(db)
    for minute in range(5):
        store(db, later(HEADER, 60 * minute), dirs(a=minute))
    assert db.execute("SELECT max(id), count(*) FROM trackdir.path").fetchone() == (
        2,
        2,
    )
    store(db, later(HEADER, 600), dirs(a=4, b=1))
    assert db.execute("SELECT max(id) FROM trackdir.path").fetchone() == (3,)


def test_non_utf8_path_round_trips_through_staging(db):
    init_schema(db)
    name = b"caf\xe9"
    for minute, size in enumerate((1, 2)):
        root = DirProbe(ROOT, rbytes=size, children=(name,))
        store(db, later(HEADER, 60 * minute), [root, DirProbe(name, rbytes=size)])
    assert db.execute(
        "SELECT rbytes FROM trackdir.current_dir_state WHERE path = %s", (name,)
    ).fetchone() == (2,)
    assert history(db, name, "prev_rbytes") == [(None,), (1,)]


_COUNTS_DIFFER = """SELECT count(*) FROM trackdir.sample s, LATERAL (
    SELECT count(*) FILTER (WHERE d.created) AS new,
           count(*) FILTER (WHERE NOT d.created AND NOT d.deleted) AS modified,
           count(*) FILTER (WHERE d.deleted) AS deleted
    FROM trackdir.dir_state d WHERE d.sample_id = s.id
) rows
WHERE (s.new_dirs, s.modified_dirs, s.deleted_dirs)
      IS DISTINCT FROM (rows.new, rows.modified, rows.deleted)"""


def test_sample_counts_match_its_rows(db):
    init_schema(db)
    steps = [dirs(a=1, b=2), dirs(a=5, c=3), dirs(a=5, c=3), dirs(b=1)]
    ids = [
        store(db, later(HEADER, 60 * minute), probes)
        for minute, probes in enumerate(steps)
    ]
    assert db.execute(
        "SELECT new_dirs, modified_dirs, deleted_dirs FROM trackdir.sample"
        " WHERE id = ANY(%s) ORDER BY id",
        (ids,),
    ).fetchall() == [
        (3, 0, 0),  # ROOT, a, b
        (1, 2, 1),  # c; ROOT and a; b
        (0, 0, 0),
        (1, 1, 2),  # b again; ROOT; a and c
    ]
    assert db.execute(_COUNTS_DIFFER).fetchone() == (0,)


def test_sample_records_phase_times(db):
    init_schema(db)
    sample_id = store(db, HEADER, dirs(a=1), baseline_seconds=1.5)
    baseline_seconds, store_seconds = db.execute(
        "SELECT baseline_seconds, store_seconds FROM trackdir.sample WHERE id = %s",
        (sample_id,),
    ).fetchone()
    assert baseline_seconds == 1.5
    assert 0 < store_seconds < 60


# Review fixes: the documented queries and procedures, and ROOT's lookup


def doc_sql(marker: str) -> str:
    """Return the SQL example of docs/schema.md that contains MARKER."""
    doc = (SRC.parent / "docs" / "schema.md").read_text()
    (block,) = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if marker in b]
    return block


def test_window_example_gives_no_growth_when_the_start_is_unknown(db):
    init_schema(db)
    failing = DirProbe(b"e", error=ProbeError(Stage.RSTATS, "EIO"))
    store(
        db,
        HEADER,
        [
            DirProbe(ROOT, rbytes=5, children=(b"e", b"gone")),
            failing,
            DirProbe(b"gone", rbytes=5),
        ],
    )
    store(db, later(HEADER, 60), dirs(e=1000, new=7))
    store(db, later(HEADER, 120), dirs(e=1500, new=7))
    query = doc_sql("AS grew").replace("now() - interval '7 days'", "%s")
    assert "%s" in query
    since = T0 + timedelta(seconds=60)
    grew = {path: growth for path, growth, *_ in db.execute(query, (since,))}
    assert grew == {
        ROOT: 1507 - 5,
        b"e": None,  # its rstats before the window were not read
        b"new": 7,  # created in the window: grew from nothing
        b"gone": None,  # deleted
    }


def test_documented_tree_removal_indexes_what_it_checks_and_leaves_no_index(db):
    init_schema(db)
    other = replace(DEEP, fs_path="/y")
    store(db, DEEP, INITIAL)
    store(db, later(DEEP, 60), SECOND)
    store(db, other, INITIAL)
    kept = load_baseline(db, tree_key_of(other))
    indexes = "SELECT count(*) FROM pg_indexes WHERE schemaname = 'trackdir'"
    before = db.execute(indexes).fetchone()
    block = doc_sql("DELETE FROM trackdir.tree")
    # Deleting a sample or a path checks current_dir_state for references,
    # which no permanent index serves.
    for column in ("sample_id", "path_id"):
        assert re.search(
            rf"CREATE INDEX \w+ ON trackdir.current_dir_state \({column}\)", block
        )
    (removed,) = db.execute(
        "SELECT id FROM trackdir.tree WHERE fs_path = %s", (b"/x",)
    ).fetchone()
    assert "tree_id = 3" in block and "WHERE id = 3" in block
    db.execute(
        block.replace("tree_id = 3", f"tree_id = {removed}").replace(
            "WHERE id = 3", f"WHERE id = {removed}"
        )
    )
    assert db.execute(indexes).fetchone() == before
    assert counts(db) == {
        "tree": 1,
        "sample": 1,
        "path": len(INITIAL),
        "dir_state": len(INITIAL),
        "current_dir_state": len(INITIAL),
    }
    assert load_baseline(db, tree_key_of(other)) == kept
    assert_invariants(db)


@pytest.mark.parametrize("tree", [None, 1])
def test_root_totals_are_found_through_indexes(db, tree):
    init_schema(db)
    store(db, DEEP, INITIAL)
    db.execute("SET enable_seqscan = off")  # a scan then shows only if unavoidable
    plan = db.execute(
        "EXPLAIN " + db_module._ROOT_NOW, {"tree": tree, "root": ROOT}
    ).fetchall()
    scans = [line for (line,) in plan if "Seq Scan on" in line]
    assert [s for s in scans if "current_dir_state" in s or " path " in s] == []


def test_current_size_example_is_found_through_indexes(db):
    init_schema(db)
    store(db, DEEP, [DirProbe(ROOT, rbytes=9, rfiles=1, rsubdirs=1)])
    query = doc_sql("SELECT c.rbytes, c.rfiles, c.rsubdirs")
    assert db.execute(query).fetchall() == [(9, 1, 1)]
    db.execute("SET enable_seqscan = off")
    plan = "".join(line for (line,) in db.execute("EXPLAIN " + query))
    assert "Seq Scan" not in plan
