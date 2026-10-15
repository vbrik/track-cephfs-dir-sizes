import errno
import os

import pytest

from trackdir.model import ROOT
from trackdir.safeopen import SafeRoot, openat2_supported


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "f").touch()
    (tmp_path / "l").symlink_to("d")
    (tmp_path / "d" / "lsub").symlink_to("sub")
    return tmp_path


def ino_of(root: SafeRoot, path: bytes, flags: int = os.O_RDONLY) -> int:
    fd = root.open(path, flags)
    try:
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


def open_errno(root: SafeRoot, path: bytes, flags: int = os.O_RDONLY) -> int:
    with pytest.raises(OSError) as exc:
        os.close(root.open(path, flags))
    return exc.value.errno


def test_opens_root_and_nested_dirs(tree, use_openat2):
    sub_ino = (tree / "d" / "sub").stat().st_ino
    with SafeRoot(tree, use_openat2=use_openat2) as root:
        assert ino_of(root, ROOT) == tree.stat().st_ino
        assert ino_of(root, b"d/sub") == sub_ino
        assert ino_of(root, b"d/sub", os.O_PATH) == sub_ino


@pytest.mark.parametrize("path", [b"l", b"l/sub", b"d/lsub"])
@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_PATH])
def test_symlink_anywhere_in_path_gives_eloop(tree, use_openat2, path, flags):
    with SafeRoot(tree, use_openat2=use_openat2) as root:
        assert open_errno(root, path, flags) == errno.ELOOP


def test_file_gives_enotdir_and_missing_gives_enoent(tree, use_openat2):
    with SafeRoot(tree, use_openat2=use_openat2) as root:
        assert open_errno(root, b"f") == errno.ENOTDIR
        assert open_errno(root, b"nope") == errno.ENOENT


def test_mount_crossing_gives_exdev(use_openat2):
    if not os.path.ismount("/proc"):
        pytest.skip("/proc is not a mount point here")
    with SafeRoot("/", use_openat2=use_openat2) as root:
        assert open_errno(root, b"proc") == errno.EXDEV


@pytest.mark.parametrize(
    "path", [b"", b"/d", b"d/", b"./d", b"d/..", b"..", b"d/./sub"]
)
def test_rejects_paths_that_are_not_normalized(tree, use_openat2, path):
    with SafeRoot(tree, use_openat2=use_openat2) as root, pytest.raises(ValueError):
        root.open(path, os.O_RDONLY)


def test_root_must_not_be_a_symlink(tree):
    with pytest.raises(OSError) as exc:
        SafeRoot(tree / "l", use_openat2=False)
    assert exc.value.errno == errno.ELOOP


def test_resolved_path(tree, use_openat2):
    with SafeRoot(tree, use_openat2=use_openat2) as root:
        assert root.resolved_path() == str(tree.resolve())


def test_openat2_supported_returns_bool():
    assert isinstance(openat2_supported(), bool)
