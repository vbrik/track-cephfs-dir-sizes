import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import SUBTREE

from trackdir import db as db_module
from trackdir.baseline import ROW_COLUMNS, Baseline, diff
from trackdir.capture import CaptureHeader, load_capture
from trackdir.db import (
    DIR_STATE_COLUMNS,
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
    load_baseline,
    store_sample,
    tree_key_of,
)
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, tree_key

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
MAX_DIRS = 10**6
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
    max_dirs: int = MAX_DIRS,
) -> int:
    """Store PROBES as a sample of HEADER's tree against BASELINE (default: loaded)."""
    key = tree_key_of(header, max_dirs)
    if baseline is None:
        baseline = load_baseline(conn, key)
    sample_diff = diff(baseline, probes, header.max_depth)
    return store_sample(conn, header, key, baseline, sample_diff, replayed_from)


def read_sample(conn, sample_id) -> list[DirProbe]:
    """Return the non-tombstone rows of SAMPLE_ID as probes in tree order."""
    columns = ", ".join(("path", *ROW_COLUMNS))
    rows = conn.execute(
        f"SELECT {columns} FROM trackdir.dir_state"
        " WHERE sample_id = %s AND NOT deleted",
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
        for table in ("tree", "sample", "dir_state")
    }


NOTHING_STORED = {"tree": 0, "sample": 0, "dir_state": 0}


# Without a database


def test_dir_state_row_full():
    row = dir_state_row(FULL)
    assert len(row) == len(DIR_STATE_COLUMNS) - 2  # not sample_id, not deleted
    assert row == (b"a/b", 1, 0o40755, 2, 3, 4, 5, 6, 7, 8, None, None)


def test_dir_state_row_missing_fields_are_none():
    assert dir_state_row(DirProbe(ROOT)) == (ROOT,) + (None,) * 11


def test_dir_state_row_splits_error():
    probe = DirProbe(b"a", uid=0, error=ProbeError(Stage.OPEN, "EACCES"))
    row = dict(zip(DIR_STATE_COLUMNS[1:-1], dir_state_row(probe), strict=True))
    assert row["error_stage"] == "open"
    assert type(row["error_stage"]) is str
    assert row["error_code"] == "EACCES"
    assert row["uid"] == 0


def test_dir_state_row_keeps_non_utf8_path():
    assert dir_state_row(DirProbe(b"caf\xe9"))[0] == b"caf\xe9"


def test_dir_state_columns_end_with_deleted():
    assert DIR_STATE_COLUMNS[:2] == ("sample_id", "path")
    assert DIR_STATE_COLUMNS[2:-1] == ROW_COLUMNS
    assert DIR_STATE_COLUMNS[-1] == "deleted"


def test_tree_key_of_encodes_fs_path():
    header = replace(HEADER, fs_name="cephfs", fs_path="/caf\udce9", max_depth=3)
    assert tree_key_of(header, 5) == TreeKey(
        cluster_fsid=HEADER.cluster_fsid,
        fs_name="cephfs",
        fs_path=b"/caf\xe9",
        max_depth=3,
        max_dirs=5,
    )


def test_check_order():
    key = tree_key_of(HEADER, MAX_DIRS)
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
    key = tree_key_of(HEADER, MAX_DIRS)
    empty = diff(Baseline.empty(), (), HEADER.max_depth)
    with pytest.raises(ValueError, match="not the tree of the header"):
        store_sample(
            None, HEADER, replace(key, max_depth=2), Baseline.empty(), empty, None
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
    assert db.execute("SELECT schema_version FROM trackdir.meta").fetchall() == [(2,)]
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
        "SELECT started, finished, dirs, errors, changed, deleted, replayed_from"
        " FROM trackdir.sample WHERE id = %s",
        (sample_id,),
    ).fetchone()
    assert row == (
        datetime.fromisoformat(c.header.started),
        datetime.fromisoformat(c.header.finished),
        c.header.dirs,
        c.header.errors,
        c.header.dirs,
        0,
        "/abs/sub",
    )
    current = db.execute("SELECT count(*) FROM trackdir.current_dir_state")
    assert current.fetchone()[0] == len(c.probes)


def test_load_baseline_empty_for_unknown_tree_and_for_tree_without_samples(db):
    init_schema(db)
    key = tree_key_of(HEADER, MAX_DIRS)
    assert load_baseline(db, key) == Baseline.empty()
    db.execute(
        "INSERT INTO trackdir.tree (cluster_fsid, fs_name, fs_path, max_depth,"
        " max_dirs) VALUES (%s, %s, %s, %s, %s)",
        (key.cluster_fsid, key.fs_name, key.fs_path, key.max_depth, key.max_dirs),
    )
    assert load_baseline(db, key) == Baseline.empty()


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
    baseline = load_baseline(db, tree_key_of(DEEP, MAX_DIRS))
    assert baseline.sample_id == second_id
    assert baseline.started == datetime.fromisoformat(second.started)
    assert baseline.rows == {
        ROOT: (1, None, None, None, None, 20, None, None, None, None, None),
        b"a": (2, None, None, None, None, 20, None, None, None, None, None),
    }


def test_tombstone_rows_satisfy_the_check_and_the_view_hides_them(db):
    import psycopg

    init_schema(db)
    store(db, DEEP, INITIAL)
    second_id = store(db, later(DEEP, 60), SECOND)
    columns = ", ".join(("deleted", *ROW_COLUMNS))
    tombstone = db.execute(
        f"SELECT {columns} FROM trackdir.dir_state WHERE sample_id = %s AND path = %s",
        (second_id, b"a/b"),
    ).fetchone()
    assert tombstone == (True,) + (None,) * len(ROW_COLUMNS)
    assert db.execute(
        "SELECT changed, deleted FROM trackdir.sample WHERE id = %s", (second_id,)
    ).fetchone() == (2, 1)
    current = db.execute("SELECT path FROM trackdir.current_dir_state").fetchall()
    assert sorted(current) == [(ROOT,), (b"a",)]  # a/b's older row stays hidden
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO trackdir.dir_state (sample_id, path, ino, deleted)"
            " VALUES (%s, %s, 1, true)",
            (second_id, b"x"),
        )


def test_stale_baseline_is_refused(db):
    init_schema(db)
    key = tree_key_of(HEADER, MAX_DIRS)
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
    key = tree_key_of(HEADER, MAX_DIRS)
    first_id = store(db, HEADER, [DirProbe(ROOT, rctime=1)])
    baseline = load_baseline(db, key)
    loser_header = later(HEADER, 120)
    loser_diff = diff(baseline, [DirProbe(ROOT, rctime=3)], HEADER.max_depth)
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
                    "INSERT INTO trackdir.sample (tree_id, root, max_depth,"
                    " rstat_mode, openat2, jobs, started, finished, euid, host,"
                    " kernel, trackdir_version, dirs, errors, changed, deleted,"
                    " anonymized) SELECT tree_id, root, max_depth, rstat_mode,"
                    " openat2, jobs, started + interval '1 minute', finished,"
                    " euid, host, kernel, trackdir_version, dirs, errors, 0, 0,"
                    " anonymized FROM trackdir.sample WHERE id = %s",
                    (first_id,),
                )
                pending = pool.submit(
                    store_sample, loser, loser_header, key, baseline, loser_diff, None
                )
                _wait_for_lock_wait(db, loser.info.backend_pid)
            # The rival committed; the loser now gets the lock.
            with pytest.raises(ConcurrentSample):
                pending.result(timeout=10)
    assert counts(db) == {"tree": 1, "sample": 2, "dir_state": 1}


def test_initial_sample_racing_an_initial_sample_is_refused(db):
    init_schema(db)
    key = tree_key_of(HEADER, MAX_DIRS)
    assert key.fs_name is None
    store(db, HEADER, [DirProbe(ROOT)], baseline=Baseline.empty())
    with pytest.raises(ConcurrentSample) as exc:
        store(db, HEADER, [DirProbe(ROOT)], baseline=Baseline.empty())
    assert exc.value.key == key
    assert counts(db) == {"tree": 1, "sample": 1, "dir_state": 1}


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


def test_trees_differ_by_depth_and_max_dirs(db):
    init_schema(db)
    trees = [
        (HEADER, MAX_DIRS),
        (replace(HEADER, max_depth=2), MAX_DIRS),
        (HEADER, MAX_DIRS + 1),
        (replace(HEADER, fs_name="a"), MAX_DIRS),
        (replace(HEADER, fs_path="/other"), MAX_DIRS),
    ]
    for ino, (header, max_dirs) in enumerate(trees):
        store(db, header, [DirProbe(ROOT, ino=ino)], max_dirs=max_dirs)
    assert counts(db)["tree"] == len(trees)
    for ino, (header, max_dirs) in enumerate(trees):
        baseline = load_baseline(db, tree_key_of(header, max_dirs))
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
    baseline = load_baseline(db, tree_key_of(HEADER, MAX_DIRS))
    assert diff(baseline, probes, HEADER.max_depth).changed == []


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
