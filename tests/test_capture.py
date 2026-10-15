import dataclasses
import gzip
import json
import os
import stat
from dataclasses import replace

import pytest

from trackdir.capture import (
    CaptureFormatError,
    CaptureHeader,
    CaptureWriter,
    ReplayBackend,
    load_capture,
    write_capture,
)
from trackdir.model import ROOT, DirProbe, RStats
from trackdir.probe import LiveBackend
from trackdir.safeopen import SafeRoot, openat2_supported
from trackdir.walker import walk

HEADER = CaptureHeader(
    root="/mnt/cephfs/x",
    max_depth=2,
    cluster_fsid="00000000-0000-0000-0000-000000000000",
    rstat_mode="dirstat",
    openat2=True,
    jobs=4,
    started="2026-10-15T00:00:00+00:00",
    finished="2026-10-15T00:00:01+00:00",
    euid=0,
    host="host",
    kernel="7.2.10",
    trackdir_version="0.1.0",
    dirs=0,
    errors=0,
    anonymized=False,
)
AWKWARD_NAMES = [b"plain", b"caf\xe9", b"new\nline", b"back\\slash", b'quo"te', b"s p"]


def above(max_depth):
    return lambda probe: probe.depth < max_depth


def fake_read_rstats(fd):
    return RStats(rctime=1_792_088_576_117_716_441, rbytes=10, rfiles=2, rsubdirs=3)


@pytest.fixture
def captured(tmp_path):
    """Walk a real tree with awkward names at depth 2; return (probes, capture)."""
    tree = os.fsencode(tmp_path / "tree")
    for name in AWKWARD_NAMES:
        os.makedirs(tree + b"/" + name + b"/child/grandchild")
    with SafeRoot(tree, use_openat2=openat2_supported()) as root:
        backend = LiveBackend(root, fake_read_rstats)
        probes = walk(backend, above(2), max_dirs=100, jobs=4)
    out = tmp_path / "c.jsonl.gz"
    write_capture(out, replace(HEADER, dirs=len(probes)), probes)
    return probes, out


def test_round_trip_is_exact(captured):
    probes, out = captured
    capture = load_capture(out)
    assert list(capture.probes.values()) == probes
    assert capture.header == replace(HEADER, dirs=len(probes))
    assert {p.path for p in probes if p.depth == 1} == set(AWKWARD_NAMES)


def test_replay_walk_reproduces_the_capture(captured):
    probes, out = captured
    backend = ReplayBackend(load_capture(out), 2)
    assert walk(backend, above(2), max_dirs=100, jobs=4) == probes


def test_replay_at_smaller_depth_equals_depth_filtering(captured):
    probes, out = captured
    backend = ReplayBackend(load_capture(out), 1)
    expected = [
        replace(p, children=None) if p.depth == 1 else p for p in probes if p.depth <= 1
    ]
    assert walk(backend, above(1), max_dirs=100, jobs=4) == expected


def test_replay_deeper_than_the_capture_is_rejected(captured):
    with pytest.raises(ValueError, match="depth 2"):
        ReplayBackend(load_capture(captured[1]), 3)


def test_file_is_private_and_deterministic(tmp_path, captured):
    probes, out = captured
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    again = tmp_path / "again.jsonl.gz"
    write_capture(again, replace(HEADER, dirs=len(probes)), probes)
    assert again.read_bytes() == out.read_bytes()


def test_lines_are_ascii_json_with_the_header_first(captured):
    lines = gzip.decompress(captured[1].read_bytes()).splitlines()
    assert all(line.isascii() for line in lines)
    assert json.loads(lines[0])["format"] == "trackdir-capture"
    assert json.loads(lines[1])["path"] == "."


def test_never_overwrites(tmp_path):
    out = tmp_path / "c.jsonl.gz"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        write_capture(out, HEADER, [DirProbe(ROOT)])
    assert out.read_bytes() == b"precious"
    assert [p.name for p in tmp_path.iterdir()] == ["c.jsonl.gz"]


def test_failure_before_commit_leaves_nothing(tmp_path):
    with pytest.raises(RuntimeError), CaptureWriter(tmp_path / "c.jsonl.gz"):
        raise RuntimeError("walk failed")
    assert list(tmp_path.iterdir()) == []


def test_failure_during_commit_leaves_nothing(tmp_path):
    def probes():
        yield DirProbe(ROOT)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_capture(tmp_path / "c.jsonl.gz", HEADER, probes())
    assert list(tmp_path.iterdir()) == []


def header_json(**changes):
    fields = dataclasses.asdict(replace(HEADER, **changes))
    return {"format": "trackdir-capture", "version": 1, **fields}


EACCES = {"stage": "open", "code": "EACCES"}


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ([{"format": "other"}], "not a trackdir capture"),
        ([{**header_json(), "version": 2}], "version"),
        ([{**header_json(), "max_depth": "2"}], "bad header"),
        ([header_json(), {"path": "a"}], "first record"),
        ([header_json(), {"path": "."}, {"path": "."}], "duplicate"),
        ([header_json(), {"path": "."}, {"path": "a/b"}], "listed parent"),
        ([header_json(max_depth=0), {"path": "."}, {"path": "a"}], "deeper"),
        (
            [
                header_json(),
                {"path": "."},
                {"path": "a", "error": EACCES},
                {"path": "a/b"},
            ],
            "listed parent",
        ),
        ([header_json(), {"path": "."}, {"path": "../x"}], "bad record"),
        ([header_json(), {"path": ".", "rbytes": "12"}], "bad record"),
    ],
)
def test_malformed_captures_are_rejected(tmp_path, objects, message):
    path = tmp_path / "bad.jsonl.gz"
    lines = b"".join(json.dumps(obj).encode() + b"\n" for obj in objects)
    path.write_bytes(gzip.compress(lines))
    with pytest.raises(CaptureFormatError, match=message):
        load_capture(path)


def test_a_file_that_is_not_gzip_is_rejected(tmp_path):
    path = tmp_path / "plain"
    path.write_bytes(b"plain text")
    with pytest.raises(CaptureFormatError):
        load_capture(path)
