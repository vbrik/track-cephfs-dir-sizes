import os
from pathlib import Path

import pytest
from conftest import collapse

from trackdir import live
from trackdir.live import LiveRoot, open_live_root, walk_live, walk_within
from trackdir.messages import FatalError
from trackdir.model import ROOT
from trackdir.mount import CephMount, MountError
from trackdir.probe import LiveBackend
from trackdir.rstats import RStatMode


def test_missing_fsid_is_fatal(tree, fake_ceph, monkeypatch):
    monkeypatch.setattr(live, "cluster_fsid", lambda fd: None)
    with pytest.raises(FatalError) as exc, open_live_root(tree, print):
        pass
    assert "ceph.cluster_fsid" in str(exc.value)
    assert str(tree) in str(exc.value)


def test_unidentifiable_mount_is_fatal(tree, fake_ceph, monkeypatch):
    def unidentifiable(fd, root):
        raise MountError("filesystem type is ext4, not ceph")

    monkeypatch.setattr(live, "identify", unidentifiable)
    with pytest.raises(FatalError) as exc, open_live_root(tree, print):
        pass
    message = collapse(str(exc.value))
    assert "filesystem type is ext4, not ceph." in message
    assert "kernel client" in message


def test_unreadable_mount_info_is_fatal(tree, fake_ceph, monkeypatch):
    def unreadable(fd, root):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(live, "identify", unreadable)
    with pytest.raises(FatalError) as exc, open_live_root(tree, print):
        pass
    assert str(exc.value) == f"cannot read ROOT {tree}: Permission denied."


def test_fsid_mismatch_is_fatal(tree, fake_ceph, monkeypatch):
    monkeypatch.setattr(
        live, "identify", lambda fd, root: CephMount("other", None, "/")
    )
    with pytest.raises(FatalError) as exc, open_live_root(tree, print):
        pass
    message = collapse(str(exc.value))
    assert "reports cluster fsid fsid, but its mount names other" in message


def test_identity_uses_the_resolved_root(tree, fake_ceph, monkeypatch):
    seen = []

    def identify(fd, root):
        seen.append(root)
        return CephMount(None, None, "/x")

    monkeypatch.setattr(live, "identify", identify)
    monkeypatch.chdir(tree.parent)
    with open_live_root(Path("./root/"), print) as root:
        assert root.path == str(tree.resolve())
    assert seen == [str(tree.resolve())]


def test_live_root_carries_identity(tree, fake_ceph, monkeypatch):
    monkeypatch.setattr(
        live, "identify", lambda fd, root: CephMount("fsid", "cephfs", "/p/q")
    )
    with open_live_root(tree, print) as root:
        assert isinstance(root, LiveRoot)
        assert root.path == str(tree.resolve())
        assert root.cluster_fsid == "fsid"
        assert root.fs_name == "cephfs"
        assert root.fs_path == "/p/q"
        assert root.rstat_mode is RStatMode.DIRSTAT
        assert root.openat2 is live.openat2_supported()
        fd = root.safe_root.fd
    with pytest.raises(OSError):  # closed on exit
        os.fstat(fd)


def test_walk_live_builds_the_header(tree, fake_ceph, monkeypatch):
    monkeypatch.setattr(
        live, "identify", lambda fd, root: CephMount("fsid", "cephfs", "/p/q")
    )
    with open_live_root(tree, print) as root:
        result = walk_live(root, max_depth=1, max_dirs=100, jobs=2)
    header = result.header
    assert [p.path for p in result.probes] == [b".", b"a", b"z"]
    assert result.elapsed >= 0
    assert header.root == str(tree.resolve())
    assert header.max_depth == 1
    assert header.jobs == 2
    assert header.cluster_fsid == "fsid"
    assert (header.fs_name, header.fs_path) == ("cephfs", "/p/q")
    assert header.rstat_mode == "dirstat"
    assert header.openat2 is root.openat2
    assert header.dirs == 3
    assert header.errors == 0
    assert not header.anonymized


def test_walk_within_stops_at_max_depth(tree, fake_ceph):
    with open_live_root(tree, print) as root:
        backend = LiveBackend(root.safe_root, lambda fd: live.read_rstats(fd, None))
        probes = walk_within(backend, max_depth=2, max_dirs=100, jobs=2)
    assert [p.path for p in probes] == [b".", b"a", b"a/b", b"z"]
    assert probes[2].children is None  # at max depth: counted, not listed


def test_walk_within_reports_the_dir_limit(tree, fake_ceph):
    with open_live_root(tree, print) as root:
        backend = LiveBackend(root.safe_root, lambda fd: live.read_rstats(fd, None))
        with pytest.raises(FatalError) as exc:
            walk_within(backend, max_depth=2, max_dirs=2, jobs=2)
    message = collapse(str(exc.value))
    assert message.startswith("ROOT has more than 2 directories within depth 2.")
    assert "--max-dirs" in message and "--max-depth" in message


def test_walk_within_honors_a_custom_should_list(tree, fake_ceph):
    with open_live_root(tree, print) as root:
        backend = LiveBackend(root.safe_root, lambda fd: live.read_rstats(fd, None))
        probes = walk_within(
            backend,
            max_depth=3,
            max_dirs=100,
            jobs=2,
            should_list=lambda probe: probe.path == ROOT,
        )
    assert [p.path for p in probes] == [b".", b"a", b"z"]
    assert probes[0].children is not None
    assert probes[1].children is None  # within max depth, but not accepted


def test_walk_within_never_lists_at_max_depth_whatever_should_list_says(
    tree, fake_ceph
):
    with open_live_root(tree, print) as root:
        backend = LiveBackend(root.safe_root, lambda fd: live.read_rstats(fd, None))
        probes = walk_within(
            backend, max_depth=2, max_dirs=100, jobs=2, should_list=lambda probe: True
        )
    assert [p.path for p in probes] == [b".", b"a", b"a/b", b"z"]
    assert probes[2].children is None


def test_dir_limit_message_uses_the_given_advice(tree, fake_ceph):
    with open_live_root(tree, print) as root:
        backend = LiveBackend(root.safe_root, lambda fd: live.read_rstats(fd, None))
        with pytest.raises(FatalError) as exc:
            walk_within(backend, max_depth=2, max_dirs=2, jobs=2, limit_advice="X")
    assert collapse(str(exc.value)) == (
        "ROOT has more than 2 directories within depth 2. X"
    )
