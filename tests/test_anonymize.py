import stat
import subprocess
from pathlib import Path

import pytest

from trackdir.anonymize import (
    AnonymizationError,
    Anonymizer,
    anonymize,
    key_recipe,
    load_key,
)
from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, split, tree_key

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
HEADER = CaptureHeader(
    root="/mnt/ceph2/x",
    max_depth=2,
    cluster_fsid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    rstat_mode="dirstat",
    openat2=True,
    jobs=4,
    started="2026-10-15T00:00:00+00:00",
    finished="2026-10-15T00:00:01+00:00",
    euid=1000,
    host="host.example",
    kernel="7.2.10",
    trackdir_version="0.1.0",
    dirs=5,
    errors=1,
    anonymized=False,
)
EACCES = ProbeError(Stage.OPEN, "EACCES")
PROBES = [
    DirProbe(ROOT, ino=1, uid=0, gid=0, rbytes=30, children=(b"alice", b"proj")),
    DirProbe(b"alice", ino=2, uid=1000, gid=1000, rbytes=10, children=(b"2019",)),
    DirProbe(b"alice/2019", ino=3, uid=1000, gid=1000, rbytes=10),
    DirProbe(b"proj", ino=4, uid=2000, gid=1000, rbytes=20, children=(b"2019",)),
    DirProbe(b"proj/2019", ino=5, uid=0, gid=0, error=EACCES),
]


def kept_fields(probe):
    return (probe.ino, probe.mode, probe.nlink, probe.rctime, probe.rbytes, probe.error)


def test_names_ids_and_header_are_replaced():
    header, probes = anonymize(HEADER, PROBES, KEY)
    an = Anonymizer(KEY)
    assert {p.path for p in probes} == {an.path(p.path) for p in PROBES}
    assert not any(b"alice" in p.path or b"proj" in p.path for p in probes)
    assert {p.uid for p in probes} == {0, an.numeric_id(1000), an.numeric_id(2000)}
    assert header.anonymized
    assert header.host is None
    assert header.root.count("/") == 3
    assert "ceph2" not in header.root
    assert len(header.cluster_fsid) == 32
    assert header.cluster_fsid != HEADER.cluster_fsid
    assert header.euid == an.numeric_id(1000)


def test_other_fields_and_tree_shape_are_kept():
    _, probes = anonymize(HEADER, PROBES, KEY)
    assert [p.path for p in probes] == sorted((p.path for p in probes), key=tree_key)
    assert sorted(map(kept_fields, probes), key=repr) == sorted(
        map(kept_fields, PROBES), key=repr
    )
    children_of = {}
    for p in probes:
        if p.path != ROOT:
            parent, name = split(p.path)
            children_of.setdefault(parent, set()).add(name)
    assert children_of.keys() <= {p.path for p in probes}
    for p in probes:
        assert set(p.children or ()) == children_of.get(p.path, set())


def test_children_stay_sorted():
    names = tuple(b"%d" % i for i in range(10))
    probes = [DirProbe(ROOT, children=names), *(DirProbe(name) for name in names)]
    _, anonymized = anonymize(HEADER, probes, KEY)
    assert anonymized[0].children == tuple(sorted(anonymized[0].children))


def test_repeated_names_stay_repeated():
    an = Anonymizer(KEY)
    assert an.path(b"alice/2019").split(b"/")[1] == an.path(b"proj/2019").split(b"/")[1]
    assert len(an.name(b"alice")) == 16


def test_ids():
    an = Anonymizer(KEY)
    assert an.numeric_id(0) == 0
    assert 1 <= an.numeric_id(1000) <= 2**32 - 2
    assert an.numeric_id(1000) != an.numeric_id(1001)


def test_stable_for_a_key_and_different_across_keys():
    assert anonymize(HEADER, PROBES, KEY) == anonymize(HEADER, PROBES, KEY)
    assert anonymize(HEADER, PROBES, KEY)[1] != anonymize(HEADER, PROBES, OTHER_KEY)[1]


def test_sibling_name_collision_fails(monkeypatch):
    monkeypatch.setattr(Anonymizer, "name", lambda self, name: b"same")
    with pytest.raises(AnonymizationError, match="sibling"):
        anonymize(HEADER, PROBES, KEY)


def test_id_collision_fails(monkeypatch):
    monkeypatch.setattr(Anonymizer, "numeric_id", lambda self, n: 0 if n == 0 else 7)
    with pytest.raises(AnonymizationError, match="ids"):
        anonymize(HEADER, PROBES, KEY)


def write_key(path, data=KEY, mode=0o600):
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_load_key(tmp_path):
    assert load_key(write_key(tmp_path / "good.key")) == KEY
    with pytest.raises(ValueError, match="32"):
        load_key(write_key(tmp_path / "short.key", KEY[:31]))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620])
def test_load_key_refuses_a_key_others_can_access(tmp_path, mode):
    with pytest.raises(ValueError, match="chmod 600"):
        load_key(write_key(tmp_path / "k.key", mode=mode))


def test_load_key_refuses_a_file_that_is_not_regular():
    with pytest.raises(ValueError, match="regular file"):
        load_key(Path("/dev/null"))


def test_key_recipe_creates_a_usable_private_key(tmp_path):
    path = tmp_path / "new key"
    subprocess.run(["sh", "-c", key_recipe(path)], check=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(load_key(path)) == 32
