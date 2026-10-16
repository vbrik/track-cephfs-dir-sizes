"""Open and inspect ROOT on a live CephFS, then walk it.

Two steps, so a command can act between them: `open_live_root` opens ROOT and
establishes what it is (cluster, filesystem, path, rstat mode); `walk_live`
walks it and builds the capture header. Capture creates its output writer
between the two; sample runs its database pre-check.
"""

import contextlib
import functools
import os
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trackdir import __version__
from trackdir.capture import CaptureHeader
from trackdir.messages import FatalError
from trackdir.model import ROOT, Backend, DirProbe, error_counts, list_above
from trackdir.mount import MountError, identify
from trackdir.probe import LiveBackend
from trackdir.rstats import (
    NotCephFSError,
    RStatMode,
    cluster_fsid,
    detect_mode,
    read_rstats,
)
from trackdir.safeopen import SafeRoot, openat2_supported
from trackdir.walker import DirLimitExceeded, walk

# Probes stay in memory until the end: ~0.6 KB each (measured), so ~0.6 GB.
DEFAULT_MAX_DIRS = 1_000_000
DEFAULT_JOBS = 16  # concurrent probes; each mostly waits on MDS round trips

MSG_NO_OPENAT2_AS_ROOT = (
    "this kernel does not provide openat2 (Linux 5.6+), which trackdir needs "
    "to walk user-writable trees safely as root. Run it on a newer kernel."
)
MSG_NO_OPENAT2 = (
    "this kernel does not provide openat2 (Linux 5.6+), so directories are "
    "opened one path component at a time. Symlinks are still never followed, "
    "but mount points are detected only after they are opened."
)
MSG_NO_DIRSTAT = (
    "ROOT's mount lacks the dirstat option, so each directory costs four MDS "
    "round trips instead of one. Mount with -o dirstat (a separate mount for "
    "trackdir is enough) to cut the MDS load."
)


@dataclass(frozen=True, slots=True)
class LiveRoot:
    """An open ROOT and what is known about it; valid inside open_live_root."""

    safe_root: SafeRoot
    path: str  # resolved
    cluster_fsid: str
    fs_name: str | None  # None: the cluster's default filesystem
    fs_path: str  # ROOT's absolute path within the filesystem
    rstat_mode: RStatMode
    openat2: bool


@dataclass(frozen=True, slots=True)
class LiveWalk:
    """A finished walk of ROOT."""

    header: CaptureHeader  # not anonymized
    probes: list[DirProbe]
    elapsed: float  # seconds spent walking


@contextlib.contextmanager
def open_live_root(path: Path, warn: Callable[[str], None]) -> Iterator[LiveRoot]:
    """Open ROOT at PATH, inspect it, and yield it; close it on exit.

    Calls WARN once per degraded mode (no openat2, no dirstat) before
    yielding. Raises FatalError if ROOT cannot be opened, is not a CephFS
    directory whose cluster fsid and mount can be identified, or is on a mount
    that names a different cluster fsid than ROOT reports.
    """
    use_openat2 = openat2_supported()
    if not use_openat2:
        if os.geteuid() == 0:
            raise FatalError(MSG_NO_OPENAT2_AS_ROOT)
        warn(MSG_NO_OPENAT2)
    with _open_root(path, use_openat2) as safe_root:
        resolved = safe_root.resolved_path()
        live = _inspect(safe_root, path, resolved, use_openat2)
        if live.rstat_mode is RStatMode.GETXATTR:
            warn(MSG_NO_DIRSTAT)
        yield live


def walk_within(
    backend: Backend, *, max_depth: int, max_dirs: int, jobs: int
) -> list[DirProbe]:
    """Walk BACKEND down to MAX_DEPTH and return the probes in tree order.

    Raises FatalError if there are more than MAX_DIRS directories.
    """
    try:
        return walk(backend, list_above(max_depth), max_dirs=max_dirs, jobs=jobs)
    except DirLimitExceeded as e:
        raise FatalError(
            f"ROOT has more than {e.limit} directories within depth "
            f"{max_depth}. Raise --max-dirs if memory allows, or lower "
            "--max-depth."
        ) from e


def walk_live(live: LiveRoot, *, max_depth: int, max_dirs: int, jobs: int) -> LiveWalk:
    """Walk LIVE.safe_root and build the header (anonymized=False)."""
    backend = LiveBackend(
        live.safe_root, functools.partial(read_rstats, mode=live.rstat_mode)
    )
    started = datetime.now(UTC)
    clock = time.monotonic()
    probes = walk_within(backend, max_depth=max_depth, max_dirs=max_dirs, jobs=jobs)
    elapsed = time.monotonic() - clock
    header = CaptureHeader(
        root=live.path,
        max_depth=max_depth,
        cluster_fsid=live.cluster_fsid,
        fs_name=live.fs_name,
        fs_path=live.fs_path,
        rstat_mode=live.rstat_mode.value,
        openat2=live.openat2,
        jobs=jobs,
        started=started.isoformat(),
        finished=datetime.now(UTC).isoformat(),
        euid=os.geteuid(),
        host=socket.gethostname(),
        kernel=os.uname().release,
        trackdir_version=__version__,
        dirs=len(probes),
        errors=error_counts(probes).total(),
        anonymized=False,
    )
    return LiveWalk(header, probes, elapsed)


def _open_root(path: Path, use_openat2: bool) -> SafeRoot:
    try:
        return SafeRoot(path, use_openat2=use_openat2)
    except OSError as e:
        raise FatalError(
            f"cannot open ROOT {path}: {e.strerror}. ROOT must be a directory, "
            "not a symlink."
        ) from e


def _inspect(
    safe_root: SafeRoot, path: Path, resolved: str, use_openat2: bool
) -> LiveRoot:
    """Return the identity of the filesystem and directory of SAFE_ROOT.

    PATH is ROOT as given, for messages; RESOLVED is its resolved path.
    """
    try:
        fd = safe_root.open(ROOT, os.O_RDONLY)
        try:
            mode = detect_mode(fd)
            fsid = cluster_fsid(fd)
            if fsid is None:
                raise FatalError(
                    f"ROOT {path} reports no Ceph cluster fsid "
                    "(ceph.cluster_fsid), so trackdir cannot tell which "
                    "cluster it belongs to."
                )
            mount = identify(fd, resolved)
        finally:
            os.close(fd)
    except NotCephFSError:
        raise FatalError(
            f"ROOT {path} is not on CephFS (it has no ceph.dir.rctime)."
        ) from None
    except MountError as e:
        raise FatalError(
            f"cannot identify the CephFS filesystem of ROOT {path}: {e}. "
            "trackdir needs the CephFS kernel client."
        ) from e
    except OSError as e:
        raise FatalError(f"cannot read ROOT {path}: {e.strerror}.") from e
    if mount.fsid is not None and mount.fsid != fsid:
        raise FatalError(
            f"ROOT {path} reports cluster fsid {fsid}, but its mount names "
            f"{mount.fsid}. Check the mount."
        )
    return LiveRoot(
        safe_root=safe_root,
        path=resolved,
        cluster_fsid=fsid,
        fs_name=mount.fs_name,
        fs_path=mount.fs_path,
        rstat_mode=mode,
        openat2=use_openat2,
    )
