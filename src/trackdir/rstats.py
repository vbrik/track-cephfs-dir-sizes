"""Read CephFS recursive statistics (rstats) of an open directory.

Every ceph.dir.* getxattr is a synchronous MDS round trip. On a mount with the
dirstat option, one getxattr forces the client to refresh all rstats, and
pread() on the directory fd then returns them as text: one round trip per
directory instead of four. That the text reflects the refresh is observed
kernel behavior, not a documented contract, so read_rstats() checks it on
every call (DIRSTAT_STALE).
"""

import errno
import os
import re
from enum import StrEnum

from trackdir.model import DIRSTAT_STALE, PARSE, RStats, errno_name


class RStatMode(StrEnum):
    """How rstats are read on a mount."""

    DIRSTAT = "dirstat"  # one getxattr plus pread() per directory
    GETXATTR = "getxattr"  # one getxattr per rstat


class RStatsError(Exception):
    """Reading rstats failed; code is an errno name, DIRSTAT_STALE or PARSE."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotCephFSError(Exception):
    """The directory has no CephFS rstats."""


DIRSTAT_BUFSIZE = 4096  # the kernel formats about 256 bytes
_NS_PER_S = 1_000_000_000
_INT_RE = re.compile(rb"[0-9]+")
_RCTIME_RE = re.compile(rb"([0-9]+)\.([0-9]{9})")
_RCTIME = "ceph.dir.rctime"


def parse_int(raw: bytes) -> int:
    """Parse a decimal rstat value. Raises RStatsError(PARSE)."""
    if not _INT_RE.fullmatch(raw):
        raise RStatsError(PARSE)
    return int(raw)


def parse_rctime(raw: bytes) -> int:
    """Parse b"SECONDS.NANOSECONDS" into nanoseconds. Raises RStatsError(PARSE)."""
    match = _RCTIME_RE.fullmatch(raw)
    if not match:
        raise RStatsError(PARSE)
    return int(match[1]) * _NS_PER_S + int(match[2])


def parse_dirstat(text: bytes) -> RStats:
    """Parse what pread() returns for a directory on a dirstat mount.

    Raises RStatsError(PARSE).
    """
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(b":")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        return RStats(
            rctime=parse_rctime(fields[b"rctime"]),
            rbytes=parse_int(fields[b"rbytes"]),
            rfiles=parse_int(fields[b"rfiles"]),
            rsubdirs=parse_int(fields[b"rsubdirs"]),
        )
    except KeyError:
        raise RStatsError(PARSE) from None


def read_rstats(fd: int, mode: RStatMode) -> RStats:
    """Return the rstats of the directory open as FD (opened O_RDONLY).

    Never moves FD's file offset, because readdir on the same fd may follow.
    Raises RStatsError.
    """
    try:
        if mode is RStatMode.DIRSTAT:
            refreshed = parse_rctime(os.getxattr(fd, _RCTIME))
            rstats = parse_dirstat(os.pread(fd, DIRSTAT_BUFSIZE, 0))
            if rstats.rctime < refreshed:
                raise RStatsError(DIRSTAT_STALE)
            return rstats
        # rctime first: a change between the reads leaves the stored rctime
        # older than the other values, so the next sample still sees it.
        rctime = parse_rctime(os.getxattr(fd, _RCTIME))
        rbytes = parse_int(os.getxattr(fd, "ceph.dir.rbytes"))
        rfiles = parse_int(os.getxattr(fd, "ceph.dir.rfiles"))
        rsubdirs = parse_int(os.getxattr(fd, "ceph.dir.rsubdirs"))
    except OSError as e:
        raise RStatsError(errno_name(e.errno)) from e
    return RStats(rctime, rbytes, rfiles, rsubdirs)


def detect_mode(fd: int) -> RStatMode:
    """Return how to read rstats on the mount of FD (a directory opened O_RDONLY).

    Raises NotCephFSError if FD has no ceph.dir.rctime; other OSErrors propagate.
    """
    try:
        os.getxattr(fd, _RCTIME)
    except OSError as e:
        if e.errno in (errno.ENODATA, errno.EOPNOTSUPP):
            raise NotCephFSError from e
        raise
    try:
        os.pread(fd, DIRSTAT_BUFSIZE, 0)
    except IsADirectoryError:
        return RStatMode.GETXATTR
    return RStatMode.DIRSTAT


def cluster_fsid(fd: int) -> str | None:
    """Return the Ceph cluster fsid of FD's filesystem, or None if not reported."""
    try:
        return os.getxattr(fd, "ceph.cluster_fsid").decode("ascii")
    except OSError as e:
        if e.errno in (errno.ENODATA, errno.EOPNOTSUPP):
            return None
        raise
