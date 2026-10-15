# trackdir capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `trackdir capture`, which records the state of every directory under a CephFS ROOT (down to a max depth) into a gzipped JSON Lines file for offline debugging, development and tests, optionally anonymized.

**Architecture:** A `Backend` protocol separates filesystem access from traversal. `LiveBackend` probes one directory (openat2-safe open, fstat, CephFS rstats, subdir names); `walker.walk()` runs probes on a thread pool and returns them in tree order; `capture.py` writes/loads capture files and replays them as another `Backend`; `anonymize.py` replaces names and ids with HMAC tokens; `cli.py` wires it together.

**Tech Stack:** Python ≥ 3.12 standard library only (ctypes for openat2, concurrent.futures, gzip, json, hmac). pytest and ruff for development (already installed system-wide).

**Spec:** `docs/superpowers/specs/2026-10-15-trackdir-capture-design.md`

## Global Constraints

- `requires-python = ">=3.12"`; no runtime dependencies. Do not use 3.13+ APIs (e.g. `copy.replace`, `warnings.deprecated`); use `dataclasses.replace`.
- Linux only. Command and package name: `trackdir`.
- Never write to `/mnt/ceph1` or `/mnt/ceph2`. They are production clusters, mounted read-only for development; only Task 9 reads them, at the depths and limits it states.
- Do not run `git commit` or `git push`. Each task ends by displaying a proposed commit subject and body. The body must include the design choices and the intentions behind them (AGENTS.md).
- Do not install anything (no `pip install`, no venv changes). Run tests with `pytest` from the repo root; `pyproject.toml` puts `src` on the path.
- Every task ends with `ruff format .` and `ruff check .` clean, and `pytest -q` passing.
- Messages, `--help`, README and docstrings follow `AGENTS.md.d/CLI-WRITING-STYLE-v1.md`: important facts first, one paragraph per message, `ERROR:`/`WARNING:` prefixes, stderr only.
- Paths are `bytes` internally (ROOT is `b"."`); times are integer nanoseconds; error codes are errno names or `DIRSTAT_STALE`/`PARSE`.
- Reading rstats must never move a directory fd's file offset (use `os.pread(fd, n, 0)`, never `os.read`).

## Review Focus

1. Directory names with newline, backslash, double quote, space or non-UTF-8 bytes are captured, written, loaded and replayed byte-exact. → Task 6, `captured` fixture and `test_round_trip_is_exact`.
2. ROOT and `-o` given as relative paths (ROOT with a trailing slash) with the output inside ROOT are refused before any file is created. → Task 8, `test_output_inside_root_is_refused_with_relative_paths`.
3. Ctrl-C (or any exception) during the walk leaves neither the output nor the hidden temp file. → Task 8, `test_interrupt_leaves_nothing`.
4. A missing or unwritable output directory fails before the walk starts. → Task 8, `test_missing_output_directory_fails_before_walking`, `test_unwritable_output_directory_fails_before_walking`.
5. A directory with thousands of files and few subdirectories returns only the subdirectories. → Task 4, `test_many_files_few_subdirs`.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Metadata, console script, ruff and pytest config |
| `.gitignore` | Keeps keys and raw captures out of git |
| `src/trackdir/__init__.py` | `__version__` (single source of truth) |
| `src/trackdir/__main__.py` | `python -m trackdir` |
| `src/trackdir/model.py` | `DirProbe`, `ProbeError`, `RStats`, `Stage`, `Backend`, path helpers, `tree_key`, `errno_name` |
| `src/trackdir/safeopen.py` | `SafeRoot`, openat2 via ctypes, per-component fallback |
| `src/trackdir/rstats.py` | rstat parsing, `read_rstats`, `detect_mode`, `cluster_fsid` |
| `src/trackdir/probe.py` | `LiveBackend` |
| `src/trackdir/walker.py` | `walk`, `DirLimitExceeded` |
| `src/trackdir/capture.py` | `CaptureHeader`, `CaptureWriter`, `write_capture`, `load_capture`, `ReplayBackend` |
| `src/trackdir/anonymize.py` | `Anonymizer`, `anonymize`, `load_key` |
| `src/trackdir/cli.py` | `run_capture`, `render_summary`, `main` |
| `tests/conftest.py` | `use_openat2`, `safe_root` fixtures |
| `tests/test_*.py` | One test module per source module |
| `tests/data/*.jsonl.gz` | Anonymized real captures (Task 9) |
| `README.md`, `docs/terms.md`, `docs/conventions.md` | User docs, nomenclature, conventions |

Deviations from the spec, both simplifications:
- `Backend.probe(path, should_list)` takes no `depth`; it is derived from the path (`DirProbe.depth`), so the two can never disagree.
- `CaptureHeader` lives in `capture.py`, next to its serialization, not in `model.py`.

---

### Task 1: Project scaffolding, model, and nomenclature docs

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md` (stub), `docs/terms.md`, `docs/conventions.md`
- Create: `src/trackdir/__init__.py`, `src/trackdir/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`trackdir.model`): `ROOT: bytes`; `DIRSTAT_STALE`, `PARSE: str`; `class Stage(StrEnum)` with `OPEN`, `STAT`, `RSTATS`, `READDIR`; `ProbeError(stage: Stage, code: str)`; `RStats(rctime: int, rbytes: int, rfiles: int, rsubdirs: int)`; `DirProbe(path: bytes, ino, mode, uid, gid, nlink, rctime, rbytes, rfiles, rsubdirs: int | None = None, children: tuple[bytes, ...] | None = None, error: ProbeError | None = None)` with property `depth: int`; `type ShouldList = Callable[[DirProbe], bool]`; `class Backend(Protocol)` with `probe(self, path: bytes, should_list: ShouldList) -> DirProbe`; `check_path(path: bytes) -> None` (raises `ValueError`); `path_depth(path: bytes) -> int`; `tree_key(path: bytes) -> tuple[bytes, ...]`; `join(parent: bytes, name: bytes) -> bytes`; `split(path: bytes) -> tuple[bytes, bytes]`; `errno_name(err: int | None) -> str`. `trackdir.__version__ = "0.1.0"`.

- [ ] **Step 1: Create the project files**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "trackdir"
dynamic = ["version"]
description = "Track CephFS directory sizes over time using recursive stats (rbytes, rctime)"
readme = "README.md"
license = "MIT"
license-files = ["LICENSE"]
requires-python = ">=3.12"
dependencies = []
keywords = [
    "cephfs",
    "ceph",
    "directory size",
    "disk usage",
    "rbytes",
    "rctime",
    "storage monitoring",
]
classifiers = [
    "Environment :: Console",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
    "Topic :: System :: Filesystems",
    "Topic :: System :: Monitoring",
]

[project.scripts]
trackdir = "trackdir.cli:main"

[tool.setuptools.dynamic]
version = { attr = "trackdir.__version__" }

[tool.ruff]
target-version = "py312"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

`.gitignore`:

```gitignore
__pycache__/
*.egg-info/
/build/
/dist/
.pytest_cache/
.ruff_cache/
# Anonymization keys and raw (not anonymized) captures must never be committed.
*.key
/.keys/
/captures/
```

`README.md` (stub; Task 8 replaces it):

```markdown
# trackdir — track CephFS directory sizes over time

Work in progress. Terms: [docs/terms.md](docs/terms.md). Conventions:
[docs/conventions.md](docs/conventions.md).
```

`src/trackdir/__init__.py`:

```python
"""trackdir: track CephFS directory sizes over time."""

__version__ = "0.1.0"
```

`docs/terms.md`:

```markdown
# Terms

| Term | Meaning |
|---|---|
| ROOT | The directory whose subtree trackdir captures. Paths in captures are relative to it; ROOT itself is `.`. |
| depth | Number of path components below ROOT; ROOT is depth 0. |
| max depth | Deepest level recorded. Directories at max depth are probed but not listed. |
| tracked space | All directories from ROOT down to max depth. |
| probe | Reading one directory: open, fstat, rstats and, if it is listed, its subdirectory names. |
| listed | A directory whose subdirectory names were read. |
| rstats | CephFS recursive statistics, covering a directory's whole subtree: `rbytes`, `rfiles`, `rsubdirs`, `rctime`. |
| rbytes | Total size of the files in the subtree, in bytes. |
| rfiles, rsubdirs | Number of files and directories in the subtree; `rsubdirs` counts the directory itself. |
| rctime | Latest change time (ctime) in the subtree, as integer nanoseconds since the epoch. |
| pinned rctime | An rctime in the future, often `4294967295.000000000`. Because rctime is a maximum, it no longer changes when the subtree does. |
| MDS round trip | One request to the Ceph metadata server. Every `ceph.dir.*` getxattr costs one. |
| dirstat mode | Reading all rstats with one getxattr plus `pread()` on the directory; needs the `dirstat` mount option. |
| getxattr mode | Reading each rstat with its own getxattr; used when the mount lacks `dirstat`. |
| capture | A file with one probe result per directory of the tracked space, for offline use. |
| replay | Serving a capture to the walker instead of a live filesystem. |
| anonymized capture | A capture whose names, uids, gids and cluster fsid are replaced by HMAC tokens. |
| sample | (Planned) A sparse record of the directories that changed since their last known state. |
```

`docs/conventions.md`:

```markdown
# Conventions

- Paths are `bytes` internally, relative to ROOT and joined with `/`; ROOT is
  `b"."`. They become `str` only in JSON, through `os.fsdecode`
  (surrogateescape), so names that are not UTF-8 round-trip exactly.
- Times are integer nanoseconds since the epoch, never floats. Capture
  headers use ISO 8601 UTC strings.
- Error codes are errno names (`EACCES`), or `DIRSTAT_STALE` and `PARSE`.
- Output is in tree order (`model.tree_key`): ROOT first, each directory
  right before its subtree.
- Directories are opened only through `SafeRoot.open`, never by absolute
  path.
- Every `ceph.dir.*` getxattr is an MDS round trip; count them when
  changing how directories are probed.
- Code that reads rstats never moves a directory fd's offset: `pread`, not
  `read`, because readdir on the same fd may follow.
- Messages go to stderr as one wrapped paragraph each, prefixed `ERROR:` or
  `WARNING:`; stdout carries results only.
- Tests need neither Ceph nor root. Real CephFS data enters tests only as
  anonymized captures in `tests/data/`.
```

- [ ] **Step 2: Write the failing tests**

`tests/test_model.py`:

```python
import errno

import pytest

from trackdir.model import (
    ROOT,
    DirProbe,
    check_path,
    errno_name,
    join,
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest -q tests/test_model.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.model'`

- [ ] **Step 4: Write the implementation**

`src/trackdir/model.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -q tests/test_model.py`
Expected: all PASS.

- [ ] **Step 6: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Display the commit proposal (do not commit)**

```
Add trackdir project skeleton and shared model

Start the trackdir package (Python >= 3.12, stdlib only) with the types
shared by filesystem access, the walker and capture files.

- Paths are bytes relative to ROOT (b"."), so names that are not UTF-8
  need no special handling until the JSON boundary.
- tree_key() maps ROOT to () so ROOT sorts first even before names such
  as "-x"; one function is shared by everything that sorts.
- The Backend protocol is the seam between filesystem access and
  traversal, so live, replayed and fake filesystems are interchangeable.
- pytest runs from src without installing anything into a venv.
- docs/terms.md and docs/conventions.md fix the vocabulary up front.
```

---

### Task 2: Safe opening beneath ROOT (`safeopen.py`)

**Files:**
- Create: `src/trackdir/safeopen.py`, `tests/conftest.py`
- Test: `tests/test_safeopen.py`

**Interfaces:**
- Consumes: `trackdir.model.ROOT`, `check_path`.
- Produces (`trackdir.safeopen`): `openat2_supported() -> bool`; `class SafeRoot` with `__init__(self, path: str | bytes | os.PathLike, *, use_openat2: bool)` (raises `OSError`; ELOOP if ROOT is a symlink), attributes `fd: int`, `dev: int`, `use_openat2: bool`, methods `open(self, path: bytes, flags: int) -> int` (FLAGS is `os.O_RDONLY` or `os.O_PATH`; raises `OSError` ELOOP/EXDEV/ENOTDIR/ENOENT/EACCES…, `ValueError` for unnormalized paths), `resolved_path(self) -> str`, `close(self)`, context manager. Test fixtures in `tests/conftest.py`: `use_openat2` (params True/False, ids `openat2`/`fallback`) and `safe_root` (a `SafeRoot` on `tmp_path`).

- [ ] **Step 1: Write the fixtures and failing tests**

`tests/conftest.py`:

```python
import pytest

from trackdir.safeopen import SafeRoot, openat2_supported


@pytest.fixture(params=[True, False], ids=["openat2", "fallback"])
def use_openat2(request) -> bool:
    """Run a test once with openat2 and once with the per-component fallback."""
    if request.param and not openat2_supported():
        pytest.skip("this kernel lacks openat2")
    return request.param


@pytest.fixture
def safe_root(tmp_path, use_openat2):
    with SafeRoot(tmp_path, use_openat2=use_openat2) as root:
        yield root
```

`tests/test_safeopen.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_safeopen.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.safeopen'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/safeopen.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_safeopen.py`
Expected: all PASS (the `openat2` variants are skipped only on kernels older than 5.6).

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Open directories beneath ROOT with openat2

SafeRoot opens relative paths beneath the ROOT fd so that a user-writable
tree cannot redirect a root-run trackdir.

- openat2 with RESOLVE_BENEATH|NO_SYMLINKS|NO_XDEV, called through ctypes
  (CPython has no wrapper), resolves a multi-component path in one
  syscall, so work items can be plain paths and no parent fds are held.
- Without openat2 (the caller decides; root must refuse), paths are
  opened one component at a time with O_NOFOLLOW, and st_dev is compared
  to detect mount crossings.
- The fallback reports symlinks as ELOOP, like openat2, so captures do
  not depend on the mode.
- EAGAIN and EINTR are retried because PEP 475 does not cover ctypes.
```

---

### Task 3: Reading rstats (`rstats.py`)

**Files:**
- Create: `src/trackdir/rstats.py`
- Test: `tests/test_rstats.py`

**Interfaces:**
- Consumes: `trackdir.model.RStats`, `DIRSTAT_STALE`, `PARSE`, `errno_name`.
- Produces (`trackdir.rstats`): `class RStatMode(StrEnum)` with `DIRSTAT = "dirstat"`, `GETXATTR = "getxattr"`; `class RStatsError(Exception)` with attribute `code: str`; `class NotCephFSError(Exception)`; `parse_int(raw: bytes) -> int`; `parse_rctime(raw: bytes) -> int`; `parse_dirstat(text: bytes) -> RStats`; `read_rstats(fd: int, mode: RStatMode) -> RStats` (raises `RStatsError`); `detect_mode(fd: int) -> RStatMode` (raises `NotCephFSError`); `cluster_fsid(fd: int) -> str | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_rstats.py`:

```python
import errno
import os

import pytest

from trackdir.model import DIRSTAT_STALE, PARSE, RStats
from trackdir.rstats import (
    NotCephFSError,
    RStatMode,
    RStatsError,
    cluster_fsid,
    detect_mode,
    parse_dirstat,
    parse_int,
    parse_rctime,
    read_rstats,
)

# The text fs/ceph/dir.c:ceph_read_dir() returns for a directory on a dirstat mount.
KERNEL_DIRSTAT_FORMAT = (
    "entries:   %20d\n"
    " files:    %20d\n"
    " subdirs:  %20d\n"
    "rentries:  %20d\n"
    " rfiles:   %20d\n"
    " rsubdirs: %20d\n"
    "rbytes:    %20d\n"
    "rctime:    %10d.%09d\n"
)
PINNED_NS = 4294967295 * 10**9
XATTRS = {
    "ceph.dir.rctime": b"1791915209.387690940",
    "ceph.dir.rbytes": b"4773841593393",
    "ceph.dir.rfiles": b"3260577",
    "ceph.dir.rsubdirs": b"9068",
}
EXPECTED = RStats(
    rctime=1791915209387690940, rbytes=4773841593393, rfiles=3260577, rsubdirs=9068
)


def dirstat_text(rctime_ns: int) -> bytes:
    sec, nsec = divmod(rctime_ns, 10**9)
    r = EXPECTED
    values = (12, 2, 10, r.rfiles + r.rsubdirs, r.rfiles, r.rsubdirs, r.rbytes)
    return (KERNEL_DIRSTAT_FORMAT % (*values, sec, nsec)).encode()


class FakeCeph:
    """Stands in for os.getxattr and os.pread; an int value is raised as an errno."""

    def __init__(self, xattrs, dirstat):
        self.xattrs = xattrs
        self.dirstat = dirstat
        self.calls = []

    def getxattr(self, fd, name):
        self.calls.append(name)
        return self._result(self.xattrs.get(name, errno.ENODATA))

    def pread(self, fd, size, offset):
        self.calls.append(("pread", offset))
        return self._result(self.dirstat)

    @staticmethod
    def _result(value):
        if isinstance(value, int):
            raise OSError(value, os.strerror(value))
        return value


@pytest.fixture
def fake_ceph(monkeypatch):
    def install(xattrs, dirstat=errno.EISDIR):
        fake = FakeCeph(xattrs, dirstat)
        monkeypatch.setattr(os, "getxattr", fake.getxattr)
        monkeypatch.setattr(os, "pread", fake.pread)
        return fake

    return install


def test_parse_rctime():
    assert parse_rctime(b"1792088576.117716441") == 1792088576117716441
    assert parse_rctime(b"4294967295.000000000") == PINNED_NS


@pytest.mark.parametrize(
    "raw",
    [b"", b"1.5", b"1792088576", b" 1.000000000", b"-1.000000000", b"1.0000000001"],
)
def test_parse_rctime_rejects_malformed(raw):
    with pytest.raises(RStatsError) as exc:
        parse_rctime(raw)
    assert exc.value.code == PARSE


def test_parse_int():
    assert parse_int(b"11587802491317666") == 11587802491317666


@pytest.mark.parametrize("raw", [b"", b"-1", b"1 ", b"0x10", b"1.0"])
def test_parse_int_rejects_malformed(raw):
    with pytest.raises(RStatsError) as exc:
        parse_int(raw)
    assert exc.value.code == PARSE


def test_parse_dirstat_kernel_format():
    assert parse_dirstat(dirstat_text(EXPECTED.rctime)) == EXPECTED


def test_parse_dirstat_missing_field():
    lines = dirstat_text(EXPECTED.rctime).splitlines(keepends=True)
    text = b"".join(line for line in lines if not line.startswith(b"rbytes"))
    with pytest.raises(RStatsError) as exc:
        parse_dirstat(text)
    assert exc.value.code == PARSE


def test_dirstat_mode_is_one_getxattr_then_pread_at_offset_zero(fake_ceph):
    fake = fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime))
    assert read_rstats(3, RStatMode.DIRSTAT) == EXPECTED
    assert fake.calls == ["ceph.dir.rctime", ("pread", 0)]


def test_dirstat_newer_than_the_forced_getxattr_is_accepted(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime + 1))
    assert read_rstats(3, RStatMode.DIRSTAT).rctime == EXPECTED.rctime + 1


def test_dirstat_older_than_the_forced_getxattr_is_stale(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime - 1))
    with pytest.raises(RStatsError) as exc:
        read_rstats(3, RStatMode.DIRSTAT)
    assert exc.value.code == DIRSTAT_STALE


def test_getxattr_mode_reads_rctime_first(fake_ceph):
    fake = fake_ceph(XATTRS)
    assert read_rstats(3, RStatMode.GETXATTR) == EXPECTED
    assert fake.calls == [
        "ceph.dir.rctime",
        "ceph.dir.rbytes",
        "ceph.dir.rfiles",
        "ceph.dir.rsubdirs",
    ]


def test_getxattr_failure_reports_the_errno_name(fake_ceph):
    fake_ceph({**XATTRS, "ceph.dir.rfiles": errno.EIO})
    with pytest.raises(RStatsError) as exc:
        read_rstats(3, RStatMode.GETXATTR)
    assert exc.value.code == "EIO"


def test_detect_mode_with_and_without_dirstat(fake_ceph):
    fake_ceph(XATTRS, dirstat_text(EXPECTED.rctime))
    assert detect_mode(3) is RStatMode.DIRSTAT
    fake_ceph(XATTRS, errno.EISDIR)
    assert detect_mode(3) is RStatMode.GETXATTR


@pytest.mark.parametrize("err", [errno.ENODATA, errno.EOPNOTSUPP])
def test_detect_mode_rejects_other_filesystems(fake_ceph, err):
    fake_ceph({"ceph.dir.rctime": err})
    with pytest.raises(NotCephFSError):
        detect_mode(3)


def test_detect_mode_on_a_real_local_directory(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(NotCephFSError):
            detect_mode(fd)
    finally:
        os.close(fd)


def test_cluster_fsid(fake_ceph):
    fsid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    fake_ceph({"ceph.cluster_fsid": fsid.encode()})
    assert cluster_fsid(3) == fsid
    fake_ceph({})
    assert cluster_fsid(3) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_rstats.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.rstats'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/rstats.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_rstats.py`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Read CephFS rstats in dirstat or getxattr mode

Each ceph.dir.* getxattr is a full MDS round trip (0.5-1.4 ms measured).
With the dirstat mount option one forced getxattr refreshes the client's
copy of all rstats and pread() returns them, cutting MDS requests per
directory from four to one.

- pread at offset 0, never read(): the fd offset is shared with readdir.
- Every dirstat read is checked against the forced rctime
  (DIRSTAT_STALE), because the refresh is observed, not documented,
  kernel behavior.
- getxattr mode reads rctime first, so a change during the reads cannot
  hide behind an up-to-date rctime.
- rctime is integer nanoseconds; a float would lose the nanoseconds.
```

---

### Task 4: Probing one directory (`probe.py`)

**Files:**
- Create: `src/trackdir/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: `trackdir.model` (`DirProbe`, `ProbeError`, `RStats`, `ShouldList`, `Stage`, `errno_name`, `ROOT`); `trackdir.rstats.RStatsError`; `trackdir.safeopen.SafeRoot`; fixtures `safe_root`, `use_openat2` from `tests/conftest.py`.
- Produces (`trackdir.probe`): `NLINK_NO_SUBDIRS = 2`; `class LiveBackend` with `__init__(self, root: SafeRoot, read_rstats: Callable[[int], RStats])` and `probe(self, path: bytes, should_list: ShouldList) -> DirProbe`; module function `_subdir_names(fd: int) -> tuple[bytes, ...]` (tests patch it).

- [ ] **Step 1: Write the failing tests**

`tests/test_probe.py`:

```python
import errno
import os

import pytest

from trackdir import probe as probe_module
from trackdir.model import ROOT, DirProbe, ProbeError, RStats, Stage
from trackdir.probe import LiveBackend
from trackdir.rstats import RStatsError

FAKE_RSTATS = RStats(rctime=1_000_000_000, rbytes=10, rfiles=2, rsubdirs=3)


def fake_read_rstats(fd: int) -> RStats:
    return FAKE_RSTATS


def failing_read_rstats(fd: int) -> RStats:
    raise RStatsError("EIO")


def list_all(probe: DirProbe) -> bool:
    return True


def set_nlink(monkeypatch, nlink: int) -> None:
    """Make os.fstat report NLINK: 2 imitates a CephFS leaf, 1 imitates btrfs."""
    real_fstat = os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        fields = (st.st_mode, st.st_ino, st.st_dev, nlink, st.st_uid, st.st_gid)
        return os.stat_result((*fields, st.st_size, 0, 0, 0))

    monkeypatch.setattr(os, "fstat", fstat)


@pytest.fixture
def backend(safe_root):
    return LiveBackend(safe_root, fake_read_rstats)


def test_reports_stat_rstats_and_only_subdirs(tmp_path, backend):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "file").touch()
    (tmp_path / "link").symlink_to("a")
    probe = backend.probe(ROOT, list_all)
    st = tmp_path.stat()
    assert (probe.ino, probe.mode, probe.uid, probe.gid) == (
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_gid,
    )
    assert (probe.rctime, probe.rbytes, probe.rfiles, probe.rsubdirs) == (
        1_000_000_000,
        10,
        2,
        3,
    )
    assert sorted(probe.children) == [b"a", b"b"]
    assert probe.error is None


def test_should_list_sees_rstats_and_can_refuse(tmp_path, backend):
    (tmp_path / "a").mkdir()
    seen = []

    def refuse(probe):
        seen.append(probe)
        return False

    assert backend.probe(ROOT, refuse).children is None
    assert seen[0].rbytes == 10
    assert seen[0].children is None


def test_rstats_error_is_recorded_and_dir_still_listed(tmp_path, safe_root):
    (tmp_path / "a").mkdir()
    probe = LiveBackend(safe_root, failing_read_rstats).probe(ROOT, list_all)
    assert probe.error == ProbeError(Stage.RSTATS, "EIO")
    assert probe.rbytes is None
    assert probe.children == (b"a",)


def test_readdir_error_replaces_rstats_error(tmp_path, safe_root, monkeypatch):
    (tmp_path / "a").mkdir()

    def broken(fd):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(probe_module, "_subdir_names", broken)
    probe = LiveBackend(safe_root, failing_read_rstats).probe(ROOT, list_all)
    assert probe.error == ProbeError(Stage.READDIR, "EIO")
    assert probe.children is None


def test_symlink_file_and_vanished_dir_give_open_errors(tmp_path, backend):
    (tmp_path / "d").mkdir()
    (tmp_path / "l").symlink_to("d")
    (tmp_path / "f").touch()
    for path, code in [(b"l", "ELOOP"), (b"f", "ENOTDIR"), (b"gone", "ENOENT")]:
        error = ProbeError(Stage.OPEN, code)
        assert backend.probe(path, list_all) == DirProbe(path, error=error)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_unreadable_dir_keeps_owner(tmp_path, backend):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        probe = backend.probe(b"locked", list_all)
    finally:
        locked.chmod(0o700)
    assert probe.error == ProbeError(Stage.OPEN, "EACCES")
    assert probe.uid == os.getuid()
    assert probe.ino == locked.stat().st_ino
    assert probe.rbytes is None
    assert probe.children is None


def test_nlink_two_means_no_subdirs_without_readdir(backend, monkeypatch):
    set_nlink(monkeypatch, 2)
    monkeypatch.setattr(
        probe_module, "_subdir_names", lambda fd: pytest.fail("readdir called")
    )
    assert backend.probe(ROOT, list_all).children == ()


def test_nlink_one_as_on_btrfs_still_lists(tmp_path, backend, monkeypatch):
    (tmp_path / "a").mkdir()
    set_nlink(monkeypatch, 1)
    assert backend.probe(ROOT, list_all).children == (b"a",)


def test_names_that_are_not_utf8(tmp_path, backend):
    name = b"caf\xe9"
    os.mkdir(os.fsencode(tmp_path) + b"/" + name)
    assert backend.probe(ROOT, list_all).children == (name,)
    assert backend.probe(name, list_all).error is None


def test_many_files_few_subdirs(tmp_path, backend):
    for i in range(2000):
        (tmp_path / f"file{i}").touch()
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    assert sorted(backend.probe(ROOT, list_all).children) == [b"x", b"y"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_probe.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.probe'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/probe.py`:

```python
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
    """Return the names of FD's subdirectories; symlinks are not followed."""
    with os.scandir(fd) as entries:
        return tuple(
            os.fsencode(entry.name)
            for entry in entries
            if entry.is_dir(follow_symlinks=False)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_probe.py`
Expected: all PASS (`test_unreadable_dir_keeps_owner` is skipped when running as root).

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Probe one directory: owner, rstats, subdirectories

LiveBackend.probe() opens a directory beneath ROOT and records stat
fields, rstats and, if the caller's should_list() agrees, subdirectory
names. Problems become per-directory errors, never exceptions.

- should_list() runs inside the probe, so the future sampler can decide
  from fresh rstats without opening the directory twice.
- nlink == 2 means no subdirectories (CephFS reports 2 + subdirs), which
  skips readdir of leaf directories that may hold millions of files.
- EACCES (as non-root) still records the owner via an O_PATH fd.
- A readdir error drops partial names and replaces an rstats error, so
  the stored error stage always tells whether the directory was listed.
```

---

### Task 5: Concurrent walker (`walker.py`)

**Files:**
- Create: `src/trackdir/walker.py`
- Test: `tests/test_walker.py`

**Interfaces:**
- Consumes: `trackdir.model` (`ROOT`, `Backend`, `DirProbe`, `ShouldList`, `join`, `tree_key`).
- Produces (`trackdir.walker`): `class DirLimitExceeded(Exception)` with attribute `limit: int`; `walk(backend: Backend, should_list: ShouldList, *, max_dirs: int, jobs: int) -> list[DirProbe]` (tree order).

- [ ] **Step 1: Write the failing tests**

`tests/test_walker.py`:

```python
import random
import time
from dataclasses import replace

import pytest

from trackdir.model import ROOT, DirProbe, join
from trackdir.walker import DirLimitExceeded, walk


class TreeBackend:
    """Backend over {path: [child names]}; JITTER shuffles completion order."""

    def __init__(self, tree, jitter=False):
        self.tree = tree
        self.jitter = jitter

    def probe(self, path, should_list):
        if self.jitter:
            time.sleep(random.random() / 1000)
        probe = DirProbe(path, nlink=2 + len(self.tree[path]))
        if not should_list(probe):
            return probe
        return replace(probe, children=tuple(self.tree[path]))


TREE = {
    ROOT: [b"b", b"a"],
    b"a": [b"y", b"x"],
    b"a/x": [b"deep"],
    b"a/x/deep": [],
    b"a/y": [],
    b"b": [],
}


def above(max_depth):
    return lambda probe: probe.depth < max_depth


def paths(probes):
    return [probe.path for probe in probes]


def wide_tree(fanout, depth):
    tree = {}

    def add(path, level):
        names = [b"%d" % i for i in range(fanout)] if level < depth else []
        tree[path] = names
        for name in names:
            add(join(path, name), level + 1)

    add(ROOT, 0)
    return tree


def test_returns_every_dir_in_tree_order():
    probes = walk(TreeBackend(TREE), above(10), max_dirs=100, jobs=4)
    assert paths(probes) == [ROOT, b"a", b"a/x", b"a/x/deep", b"a/y", b"b"]


def test_dirs_at_max_depth_are_probed_but_not_listed():
    probes = walk(TreeBackend(TREE), above(1), max_dirs=100, jobs=4)
    assert paths(probes) == [ROOT, b"a", b"b"]
    assert [probe.children is None for probe in probes] == [False, True, True]


def test_max_depth_zero_probes_root_only():
    assert paths(walk(TreeBackend(TREE), above(0), max_dirs=100, jobs=4)) == [ROOT]


def test_dir_limit_counts_root_and_allows_exactly_max_dirs():
    assert len(walk(TreeBackend(TREE), above(10), max_dirs=6, jobs=4)) == 6
    with pytest.raises(DirLimitExceeded) as exc:
        walk(TreeBackend(TREE), above(10), max_dirs=5, jobs=4)
    assert exc.value.limit == 5


def test_output_does_not_depend_on_jobs():
    tree = wide_tree(fanout=5, depth=3)  # 1 + 5 + 25 + 125 directories
    one = walk(TreeBackend(tree, jitter=True), above(10), max_dirs=1000, jobs=1)
    many = walk(TreeBackend(tree, jitter=True), above(10), max_dirs=1000, jobs=16)
    assert len(one) == 156
    assert one == many


def test_probe_exception_aborts_the_walk():
    class Broken(TreeBackend):
        def probe(self, path, should_list):
            if path == b"a/x":
                raise RuntimeError("bug")
            return super().probe(path, should_list)

    with pytest.raises(RuntimeError, match="bug"):
        walk(Broken(TREE), above(10), max_dirs=100, jobs=4)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_walker.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.walker'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/walker.py`:

```python
"""Concurrent directory traversal over any Backend.

Probes run on a thread pool: they spend their time in syscalls (MDS round
trips) that release the GIL. Completed probes reach the main thread through a
queue, which costs O(1) per directory; concurrent.futures.wait() would rescan
all pending futures each time.
"""

import queue
from concurrent.futures import Future, ThreadPoolExecutor

from trackdir.model import ROOT, Backend, DirProbe, ShouldList, join, tree_key


class DirLimitExceeded(Exception):
    """The walk would probe more directories than allowed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit} directories")
        self.limit = limit


def walk(
    backend: Backend, should_list: ShouldList, *, max_dirs: int, jobs: int
) -> list[DirProbe]:
    """Probe ROOT and, recursively, the subdirectories of every listed directory.

    Returns the probes in tree order, the same for any JOBS. Raises
    DirLimitExceeded as soon as more than MAX_DIRS directories (ROOT included)
    would be probed; exceptions from backend.probe propagate. On any exception,
    including KeyboardInterrupt, pending probes are cancelled and running ones
    are awaited.
    """
    done: queue.SimpleQueue[Future[DirProbe]] = queue.SimpleQueue()
    probes: list[DirProbe] = []
    submitted = in_flight = 0
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="probe") as pool:

        def submit(path: bytes) -> None:
            nonlocal submitted, in_flight
            submitted += 1
            if submitted > max_dirs:
                raise DirLimitExceeded(max_dirs)
            future = pool.submit(backend.probe, path, should_list)
            future.add_done_callback(done.put)
            in_flight += 1

        try:
            submit(ROOT)
            while in_flight:
                probe = done.get().result()
                in_flight -= 1
                probes.append(probe)
                for name in probe.children or ():
                    submit(join(probe.path, name))
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    probes.sort(key=lambda probe: tree_key(probe.path))
    return probes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_walker.py`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Walk directories concurrently over any Backend

walk() probes ROOT on a thread pool and recursively submits the children
of each listed directory, for live, replayed and fake backends alike.

- Threads, not processes: probes wait on MDS round trips in syscalls
  that release the GIL. Work items are paths, so a process pool could
  replace the thread pool without changing the interface.
- Completions come back through a SimpleQueue (O(1) per directory)
  instead of futures.wait(), which rescans every pending future.
- The directory limit is checked at submit time, so the walk stops
  before memory grows past max_dirs.
- Results are sorted with tree_key, so output is identical for any
  --jobs.
```

---

### Task 6: Capture files and replay (`capture.py`)

**Files:**
- Create: `src/trackdir/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `trackdir.model` (`ROOT`, `DirProbe`, `ProbeError`, `ShouldList`, `Stage`, `check_path`, `split`); `trackdir.probe.LiveBackend`, `trackdir.safeopen.SafeRoot`, `openat2_supported`, `trackdir.walker.walk` (tests only).
- Produces (`trackdir.capture`): `FORMAT = "trackdir-capture"`, `VERSION = 1`; `CaptureHeader(root: str, max_depth: int, cluster_fsid: str | None, rstat_mode: str, openat2: bool, jobs: int, started: str, finished: str, euid: int, host: str | None, kernel: str, trackdir_version: str, dirs: int, errors: int, anonymized: bool)`; `Capture(header: CaptureHeader, probes: dict[bytes, DirProbe])`; `CaptureFormatError`; `is_listed(probe: DirProbe, max_depth: int) -> bool`; `class CaptureWriter` with `__init__(self, out: Path)` (raises `FileExistsError`/`OSError`), `commit(self, header, probes: Iterable[DirProbe]) -> None`, `close()`, context manager; `write_capture(out: Path, header, probes) -> None`; `load_capture(path: Path) -> Capture`; `class ReplayBackend` with `__init__(self, capture: Capture, max_depth: int)` (raises `ValueError`) and `probe(self, path, should_list) -> DirProbe`.

- [ ] **Step 1: Write the failing tests**

`tests/test_capture.py`:

```python
import dataclasses
import gzip
import json
import os
import stat
from dataclasses import replace

import pytest

from trackdir.capture import (
    CaptureFormatError,
    CaptureHeader,
    CaptureWriter,
    ReplayBackend,
    load_capture,
    write_capture,
)
from trackdir.model import ROOT, DirProbe, RStats
from trackdir.probe import LiveBackend
from trackdir.safeopen import SafeRoot, openat2_supported
from trackdir.walker import walk

HEADER = CaptureHeader(
    root="/mnt/cephfs/x",
    max_depth=2,
    cluster_fsid="00000000-0000-0000-0000-000000000000",
    rstat_mode="dirstat",
    openat2=True,
    jobs=4,
    started="2026-10-15T00:00:00+00:00",
    finished="2026-10-15T00:00:01+00:00",
    euid=0,
    host="host",
    kernel="7.2.10",
    trackdir_version="0.1.0",
    dirs=0,
    errors=0,
    anonymized=False,
)
AWKWARD_NAMES = [b"plain", b"caf\xe9", b"new\nline", b"back\\slash", b'quo"te', b"s p"]


def above(max_depth):
    return lambda probe: probe.depth < max_depth


def fake_read_rstats(fd):
    return RStats(rctime=1_792_088_576_117_716_441, rbytes=10, rfiles=2, rsubdirs=3)


@pytest.fixture
def captured(tmp_path):
    """Walk a real tree with awkward names at depth 2; return (probes, capture)."""
    tree = os.fsencode(tmp_path / "tree")
    for name in AWKWARD_NAMES:
        os.makedirs(tree + b"/" + name + b"/child/grandchild")
    with SafeRoot(tree, use_openat2=openat2_supported()) as root:
        backend = LiveBackend(root, fake_read_rstats)
        probes = walk(backend, above(2), max_dirs=100, jobs=4)
    out = tmp_path / "c.jsonl.gz"
    write_capture(out, replace(HEADER, dirs=len(probes)), probes)
    return probes, out


def test_round_trip_is_exact(captured):
    probes, out = captured
    capture = load_capture(out)
    assert list(capture.probes.values()) == probes
    assert capture.header == replace(HEADER, dirs=len(probes))
    assert {p.path for p in probes if p.depth == 1} == set(AWKWARD_NAMES)


def test_replay_walk_reproduces_the_capture(captured):
    probes, out = captured
    backend = ReplayBackend(load_capture(out), 2)
    assert walk(backend, above(2), max_dirs=100, jobs=4) == probes


def test_replay_at_smaller_depth_equals_depth_filtering(captured):
    probes, out = captured
    backend = ReplayBackend(load_capture(out), 1)
    expected = [
        replace(p, children=None) if p.depth == 1 else p for p in probes if p.depth <= 1
    ]
    assert walk(backend, above(1), max_dirs=100, jobs=4) == expected


def test_replay_deeper_than_the_capture_is_rejected(captured):
    with pytest.raises(ValueError, match="depth 2"):
        ReplayBackend(load_capture(captured[1]), 3)


def test_file_is_private_and_deterministic(tmp_path, captured):
    probes, out = captured
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    again = tmp_path / "again.jsonl.gz"
    write_capture(again, replace(HEADER, dirs=len(probes)), probes)
    assert again.read_bytes() == out.read_bytes()


def test_lines_are_ascii_json_with_the_header_first(captured):
    lines = gzip.decompress(captured[1].read_bytes()).splitlines()
    assert all(line.isascii() for line in lines)
    assert json.loads(lines[0])["format"] == "trackdir-capture"
    assert json.loads(lines[1])["path"] == "."


def test_never_overwrites(tmp_path):
    out = tmp_path / "c.jsonl.gz"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        write_capture(out, HEADER, [DirProbe(ROOT)])
    assert out.read_bytes() == b"precious"
    assert [p.name for p in tmp_path.iterdir()] == ["c.jsonl.gz"]


def test_failure_before_commit_leaves_nothing(tmp_path):
    with pytest.raises(RuntimeError), CaptureWriter(tmp_path / "c.jsonl.gz"):
        raise RuntimeError("walk failed")
    assert list(tmp_path.iterdir()) == []


def test_failure_during_commit_leaves_nothing(tmp_path):
    def probes():
        yield DirProbe(ROOT)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        write_capture(tmp_path / "c.jsonl.gz", HEADER, probes())
    assert list(tmp_path.iterdir()) == []


def header_json(**changes):
    fields = dataclasses.asdict(replace(HEADER, **changes))
    return {"format": "trackdir-capture", "version": 1, **fields}


EACCES = {"stage": "open", "code": "EACCES"}


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ([{"format": "other"}], "not a trackdir capture"),
        ([{**header_json(), "version": 2}], "version"),
        ([{**header_json(), "max_depth": "2"}], "bad header"),
        ([header_json(), {"path": "a"}], "first record"),
        ([header_json(), {"path": "."}, {"path": "."}], "duplicate"),
        ([header_json(), {"path": "."}, {"path": "a/b"}], "listed parent"),
        ([header_json(max_depth=0), {"path": "."}, {"path": "a"}], "deeper"),
        (
            [
                header_json(),
                {"path": "."},
                {"path": "a", "error": EACCES},
                {"path": "a/b"},
            ],
            "listed parent",
        ),
        ([header_json(), {"path": "."}, {"path": "../x"}], "bad record"),
        ([header_json(), {"path": ".", "rbytes": "12"}], "bad record"),
    ],
)
def test_malformed_captures_are_rejected(tmp_path, objects, message):
    path = tmp_path / "bad.jsonl.gz"
    lines = b"".join(json.dumps(obj).encode() + b"\n" for obj in objects)
    path.write_bytes(gzip.compress(lines))
    with pytest.raises(CaptureFormatError, match=message):
        load_capture(path)


def test_a_file_that_is_not_gzip_is_rejected(tmp_path):
    path = tmp_path / "plain"
    path.write_bytes(b"plain text")
    with pytest.raises(CaptureFormatError):
        load_capture(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_capture.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.capture'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/capture.py`:

```python
"""Capture files: gzipped JSON Lines of directory probes, for offline use.

Line 1 is a CaptureHeader; each further line is one DirProbe, in tree order,
without children (every listed subdirectory has its own line). Paths are
decoded with surrogateescape and written with ensure_ascii, so names that are
not UTF-8 round-trip exactly and the file stays ASCII.
"""

import dataclasses
import errno
import gzip
import json
import os
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

from trackdir.model import (
    ROOT,
    DirProbe,
    ProbeError,
    ShouldList,
    Stage,
    check_path,
    split,
)

FORMAT = "trackdir-capture"
VERSION = 1
GZIP_LEVEL = 6  # the stdlib default, 9, is several times slower for a few % less
_STAT_FIELDS = (
    "ino",
    "mode",
    "uid",
    "gid",
    "nlink",
    "rctime",
    "rbytes",
    "rfiles",
    "rsubdirs",
)


@dataclass(frozen=True, slots=True)
class CaptureHeader:
    """Metadata on line 1 of a capture file."""

    root: str  # ROOT as the kernel resolved it
    max_depth: int
    cluster_fsid: str | None
    rstat_mode: str  # an RStatMode value
    openat2: bool
    jobs: int
    started: str  # ISO 8601, UTC
    finished: str
    euid: int
    host: str | None
    kernel: str
    trackdir_version: str
    dirs: int
    errors: int  # directories with an error
    anonymized: bool


@dataclass(frozen=True, slots=True)
class Capture:
    """A loaded capture; probes are keyed by path, in tree order, with children."""

    header: CaptureHeader
    probes: dict[bytes, DirProbe]


class CaptureFormatError(Exception):
    """A capture file is malformed or of an unsupported format or version."""


def is_listed(probe: DirProbe, max_depth: int) -> bool:
    """Return whether a capture taken at MAX_DEPTH listed PROBE's directory."""
    if probe.depth >= max_depth:
        return False
    return probe.error is None or probe.error.stage == Stage.RSTATS


class CaptureWriter:
    """Writes one capture file atomically and never overwrites.

    The constructor creates a hidden temp file next to OUT, so an unusable
    output directory fails before a long walk. commit() links it to OUT;
    close() removes it. Without commit(), no output appears.
    """

    def __init__(self, out: Path) -> None:
        """Raises FileExistsError if OUT exists, OSError if the temp file fails."""
        if out.exists() or out.is_symlink():
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(out))
        fd, tmp = tempfile.mkstemp(
            dir=out.parent, prefix=f".{out.name}.", suffix=".tmp"
        )
        self._out = out
        self._tmp = Path(tmp)
        self._file = open(fd, "wb")  # noqa: SIM115 -- closed by close()

    def commit(self, header: CaptureHeader, probes: Iterable[DirProbe]) -> None:
        """Write HEADER and PROBES (in tree order), then publish the file as OUT.

        Raises FileExistsError if OUT appeared meanwhile; other OSErrors propagate.
        """
        # mtime=0 keeps the bytes reproducible for identical content.
        with gzip.GzipFile(
            fileobj=self._file, mode="wb", compresslevel=GZIP_LEVEL, mtime=0
        ) as gz:
            gz.write(_json_line(_header_to_json(header)))
            for probe in probes:
                gz.write(_json_line(_probe_to_json(probe)))
        self._file.flush()
        os.fsync(self._file.fileno())
        os.link(self._tmp, self._out)

    def close(self) -> None:
        self._file.close()
        self._tmp.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def write_capture(out: Path, header: CaptureHeader, probes: Iterable[DirProbe]) -> None:
    """Write a capture file to OUT; see CaptureWriter."""
    with CaptureWriter(out) as writer:
        writer.commit(header, probes)


def load_capture(path: Path) -> Capture:
    """Read and validate a capture file. Raises CaptureFormatError or OSError."""
    try:
        with gzip.open(path, "rb") as f:
            header = _header_from_json(json.loads(f.readline()))
            records = [_probe_from_json(json.loads(line)) for line in f]
    # ValueError covers JSON and UTF-8 decoding; the others, gzip damage.
    except (EOFError, gzip.BadGzipFile, zlib.error, ValueError) as e:
        raise CaptureFormatError(f"{path}: {e}") from e
    return Capture(header, _with_children(records, header.max_depth))


class ReplayBackend:
    """Backend that serves a loaded capture, so walks run without a filesystem."""

    def __init__(self, capture: Capture, max_depth: int) -> None:
        """MAX_DEPTH is the walk's max depth; it must not exceed the capture's."""
        if max_depth > capture.header.max_depth:
            raise ValueError(
                f"cannot replay to depth {max_depth}: the capture was taken at "
                f"depth {capture.header.max_depth}"
            )
        self._probes = capture.probes

    def probe(self, path: bytes, should_list: ShouldList) -> DirProbe:
        """Return PATH's stored probe, with children only if should_list agrees."""
        stored = self._probes[path]
        unlisted = replace(stored, children=None)
        return stored if should_list(unlisted) else unlisted


def _json_line(obj: dict[str, object]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii")


def _header_to_json(header: CaptureHeader) -> dict[str, object]:
    return {"format": FORMAT, "version": VERSION, **dataclasses.asdict(header)}


def _header_from_json(obj: object) -> CaptureHeader:
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise CaptureFormatError("not a trackdir capture")
    if obj.get("version") != VERSION:
        raise CaptureFormatError(
            f"unsupported capture version {obj.get('version')!r}; "
            f"this trackdir reads version {VERSION}"
        )
    fields = {k: v for k, v in obj.items() if k not in ("format", "version")}
    try:
        header = CaptureHeader(**fields)
    except TypeError as e:
        raise CaptureFormatError(f"bad header: {e}") from e
    if type(header.max_depth) is not int or header.max_depth < 0:
        raise CaptureFormatError(f"bad header: max_depth {header.max_depth!r}")
    return header


def _probe_to_json(probe: DirProbe) -> dict[str, object]:
    record: dict[str, object] = {"path": os.fsdecode(probe.path)}
    for name in _STAT_FIELDS:
        value = getattr(probe, name)
        if value is not None:
            record[name] = value
    if probe.error is not None:
        record["error"] = {"stage": str(probe.error.stage), "code": probe.error.code}
    return record


def _probe_from_json(record: object) -> DirProbe:
    try:
        if not isinstance(record, dict):
            raise TypeError("not an object")
        path = os.fsencode(record["path"])
        check_path(path)
        error = record.get("error")
        return DirProbe(
            path=path,
            **{name: _optional_int(record, name) for name in _STAT_FIELDS},
            error=None
            if error is None
            else ProbeError(Stage(error["stage"]), str(error["code"])),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise CaptureFormatError(f"bad record {record!r}: {e}") from e


def _optional_int(record: dict[str, object], name: str) -> int | None:
    value = record.get(name)
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} is not an integer")
    return value


def _with_children(records: list[DirProbe], max_depth: int) -> dict[bytes, DirProbe]:
    """Index RECORDS by path and rebuild children; RECORDS must be in tree order."""
    if not records or records[0].path != ROOT:
        raise CaptureFormatError('the first record must be ROOT (".")')
    probes: dict[bytes, DirProbe] = {}
    children: dict[bytes, list[bytes]] = {}
    for probe in records:
        if probe.path in probes:
            raise CaptureFormatError(f"duplicate path {probe.path!r}")
        if probe.depth > max_depth:
            raise CaptureFormatError(
                f"{probe.path!r} is deeper than max_depth {max_depth}"
            )
        if probe.path != ROOT:
            parent, name = split(probe.path)
            if parent not in children:
                raise CaptureFormatError(f"{probe.path!r} has no listed parent")
            children[parent].append(name)
        probes[probe.path] = probe
        if is_listed(probe, max_depth):
            children[probe.path] = []
    return {
        path: replace(
            probe, children=tuple(children[path]) if path in children else None
        )
        for path, probe in probes.items()
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_capture.py`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Write, load and replay capture files

A capture is gzipped JSON Lines: a header, then one probe per directory
in tree order. ReplayBackend serves a loaded capture to the walker, so
anything built on walk() runs offline on real data.

- One probe result per directory, not raw syscall results: replay stays
  simple, and the probe itself is tested on local directories.
- Children are not stored; the loader rebuilds them from the paths and
  validates that every record has a listed parent.
- Names that are not UTF-8 round-trip via surrogateescape plus
  ensure_ascii; the file stays valid ASCII JSON.
- The writer creates its temp file before the walk (fail early),
  publishes it with a hard link (atomic, never overwrites), and removes
  it on any failure. gzip mtime=0 makes identical captures identical
  bytes.
```

---

### Task 7: Anonymization (`anonymize.py`)

**Files:**
- Create: `src/trackdir/anonymize.py`
- Test: `tests/test_anonymize.py`

**Interfaces:**
- Consumes: `trackdir.capture.CaptureHeader`; `trackdir.model` (`ROOT`, `DirProbe`, `tree_key`).
- Produces (`trackdir.anonymize`): `KEY_MIN_BYTES = 32`; `class AnonymizationError(Exception)`; `load_key(path: Path) -> bytes` (raises `ValueError` if short, `OSError` if unreadable); `class Anonymizer` with `__init__(self, key: bytes)`, `name(self, name: bytes) -> bytes`, `path(self, path: bytes) -> bytes`, `numeric_id(self, n: int) -> int`, `fsid(self, fsid: str) -> str`, `abs_path(self, path: str) -> str`; `anonymize(header: CaptureHeader, probes: Sequence[DirProbe], key: bytes) -> tuple[CaptureHeader, list[DirProbe]]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_anonymize.py`:

```python
import pytest

from trackdir.anonymize import AnonymizationError, Anonymizer, anonymize, load_key
from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, split, tree_key

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
HEADER = CaptureHeader(
    root="/mnt/ceph2/x",
    max_depth=2,
    cluster_fsid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    rstat_mode="dirstat",
    openat2=True,
    jobs=4,
    started="2026-10-15T00:00:00+00:00",
    finished="2026-10-15T00:00:01+00:00",
    euid=1000,
    host="host.example",
    kernel="7.2.10",
    trackdir_version="0.1.0",
    dirs=5,
    errors=1,
    anonymized=False,
)
EACCES = ProbeError(Stage.OPEN, "EACCES")
PROBES = [
    DirProbe(ROOT, ino=1, uid=0, gid=0, rbytes=30, children=(b"alice", b"proj")),
    DirProbe(b"alice", ino=2, uid=1000, gid=1000, rbytes=10, children=(b"2019",)),
    DirProbe(b"alice/2019", ino=3, uid=1000, gid=1000, rbytes=10),
    DirProbe(b"proj", ino=4, uid=2000, gid=1000, rbytes=20, children=(b"2019",)),
    DirProbe(b"proj/2019", ino=5, uid=0, gid=0, error=EACCES),
]


def kept_fields(probe):
    return (probe.ino, probe.mode, probe.nlink, probe.rctime, probe.rbytes, probe.error)


def test_names_ids_and_header_are_replaced():
    header, probes = anonymize(HEADER, PROBES, KEY)
    an = Anonymizer(KEY)
    assert {p.path for p in probes} == {an.path(p.path) for p in PROBES}
    assert not any(b"alice" in p.path or b"proj" in p.path for p in probes)
    assert {p.uid for p in probes} == {0, an.numeric_id(1000), an.numeric_id(2000)}
    assert header.anonymized
    assert header.host is None
    assert header.root.count("/") == 3
    assert "ceph2" not in header.root
    assert len(header.cluster_fsid) == 32
    assert header.cluster_fsid != HEADER.cluster_fsid
    assert header.euid == an.numeric_id(1000)


def test_other_fields_and_tree_shape_are_kept():
    _, probes = anonymize(HEADER, PROBES, KEY)
    assert [p.path for p in probes] == sorted((p.path for p in probes), key=tree_key)
    assert sorted(map(kept_fields, probes), key=repr) == sorted(
        map(kept_fields, PROBES), key=repr
    )
    children_of = {}
    for p in probes:
        if p.path != ROOT:
            parent, name = split(p.path)
            children_of.setdefault(parent, set()).add(name)
    assert children_of.keys() <= {p.path for p in probes}
    for p in probes:
        assert set(p.children or ()) == children_of.get(p.path, set())


def test_repeated_names_stay_repeated():
    an = Anonymizer(KEY)
    assert an.path(b"alice/2019").split(b"/")[1] == an.path(b"proj/2019").split(b"/")[1]
    assert len(an.name(b"alice")) == 16


def test_ids():
    an = Anonymizer(KEY)
    assert an.numeric_id(0) == 0
    assert 1 <= an.numeric_id(1000) <= 2**32 - 2
    assert an.numeric_id(1000) != an.numeric_id(1001)


def test_stable_for_a_key_and_different_across_keys():
    assert anonymize(HEADER, PROBES, KEY) == anonymize(HEADER, PROBES, KEY)
    assert anonymize(HEADER, PROBES, KEY)[1] != anonymize(HEADER, PROBES, OTHER_KEY)[1]


def test_sibling_name_collision_fails(monkeypatch):
    monkeypatch.setattr(Anonymizer, "name", lambda self, name: b"same")
    with pytest.raises(AnonymizationError, match="sibling"):
        anonymize(HEADER, PROBES, KEY)


def test_id_collision_fails(monkeypatch):
    monkeypatch.setattr(Anonymizer, "numeric_id", lambda self, n: 0 if n == 0 else 7)
    with pytest.raises(AnonymizationError, match="ids"):
        anonymize(HEADER, PROBES, KEY)


def test_load_key(tmp_path):
    good = tmp_path / "good.key"
    good.write_bytes(KEY)
    assert load_key(good) == KEY
    short = tmp_path / "short.key"
    short.write_bytes(KEY[:31])
    with pytest.raises(ValueError, match="32"):
        load_key(short)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_anonymize.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.anonymize'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/anonymize.py`:

```python
"""Replace names and ids in a capture with stable keyed tokens (HMAC-SHA256).

Unkeyed hashes of guessable names (users, projects, years) can be reversed by
hashing guesses; the secret key prevents that. The same key always yields the
same tokens, so captures taken with one key line up. Each path component is
tokenized separately, which keeps the tree shape and keeps repeated names
recognizable as repeats.
"""

import hmac
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from trackdir.capture import CaptureHeader
from trackdir.model import ROOT, DirProbe, tree_key

KEY_MIN_BYTES = 32
NAME_TOKEN_HEX = 16  # 64-bit name tokens
FSID_TOKEN_HEX = 32
# Fake ids are 1 .. 2**32 - 2: 0 (root) is kept, and 2**32 - 1 means "no id".
_ID_SPACE = 2**32 - 2


class AnonymizationError(Exception):
    """Two different names or ids would get the same token."""


def load_key(path: Path) -> bytes:
    """Return the key stored in PATH.

    Raises ValueError if it is shorter than KEY_MIN_BYTES, OSError if unreadable.
    """
    key = path.read_bytes()
    if len(key) < KEY_MIN_BYTES:
        raise ValueError(
            f"key file {path} has {len(key)} bytes; it needs at least "
            f"{KEY_MIN_BYTES} random bytes"
        )
    return key


class Anonymizer:
    """Keyed, deterministic replacements for names, ids and fsids."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, domain: bytes, data: bytes) -> bytes:
        return hmac.digest(self._key, domain + b"\0" + data, "sha256")

    def name(self, name: bytes) -> bytes:
        """Return the token for one path component."""
        return self._mac(b"name", name).hex()[:NAME_TOKEN_HEX].encode("ascii")

    def path(self, path: bytes) -> bytes:
        """Return PATH with every component tokenized; ROOT stays ROOT."""
        if path == ROOT:
            return ROOT
        return b"/".join(self.name(part) for part in path.split(b"/"))

    def numeric_id(self, n: int) -> int:
        """Return the fake uid/gid for N; 0 stays 0."""
        if n == 0:
            return 0
        digest = self._mac(b"id", str(n).encode("ascii"))
        return 1 + int.from_bytes(digest[:4]) % _ID_SPACE

    def fsid(self, fsid: str) -> str:
        """Return the token for a cluster fsid."""
        return self._mac(b"fsid", fsid.encode("ascii")).hex()[:FSID_TOKEN_HEX]

    def abs_path(self, path: str) -> str:
        """Return absolute PATH with every component tokenized."""
        parts = [p for p in os.fsencode(path).split(b"/") if p]
        return "/" + "/".join(self.name(p).decode("ascii") for p in parts)


def anonymize(
    header: CaptureHeader, probes: Sequence[DirProbe], key: bytes
) -> tuple[CaptureHeader, list[DirProbe]]:
    """Return HEADER and PROBES with names, uids, gids and fsid replaced.

    The probes are returned in tree order. Raises AnonymizationError if two
    sibling names or two ids map to the same token.
    """
    an = Anonymizer(key)
    ids = {header.euid}
    ids.update(p.uid for p in probes if p.uid is not None)
    ids.update(p.gid for p in probes if p.gid is not None)
    fake_ids = {n: an.numeric_id(n) for n in ids}
    if len(set(fake_ids.values())) != len(fake_ids):
        raise AnonymizationError(
            "two uids or gids map to the same fake id; use another key"
        )
    anonymized = [
        replace(
            p,
            path=an.path(p.path),
            uid=None if p.uid is None else fake_ids[p.uid],
            gid=None if p.gid is None else fake_ids[p.gid],
            children=None if p.children is None else tuple(map(an.name, p.children)),
        )
        for p in probes
    ]
    # Distinct paths can only coincide if two siblings got the same token.
    if len({p.path for p in anonymized}) != len(anonymized):
        raise AnonymizationError(
            "two sibling names map to the same token; use another key"
        )
    anonymized.sort(key=lambda p: tree_key(p.path))
    new_header = replace(
        header,
        root=an.abs_path(header.root),
        cluster_fsid=None
        if header.cluster_fsid is None
        else an.fsid(header.cluster_fsid),
        euid=fake_ids[header.euid],
        host=None,
        anonymized=True,
    )
    return new_header, anonymized
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_anonymize.py`
Expected: all PASS.

- [ ] **Step 5: Lint**

Run: `ruff format . && ruff check --fix . && ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: Display the commit proposal (do not commit)**

```
Anonymize captures with keyed HMAC tokens

Real captures become test fixtures in a public repository, so names,
uids/gids, the cluster fsid and the hostname must not be recoverable.

- HMAC-SHA256 with a secret key, not a plain hash: plain hashes of
  guessable names (users, projects, years) are reversed by hashing
  guesses.
- The same key gives the same tokens, so a series of captures still
  lines up for deletion and change detection.
- Each path component is tokenized separately: the tree shape is kept
  and repeated names stay recognizable.
- uid 0 stays 0; sizes, counts, times, modes and errors are kept because
  tests need them. Token collisions fail loudly instead of merging dirs.
```

---

### Task 8: CLI (`cli.py`), `python -m trackdir`, README

**Files:**
- Create: `src/trackdir/cli.py`, `src/trackdir/__main__.py`
- Modify: `README.md` (replace the stub)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above: `SafeRoot`, `openat2_supported`; `RStatMode`, `NotCephFSError`, `read_rstats`, `detect_mode`, `cluster_fsid`; `LiveBackend`; `walk`, `DirLimitExceeded`; `CaptureHeader`, `CaptureWriter`; `anonymize`, `load_key`, `AnonymizationError`; `trackdir.__version__`.
- Produces (`trackdir.cli`): `DEFAULT_MAX_DIRS = 1_000_000`, `DEFAULT_JOBS = 16`; `CaptureOptions(root: Path, max_depth: int, output: Path, max_dirs: int = DEFAULT_MAX_DIRS, jobs: int = DEFAULT_JOBS, key_file: Path | None = None)`; `CaptureResult(root: str, output: Path, max_depth: int, dirs: int, errors: Counter[tuple[str, str]], elapsed: float, rstat_mode: RStatMode, openat2: bool, anonymized: bool)`; `class FatalError(Exception)`; `run_capture(opts: CaptureOptions, warn: Callable[[str], None]) -> CaptureResult`; `render_summary(result: CaptureResult) -> str`; `main(argv: Sequence[str] | None = None) -> int`. Tests monkeypatch the module attributes `detect_mode`, `cluster_fsid`, `read_rstats`, `openat2_supported`, `walk`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import os
from collections import Counter
from pathlib import Path

import pytest

from trackdir import cli
from trackdir.capture import load_capture
from trackdir.cli import (
    CaptureOptions,
    CaptureResult,
    FatalError,
    main,
    render_summary,
    run_capture,
)
from trackdir.model import RStats
from trackdir.rstats import RStatMode


def collapse(text: str) -> str:
    return " ".join(text.split())


def must_not_walk(*args, **kwargs):
    pytest.fail("walked although an earlier check should have failed")


@pytest.fixture
def fake_ceph(monkeypatch):
    """Make local directories look like CephFS mounted with dirstat."""
    monkeypatch.setattr(cli, "detect_mode", lambda fd: RStatMode.DIRSTAT)
    monkeypatch.setattr(cli, "cluster_fsid", lambda fd: "fsid")
    monkeypatch.setattr(
        cli, "read_rstats", lambda fd, mode: RStats(1_000_000_000, 10, 2, 1)
    )


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "z").mkdir()
    return root


def options(tree, tmp_path, **changes):
    fields = {"root": tree, "max_depth": 2, "output": tmp_path / "out.jsonl.gz"}
    return CaptureOptions(**(fields | changes))


def test_capture_writes_the_file_and_reports_counts(tree, tmp_path, fake_ceph):
    warned = []
    result = run_capture(options(tree, tmp_path), warned.append)
    capture = load_capture(tmp_path / "out.jsonl.gz")
    paths = [p.path for p in capture.probes.values()]
    assert paths == [b".", b"a", b"a/b", b"z"]
    assert capture.header.root == str(tree.resolve())
    assert capture.header.max_depth == 2
    assert capture.header.dirs == 4
    assert capture.header.rstat_mode == "dirstat"
    assert capture.header.cluster_fsid == "fsid"
    assert not capture.header.anonymized
    assert result.dirs == 4
    assert not result.errors
    assert warned == []


def test_anonymized_capture(tree, tmp_path, fake_ceph):
    key = tmp_path / "k.key"
    key.write_bytes(bytes(range(32)))
    result = run_capture(options(tree, tmp_path, key_file=key), print)
    capture = load_capture(tmp_path / "out.jsonl.gz")
    assert result.anonymized
    assert capture.header.anonymized
    assert capture.header.host is None
    names = [n for p in capture.probes if p != b"." for n in p.split(b"/")]
    assert names
    assert all(len(name) == 16 for name in names)


def test_summary_text(tree, tmp_path, fake_ceph):
    result = run_capture(options(tree, tmp_path), print)
    expected = (
        f"Captured 4 directories under {tree.resolve()} to depth 2 into "
        f"{tmp_path / 'out.jsonl.gz'} in "
    )
    assert collapse(render_summary(result)).startswith(expected)
    assert collapse(render_summary(result)).endswith("s; none had errors.")


def test_summary_lists_errors_by_stage_and_code():
    result = CaptureResult(
        root="/r",
        output=Path("o"),
        max_depth=1,
        dirs=5,
        errors=Counter({("open", "ENOENT"): 1, ("open", "EACCES"): 2}),
        elapsed=2.0,
        rstat_mode=RStatMode.DIRSTAT,
        openat2=True,
        anonymized=False,
    )
    assert collapse(render_summary(result)) == (
        "Captured 5 directories under /r to depth 1 into o in 2.0 s; "
        "3 had errors: open EACCES 2, open ENOENT 1."
    )


def test_existing_output_is_refused_before_walking(tree, tmp_path, monkeypatch):
    (tmp_path / "out.jsonl.gz").write_bytes(b"x")
    monkeypatch.setattr(cli, "walk", must_not_walk)
    with pytest.raises(FatalError, match="already exists"):
        run_capture(options(tree, tmp_path), print)


def test_output_inside_root_is_refused_with_relative_paths(tree, monkeypatch):
    monkeypatch.chdir(tree.parent)
    opts = CaptureOptions(
        root=Path("root/"), max_depth=1, output=Path("root/a/out.jsonl.gz")
    )
    with pytest.raises(FatalError, match="inside ROOT"):
        run_capture(opts, print)
    assert sorted(os.listdir(tree / "a")) == ["b"]


def test_missing_output_directory_fails_before_walking(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "walk", must_not_walk)
    with pytest.raises(FatalError, match="does not exist"):
        run_capture(options(tree, tmp_path, output=tmp_path / "no" / "o.gz"), print)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_output_directory_fails_before_walking(
    tree, tmp_path, fake_ceph, monkeypatch
):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setattr(cli, "walk", must_not_walk)
    try:
        with pytest.raises(FatalError, match="cannot create"):
            run_capture(options(tree, tmp_path, output=readonly / "o.gz"), print)
    finally:
        readonly.chmod(0o700)


def test_short_key_is_refused(tree, tmp_path):
    key = tmp_path / "short.key"
    key.write_bytes(b"x" * 31)
    with pytest.raises(FatalError, match="32"):
        run_capture(options(tree, tmp_path, key_file=key), print)


def test_symlink_root_is_refused(tree, tmp_path, fake_ceph):
    link = tmp_path / "link"
    link.symlink_to(tree)
    with pytest.raises(FatalError, match="cannot open ROOT"):
        run_capture(options(link, tmp_path), print)


def test_local_directory_is_not_cephfs(tree, tmp_path):
    with pytest.raises(FatalError, match="not on CephFS"):
        run_capture(options(tree, tmp_path), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_dir_limit_leaves_nothing(tree, tmp_path, fake_ceph):
    with pytest.raises(FatalError, match="more than 2 directories"):
        run_capture(options(tree, tmp_path, max_dirs=2), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_interrupt_leaves_nothing(tree, tmp_path, fake_ceph, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "walk", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_capture(options(tree, tmp_path), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_missing_openat2_warns_when_not_root(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "openat2_supported", lambda: False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    warned = []
    run_capture(options(tree, tmp_path), warned.append)
    assert len(warned) == 1
    assert "openat2" in warned[0]


def test_missing_openat2_is_fatal_for_root(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "openat2_supported", lambda: False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(FatalError, match="openat2"):
        run_capture(options(tree, tmp_path), print)


def test_getxattr_mode_warns(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "detect_mode", lambda fd: RStatMode.GETXATTR)
    warned = []
    run_capture(options(tree, tmp_path), warned.append)
    assert len(warned) == 1
    assert "dirstat" in warned[0]


def test_main_success(tree, tmp_path, fake_ceph, capsys):
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("Captured 3 directories")


def test_main_error(tree, tmp_path, capsys):
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 1
    assert collapse(capsys.readouterr().err).startswith("ERROR: ROOT")


def test_main_interrupted(tree, tmp_path, fake_ceph, monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "walk", interrupted)
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 130
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["capture", "r", "-o", "o"],
        ["capture", "r", "--max-depth", "1"],
        ["capture", "r", "--max-depth", "-1", "-o", "o"],
        ["capture", "r", "--max-depth", "x", "-o", "o"],
        ["capture", "r", "--max-depth", "1", "-o", "o", "--jobs", "0"],
        ["capture", "r", "--max-depth", "1", "-o", "o", "--max-dirs", "0"],
        ["frobnicate"],
    ],
)
def test_usage_errors_exit_2(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_cli.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trackdir.cli'`

- [ ] **Step 3: Write the implementation**

`src/trackdir/cli.py`:

```python
"""Command-line interface: trackdir capture."""

import argparse
import functools
import os
import socket
import sys
import textwrap
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trackdir import __version__
from trackdir.anonymize import AnonymizationError, anonymize, load_key
from trackdir.capture import CaptureHeader, CaptureWriter
from trackdir.model import ROOT, DirProbe, ShouldList
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

DEFAULT_MAX_DIRS = 1_000_000  # probes stay in memory until the end, ~1 KB each
DEFAULT_JOBS = 16  # concurrent probes; each mostly waits on MDS round trips
WRAP_WIDTH = 79

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

CAPTURE_DESCRIPTION = """\
Record every directory under ROOT, down to --max-depth, into one gzipped JSON
Lines file: owner, mode, nlink, CephFS rstats (rbytes, rfiles, rsubdirs,
rctime) and per-directory errors. Use captures to develop, debug and test
trackdir offline with data from a real filesystem.

Read-only on ROOT. FILE appears only if the capture succeeds and is never
overwritten.
"""

CAPTURE_EPILOG = """\
Run as root for a complete capture. As another user, directories you cannot
read are recorded as EACCES errors, without rstats.

--anonymize makes a capture safe to publish. Create the key once and keep it
out of the repository; captures taken with the same key line up:
  head -c 32 /dev/urandom > trackdir.key

Exit status: 0 success (per-directory errors included), 1 error, 2 usage
error, 130 interrupted.
"""


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    """What to capture and where; see `trackdir capture --help`."""

    root: Path
    max_depth: int
    output: Path
    max_dirs: int = DEFAULT_MAX_DIRS
    jobs: int = DEFAULT_JOBS
    key_file: Path | None = None


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Outcome of a successful capture."""

    root: str
    output: Path
    max_depth: int
    dirs: int
    errors: Counter[tuple[str, str]]  # (stage, code) -> number of directories
    elapsed: float  # seconds spent walking
    rstat_mode: RStatMode
    openat2: bool
    anonymized: bool


class FatalError(Exception):
    """Stops the run; the message is printed as one ERROR: paragraph."""


def run_capture(opts: CaptureOptions, warn: Callable[[str], None]) -> CaptureResult:
    """Capture opts.root into opts.output and return what happened.

    Calls WARN once per degraded mode, before walking. Raises FatalError;
    KeyboardInterrupt propagates. The output file exists only if this returns.
    """
    _check_output(opts.output, opts.root)
    key = _load_key(opts.key_file) if opts.key_file is not None else None
    use_openat2 = openat2_supported()
    if not use_openat2:
        if os.geteuid() == 0:
            raise FatalError(MSG_NO_OPENAT2_AS_ROOT)
        warn(MSG_NO_OPENAT2)
    with _open_root(opts.root, use_openat2) as root:
        root_path = root.resolved_path()
        mode, fsid = _inspect_root(root, opts.root)
        if mode is RStatMode.GETXATTR:
            warn(MSG_NO_DIRSTAT)
        backend = LiveBackend(root, functools.partial(read_rstats, mode=mode))
        with _create_writer(opts.output) as writer:
            started = datetime.now(UTC)
            clock = time.monotonic()
            probes = _walk(backend, opts)
            elapsed = time.monotonic() - clock
            header = CaptureHeader(
                root=root_path,
                max_depth=opts.max_depth,
                cluster_fsid=fsid,
                rstat_mode=mode.value,
                openat2=use_openat2,
                jobs=opts.jobs,
                started=started.isoformat(),
                finished=datetime.now(UTC).isoformat(),
                euid=os.geteuid(),
                host=socket.gethostname(),
                kernel=os.uname().release,
                trackdir_version=__version__,
                dirs=len(probes),
                errors=sum(p.error is not None for p in probes),
                anonymized=False,
            )
            if key is not None:
                header, probes = _anonymize(header, probes, key)
            _commit(writer, header, probes, opts.output)
    return CaptureResult(
        root=root_path,
        output=opts.output,
        max_depth=opts.max_depth,
        dirs=len(probes),
        errors=Counter((str(p.error.stage), p.error.code) for p in probes if p.error),
        elapsed=elapsed,
        rstat_mode=mode,
        openat2=use_openat2,
        anonymized=key is not None,
    )


def render_summary(result: CaptureResult) -> str:
    """Return the success message for RESULT as one wrapped paragraph."""
    if result.errors:
        counts = ", ".join(
            f"{stage} {code} {n}" for (stage, code), n in sorted(result.errors.items())
        )
        outcome = f"{result.errors.total()} had errors: {counts}"
    else:
        outcome = "none had errors"
    return _paragraph(
        f"Captured {result.dirs} directories under {result.root} to depth "
        f"{result.max_depth} into {result.output} in {result.elapsed:.1f} s; "
        f"{outcome}."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the trackdir command line and return the exit status."""
    args = _parser().parse_args(argv)
    opts = CaptureOptions(
        root=args.root,
        max_depth=args.max_depth,
        output=args.output,
        max_dirs=args.max_dirs,
        jobs=args.jobs,
        key_file=args.anonymize,
    )
    try:
        result = run_capture(opts, _warn)
    except FatalError as e:
        sys.stderr.write(_paragraph(f"ERROR: {e}"))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write(_paragraph("ERROR: interrupted; no capture was written."))
        return 130
    sys.stderr.write(render_summary(result))
    return 0


def _list_above(max_depth: int) -> ShouldList:
    """List directories above MAX_DEPTH, so the walk stops there."""
    return lambda probe: probe.depth < max_depth


def _check_output(output: Path, root: Path) -> None:
    if output.exists() or output.is_symlink():
        raise FatalError(
            f"{output} already exists; trackdir never overwrites a capture. "
            "Choose another name."
        )
    parent = output.parent.resolve()
    if not parent.is_dir():
        raise FatalError(f"the output directory {output.parent} does not exist.")
    if parent.is_relative_to(root.resolve()):
        raise FatalError(
            f"the output {output} is inside ROOT {root}; writing there would "
            "change the tree being captured. Write it elsewhere."
        )


def _load_key(path: Path) -> bytes:
    try:
        return load_key(path)
    except OSError as e:
        raise FatalError(f"cannot read the key file {path}: {e.strerror}.") from e
    except ValueError as e:
        raise FatalError(
            f"{e}. Create a key with: head -c 32 /dev/urandom > {path}"
        ) from e


def _open_root(path: Path, use_openat2: bool) -> SafeRoot:
    try:
        return SafeRoot(path, use_openat2=use_openat2)
    except OSError as e:
        raise FatalError(
            f"cannot open ROOT {path}: {e.strerror}. ROOT must be a directory, "
            "not a symlink."
        ) from e


def _inspect_root(root: SafeRoot, path: Path) -> tuple[RStatMode, str | None]:
    """Return the rstat mode and cluster fsid of ROOT's filesystem."""
    try:
        fd = root.open(ROOT, os.O_RDONLY)
        try:
            return detect_mode(fd), cluster_fsid(fd)
        finally:
            os.close(fd)
    except NotCephFSError:
        raise FatalError(
            f"ROOT {path} is not on CephFS (it has no ceph.dir.rctime)."
        ) from None
    except OSError as e:
        raise FatalError(f"cannot read ROOT {path}: {e.strerror}.") from e


def _create_writer(output: Path) -> CaptureWriter:
    try:
        return CaptureWriter(output)
    except OSError as e:
        raise FatalError(f"cannot create {output}: {e.strerror}.") from e


def _walk(backend: LiveBackend, opts: CaptureOptions) -> list[DirProbe]:
    try:
        return walk(
            backend,
            _list_above(opts.max_depth),
            max_dirs=opts.max_dirs,
            jobs=opts.jobs,
        )
    except DirLimitExceeded as e:
        raise FatalError(
            f"ROOT has more than {e.limit} directories within depth "
            f"{opts.max_depth}. Raise --max-dirs if memory allows, or lower "
            "--max-depth."
        ) from e


def _anonymize(
    header: CaptureHeader, probes: list[DirProbe], key: bytes
) -> tuple[CaptureHeader, list[DirProbe]]:
    try:
        return anonymize(header, probes, key)
    except AnonymizationError as e:
        raise FatalError(f"{e}.") from e


def _commit(
    writer: CaptureWriter, header: CaptureHeader, probes: list[DirProbe], output: Path
) -> None:
    try:
        writer.commit(header, probes)
    except OSError as e:
        raise FatalError(f"cannot write {output}: {e.strerror}.") from e


def _paragraph(text: str) -> str:
    wrapped = textwrap.fill(
        text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    return wrapped + "\n"


def _warn(message: str) -> None:
    sys.stderr.write(_paragraph(f"WARNING: {message}"))


def _int_at_least(minimum: int) -> Callable[[str], int]:
    """Return an argparse type for integers >= MINIMUM."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}: {value}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trackdir", description="Track CephFS directory sizes over time."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    capture = commands.add_parser(
        "capture",
        help="record directory state under ROOT for offline use",
        description=CAPTURE_DESCRIPTION,
        epilog=CAPTURE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    capture.add_argument("root", metavar="ROOT", type=Path, help="CephFS directory")
    capture.add_argument(
        "--max-depth",
        type=_int_at_least(0),
        required=True,
        metavar="N",
        help="record directories at most N levels below ROOT (0: ROOT only)",
    )
    capture.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="FILE",
        help="capture file to create (.jsonl.gz)",
    )
    capture.add_argument(
        "--max-dirs",
        type=_int_at_least(1),
        default=DEFAULT_MAX_DIRS,
        metavar="N",
        help="fail if there are more than N directories (default: %(default)s)",
    )
    capture.add_argument(
        "--jobs",
        type=_int_at_least(1),
        default=DEFAULT_JOBS,
        metavar="N",
        help="directories probed concurrently (default: %(default)s)",
    )
    capture.add_argument(
        "--anonymize",
        type=Path,
        metavar="KEYFILE",
        help="replace names and ids with HMAC tokens keyed by KEYFILE",
    )
    return parser
```

`src/trackdir/__main__.py`:

```python
"""Run trackdir as `python -m trackdir`."""

import sys

from trackdir.cli import main

sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/test_cli.py`
Expected: all PASS (the unwritable-directory test is skipped as root).

- [ ] **Step 5: Check `--help` reads well**

Run: `PYTHONPATH=src python3 -m trackdir capture --help`
Expected: the usage line, the two description paragraphs, one line per option, and the epilog with the key one-liner and exit codes. Fix any wording that breaks the Global Constraints' writing rules.

- [ ] **Step 6: Replace `README.md`**

```markdown
# trackdir — track CephFS directory sizes over time

trackdir records how the sizes of directories on a CephFS filesystem change
over time. It reads CephFS recursive statistics (`ceph.dir.rbytes`,
`rfiles`, `rsubdirs`, `rctime`), so the size of a whole subtree costs one
metadata request instead of a full scan.

Status: `trackdir capture` works. Periodic sampling into PostgreSQL
(`trackdir sample`) is planned.

## Requirements

- Linux with the CephFS kernel client.
- openat2 (Linux 5.6+); required when running as root.
- Python 3.12 or newer; no other dependencies.
- Recommended: the `dirstat` mount option. It cuts the metadata requests
  per directory from four to one; trackdir warns when it is missing.

## Install

    pip install .

Or run from a checkout without installing: `PYTHONPATH=src python3 -m trackdir`.

## Capture directory state

    trackdir capture /mnt/cephfs/projects --max-depth 3 -o projects.jsonl.gz

`capture` records every directory under ROOT down to `--max-depth` into one
file, for offline debugging, development and tests. It is read-only on ROOT
and writes only the output file, which appears only if the capture succeeds
and is never overwritten. Run it as root for a complete capture: as another
user, unreadable directories are recorded as EACCES errors.

`--max-dirs` (default 1,000,000) stops the run before memory runs out;
`--jobs` (default 16) sets how many directories are probed concurrently.

To share a capture, anonymize it with a secret key kept out of the
repository. Captures taken with the same key line up:

    head -c 32 /dev/urandom > trackdir.key
    trackdir capture /mnt/cephfs/projects --max-depth 3 \
        --anonymize trackdir.key -o projects-anon.jsonl.gz

### Capture file format

A gzipped JSON Lines file. Line 1 is a header (`format`, `version`, `root`,
`max_depth`, `rstat_mode`, timestamps, counts, `anonymized`, …). Every other
line is one directory, in tree order:

    {"path":"proj/2019","ino":1099511628279,"mode":16877,"uid":0,"gid":0,"nlink":5,"rctime":1792088576117716441,"rbytes":172057275971107,"rfiles":1234,"rsubdirs":4}
    {"path":"secret","ino":1099511627777,"mode":16872,"uid":0,"gid":0,"nlink":6,"error":{"stage":"open","code":"EACCES"}}

`path` is relative to ROOT (`.` is ROOT itself); `rctime` is in
nanoseconds; fields that could not be read are omitted. Inspect a capture
with `zcat FILE | jq`.

## Development

    pytest
    ruff format . && ruff check .

Tests need neither Ceph nor root; real data comes from anonymized captures
in `tests/data/`.

## Documentation

- [docs/terms.md](docs/terms.md): terms used in code and docs.
- [docs/conventions.md](docs/conventions.md): project conventions.
- [Design of capture](docs/superpowers/specs/2026-10-15-trackdir-capture-design.md).
```

- [ ] **Step 7: Run the whole suite and lint**

Run: `pytest -q && ruff format . && ruff check --fix . && ruff check .`
Expected: all tests PASS; `All checks passed!`

- [ ] **Step 8: Display the commit proposal (do not commit)**

```
Add the trackdir capture command

`trackdir capture ROOT --max-depth N -o FILE` walks ROOT read-only and
writes a capture file; --anonymize KEYFILE makes it publishable.

- run_capture() decides and returns a typed CaptureResult;
  render_summary() prints it. Tests assert on results, not on text.
- Every check that can fail runs before the walk: output exists or is
  inside ROOT, output directory missing or unwritable (the temp file is
  created up front), bad key, ROOT a symlink or not CephFS, openat2
  missing as root.
- Without openat2 as non-root, and without dirstat, the run proceeds
  with one WARNING each, saying what is degraded and what to do.
- Defaults: --jobs 16, --max-dirs 1,000,000 (the safety limit against
  running out of memory).
```

---

### Task 9: Real captures as test fixtures

Reads `/mnt/ceph1` and `/mnt/ceph2` (read-only, production). Never pass a path under them to `-o`. Keep `--max-dirs 5000`: it caps MDS load and fixture size.

**Files:**
- Create: `tests/data/ceph1-depth3.jsonl.gz`, `tests/data/ceph2-depth3.jsonl.gz` (or `-depth2`, see Step 4)
- Create (gitignored, not committed): `.keys/fixtures.key`, `captures/*.jsonl.gz`
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Consumes: `trackdir.capture.load_capture`, `ReplayBackend`; `trackdir.walker.walk`; `trackdir.model.Stage`.
- Produces: committed anonymized captures that later work (the sampler) can replay.

- [ ] **Step 1: Create the fixture key (outside git)**

Run: `mkdir -p .keys captures && test -e .keys/fixtures.key || head -c 32 /dev/urandom > .keys/fixtures.key`
Expected: `.keys/fixtures.key` is 32 bytes; `git status --short` does not list `.keys/`. Tell the user to back up this key: later captures line up with these fixtures only if they use the same key.

- [ ] **Step 2: Take a raw capture and check the dirstat/readdir interaction**

Run: `PYTHONPATH=src python3 -m trackdir capture /mnt/ceph2 --max-depth 2 --max-dirs 5000 -o captures/ceph2-depth2-raw.jsonl.gz`
Expected: a `Captured N directories … none had errors.` summary (or only EACCES errors) and no WARNING about dirstat.

Then run:

```bash
PYTHONPATH=src python3 - <<'EOF'
from pathlib import Path
from trackdir.capture import load_capture
c = load_capture(Path("captures/ceph2-depth2-raw.jsonl.gz"))
print("mode:", c.header.rstat_mode, "dirs:", len(c.probes))
bad = [(p.path, p.nlink, len(p.children)) for p in c.probes.values()
       if p.children is not None and p.nlink != 2 + len(p.children)]
print("nlink mismatches:", bad)
EOF
```

Expected: `mode: dirstat` and `nlink mismatches: []`. If there are mismatches, compare each with the live mount (`stat -c %h DIR`; `find DIR -mindepth 1 -maxdepth 1 -type d | wc -l`). If the directory changed since, re-capture. If it did not, stop and report: the pread/readdir interaction or the nlink assumption is broken.

- [ ] **Step 3: Write the fixture tests**

`tests/test_fixtures.py`:

```python
"""Invariants of real, anonymized captures from production CephFS clusters."""

from pathlib import Path

import pytest

from trackdir.capture import ReplayBackend, load_capture
from trackdir.model import Stage
from trackdir.walker import walk

DATA = Path(__file__).parent / "data"
FIXTURES = sorted(DATA.glob("*.jsonl.gz"))
PINNED_NS = 4294967295 * 10**9


@pytest.fixture(params=FIXTURES, ids=lambda path: path.name)
def capture(request):
    return load_capture(request.param)


def test_fixtures_exist():
    assert FIXTURES, f"no captures in {DATA}"


def test_only_anonymized_captures_are_committed(capture):
    assert capture.header.anonymized
    assert capture.header.host is None


def test_listed_dirs_have_nlink_minus_two_subdirs(capture):
    # CephFS reports nlink = 2 + subdirs; a mismatch means lost or invented names.
    mismatched = [
        p.path
        for p in capture.probes.values()
        if p.children is not None and p.nlink != 2 + len(p.children)
    ]
    assert mismatched == []


def test_rsubdirs_count_the_dir_itself(capture):
    rsubdirs = [p.rsubdirs for p in capture.probes.values() if p.rsubdirs is not None]
    assert rsubdirs
    assert min(rsubdirs) >= 1


def test_replay_reproduces_the_capture(capture):
    depth = capture.header.max_depth
    probes = walk(
        ReplayBackend(capture, depth),
        lambda probe: probe.depth < depth,
        max_dirs=len(capture.probes),
        jobs=4,
    )
    assert probes == list(capture.probes.values())


def test_fixtures_cover_pinned_rctime_and_permission_errors():
    probes = [p for path in FIXTURES for p in load_capture(path).probes.values()]
    assert any(p.rctime == PINNED_NS for p in probes)
    assert any(
        p.error is not None and p.error.stage == Stage.OPEN and p.error.code == "EACCES"
        for p in probes
    )
```

Run: `pytest -q tests/test_fixtures.py`
Expected: `test_fixtures_exist` and `test_fixtures_cover_…` FAIL (no fixtures yet); the parametrized tests are skipped with an empty parameter set.

- [ ] **Step 4: Take the anonymized fixture captures**

Run:

```bash
PYTHONPATH=src python3 -m trackdir capture /mnt/ceph1 --max-depth 3 --max-dirs 5000 \
    --anonymize .keys/fixtures.key -o tests/data/ceph1-depth3.jsonl.gz
PYTHONPATH=src python3 -m trackdir capture /mnt/ceph2 --max-depth 3 --max-dirs 5000 \
    --anonymize .keys/fixtures.key -o tests/data/ceph2-depth3.jsonl.gz
```

Expected: each prints a `Captured N directories …` summary; ceph1 reports some `open EACCES` errors (uid 1000 cannot read root-only dirs). If a run fails with `more than 5000 directories`, rerun it with `--max-depth 2` and the file name `…-depth2.jsonl.gz`. Then run `ls -l tests/data` and check that each file is under 1 MB.

- [ ] **Step 5: Run the fixture tests**

Run: `pytest -q tests/test_fixtures.py`
Expected: all PASS. If the nlink test fails, apply Step 2's procedure to the reported paths (they are anonymized; take a raw capture of the same tree to find them).

- [ ] **Step 6: Run everything and lint**

Run: `pytest -q && ruff format . && ruff check --fix . && ruff check .`
Expected: all PASS; `All checks passed!`

- [ ] **Step 7: Display the commit proposal (do not commit)**

```
Add anonymized real captures as test fixtures

Two captures of production CephFS clusters (anonymized with a key kept
outside the repository) give tests real data: pinned rctime chains,
EACCES directories, real nlink/rsubdirs values.

- Fixture tests check invariants on real data: listed directories have
  nlink - 2 child records (this also guards the pread/readdir
  interaction on a shared fd), rsubdirs >= 1, replay reproduces the
  capture, and only anonymized captures are committed.
- Captured with --max-dirs 5000 to bound MDS load and fixture size.
```

---

### Task 10 (only if the user approves): Run the suite under Python 3.12

Only Python 3.14 and 3.15 are installed locally. Skip this task unless the user approves pulling the `python:3.12-slim` image with podman.

- [ ] **Step 1: Run the tests in a throwaway 3.12 container**

Run:

```bash
podman run --rm -v "$PWD":/src:ro,Z -w /src -e PYTHONDONTWRITEBYTECODE=1 \
    docker.io/library/python:3.12-slim \
    sh -c "pip install -q pytest && python -m pytest -q -p no:cacheprovider"
```

Expected: all tests PASS, except that tests needing openat2 may skip if the container's seccomp profile hides it. Fix any 3.13+ API that slipped in, rerun the full local suite, and display a commit proposal for the fixes.
