import errno
import os

import pytest

from trackdir.model import DIRSTAT_STALE, PARSE, RStats
from trackdir.rstats import (
    NotCephFSError,
    RStatMode,
    RStatsError,
    cluster_fsid,
    detect_mode,
    parse_dirstat,
    parse_int,
    parse_rctime,
    read_rstats,
)

# The text fs/ceph/dir.c:ceph_read_dir() returns for a directory on a dirstat mount.
KERNEL_DIRSTAT_FORMAT = (
    "entries:   %20d\n"
    " files:    %20d\n"
    " subdirs:  %20d\n"
    "rentries:  %20d\n"
    " rfiles:   %20d\n"
    " rsubdirs: %20d\n"
    "rbytes:    %20d\n"
    "rctime:    %10d.%09d\n"
)
PINNED_NS = 4294967295 * 10**9
XATTRS = {
    "ceph.dir.rctime": b"1791915209.387690940",
    "ceph.dir.rbytes": b"4773841593393",
    "ceph.dir.rfiles": b"3260577",
    "ceph.dir.rsubdirs": b"9068",
}
EXPECTED = RStats(
    rctime=1791915209387690940, rbytes=4773841593393, rfiles=3260577, rsubdirs=9068
)


def dirstat_text(rctime_ns: int) -> bytes:
    sec, nsec = divmod(rctime_ns, 10**9)
    r = EXPECTED
    values = (12, 2, 10, r.rfiles + r.rsubdirs, r.rfiles, r.rsubdirs, r.rbytes)
    return (KERNEL_DIRSTAT_FORMAT % (*values, sec, nsec)).encode()


class FakeCeph:
    """Stands in for os.getxattr and os.pread; an int value is raised as an errno."""

    def __init__(self, xattrs, dirstat):
        self.xattrs = xattrs
        self.dirstat = dirstat
        self.calls = []

    def getxattr(self, fd, name):
        self.calls.append(name)
        return self._result(self.xattrs.get(name, errno.ENODATA))

    def pread(self, fd, size, offset):
        self.calls.append(("pread", offset))
        return self._result(self.dirstat)

    @staticmethod
    def _result(value):
        if isinstance(value, int):
            raise OSError(value, os.strerror(value))
        return value


@pytest.fixture
def fake_ceph(monkeypatch):
    def install(xattrs, dirstat=errno.EISDIR):
        fake = FakeCeph(xattrs, dirstat)
        monkeypatch.setattr(os, "getxattr", fake.getxattr)
        monkeypatch.setattr(os, "pread", fake.pread)
        return fake

    return install


def test_parse_rctime():
    assert parse_rctime(b"1792088576.117716441") == 1792088576117716441
    assert parse_rctime(b"4294967295.000000000") == PINNED_NS


@pytest.mark.parametrize(
    "raw",
    [b"", b"1.5", b"1792088576", b" 1.000000000", b"-1.000000000", b"1.0000000001"],
)
def test_parse_rctime_rejects_malformed(raw):
    with pytest.raises(RStatsError) as exc:
        parse_rctime(raw)
    assert exc.value.code == PARSE


def test_parse_int():
    assert parse_int(b"11587802491317666") == 11587802491317666


@pytest.mark.parametrize("raw", [b"", b"-1", b"1 ", b"0x10", b"1.0"])
def test_parse_int_rejects_malformed(raw):
    with pytest.raises(RStatsError) as exc:
        parse_int(raw)
    assert exc.value.code == PARSE


def test_parse_dirstat_kernel_format():
    assert parse_dirstat(dirstat_text(EXPECTED.rctime)) == EXPECTED


def test_parse_dirstat_missing_field():
    lines = dirstat_text(EXPECTED.rctime).splitlines(keepends=True)
    text = b"".join(line for line in lines if not line.startswith(b"rbytes"))
    with pytest.raises(RStatsError) as exc:
        parse_dirstat(text)
    assert exc.value.code == PARSE


def test_dirstat_mode_is_one_getxattr_then_pread_at_offset_zero(fake_ceph):
    fake = fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime))
    assert read_rstats(3, RStatMode.DIRSTAT) == EXPECTED
    assert fake.calls == ["ceph.dir.rctime", ("pread", 0)]


def test_dirstat_newer_than_the_forced_getxattr_is_accepted(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime + 1))
    assert read_rstats(3, RStatMode.DIRSTAT).rctime == EXPECTED.rctime + 1


def test_dirstat_older_than_the_forced_getxattr_is_stale(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime - 1))
    with pytest.raises(RStatsError) as exc:
        read_rstats(3, RStatMode.DIRSTAT)
    assert exc.value.code == DIRSTAT_STALE


def test_getxattr_mode_reads_rctime_first(fake_ceph):
    fake = fake_ceph(XATTRS)
    assert read_rstats(3, RStatMode.GETXATTR) == EXPECTED
    assert fake.calls == [
        "ceph.dir.rctime",
        "ceph.dir.rbytes",
        "ceph.dir.rfiles",
        "ceph.dir.rsubdirs",
    ]


def test_getxattr_failure_reports_the_errno_name(fake_ceph):
    fake_ceph({**XATTRS, "ceph.dir.rfiles": errno.EIO})
    with pytest.raises(RStatsError) as exc:
        read_rstats(3, RStatMode.GETXATTR)
    assert exc.value.code == "EIO"


def test_detect_mode_with_and_without_dirstat(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime))
    assert detect_mode(3) is RStatMode.DIRSTAT
    fake_ceph(XATTRS, errno.EISDIR)
    assert detect_mode(3) is RStatMode.GETXATTR


@pytest.mark.parametrize("err", [errno.ENODATA, errno.EOPNOTSUPP])
def test_detect_mode_rejects_other_filesystems(fake_ceph, err):
    fake_ceph({"ceph.dir.rctime": err})
    with pytest.raises(NotCephFSError):
        detect_mode(3)


def test_detect_mode_on_a_real_local_directory(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(NotCephFSError):
            detect_mode(fd)
    finally:
        os.close(fd)


def test_cluster_fsid(fake_ceph):
    fsid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    fake_ceph({"ceph.cluster_fsid": fsid.encode()})
    assert cluster_fsid(3) == fsid
    fake_ceph({})
    assert cluster_fsid(3) is None
