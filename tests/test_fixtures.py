"""Invariants of real, anonymized captures from production CephFS clusters."""

from pathlib import Path

import pytest

from trackdir.capture import ReplayBackend, load_capture
from trackdir.model import Stage
from trackdir.walker import walk

DATA = Path(__file__).parent / "data"
FIXTURES = sorted(DATA.glob("*.jsonl.gz"))
PINNED_NS = 4294967295 * 10**9


@pytest.fixture(params=FIXTURES, ids=lambda path: path.name)
def capture(request):
    return load_capture(request.param)


def test_fixtures_exist():
    assert FIXTURES, f"no captures in {DATA}"


def test_only_anonymized_captures_are_committed(capture):
    assert capture.header.anonymized
    assert capture.header.host is None


def test_listed_dirs_have_nlink_minus_two_subdirs(capture):
    # CephFS reports nlink = 2 + subdirs; a mismatch means lost or invented names.
    mismatched = [
        p.path
        for p in capture.probes.values()
        if p.children is not None and p.nlink != 2 + len(p.children)
    ]
    assert mismatched == []


def test_nlink_two_means_rsubdirs_one(capture):
    # The check above is circular for dirs whose listing nlink == 2 skipped;
    # rsubdirs (counting the dir itself) is an independent count from the MDS.
    mismatched = [
        p.path
        for p in capture.probes.values()
        if p.nlink == 2 and p.rsubdirs is not None and p.rsubdirs != 1
    ]
    assert mismatched == []


def test_rsubdirs_count_the_dir_itself(capture):
    rsubdirs = [p.rsubdirs for p in capture.probes.values() if p.rsubdirs is not None]
    assert rsubdirs
    assert min(rsubdirs) >= 1


def test_replay_reproduces_the_capture(capture):
    depth = capture.header.max_depth
    probes = walk(
        ReplayBackend(capture, depth),
        lambda probe: probe.depth < depth,
        max_dirs=len(capture.probes),
        jobs=4,
    )
    assert probes == list(capture.probes.values())


def test_fixtures_cover_pinned_rctime_and_permission_errors():
    probes = [p for path in FIXTURES for p in load_capture(path).probes.values()]
    assert any(p.rctime == PINNED_NS for p in probes)
    assert any(
        p.error is not None and p.error.stage == Stage.OPEN and p.error.code == "EACCES"
        for p in probes
    )
