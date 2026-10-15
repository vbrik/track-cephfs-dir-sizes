"""Probe one directory of a live filesystem: owner, rstats, subdirectory names."""

import errno
import os
from collections.abc import Callable
from dataclasses import replace

from trackdir.model import DirProbe, ProbeError, RStats, ShouldList, Stage, errno_name
from trackdir.rstats import RStatsError
from trackdir.safeopen import SafeRoot

# CephFS and most local filesystems report a directory's nlink as 2 + subdirs,
# so 2 means no subdirectories; btrfs reports 1, which falls through to readdir.
NLINK_NO_SUBDIRS = 2


class LiveBackend:
    """Backend over a real filesystem; thread-safe."""

    def __init__(self, root: SafeRoot, read_rstats: Callable[[int], RStats]) -> None:
        """Open directories beneath ROOT; READ_RSTATS(fd) may raise RStatsError."""
        self._root = root
        self._read_rstats = read_rstats

    def probe(self, path: bytes, should_list: ShouldList) -> DirProbe:
        """Probe PATH; see the Backend protocol.

        At most one error is recorded: the first, except that a readdir error
        replaces an rstats error, because the stage tells readers whether the
        directory was listed.
        """
        try:
            fd = self._root.open(path, os.O_RDONLY)
        except OSError as e:
            if e.errno == errno.EACCES:
                return self._probe_unreadable(path)
            return DirProbe(path, error=ProbeError(Stage.OPEN, errno_name(e.errno)))
        try:
            return self._probe_open(path, fd, should_list)
        finally:
            os.close(fd)

    def _probe_unreadable(self, path: bytes) -> DirProbe:
        """Report EACCES plus the owner, which an O_PATH fd still reveals."""
        unreadable = DirProbe(path, error=ProbeError(Stage.OPEN, "EACCES"))
        try:
            fd = self._root.open(path, os.O_PATH)
        except OSError:
            return unreadable
        try:
            st = os.fstat(fd)
        except OSError:
            return unreadable
        finally:
            os.close(fd)
        return _with_stat(unreadable, st)

    def _probe_open(self, path: bytes, fd: int, should_list: ShouldList) -> DirProbe:
        try:
            st = os.fstat(fd)
        except OSError as e:
            return DirProbe(path, error=ProbeError(Stage.STAT, errno_name(e.errno)))
        probe = _with_stat(DirProbe(path), st)
        try:
            rs = self._read_rstats(fd)
        except RStatsError as e:
            probe = replace(probe, error=ProbeError(Stage.RSTATS, e.code))
        else:
            probe = replace(
                probe,
                rctime=rs.rctime,
                rbytes=rs.rbytes,
                rfiles=rs.rfiles,
                rsubdirs=rs.rsubdirs,
            )
        if not should_list(probe):
            return probe
        if probe.nlink == NLINK_NO_SUBDIRS:
            return replace(probe, children=())
        try:
            children = _subdir_names(fd)
        except OSError as e:
            # Partial results are dropped: missing names would look like deletions.
            error = ProbeError(Stage.READDIR, errno_name(e.errno))
            return replace(probe, error=error)
        return replace(probe, children=children)


def _with_stat(probe: DirProbe, st: os.stat_result) -> DirProbe:
    return replace(
        probe,
        ino=st.st_ino,
        mode=st.st_mode,
        uid=st.st_uid,
        gid=st.st_gid,
        nlink=st.st_nlink,
    )


def _subdir_names(fd: int) -> tuple[bytes, ...]:
    """Return the sorted names of FD's subdirectories; symlinks are not followed.

    Sorted because readdir order is arbitrary and probes must be deterministic.
    """
    with os.scandir(fd) as entries:
        return tuple(
            sorted(
                os.fsencode(entry.name)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            )
        )
