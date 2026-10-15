"""Open directories beneath ROOT without following symlinks or leaving its mount.

Production runs as root over user-writable trees, so a user must not be able to
redirect trackdir with a symlink. openat2(2) with RESOLVE_BENEATH |
RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV resolves a whole relative path safely in
one syscall, so work items can be plain paths and no parent fds are held.
CPython has no wrapper, so it is called through ctypes. Without openat2, paths
are opened one component at a time with O_NOFOLLOW; that detects mount
crossings only after the open.
"""

import ctypes
import errno
import os
import stat
from typing import Self

from trackdir.model import ROOT, check_path

RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_SYMLINKS = 0x04  # also blocks procfs magic links
RESOLVE_BENEATH = 0x08
_RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV

# openat2 has the same number on all architectures with the unified syscall table.
_SYS_OPENAT2 = {"x86_64": 437, "aarch64": 437}.get(os.uname().machine)
_AT_FDCWD = -100
_TRANSIENT_RETRIES = 5


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _openat2(dirfd: int, path: bytes, flags: int, resolve: int) -> int:
    """Call openat2(2) and return the new fd.

    Raises OSError; ENOSYS on architectures without a known syscall number.
    """
    if _SYS_OPENAT2 is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    how = _OpenHow(flags, 0, resolve)
    err = 0
    for _ in range(_TRANSIENT_RETRIES):
        fd = _libc.syscall(
            ctypes.c_long(_SYS_OPENAT2),
            ctypes.c_long(dirfd),
            ctypes.c_char_p(path),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
        if fd >= 0:
            return fd
        err = ctypes.get_errno()
        # EAGAIN: a concurrent rename or mount; EINTR: a signal. PEP 475
        # automatic retry does not cover ctypes calls.
        if err not in (errno.EAGAIN, errno.EINTR):
            break
    raise OSError(err, os.strerror(err), path)


def openat2_supported() -> bool:
    """Return whether openat2 is usable here; seccomp filters can hide it (EPERM)."""
    try:
        os.close(_openat2(_AT_FDCWD, b"/", os.O_PATH | os.O_CLOEXEC, 0))
    except OSError as e:
        if e.errno in (errno.ENOSYS, errno.EPERM):
            return False
        raise
    return True


def _open_nofollow(name, flags: int, dir_fd: int | None = None) -> int:
    """os.open NAME as a directory without following a final symlink.

    O_DIRECTORY|O_NOFOLLOW fails on a symlink with ENOTDIR; that case is
    reported as ELOOP, as openat2 does.
    """
    flags |= os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except NotADirectoryError:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            raise NotADirectoryError(
                errno.ENOTDIR, os.strerror(errno.ENOTDIR), name
            ) from None
        if stat.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), name) from None
        raise


class SafeRoot:
    """An open ROOT directory that relative paths are opened beneath.

    Opens never follow symlinks and never leave ROOT's mount. Use as a context
    manager or call close(). Thread-safe: threads share only the ROOT fd.
    """

    def __init__(self, path: str | bytes | os.PathLike, *, use_openat2: bool) -> None:
        """Open directory PATH; its last component must not be a symlink (ELOOP).

        Raises OSError.
        """
        self.fd = _open_nofollow(path, os.O_RDONLY)
        try:
            self.dev = os.fstat(self.fd).st_dev
        except BaseException:
            os.close(self.fd)
            raise
        self.use_openat2 = use_openat2

    def open(self, path: bytes, flags: int) -> int:
        """Open directory PATH (relative to ROOT; ROOT itself is b".").

        FLAGS is os.O_RDONLY or os.O_PATH; O_DIRECTORY and O_CLOEXEC are added.
        Returns the new fd. Raises OSError: ELOOP if any component is a
        symlink, EXDEV if PATH leaves ROOT's mount, ENOTDIR, ENOENT, EACCES and
        so on. Raises ValueError if PATH is not normalized (model.check_path).
        """
        check_path(path)
        if self.use_openat2:
            flags |= os.O_DIRECTORY | os.O_CLOEXEC
            return _openat2(self.fd, path, flags, _RESOLVE)
        return self._open_fallback(path, flags)

    def _open_fallback(self, path: bytes, flags: int) -> int:
        if path == ROOT:
            return _open_nofollow(b".", flags, dir_fd=self.fd)
        *parents, name = path.split(b"/")
        dirfd = self.fd
        try:
            for parent in parents:
                fd = self._open_component(dirfd, parent, os.O_PATH, path)
                if dirfd != self.fd:
                    os.close(dirfd)
                dirfd = fd
            return self._open_component(dirfd, name, flags, path)
        finally:
            if dirfd != self.fd:
                os.close(dirfd)

    def _open_component(self, dirfd: int, name: bytes, flags: int, path: bytes) -> int:
        fd = _open_nofollow(name, flags, dir_fd=dirfd)
        try:
            crossed = os.fstat(fd).st_dev != self.dev
        except BaseException:
            os.close(fd)
            raise
        if crossed:
            os.close(fd)
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), path)
        return fd

    def resolved_path(self) -> str:
        """Return ROOT's absolute path as the kernel resolved it."""
        return os.readlink(f"/proc/self/fd/{self.fd}")

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
