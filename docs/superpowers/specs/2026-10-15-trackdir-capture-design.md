# trackdir capture — design

Status: design approved in brainstorming on 2026-10-15; next step is the
implementation plan.

Amended 2026-10-16 by the
[initial-sample design](2026-10-16-trackdir-initial-sample-design.md): the
capture format is version 2, whose header makes `cluster_fsid` required and
adds `fs_name` and `fs_path`, and capture now fails when ROOT's identity (cluster fsid and
CephFS mount) cannot be determined.

## 1. Goal and scope

`trackdir` will track how CephFS directory sizes change over time: a sampler
records changed directories into a database that other tools read. This spec
covers only the first feature, **`trackdir capture`**, plus the components it
needs and that the sampler will reuse.

`capture` walks a CephFS tree (ROOT down to MAX_DEPTH) without pruning and
writes everything the sampler would read from the filesystem to one file. The
file supports offline debugging, offline development, and unit tests that use
data taken from real live filesystems. `--anonymize` makes captures safe to
commit to a public repository.

In scope:

- safe, symlink-proof, single-filesystem directory opening (openat2)
- reading CephFS recursive stats (rstats)
- probing one directory; the concurrent walker
- the capture file format, writer, loader, and a replay backend
- anonymization
- the `trackdir capture` CLI

Out of scope (later specs): `sample`, database backends, pruning policy,
inode tracking (renames).

## 2. Measured facts

Measured read-only on two production clusters (`/mnt/ceph1`, `/mnt/ceph2`,
kernel client, Linux 7.2, both mounted `ro,dirstat`) as uid 1000.

| Operation | Cost |
|---|---|
| open, fstat, `read()` on a dir fd | 4–60 µs; answered by the client |
| each `ceph.dir.*` getxattr | 0.5–0.7 ms (ceph2), 0.6–1.4 ms (ceph1); one MDS round trip per call, even when repeated on the same dir |

- `read()` on a dir fd (mount option `dirstat`) returns `entries`, `files`,
  `subdirs`, `rentries`, `rfiles`, `rsubdirs`, `rbytes`, `rctime` formatted
  from the client's cached inode. Right after a parent readdir it was stale
  in 162 of 1232 comparisons on busy dirs. After one getxattr of
  `ceph.dir.rctime` on the same fd, it matched in 1624 of 1624.
- `read()` of that text advances the fd's file offset (to 256), the same
  offset readdir uses. A following scandir still listed every entry on the
  two dirs tested, but nothing guarantees that. `pread(fd, 4096, 0)` returns
  the same text, leaves the offset at 0, and works before or after scandir.
- `rentries = rfiles + rsubdirs`; `entries = files + subdirs`;
  `st_nlink = subdirs + 2` for dirs (all 18 probed dirs); `rsubdirs` counts
  the dir itself (a leaf has `rsubdirs = 1`).
- rctime pinned at `4294967295.000000000` on ceph1 `/` and on a chain of
  dirs below it (four dirs, two levels). Pinning follows ancestor chains; siblings are
  unaffected.
- As non-root, a dir without read permission gives EACCES for open and for
  getxattr; `O_PATH` open plus fstat still works.
- `ceph.cluster_fsid` is readable on dirs.
- CPython has no openat2 wrapper; the kernel has it (syscall 437 on x86_64
  and arm64).

Consequence: per-dir cost is the number of MDS round trips. In `dirstat`
mode one round trip yields all rstats; otherwise each rstat costs one.

## 3. Decisions

| Topic | Decision |
|---|---|
| What capture records | One probe result per dir (stat fields, rstats, error). Not raw syscall results; not a sample. |
| rstat reading | `dirstat` mode by default (one forced getxattr, then `read()`); automatic fallback to one getxattr per rstat, with a warning. |
| Concurrency | Thread pool, default 16 (`--jobs`). Work items are relative paths, so processes could replace threads later. |
| Compression | gzip (stdlib on Python 3.12). |
| Non-UTF-8 names | Bytes internally; surrogateescape strings in JSON. |
| Failure | Any fatal error, including `--max-dirs` exceeded, leaves no output file. |
| Vanished dirs | ENOENT is recorded as a per-dir error; the sampler will treat it as a deletion. |
| Anonymization | `capture --anonymize KEYFILE`: HMAC-SHA256 tokens for names, uids/gids (0 kept), fsid. |
| Name | Command and package `trackdir`. |
| `--max-dirs` | Default 1,000,000. |
| Python | >= 3.12; no runtime dependencies. |

Decisions agreed for later phases are listed in section 14.

## 4. Components

`src/trackdir/`:

| Module | Responsibility |
|---|---|
| `model.py` | `DirProbe`, `ProbeError`, `CaptureHeader` dataclasses |
| `safeopen.py` | `SafeRoot`: opens paths beneath the ROOT fd via openat2, or the per-component fallback |
| `rstats.py` | rstat mode detection; reading and parsing rstats in both modes |
| `probe.py` | `LiveBackend.probe()`: probe one dir |
| `walker.py` | `walk()`: concurrent traversal over any backend |
| `capture.py` | capture writer and loader; `ReplayBackend` |
| `anonymize.py` | pure transform of header and records |
| `cli.py` | argparse, `capture` subcommand, messages |

The backend protocol is the seam between filesystem access and traversal:

```python
class Backend(Protocol):
    def probe(
        self, path: bytes, depth: int, should_list: Callable[[DirProbe], bool]
    ) -> DirProbe: ...
```

`LiveBackend` talks to CephFS; `ReplayBackend` serves a loaded capture; tests
add fakes. `walker.walk()` works with all three.

## 5. Safe opening (`safeopen.py`)

Production runs as root over user-writable trees, so no path may be
redirected by a symlink, and the walk must stay on one filesystem.

- ROOT is opened once with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`. Its
  leading components are trusted (admin input); a symlink as the final
  component is an error.
- **openat2 mode.** `SafeRoot.open(path, flags)` calls openat2 through ctypes
  with the ROOT fd, the relative path, and
  `resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV`. One
  syscall safely resolves a multi-component path, so work items are plain
  paths and no parent fds are held. `O_NOFOLLOW` is not passed: with
  `O_PATH` it would return an fd to a symlink instead of failing. Verified
  on Linux 7.2: a symlink anywhere in the path gives ELOOP, a non-directory
  ENOTDIR, a mount crossing EXDEV. EAGAIN is retried a few times; EINTR is
  retried too, since PEP 475 auto-retry does not cover ctypes calls.
- Paths never contain `.` or `..` components (ROOT itself is `.`), because
  `RESOLVE_BENEATH` also reports a `..` escape as EXDEV.
- **Support check.** At startup, openat2 on `/` with `O_PATH`. ENOSYS or
  EPERM (seccomp) means unsupported; so does an architecture without a known
  syscall number.
- **Policy.** Unsupported and euid 0: fatal. Unsupported otherwise: warn once
  and use the fallback.
- **Fallback.** Open each component with
  `openat(fd, name, O_PATH|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC)` (the last with
  the requested flags plus `O_NOFOLLOW`), compare `st_dev` with ROOT's and
  report EXDEV on a mismatch. It detects mount crossing but cannot prevent
  the open itself. `O_DIRECTORY|O_NOFOLLOW` fails on a symlink with ENOTDIR;
  on ENOTDIR the fallback lstats the component (`fstatat` with
  `AT_SYMLINK_NOFOLLOW`) and reports a symlink as ELOOP, as openat2 does.

## 6. Reading rstats (`rstats.py`)

The four rstats recorded are `rctime`, `rbytes`, `rfiles`, `rsubdirs`.

- **Startup checks on ROOT.** getxattr `ceph.dir.rctime` fails with ENODATA
  or EOPNOTSUPP: fatal, ROOT is not on CephFS. Then `read()` on a fresh ROOT
  fd: EISDIR selects getxattr mode, with one warning that the mount lacks
  `dirstat` and each dir costs four MDS round trips instead of one.
- **Contract:** reading rstats never moves the fd's file offset, because
  readdir on the same fd follows.
- **dirstat mode.** getxattr `ceph.dir.rctime` (forces the client to refresh
  all rstats from the MDS), then `pread(fd, 4096, 0)` on the same fd. All
  four values come from that text. If its rctime is older than the
  getxattr's, the refresh assumption failed: per-dir error `DIRSTAT_STALE`.
  This assumption is undocumented kernel behavior (section 2), so it is
  checked on every dir.
- **getxattr mode.** getxattr `rctime`, then `rbytes`, `rfiles`, `rsubdirs`.
  rctime goes first: if the dir changes between reads, the stored rctime is
  the older one, so the change is not hidden.
- **Parsing.** Integers: `^\d+$`. rctime: `^\d+\.\d{9}$` (dirstat pads with
  leading spaces; strip first), stored as integer nanoseconds. Anything else
  is per-dir error `PARSE`.

## 7. Probing one directory (`probe.py`)

```python
@dataclass(frozen=True, slots=True)
class ProbeError:
    stage: str  # "open" | "stat" | "rstats" | "readdir"
    code: str  # errno name ("EACCES") or "DIRSTAT_STALE" | "PARSE"


@dataclass(frozen=True, slots=True)
class DirProbe:
    path: bytes  # relative to ROOT; b"." is ROOT
    ino: int | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    nlink: int | None = None
    rctime: int | None = None  # ns since the epoch
    rbytes: int | None = None
    rfiles: int | None = None
    rsubdirs: int | None = None
    children: tuple[bytes, ...] | None = None  # subdir names; None = not listed
    error: ProbeError | None = None
```

`LiveBackend.probe(path, depth, should_list)`:

1. Open with `O_RDONLY|O_DIRECTORY`. On EACCES, open with `O_PATH` so fstat
   can still report the owner; record error `open EACCES` and stop after
   step 2. Any other open error: record it and stop.
2. fstat: `ino`, `mode`, `uid`, `gid`, `nlink`. An error: record and stop.
3. Read rstats (section 6). An error is recorded; processing continues.
4. If `should_list(probe_so_far)` and `nlink != 2`: scandir the fd and keep
   entries whose `is_dir(follow_symlinks=False)` is true. `nlink == 2` means
   no subdirs (CephFS and most local filesystems report `2 + subdirs`; btrfs
   reports 1, which falls through to listing), so `children = ()`. A readdir
   error discards partial results (they would look like deletions) and is
   recorded.
5. Close the fd.

At most one error is recorded per dir: the first, except that a readdir
error replaces an earlier `rstats` error, because the error stage tells
readers whether the dir was listed. A dir is listed (has `children`) only
when its error, if any, is at stage `rstats`.

A dir's depth is the number of components in its path; ROOT is depth 0.

`should_list` runs inside the probe so the sampler can decide from the dir's
fresh rstats without a second open. For capture it is `depth < max_depth`.

## 8. Walker (`walker.py`)

`walk(backend, should_list, max_dirs, jobs) -> list[DirProbe]`

- A `ThreadPoolExecutor(jobs)` runs probes; the main thread submits ROOT,
  then the children of each completed probe. Probes spend their time in
  syscalls that release the GIL.
- Dirs are counted at submit time; ROOT counts. Exceeding `max_dirs` raises
  `DirLimitExceeded` after cancelling pending probes.
- Exceptions from probes (bugs, not per-dir errors) propagate and abort.
- Ctrl-C cancels pending probes; the CLI exits 130 with no output.
- Results are sorted in tree order by `tree_key(path)`: the tuple of path
  components, with `()` for ROOT. Splitting `b"."` instead would sort ROOT
  after names such as `-foo`. The walker, writer, loader and anonymizer
  share this one function. Output is the same for any `jobs`. `max_dirs`
  bounds memory.

## 9. Capture file (`capture.py`)

gzip (level 6; the stdlib default 9 is slow) of UTF-8 JSON Lines. JSON is
written with `ensure_ascii=True`, so surrogateescaped bytes appear as
`\udcXX` and the file is valid JSON and valid UTF-8. Files are created with
mode 0600.

Line 1 is the header:

```json
{"format": "trackdir-capture", "version": 1, "root": "/mnt/ceph2/x",
 "max_depth": 2, "cluster_fsid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
 "rstat_mode": "dirstat", "openat2": true, "jobs": 16,
 "started": "2026-10-15T18:20:00.123456+00:00",
 "finished": "2026-10-15T18:20:41.654321+00:00",
 "euid": 1000, "host": "example", "kernel": "7.2.10-gentoo-dist-bin",
 "trackdir_version": "0.1.0", "dirs": 1234, "errors": 5, "anonymized": false}
```

`root` is the resolved path of the opened ROOT fd. `max_depth` is the depth
the capture was taken at.

Each following line is one dir, in tree order. Fields are omitted when
unknown; key order is fixed:

```json
{"path": ".", "ino": 1099516860310, "mode": 16877, "uid": 0, "gid": 0, "nlink": 19, "rctime": 1792088162425276962, "rbytes": 5047154896085323, "rfiles": 195211513, "rsubdirs": 4851168}
{"path": "archive", "ino": 1099511627777, "mode": 16872, "uid": 0, "gid": 0, "nlink": 6, "error": {"stage": "open", "code": "EACCES"}}
```

Child names are not stored: every listed subdir has its own record. The
loader rebuilds `children` for each record that was listed, i.e. depth below
the header's `max_depth` and no error other than at stage `rstats`.

`load_capture(path) -> Capture` validates the format name and version,
unique paths, ROOT first, and that every non-ROOT record's parent exists and
was listed.

`ReplayBackend(capture)` implements `Backend`: it returns the stored record,
with `children` only if `should_list` accepts it. Replaying at a larger
max-depth than the capture's is an error raised up front.

**Writing.** Records are written to a temp file in the output's directory,
flushed and fsynced, then hard-linked to the output name. The link fails if
the output exists, so a capture never overwrites anything. The temp file is
removed in all cases.

## 10. Anonymization (`anonymize.py`)

`--anonymize KEYFILE` applies `anonymize(header, records, key)` before
writing, so real names never reach the disk. KEYFILE holds at least 32
random bytes, is a regular file, and must not be accessible by group or
others; the tool never creates it
(`(umask 077; head -c 32 /dev/urandom > KEYFILE)`). The same key yields the same tokens,
so a series of captures stays aligned.

| Data | Replacement |
|---|---|
| Path component | `HMAC-SHA256(key, b"name\0" + component)`, first 16 hex chars |
| uid, gid, euid | 0 stays 0; else `1 + int(HMAC(key, b"id\0" + str(n)) [:4 bytes]) % (2**32 - 2)` |
| `root` (header) | each component tokenized; leading `/` kept |
| `cluster_fsid` | `HMAC(key, b"fsid\0" + fsid)`, first 32 hex chars |
| `host` | dropped |
| everything else | kept, `anonymized: true` |

Hashing each component separately keeps the tree shape, and repeated names
stay recognizable as repeats. Records are re-sorted afterwards. Two siblings
mapping to the same token, or two ids mapping to the same fake id, is a
fatal error; with 64-bit and 32-bit tokens this is very unlikely.

Unkeyed hashes would be reversible by hashing guessed names; the key
prevents that. Lose the key and new captures no longer align with old ones.

## 11. CLI (`cli.py`)

```
trackdir capture ROOT --max-depth N -o FILE [--max-dirs 1000000] [--jobs 16] [--anonymize KEYFILE]
```

- Read-only on ROOT; writes only FILE.
- `--max-depth 0` captures ROOT only. `--max-depth` and `-o` are required.
- Checks before the walk, failing early: FILE must not exist; FILE's
  resolved parent directory must not be ROOT's resolved path or below it;
  KEYFILE readable and at least 32 bytes; ROOT a directory and
  not a symlink; ROOT on CephFS; openat2 policy. Then the rstat mode is
  detected.
- stderr: `WARNING:` for the openat2 fallback and for getxattr mode, once
  each; `ERROR:` for fatal errors; on success a one-paragraph summary (dirs,
  errors grouped by stage and code, elapsed time, output path). stdout: none.
- Exit codes: 0 success (per-dir errors included), 1 fatal error, 2 usage
  error, 130 interrupted.
- Deciding is separate from printing: `run_capture()` returns a typed
  `CaptureResult` (counts, errors by stage and code, elapsed time, mode
  flags), and `render_summary()` turns it into the stderr text. Tests assert
  on the result; format tests on the rendering.
- `--help` states what capture is for, that it is read-only on ROOT, that
  non-root captures are partial, and how to make a key file.
- Entry points: console script `trackdir`, and `python -m trackdir`.

## 12. Testing

pytest; no test needs Ceph or root. Code is checked with `ruff format` and
`ruff check`.

| Area | Tests |
|---|---|
| rstats parsing | Real raw strings including `4294967295.000000000`; dirstat text with padding; malformed values give `PARSE` |
| `SafeRoot`, both modes, on temp dirs | symlink as final or intermediate component gives ELOOP; mount crossing gives EXDEV (ROOT `/`, path `proc`, skipped if `/proc` is not a mount); file in place of a dir gives ENOTDIR; `.` or `..` components rejected |
| `LiveBackend` on temp dirs with an injected rstat reader | symlinks not listed; symlink or file swapped in gives ELOOP or ENOTDIR; deleted dir gives ENOENT; `chmod 000` gives EACCES with owner kept (skipped as root); non-UTF-8 names; `nlink == 2` skips readdir; readdir error discards partial children |
| Walker with fake backends | depth limit; `DirLimitExceeded` at exactly `max_dirs + 1`; identical output for `jobs=1` and `jobs=16`; probe exception aborts |
| Capture file | write, load, replay, walk gives the same records; replay at smaller depth equals depth filtering; larger depth rejected; non-UTF-8 round trip; existing output refused; failure leaves no file |
| Anonymization | stable for the same key, different for another key; tree shape preserved; uid 0 kept; sibling collision fails (forced with a fake hash) |
| Real captures | anonymized captures of `/mnt/ceph1` (pinned chains, EACCES dirs) and `/mnt/ceph2` in `tests/data/`; load succeeds; parent invariants hold; for listed dirs, child count equals `nlink - 2` except where a child has an error or the dir changed during capture |
| CLI | argument validation; exit codes; messages, compared with whitespace collapsed |

The anonymization key for fixtures lives outside the repository.

The tests cannot exercise dirstat reads together with readdir on CephFS;
the first real capture must be checked against the `nlink - 2` invariant
before fixtures are committed.

## 13. Project layout and documentation

- `pyproject.toml`: `requires-python = ">=3.12"`, no runtime dependencies,
  console script `trackdir`; ruff `target-version = "py312"`; pytest
  `pythonpath = ["src"]`, so tests run without installing into a virtual
  environment.
- `src/trackdir/` (section 4), `tests/`, `tests/data/` (anonymized captures).
- `README.md`: what trackdir does and how to run `capture`; search-friendly
  wording (CephFS, directory size tracking, rbytes, rctime). Links the two
  files below.
- `docs/terms.md`: nomenclature (ROOT, depth, probe, listed, capture,
  sample, rstats, rctime, pinned rctime, dirstat mode, getxattr mode, round
  trip).
- `docs/conventions.md`: project conventions (paths as bytes internally,
  integer nanoseconds for times, errno names for error codes, tree order).
- `.gitignore`: key files (`*.key`) and raw captures (`/captures/`).

## 14. Decisions recorded for the sampler and database

Agreed during brainstorming; to be designed in later specs.

- Store a dir when any monitored attribute differs from its last known
  state. Pinned dirs need no special store rule.
- The pruning baseline is the last known state of each dir (possibly many
  samples old), so the database needs a current-state view.
- Any rctime in the future acts as pinned, not only `4294967295`; the exact
  rule (e.g. `rctime > sample start + slack`) is open.
- Prune a subtree only if rctime, rbytes, rfiles and rsubdirs are all
  unchanged. In getxattr mode, whether rfiles and rsubdirs are monitored
  (two extra round trips per dir) is open.
- ENOENT during a sample means the dir was deleted.
- Open: how several instances share one database (overlapping ROOTs?);
  path column type in Postgres (`bytea` or escaped text); installing
  psycopg into a virtual environment needs the user's approval.

## 15. Risks and open items

- dirstat mode relies on undocumented kernel behavior; `DIRSTAT_STALE`
  detects a violation per dir.
- Only Python 3.14 and 3.15 are installed locally, so 3.12 compatibility is
  checked only by ruff's `target-version = "py312"` unless a 3.12
  interpreter (e.g. a container image) is approved.
- A probe stuck in a hung MDS call cannot be interrupted; Ctrl-C then waits
  for it.
- Captures taken as non-root are partial.
