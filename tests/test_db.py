import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from conftest import SUBTREE

from trackdir.capture import CaptureHeader, load_capture
from trackdir.db import (
    DIR_STATE_COLUMNS,
    DbError,
    PsycopgMissing,
    SamplesExist,
    SchemaMissing,
    SchemaVersionMismatch,
    TreeKey,
    check_schema,
    connect,
    dir_state_row,
    has_samples,
    init_schema,
    store_initial_sample,
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


def read_sample(conn, sample_id) -> list[DirProbe]:
    """Return the dir_state rows of SAMPLE_ID as probes in tree order, children None."""
    columns = ", ".join(DIR_STATE_COLUMNS[1:])
    rows = conn.execute(
        f"SELECT {columns} FROM trackdir.dir_state WHERE sample_id = %s",
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
    assert len(row) == len(DIR_STATE_COLUMNS) - 1
    assert row == (b"a/b", 1, 0o40755, 2, 3, 4, 5, 6, 7, 8, None, None)


def test_dir_state_row_missing_fields_are_none():
    assert dir_state_row(DirProbe(ROOT)) == (ROOT,) + (None,) * 11


def test_dir_state_row_splits_error():
    probe = DirProbe(b"a", uid=0, error=ProbeError(Stage.OPEN, "EACCES"))
    row = dict(zip(DIR_STATE_COLUMNS[1:], dir_state_row(probe), strict=True))
    assert row["error_stage"] == "open"
    assert type(row["error_stage"]) is str
    assert row["error_code"] == "EACCES"
    assert row["uid"] == 0


def test_dir_state_row_keeps_non_utf8_path():
    assert dir_state_row(DirProbe(b"caf\xe9"))[0] == b"caf\xe9"


def test_tree_key_of_encodes_fs_path():
    header = replace(HEADER, fs_name="cephfs", fs_path="/caf\udce9")
    assert tree_key_of(header) == TreeKey(
        cluster_fsid=HEADER.cluster_fsid, fs_name="cephfs", fs_path=b"/caf\xe9"
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
    assert db.execute("SELECT schema_version FROM trackdir.meta").fetchall() == [(1,)]
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
    sample_id = store_initial_sample(db, c.header, probes, "/abs/sub")
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


def test_second_sample_of_default_fs_tree_is_refused(db):
    init_schema(db)
    key = tree_key_of(HEADER)
    assert HEADER.fs_name is None
    assert not has_samples(db, key)
    store_initial_sample(db, HEADER, [DirProbe(ROOT)], None)
    with pytest.raises(SamplesExist) as exc:
        store_initial_sample(db, HEADER, [DirProbe(ROOT)], None)
    assert exc.value.key == key
    assert has_samples(db, key)
    assert counts(db) == {"tree": 1, "sample": 1, "dir_state": 1}


def test_trees_differ_by_fs_name_and_path(db):
    init_schema(db)
    headers = [HEADER, replace(HEADER, fs_name="a"), replace(HEADER, fs_path="/other")]
    for header in headers:
        store_initial_sample(db, header, [DirProbe(ROOT)], None)
    assert counts(db)["tree"] == 3
    assert all(has_samples(db, tree_key_of(h)) for h in headers)


def test_awkward_values_round_trip(db):
    init_schema(db)
    probes = [
        DirProbe(ROOT, ino=2**63 - 1, rctime=4294967295 * 10**9),
        DirProbe(b"caf\xe9", error=ProbeError(Stage.READDIR, "EIO")),
    ]
    sample_id = store_initial_sample(db, HEADER, probes, None)
    assert read_sample(db, sample_id) == probes


def test_out_of_range_value_stores_nothing(db):
    init_schema(db)
    with pytest.raises(DbError):
        store_initial_sample(db, HEADER, [DirProbe(ROOT, rbytes=2**63)], None)
    assert counts(db) == NOTHING_STORED


def test_interrupt_during_copy_stores_nothing(db):
    init_schema(db)

    def probes():
        yield DirProbe(ROOT)
        for i in range(1, 10):
            yield DirProbe(f"d{i}".encode())
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        store_initial_sample(db, HEADER, probes(), None)
    assert counts(db) == NOTHING_STORED
