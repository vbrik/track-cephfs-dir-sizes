import errno

import pytest

from trackdir.model import (
    ROOT,
    DirProbe,
    ProbeError,
    Stage,
    check_path,
    errno_name,
    error_counts,
    join,
    list_above,
    path_depth,
    split,
    tree_key,
)


def test_path_depth_counts_components_below_root():
    assert path_depth(ROOT) == 0
    assert path_depth(b"a") == 1
    assert path_depth(b"a/b/c") == 3
    assert DirProbe(b"a/b").depth == 2


def test_join_and_split_are_inverse():
    assert join(ROOT, b"a") == b"a"
    assert join(b"a", b"b") == b"a/b"
    assert split(b"a") == (ROOT, b"a")
    assert split(b"a/b/c") == (b"a/b", b"c")


def test_tree_key_puts_root_first_and_subtrees_right_after_parents():
    # "!" and "-" sort before "." in ASCII; splitting b"." would misplace ROOT.
    paths = [b"b", b"a-c", b"a/b", ROOT, b"-x", b"a", b"!"]
    expected = [ROOT, b"!", b"-x", b"a", b"a/b", b"a-c", b"b"]
    assert sorted(paths, key=tree_key) == expected


@pytest.mark.parametrize("path", [ROOT, b"a", b"a/b", b"caf\xe9", b"a b/.hidden"])
def test_check_path_accepts_normalized_paths(path):
    check_path(path)


@pytest.mark.parametrize(
    "path", [b"", b"/a", b"a/", b"a//b", b"./a", b"a/.", b"..", b"a/../b"]
)
def test_check_path_rejects_other_paths(path):
    with pytest.raises(ValueError):
        check_path(path)


def test_errno_name():
    assert errno_name(errno.EACCES) == "EACCES"
    assert errno_name(99999) == "errno99999"
    assert errno_name(None) == "EUNKNOWN"


def test_list_above():
    should_list = list_above(1)
    assert should_list(DirProbe(ROOT))
    assert not should_list(DirProbe(b"a"))


def test_error_counts():
    probes = [
        DirProbe(ROOT),
        DirProbe(b"a", error=ProbeError(Stage.OPEN, "EACCES")),
        DirProbe(b"b", error=ProbeError(Stage.OPEN, "EACCES")),
        DirProbe(b"c", error=ProbeError(Stage.READDIR, "EIO")),
    ]
    assert error_counts(probes) == {("open", "EACCES"): 2, ("readdir", "EIO"): 1}
    assert not error_counts([DirProbe(ROOT)])
