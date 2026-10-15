"""Types and path helpers shared by filesystem access, the walker and captures.

Paths are bytes relative to ROOT, components joined by b"/"; ROOT itself is b".".
"""

import errno
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

ROOT = b"."

# Error codes that are not errno names.
DIRSTAT_STALE = "DIRSTAT_STALE"  # dirstat text older than the getxattr before it
PARSE = "PARSE"  # an rstat value had an unexpected format


class Stage(StrEnum):
    """Step of a probe that failed."""

    OPEN = "open"
    STAT = "stat"
    RSTATS = "rstats"
    READDIR = "readdir"


@dataclass(frozen=True, slots=True)
class ProbeError:
    """The error recorded for a directory: the failed step and an error code."""

    stage: Stage
    code: str  # errno name such as "EACCES", or DIRSTAT_STALE, PARSE


@dataclass(frozen=True, slots=True)
class RStats:
    """CephFS recursive statistics of one directory's subtree."""

    rctime: int  # latest ctime in the subtree, nanoseconds since the epoch
    rbytes: int
    rfiles: int
    rsubdirs: int  # counts the directory itself


@dataclass(frozen=True, slots=True)
class DirProbe:
    """What probing one directory found; fields are None when unknown.

    children holds subdirectory names, or None if the directory was not listed.
    """

    path: bytes
    ino: int | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    nlink: int | None = None
    rctime: int | None = None
    rbytes: int | None = None
    rfiles: int | None = None
    rsubdirs: int | None = None
    children: tuple[bytes, ...] | None = None
    error: ProbeError | None = None

    @property
    def depth(self) -> int:
        return path_depth(self.path)


# Decides, from a probe without children, whether to list the directory.
type ShouldList = Callable[[DirProbe], bool]


class Backend(Protocol):
    """Source of directory probes: a live filesystem, a capture, or a test fake."""

    def probe(self, path: bytes, should_list: ShouldList) -> DirProbe:
        """Probe PATH and list its subdirectories if should_list accepts the probe.

        Problems with the directory are reported in DirProbe.error, not raised.
        """
        ...


def check_path(path: bytes) -> None:
    """Raise ValueError unless PATH is ROOT or has no "", "." or ".." components."""
    if path != ROOT and any(c in (b"", b".", b"..") for c in path.split(b"/")):
        raise ValueError(f"not a normalized path below ROOT: {path!r}")


def path_depth(path: bytes) -> int:
    """Return the number of components in PATH; ROOT is depth 0."""
    return 0 if path == ROOT else path.count(b"/") + 1


def tree_key(path: bytes) -> tuple[bytes, ...]:
    """Sort key that puts ROOT first and each directory right before its subtree."""
    return () if path == ROOT else tuple(path.split(b"/"))


def join(parent: bytes, name: bytes) -> bytes:
    """Return the path of NAME inside directory PARENT."""
    return name if parent == ROOT else parent + b"/" + name


def split(path: bytes) -> tuple[bytes, bytes]:
    """Return (parent path, name) of PATH, which must not be ROOT."""
    parent, sep, name = path.rpartition(b"/")
    return (parent if sep else ROOT), name


def errno_name(err: int | None) -> str:
    """Return the symbolic name of errno ERR, e.g. "EACCES"."""
    if err is None:
        return "EUNKNOWN"
    return errno.errorcode.get(err, f"errno{err}")
