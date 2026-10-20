import io
import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trackdir import live
from trackdir.capture import Capture, is_listed, load_capture, write_capture
from trackdir.model import ROOT, DirProbe, RStats, path_depth, split, tree_key
from trackdir.mount import CephMount
from trackdir.rstats import RStatMode
from trackdir.safeopen import SafeRoot, openat2_supported
from trackdir.sample import SampleOptions, SampleResult, run_sample

# Anonymized capture of a real CephFS tree: ~72k dirs, depth 4, 41 open EACCES.
SUBTREE = Path(__file__).parent / "data" / "ceph1-sub-depth4.jsonl.gz"


def collapse(text: str) -> str:
    return " ".join(text.split())


class Terminal(io.StringIO):
    """A stream that claims to be a terminal; its width is unknown (no fileno)."""

    def isatty(self) -> bool:
        return True


def status_lines(text: str) -> list[str]:
    """Return each progress line drawn in TEXT, "" for an erasure."""
    return [chunk.removesuffix("\x1b[K") for chunk in text.split("\r")[1:]]


def status_phases(text: str) -> list[str]:
    """Return the progress lines drawn in TEXT, each walk as one "probed"."""
    shown: list[str] = []
    for line in status_lines(text):
        line = "probed" if line.startswith("probed ") else line
        if line and shown[-1:] != [line]:
            shown.append(line)
    return shown


@pytest.fixture(scope="module")
def subtree() -> Capture:
    return load_capture(SUBTREE)


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


# Key of the advisory lock that makes concurrent test runs take turns at the
# shared test database; any constant works if every run uses the same one.
_DB_LOCK = 0x74726B64  # "trkd"


def _drop_schema(dsn: str) -> None:
    from trackdir.db import connect

    with connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS trackdir CASCADE")


@pytest.fixture
def db():
    """A connection to TRACKDIR_TEST_DSN; schema trackdir is dropped before and after.

    Holds a session advisory lock throughout, so a concurrent test run waits
    instead of dropping the schema or storing samples under this test. Skips
    without TRACKDIR_TEST_DSN or psycopg. Fails unless the database name ends
    in _test, so a mistyped DSN cannot drop development data.
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
    with connect(dsn) as lock:
        lock.execute("SELECT pg_advisory_lock(%s)", (_DB_LOCK,))
        _drop_schema(dsn)
        with connect(dsn) as conn:
            yield conn
        _drop_schema(dsn)


@pytest.fixture
def dsn(db):
    """The DSN of the db fixture's database, password included."""
    return os.environ["TRACKDIR_TEST_DSN"]


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "z").mkdir()
    return root


def edited_capture(
    capture: Capture,
    out: Path,
    *,
    started: str,
    touch: Sequence[bytes] = (),
    remove: Sequence[bytes] = (),
    add: Sequence[bytes] = (),
    fix: Sequence[bytes] = (),
) -> Path:
    """Write to OUT a copy of CAPTURE as a later run would have seen it; return OUT.

    The header gets STARTED as started and finished, and recomputed dirs and
    errors. Edits apply in the order remove, add, touch, fix; a path is
    relative to ROOT, and ROOT cannot be removed or added. Raises ValueError for
    an add that no listed parent in CAPTURE could hold.
    - touch: a change at each path, as CephFS reports it: the path and every
      ancestor gain 10**9 ns of rctime and 1 byte of rbytes.
    - remove: each path and its descendants disappear; the parent chain is
      touched.
    - add: a new empty directory, at a depth the capture covers; the parent
      chain is touched.
    - fix: an errored directory becomes readable (nlink 2, no subdirectories,
      rstats set); its ancestors stay as they were, so only the retry set can
      find it.
    """
    probes = dict(capture.probes)
    max_depth = capture.header.max_depth

    def bump(path: bytes) -> None:
        """Touch PATH and its ancestors, ROOT last; skip a removed PATH."""
        while path in probes:
            probe = probes[path]
            probes[path] = replace(
                probe, rctime=probe.rctime + 10**9, rbytes=probe.rbytes + 1
            )
            if path == ROOT:
                return
            path = split(path)[0]

    below = tuple(p + b"/" for p in remove)
    probes = {
        p: v for p, v in probes.items() if p not in remove and not p.startswith(below)
    }
    bumps = [*touch, *(split(p)[0] for p in remove)]
    next_ino = max(p.ino for p in probes.values()) + 1
    for path in add:
        parent = probes.get(split(path)[0])
        if parent is None or not is_listed(parent, max_depth):
            raise ValueError(f"no listed parent for the new directory {path!r}")
        if path_depth(path) > max_depth or path in probes:
            raise ValueError(f"cannot add {path!r}: too deep or present")
        probes[path] = DirProbe(
            path,
            ino=next_ino,
            mode=parent.mode,
            uid=parent.uid,
            gid=parent.gid,
            nlink=2,
            rctime=parent.rctime + 10**9,
            rbytes=0,
            rfiles=0,
            rsubdirs=1,
        )
        next_ino += 1
        bumps.append(split(path)[0])
    for path in bumps:
        bump(path)
    for path in fix:
        old = probes[path]
        if old.error is None:
            raise ValueError(f"{path!r} has no error to fix")
        probes[path] = replace(
            old,
            error=None,
            nlink=2,
            rctime=probes[split(path)[0]].rctime,
            rbytes=1000,
            rfiles=3,
            rsubdirs=1,
        )
    ordered = [probes[p] for p in sorted(probes, key=tree_key)]
    header = replace(
        capture.header,
        started=started,
        finished=started,
        dirs=len(ordered),
        errors=sum(1 for p in ordered if p.error),
    )
    write_capture(out, header, ordered)
    return out


def replay(dsn: str, capture: Path, **options) -> SampleResult:
    return run_sample(SampleOptions(dsn=dsn, replay=capture, **options), print)


def later(capture: Capture, hours: float = 1) -> str:
    """Return the start time HOURS after CAPTURE's."""
    started = datetime.fromisoformat(capture.header.started)
    return (started + timedelta(hours=hours)).isoformat()
