import logging
from pathlib import Path

import pytest

from trackdir import live
from trackdir.config import DaemonConfig, Interval, TreeConfig, load, parse_interval
from trackdir.messages import FatalError
from trackdir.model import MAX_DEPTH_LIMIT, MAX_DIRS_LIMIT

MINIMAL = """\
interval = "6h"
db = "postgresql://x/y"

[[tree]]
root = "/mnt/cephfs/a"
max_depth = 2
"""

TWO_TREES = """\
interval = "6h"
db = "postgresql://x/y"

[[tree]]
root = "/mnt/cephfs/a"
max_depth = 2

[[tree]]
root = "/mnt/cephfs/b"
{second}
"""


@pytest.fixture(autouse=True)
def no_dsn_in_environment(monkeypatch):
    monkeypatch.delenv("TRACKDIR_DB", raising=False)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "trackdir.toml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("90m", 5400), ("6h", 21600), ("1d", 86400)],
)
def test_parse_interval(text, seconds):
    assert parse_interval(text) == Interval(seconds, text)


@pytest.mark.parametrize("text", ["0h", "6", "1.5h", "6 h", "6H", "", "-1h"])
def test_parse_interval_rejects(text):
    with pytest.raises(ValueError, match="<integer><unit>"):
        parse_interval(text)


def test_parse_interval_caps_at_a_year():
    assert parse_interval("365d").seconds == 365 * 86400
    for text in ("366d", "99999999999d"):
        with pytest.raises(ValueError, match="at most 365d"):
            parse_interval(text)


def test_load_minimal(tmp_path):
    path = write(tmp_path, MINIMAL)
    assert load(path) == DaemonConfig(
        path,
        Interval(21600, "6h"),
        "postgresql://x/y",
        logging.INFO,
        (TreeConfig(Path("/mnt/cephfs/a"), 2),),
    )
    tree = load(path).trees[0]
    assert tree.max_dirs == live.DEFAULT_MAX_DIRS
    assert tree.jobs == live.DEFAULT_JOBS


def test_load_full(tmp_path):
    text = TWO_TREES.format(second="max_depth = 4\nmax_dirs = 1000\njobs = 3")
    path = write(tmp_path, 'log_level = "DEBUG"\n' + text)
    config = load(path)
    assert config.log_level == logging.DEBUG
    assert config.trees == (
        TreeConfig(Path("/mnt/cephfs/a"), 2),
        TreeConfig(Path("/mnt/cephfs/b"), 4, max_dirs=1000, jobs=3),
    )


def test_dsn_from_environment(tmp_path, monkeypatch):
    path = write(tmp_path, MINIMAL.replace('db = "postgresql://x/y"\n', ""))
    with pytest.raises(FatalError) as error:
        load(path)
    assert "db" in str(error.value)
    assert "TRACKDIR_DB" in str(error.value)
    monkeypatch.setenv("TRACKDIR_DB", "postgresql://env/db")
    assert load(path).dsn == "postgresql://env/db"


def test_db_key_overrides_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKDIR_DB", "postgresql://env/db")
    assert load(write(tmp_path, MINIMAL)).dsn == "postgresql://x/y"


def _tree(**keys: str) -> str:
    """Return MINIMAL with tree 1's keys replaced by KEYS (TOML values)."""
    body = "".join(f"{key} = {value}\n" for key, value in keys.items())
    return 'interval = "6h"\ndb = "postgresql://x/y"\n\n[[tree]]\n' + body


REJECTED = [
    ("bad_toml", "interval = \n", ["line"]),
    ("unknown_top", 'intervall = "6h"\n' + MINIMAL, ["intervall"]),
    (
        "unknown_tree_key",
        TWO_TREES.format(second="max_depth = 1\nmax-dirs = 5"),
        ["tree 2", "max-dirs"],
    ),
    ("no_interval", MINIMAL.replace('interval = "6h"\n', ""), ["interval"]),
    ("no_root", _tree(max_depth="2"), ["tree 1", "root"]),
    ("no_max_depth", _tree(root='"/a"'), ["max_depth"]),
    (
        "tree_table",
        'interval = "6h"\ndb = "x"\n[tree]\nroot = "/a"\nmax_depth = 1\n',
        ["tree"],
    ),
    ("tree_empty", 'interval = "6h"\ndb = "x"\ntree = []\n', ["tree"]),
    ("no_tree", 'interval = "6h"\ndb = "x"\n', ["tree"]),
    ("depth_string", _tree(root='"/a"', max_depth='"4"'), ["max_depth", "integer"]),
    ("depth_bool", _tree(root='"/a"', max_depth="true"), ["max_depth", "integer"]),
    (
        "depth_range",
        _tree(root='"/a"', max_depth=str(MAX_DEPTH_LIMIT + 1)),
        ["max_depth", "at most"],
    ),
    (
        "dirs_range",
        _tree(root='"/a"', max_depth="1", max_dirs=str(MAX_DIRS_LIMIT + 1)),
        ["max_dirs", "at most"],
    ),
    ("jobs_zero", _tree(root='"/a"', max_depth="1", jobs="0"), ["jobs", "at least"]),
    ("root_type", _tree(root="1", max_depth="1"), ["root", "string"]),
    ("relative_root", _tree(root='"a/b"', max_depth="1"), ["root", "absolute"]),
    ("nul_root", _tree(root='"/a\\u0000b"', max_depth="1"), ["root", "NUL"]),
    ("huge_interval", MINIMAL.replace('"6h"', '"99999999999d"'), ["at most 365d"]),
    (
        "bad_interval",
        MINIMAL.replace('"6h"', '"6"'),
        ["interval", "<integer><unit>"],
    ),
    ("interval_int", MINIMAL.replace('"6h"', "6"), ["interval", "string"]),
    (
        "bad_level",
        'log_level = "LOUD"\n' + MINIMAL,
        ["log_level", "DEBUG, INFO, WARNING, ERROR"],
    ),
    ("db_type", MINIMAL.replace('"postgresql://x/y"', "5"), ["db", "string"]),
]


@pytest.mark.parametrize(
    ("text", "words"),
    [(text, words) for _, text, words in REJECTED],
    ids=[case for case, _, _ in REJECTED],
)
def test_load_rejects(tmp_path, text, words):
    path = write(tmp_path, text)
    with pytest.raises(FatalError) as error:
        load(path)
    message = str(error.value)
    assert str(path) in message
    for word in words:
        assert word in message


def test_load_unreadable(tmp_path):
    for path in (tmp_path, tmp_path / "missing.toml"):
        with pytest.raises(FatalError) as error:
            load(path)
        assert str(error.value).startswith(f"cannot read the config {path}: ")
