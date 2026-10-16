import errno
import os
from dataclasses import replace

import pytest

from trackdir import probe as probe_module
from trackdir.model import ROOT, DirProbe, ProbeError, RStats, Stage
from trackdir.probe import LiveBackend
from trackdir.rstats import RStatsError

FAKE_RSTATS = RStats(rctime=1_000_000_000, rbytes=10, rfiles=2, rsubdirs=3)


def fake_read_rstats(fd: int) -> RStats:
    return FAKE_RSTATS


def failing_read_rstats(fd: int) -> RStats:
    raise RStatsError("EIO")


def list_all(probe: DirProbe) -> bool:
    return True


def set_nlink(monkeypatch, nlink: int) -> None:
    """Make os.fstat report NLINK: 2 imitates a CephFS leaf, 1 imitates btrfs."""
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        fields = (st.st_mode, st.st_ino, st.st_dev, nlink, st.st_uid, st.st_gid)
        return os.stat_result((*fields, st.st_size, 0, 0, 0))

    monkeypatch.setattr(os, "fstat", fstat)


@pytest.fixture
def backend(safe_root):
    return LiveBackend(safe_root, fake_read_rstats)


def test_reports_stat_rstats_and_only_subdirs(tmp_path, backend):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "file").touch()
    (tmp_path / "link").symlink_to("a")
    probe = backend.probe(ROOT, list_all)
    st = tmp_path.stat()
    assert (probe.ino, probe.mode, probe.uid, probe.gid) == (
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_gid,
    )
    assert (probe.rctime, probe.rbytes, probe.rfiles, probe.rsubdirs) == (
        1_000_000_000,
        10,
        2,
        3,
    )
    assert sorted(probe.children) == [b"a", b"b"]
    assert probe.error is None


def test_children_are_sorted_whatever_the_readdir_order(tmp_path, backend):
    for name in ["c", "a", "b"]:
        (tmp_path / name).mkdir()
    assert backend.probe(ROOT, list_all).children == (b"a", b"b", b"c")


def test_should_list_sees_rstats_and_can_refuse(tmp_path, backend):
    (tmp_path / "a").mkdir()
    seen = []

    def refuse(probe):
        seen.append(probe)
        return False

    assert backend.probe(ROOT, refuse).children is None
    assert seen[0].rbytes == 10
    assert seen[0].children is None


def test_rstats_error_is_recorded_and_dir_still_listed(tmp_path, safe_root):
    (tmp_path / "a").mkdir()
    probe = LiveBackend(safe_root, failing_read_rstats).probe(ROOT, list_all)
    assert probe.error == ProbeError(Stage.RSTATS, "EIO")
    assert probe.rbytes is None
    assert probe.children == (b"a",)


def test_readdir_error_replaces_rstats_error(tmp_path, safe_root, monkeypatch):
    (tmp_path / "a").mkdir()

    def broken(fd):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(probe_module, "_subdir_names", broken)
    probe = LiveBackend(safe_root, failing_read_rstats).probe(ROOT, list_all)
    assert probe.error == ProbeError(Stage.READDIR, "EIO")
    assert probe.children is None


def test_symlink_file_and_vanished_dir_give_open_errors(tmp_path, backend):
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to("d")
    (tmp_path / "f").touch()
    for path, code in [(b"l", "ELOOP"), (b"f", "ENOTDIR"), (b"gone", "ENOENT")]:
        error = ProbeError(Stage.OPEN, code)
        assert backend.probe(path, list_all) == DirProbe(path, error=error)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_unreadable_dir_keeps_owner(tmp_path, backend):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        probe = backend.probe(b"locked", list_all)
    finally:
        locked.chmod(0o700)
    assert probe.error == ProbeError(Stage.OPEN, "EACCES")
    assert probe.uid == os.getuid()
    assert probe.ino == locked.stat().st_ino
    assert probe.rbytes is None
    assert probe.children is None


def test_nlink_two_means_no_subdirs_without_readdir(safe_root, monkeypatch):
    set_nlink(monkeypatch, 2)
    monkeypatch.setattr(
        probe_module, "_subdir_names", lambda fd: pytest.fail("readdir called")
    )
    leaf_rstats = replace(FAKE_RSTATS, rsubdirs=1)
    backend = LiveBackend(safe_root, lambda fd: leaf_rstats)
    assert backend.probe(ROOT, list_all).children == ()


def test_nlink_two_with_subdirs_in_rstats_is_read(backend, monkeypatch):
    set_nlink(monkeypatch, 2)
    calls = []

    def recorder(fd):
        calls.append(fd)
        return (b"x",)

    monkeypatch.setattr(probe_module, "_subdir_names", recorder)
    probe = backend.probe(ROOT, list_all)
    assert len(calls) == 1
    assert probe.children == (b"x",)


def test_nlink_two_with_failed_rstats_still_skips_readdir(safe_root, monkeypatch):
    set_nlink(monkeypatch, 2)
    monkeypatch.setattr(
        probe_module, "_subdir_names", lambda fd: pytest.fail("readdir called")
    )
    probe = LiveBackend(safe_root, failing_read_rstats).probe(ROOT, list_all)
    assert probe.children == ()


def test_nlink_one_as_on_btrfs_still_lists(tmp_path, backend, monkeypatch):
    (tmp_path / "a").mkdir()
    set_nlink(monkeypatch, 1)
    assert backend.probe(ROOT, list_all).children == (b"a",)


def test_names_that_are_not_utf8(tmp_path, backend):
    name = b"caf\xe9"
    os.mkdir(os.fsencode(tmp_path) + b"/" + name)
    assert backend.probe(ROOT, list_all).children == (name,)
    assert backend.probe(name, list_all).error is None


def test_many_files_few_subdirs(tmp_path, backend):
    for i in range(2000):
        (tmp_path / f"file{i}").touch()
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    assert sorted(backend.probe(ROOT, list_all).children) == [b"x", b"y"]
