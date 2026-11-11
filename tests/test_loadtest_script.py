"""Tests for scripts/loadtest.py at a small scale, against the test database."""

import importlib.util
from pathlib import Path

import pytest
from conftest import assert_invariants

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "loadtest.py"


@pytest.fixture(scope="module")
def loadtest():
    spec = importlib.util.spec_from_file_location("loadtest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def count(db, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM trackdir.{table}").fetchone()[0]


def test_small_run_stores_what_it_reports(loadtest, db, dsn, capsys):
    args = ["--db", dsn, "--dirs", "200", "--samples", "6", "--changes", "5"]
    bursts = ["--burst-every", "2", "--burst-dirs", "30", "--burst-life", "2"]
    assert loadtest.main(args + bursts) == 0
    assert (count(db, "tree"), count(db, "sample")) == (1, 6)
    # Bursts come with samples 3 and 5; the first is deleted by sample 5.
    assert count(db, "current_dir_state") == 200 + 31
    assert count(db, "path") == 200 + 2 * 31
    assert_invariants(db)
    out, err = capsys.readouterr()
    assert err == ""
    header, *reports = out.splitlines()
    assert header.split() == [
        "sample",
        "live_dirs",
        "paths",
        "dir_state_rows",
        "baseline_s",
        "store_s",
        "bytes_on_disk",
    ]
    assert [line.split()[0] for line in reports] == ["1", "2", "3", "4", "5", "6"]
    last = reports[-1].split()
    assert last[1:3] == ["231", "262"]
    assert int(last[3]) == count(db, "dir_state")


def test_report_every_keeps_the_first_and_the_last_sample(loadtest, db, dsn, capsys):
    args = ["--db", dsn, "--dirs", "50", "--samples", "7", "--changes", "2"]
    assert loadtest.main([*args, "--report-every", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()[1:]
    assert [line.split()[0] for line in lines] == ["1", "3", "6", "7"]


def test_same_seed_stores_the_same_rows(loadtest, db, dsn):
    args = ["--db", dsn, "--dirs", "100", "--samples", "4", "--changes", "3"]
    query = (
        "SELECT p.path, d.sample_id, d.rbytes FROM trackdir.dir_state d"
        " JOIN trackdir.path p ON p.id = d.path_id ORDER BY 2, 1"
    )
    stored = []
    for _ in range(2):
        db.execute("DROP SCHEMA IF EXISTS trackdir CASCADE")
        assert loadtest.main(args) == 0
        stored.append(db.execute(query).fetchall())
    assert stored[0] == stored[1]
    assert len(stored[0]) > 100


def test_a_database_with_a_schema_is_refused(loadtest, db, dsn, capsys):
    from trackdir.db import init_schema

    init_schema(db)
    assert loadtest.main(["--db", dsn, "--dirs", "10", "--samples", "2"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "already has a trackdir schema" in err
    assert (count(db, "tree"), count(db, "sample")) == (0, 0)
