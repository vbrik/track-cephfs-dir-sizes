import io
import os

import pytest

from trackdir import mount
from trackdir.mount import (
    CephMount,
    MountError,
    ceph_mount,
    fd_mount_id,
    identify,
    parse_mountinfo_line,
)

CEPH1 = (
    b"309 41 0:58 / /mnt/ceph1 ro,relatime shared:265 - ceph "
    b"192.0.2.192,192.0.2.191:/ ro,name=admin,secret=<hidden>,"
    b"fsid=11111111-2222-4333-8444-555555555555,dirstat,acl"
)
CEPH2 = (
    b"464 41 0:62 / /mnt/ceph2 ro,relatime shared:275 - ceph "
    b"admin@aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.default=/ "
    b"ro,name=admin,secret=<hidden>,ms_mode=prefer-crc,dirstat,acl,"
    b"mon_addr=192.0.2.219:3300/192.0.2.220:3300"
)


def line(
    *,
    root: bytes = b"/",
    mount_point: bytes = b"/mnt/x",
    fstype: bytes = b"ceph",
    source: bytes = b"mon1:/",
    options: bytes = b"rw,name=admin",
    optional: bytes = b"shared:1",
) -> bytes:
    """A synthetic mountinfo line."""
    fields = [b"7", b"1", b"0:5", root, mount_point, b"rw,relatime"]
    if optional:
        fields.append(optional)
    return b" ".join([*fields, b"-", fstype, source, options])


def mount_of(mountinfo_line: bytes, resolved_root: str) -> CephMount:
    return ceph_mount(parse_mountinfo_line(mountinfo_line), resolved_root)


def test_parse_ceph1_line():
    entry = parse_mountinfo_line(CEPH1)
    assert entry.mount_id == 309
    assert entry.root == "/"
    assert entry.mount_point == "/mnt/ceph1"
    assert entry.fstype == "ceph"
    assert "dirstat" in entry.super_options


def test_old_syntax_without_fs_option_is_default_fs():
    assert mount_of(CEPH1, "/mnt/ceph1/proj/sub") == CephMount(
        "11111111-2222-4333-8444-555555555555", None, "/proj/sub"
    )


def test_new_syntax():
    assert mount_of(CEPH2, "/mnt/ceph2") == CephMount(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "default", "/"
    )


def test_new_syntax_without_fsid():
    mountinfo = line(source=b"admin@.cephfs=/projects")
    assert mount_of(mountinfo, "/mnt/x/a") == CephMount(None, "cephfs", "/projects/a")


def test_new_syntax_fsid_option_fills_missing_fsid():
    mountinfo = line(source=b"admin@.cephfs=/", options=b"rw,fsid=abc-123")
    assert mount_of(mountinfo, "/mnt/x") == CephMount("abc-123", "cephfs", "/")


@pytest.mark.parametrize("option", ["fs", "mds_namespace"])
def test_old_syntax_fs_options(option):
    mountinfo = line(options=f"rw,name=admin,{option}=other".encode())
    mount = mount_of(mountinfo, "/mnt/x")
    assert mount.fs_name == "other"
    assert mount.fsid is None


def test_old_syntax_submount():
    mountinfo = line(source=b"mon1:6789:/projects")
    assert mount_of(mountinfo, "/mnt/x/a/b").fs_path == "/projects/a/b"


def test_bind_mount_root_field():
    mountinfo = line(root=b"/sub/dir", mount_point=b"/mnt/b", source=b"mon1:/")
    assert mount_of(mountinfo, "/mnt/b/c").fs_path == "/sub/dir/c"


def test_octal_escapes():
    mountinfo = line(mount_point=b"/mnt/my\\040ceph")
    assert parse_mountinfo_line(mountinfo).mount_point == "/mnt/my ceph"
    assert mount_of(mountinfo, "/mnt/my ceph/x").fs_path == "/x"


def test_ipv6_monitors():
    mountinfo = line(source=b"[2001:db8::1]:6789,[2001:db8::2]:6789:/vol")
    assert mount_of(mountinfo, "/mnt/x").fs_path == "/vol"


def test_mounted_at_slash():
    mountinfo = line(mount_point=b"/", source=b"mon1:/")
    assert mount_of(mountinfo, "/a").fs_path == "/a"


def test_submount_with_trailing_slash_and_bind_root_has_no_double_slash():
    mountinfo = line(root=b"/b/", source=b"mon1:/p/")
    assert mount_of(mountinfo, "/mnt/x").fs_path == "/p/b"


@pytest.mark.parametrize(
    "mountinfo",
    [
        line(source=b"mon1:/a/../b"),
        line(source=b"admin@.cephfs=/.."),
        line(root=b"/a/.."),
    ],
    ids=["old-syntax-subpath", "new-syntax-subpath", "root-field"],
)
def test_dot_dot_in_mount_paths_is_rejected(mountinfo):
    with pytest.raises(MountError, match=r"\.\."):
        mount_of(mountinfo, "/mnt/x/c")


def test_optional_fields_vary():
    none = parse_mountinfo_line(line(optional=b""))
    two = parse_mountinfo_line(line(optional=b"shared:1 master:2"))
    assert (none.fstype, none.source) == (two.fstype, two.source) == ("ceph", "mon1:/")


def test_not_ceph_is_rejected():
    mountinfo = line(fstype=b"fuse.ceph-fuse")
    with pytest.raises(MountError, match=r"fuse\.ceph-fuse"):
        mount_of(mountinfo, "/mnt/x")


def test_root_outside_mount_point_is_rejected():
    with pytest.raises(MountError):
        mount_of(line(), "/mnt/other")


def test_root_in_sibling_with_common_prefix_is_rejected():
    with pytest.raises(MountError):
        mount_of(line(), "/mnt/xy")


def test_unparsable_source_is_rejected():
    with pytest.raises(MountError):
        mount_of(line(source=b"nonsense"), "/mnt/x")


def test_malformed_line_is_rejected():
    with pytest.raises(MountError):
        parse_mountinfo_line(b"1 2 3")


def test_line_without_separator_is_rejected():
    with pytest.raises(MountError):
        parse_mountinfo_line(b"1 2 0:5 / /mnt/x rw shared:1 ceph mon1:/ rw")


def test_non_numeric_mount_id_is_rejected():
    with pytest.raises(MountError):
        parse_mountinfo_line(line().replace(b"7 ", b"x ", 1))


def test_fd_mount_id_matches_mountinfo(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mount_id = fd_mount_id(fd)
    finally:
        os.close(fd)
    with open("/proc/self/mountinfo", "rb") as f:
        ids = {int(ln.split(b" ", 1)[0]) for ln in f.read().split(b"\n") if ln}
    assert mount_id in ids


def test_identify_local_dir_is_not_ceph(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MountError):
            identify(fd, str(tmp_path))
    finally:
        os.close(fd)


def test_identify_mount_point_with_carriage_return(monkeypatch):
    # The kernel escapes space, tab, newline and backslash, but not \r.
    mountinfo = b"\n".join(
        [line().replace(b"7 ", b"6 ", 1), line(mount_point=b"/mnt/a\rb"), b""]
    )
    monkeypatch.setattr(mount, "fd_mount_id", lambda fd: 7)
    monkeypatch.setattr(
        mount, "open", lambda path, mode: io.BytesIO(mountinfo), raising=False
    )
    assert identify(3, "/mnt/a\rb/c") == CephMount(None, None, "/c")
