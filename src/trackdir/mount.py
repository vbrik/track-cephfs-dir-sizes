"""Identify the CephFS filesystem and directory that ROOT lives in.

`ceph.cluster_fsid` names the cluster, not the filesystem (a cluster can hold
several) nor the mounted subdirectory. The mount entry in
/proc/self/mountinfo carries both. The pure parsing is here, plus a thin
function that finds the entry of a directory fd.
"""

import os
import posixpath
import re
from dataclasses import dataclass

_OCTAL = re.compile(rb"\\([0-7]{3})")
# New mount syntax: [user@]FSID.FSNAME=SUBPATH
_NEW_SOURCE = re.compile(
    r"(?:[^@]*@)?(?P<fsid>[0-9a-f-]*)\.(?P<name>[^=]+)=(?P<path>/.*)"
)
_FIXED_FIELDS = 6  # up to and including the mount point's per-mount options


@dataclass(frozen=True, slots=True)
class MountEntry:
    """A /proc/self/mountinfo line; strings are unescaped and fsdecoded."""

    mount_id: int
    root: str  # field 4: the mount's root within its superblock
    mount_point: str  # field 5
    fstype: str
    source: str
    super_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CephMount:
    """Where a directory lives within a CephFS filesystem."""

    fsid: str | None  # the cluster fsid, if the mount names it
    fs_name: str | None  # None: the cluster's default filesystem
    fs_path: str  # absolute and normalized; "/" is the filesystem root


class MountError(Exception):
    """ROOT's mount is not an identifiable CephFS kernel mount.

    The message is a lowercase clause without a final period, so callers can
    embed it in a sentence.
    """


def _unescape(field: bytes) -> str:
    """Decode mountinfo's octal escapes (`\\040` is a space), then fsdecode."""
    return os.fsdecode(_OCTAL.sub(lambda m: bytes([int(m[1], 8)]), field))


def parse_mountinfo_line(line: bytes) -> MountEntry:
    """Parse one mountinfo line (without its newline).

    Raises MountError if the line is malformed.
    """
    fields = line.split(b" ")
    try:
        sep = fields.index(b"-", _FIXED_FIELDS)
        fstype, source, options = fields[sep + 1 : sep + 4]
        mount_id = int(fields[0])
    except ValueError:  # no separator, too few fields after it, or a bad id
        raise MountError("mountinfo line is malformed") from None
    return MountEntry(
        mount_id=mount_id,
        root=_unescape(fields[3]),
        mount_point=_unescape(fields[4]),
        fstype=_unescape(fstype),
        source=_unescape(source),
        super_options=tuple(_unescape(options).split(",")),
    )


def _option(entry: MountEntry, *names: str) -> str | None:
    """The value of the first super option named in `names`, else None."""
    for option in entry.super_options:
        key, eq, value = option.partition("=")
        if eq and key in names:
            return value
    return None


def ceph_mount(entry: MountEntry, resolved_root: str) -> CephMount:
    """Locate `resolved_root`, a directory under `entry`'s mount point.

    `fs_path` joins the mounted subpath, the entry's root within its
    superblock (non-"/" for bind mounts) and `resolved_root` relative to the
    mount point. Raises MountError if the mount is not a ceph mount with a
    recognizable source, the subpath or root has a ".." component, or
    `resolved_root` is not under the mount point.
    """
    if entry.fstype != "ceph":
        raise MountError(f"filesystem type is {entry.fstype}, not ceph")
    rel = posixpath.relpath(resolved_root, entry.mount_point)
    if rel == ".." or rel.startswith("../"):
        raise MountError(f"directory is not under its mount point {entry.mount_point}")
    if new := _NEW_SOURCE.fullmatch(entry.source):
        fsid, fs_name, subpath = new["fsid"] or None, new["name"], new["path"]
    elif (colon := entry.source.find(":/")) >= 0:
        fsid, subpath = None, entry.source[colon + 1 :]
        fs_name = _option(entry, "fs", "mds_namespace")
    else:
        raise MountError(f"cannot parse ceph mount source {entry.source!r}")
    fsid = fsid or _option(entry, "fsid")
    # Not collapsed lexically: a symlink in the filesystem could make that wrong.
    for path in (subpath, entry.root):
        if ".." in path.split("/"):
            raise MountError(f"mount path {path} has a .. component")
    parts = (
        part
        for path in (subpath, entry.root, rel)
        for part in path.split("/")
        if part not in ("", ".")
    )
    return CephMount(fsid, fs_name, "/" + "/".join(parts))


def fd_mount_id(fd: int) -> int:
    """The mount id of the mount that `fd` was opened on.

    Reads the `mnt_id:` line of /proc/self/fdinfo/<fd>; raises OSError if that
    cannot be read and MountError if it has no such line.
    """
    with open(f"/proc/self/fdinfo/{fd}", "rb") as f:
        for line in f:
            if line.startswith(b"mnt_id:"):
                return int(line.removeprefix(b"mnt_id:"))
    raise MountError("fdinfo does not report a mount id")


def identify(fd: int, resolved_root: str) -> CephMount:
    """Identify the CephFS filesystem and path of the directory open as `fd`.

    `resolved_root` is that directory's resolved path. The mountinfo line is
    chosen by the fd's mount id, which is exact even with mounts stacked on
    one mount point and with bind mounts. Raises MountError (no such line,
    not a recognizable ceph mount) or OSError.
    """
    wanted = str(fd_mount_id(fd)).encode()
    with open("/proc/self/mountinfo", "rb") as f:
        # Not splitlines(): mountinfo does not escape \r in paths.
        for line in f.read().split(b"\n"):
            if line.split(b" ", 1)[0] == wanted:
                return ceph_mount(parse_mountinfo_line(line), resolved_root)
    raise MountError(f"mount id {wanted.decode()} is not in mountinfo")
