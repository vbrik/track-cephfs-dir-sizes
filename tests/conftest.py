import os
from pathlib import Path

import pytest

from trackdir import live
from trackdir.model import RStats
from trackdir.mount import CephMount
from trackdir.rstats import RStatMode
from trackdir.safeopen import SafeRoot, openat2_supported

# Anonymized capture of a real CephFS tree: ~72k dirs, depth 4, 41 open EACCES.
SUBTREE = Path(__file__).parent / "data" / "ceph1-sub-depth4.jsonl.gz"


def collapse(text: str) -> str:
    return " ".join(text.split())


def must_not_walk(*args, **kwargs):
    pytest.fail("walked although an earlier check should have failed")


@pytest.fixture(params=[True, False], ids=["openat2", "fallback"])
def use_openat2(request) -> bool:
    """Run a test once with openat2 and once with the per-component fallback."""
    if request.param and not openat2_supported():
        pytest.skip("this kernel lacks openat2")
    return request.param


@pytest.fixture
def safe_root(tmp_path, use_openat2):
    with SafeRoot(tmp_path, use_openat2=use_openat2) as root:
        yield root


@pytest.fixture
def fake_ceph(monkeypatch):
    """Make local directories look like CephFS mounted with dirstat."""
    monkeypatch.setattr(live, "detect_mode", lambda fd: RStatMode.DIRSTAT)
    monkeypatch.setattr(live, "cluster_fsid", lambda fd: "fsid")
    monkeypatch.setattr(
        live, "read_rstats", lambda fd, mode: RStats(1_000_000_000, 10, 2, 1)
    )
    monkeypatch.setattr(live, "identify", lambda fd, root: CephMount(None, None, "/x"))


def _drop_schema(dsn: str) -> None:
    from trackdir.db import connect

    with connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS trackdir CASCADE")


@pytest.fixture
def db():
    """A connection to TRACKDIR_TEST_DSN; schema trackdir is dropped before and after.

    Skips without TRACKDIR_TEST_DSN or psycopg. Fails unless the database name
    ends in _test, so a mistyped DSN cannot drop development data.
    """
    dsn = os.environ.get("TRACKDIR_TEST_DSN")
    if not dsn:
        pytest.skip("TRACKDIR_TEST_DSN is not set")
    pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    from trackdir.db import connect

    dbname = str(conninfo_to_dict(dsn).get("dbname", ""))
    if not dbname.endswith("_test"):
        pytest.fail(
            f"TRACKDIR_TEST_DSN names database {dbname!r}; the database tests drop"
            " schema trackdir, so they run only on a database named *_test"
        )
    _drop_schema(dsn)
    with connect(dsn) as conn:
        yield conn
    _drop_schema(dsn)


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "z").mkdir()
    return root
