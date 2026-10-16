# trackdir initial sample Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store a tree's first sample (every directory down to max depth) in PostgreSQL, either live from CephFS or replayed from a capture. Captures become format v2, which carries the tree key (cluster fsid, CephFS fs name, ROOT's path inside the filesystem).

**Architecture:** `mount.py` identifies ROOT's CephFS filesystem from `/proc/self/fdinfo` and `/proc/self/mountinfo`. `live.py` holds the ROOT inspection and live walk that `capture` and `sample` share. `db.py` owns the schema and SQL (psycopg 3, imported lazily). `sample.py` implements `trackdir sample` and `trackdir db init`. `cli.py` keeps the parser, `capture` and `main`.

**Tech Stack:** Python ≥ 3.12 stdlib; psycopg 3 (`psycopg[binary]`, optional extra `postgres`); PostgreSQL 18 dev container (`scripts/postgres.sh`); pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-16-trackdir-initial-sample-design.md` (read it first; this plan does not repeat its rationale). Background: `docs/superpowers/specs/2026-10-15-trackdir-capture-design.md`.

## Global Constraints

- `requires-python = ">=3.12"`, ruff `target-version = "py312"`: no 3.13+ APIs (use `dataclasses.replace`, not `copy.replace`).
- Runtime dependencies stay empty; `[project.optional-dependencies] postgres = ["psycopg[binary]>=3.2"]`. `import trackdir.cli` and every non-database test must work without psycopg.
- Precondition (needs the user's approval, see Task 5 step 1): `venv/bin/pip install pytest 'psycopg[binary]>=3.2'`. From then on, run tests with `venv/bin/python -m pytest` (the system `pytest` cannot see the venv's psycopg). Run ruff as `ruff format . && ruff check .`. Run the tool as `PYTHONPATH=src venv/bin/python -m trackdir`.
- Never write to `/mnt/ceph1` or `/mnt/ceph2` (production, read-only). Only Task 4 reads them, with the commands it gives. Never pass a path under them to `-o`.
- Do not `git commit` or `git push`. Each task ends by displaying a proposed commit subject and body that include the design choices and intentions (AGENTS.md).
- Paths are `bytes` internally and `bytea` in the database; header strings become bytes through `os.fsencode`. Times are integer nanoseconds; header times are ISO 8601 UTC strings, stored as `timestamptz`.
- Messages go to stderr as one wrapped paragraph each, prefixed `ERROR:` or `WARNING:`; stdout stays empty for `capture`, `sample` and `db init`. Exit codes: 0, 1 fatal, 2 usage, 130 interrupted, 128+N on SIGTERM/SIGHUP.
- No test needs Ceph, root, or a database unless it opts in through `TRACKDIR_TEST_DSN`. Tests compare messages with whitespace collapsed.
- PostgreSQL objects live in schema `trackdir`; `SCHEMA_VERSION = 1`; the DDL is spec §5, verbatim.

## Review Focus

1. **A tree whose fs name is NULL (ceph1) gets a second "initial" sample.** Expected: refused, before walking. `=` on NULL never matches. → Task 5 `test_second_sample_of_default_fs_tree_is_refused`, Task 6 `test_second_replay_fails_before_walking`.
2. **ROOT given as a relative path with `.` components and a trailing slash.** Expected: the same `fs_path` as the absolute path. → Task 3 `test_identity_uses_the_resolved_root`.
3. **Ctrl-C or a database error in the middle of COPY.** Expected: no `tree`, `sample` or `dir_state` row remains. → Task 5 `test_interrupt_during_copy_stores_nothing`, `test_out_of_range_value_stores_nothing`.
4. **The database is unreachable or the DSN is wrong.** Expected: one `ERROR:` paragraph and exit 1, not a traceback; a live run fails before walking. → Task 6 `test_unreachable_database_fails_before_walking`.
5. **`--replay` with `--max-depth` deeper than the capture, or a file that is not a v2 capture.** Expected: `ERROR:` and exit 1. → Task 6 `test_replay_deeper_than_capture_fails`, `test_replay_of_bad_capture_fails`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/trackdir/mount.py` | create | Parse mountinfo; compute `CephMount` for an fd |
| `src/trackdir/messages.py` | create | `FatalError`, `paragraph()`, `describe_errors()` shared by commands |
| `src/trackdir/live.py` | create | openat2 policy, ROOT inspection (`open_live_root`), `walk_within`, `walk_live`, defaults, messages moved from cli |
| `src/trackdir/db.py` | create | DDL, schema checks, tree key, rows, `store_initial_sample` |
| `src/trackdir/sample.py` | create | `run_sample`, `replay_walk`, `run_db_init`, summaries |
| `src/trackdir/model.py` | modify | add `list_above()`, `error_counts()` |
| `src/trackdir/capture.py` | modify | header v2 |
| `src/trackdir/anonymize.py` | modify | anonymize `fs_name`, `fs_path` |
| `src/trackdir/cli.py` | modify | use `live`; add `sample` and `db init` subcommands |
| `pyproject.toml` | modify | `postgres` extra |
| `tests/test_mount.py`, `tests/test_live.py`, `tests/test_db.py`, `tests/test_sample.py` | create | |
| `tests/conftest.py` | modify | `db` fixture (opt-in) |
| `tests/test_capture.py`, `test_anonymize.py`, `test_cli.py`, `test_fixtures.py` | modify | v2 header, moved patch targets, header-key test |
| `tests/data/*.jsonl.gz` | replace / add | v2 fixtures (Task 4) |
| `README.md`, `docs/terms.md`, `docs/conventions.md`, capture spec | modify | per spec §9 |

---

### Task 1: Identify ROOT's CephFS filesystem (`mount.py`)

**Files:**
- Create: `src/trackdir/mount.py`
- Test: `tests/test_mount.py`

**Interfaces:**
- Consumes: nothing from the project.
- Produces (`trackdir.mount`):
  - `@dataclass(frozen=True, slots=True) class MountEntry`: `mount_id: int`, `root: str` (field 4), `mount_point: str` (field 5), `fstype: str`, `source: str`, `super_options: tuple[str, ...]`. All strings are octal-unescaped, then `os.fsdecode`d.
  - `@dataclass(frozen=True, slots=True) class CephMount`: `fsid: str | None` (named by the mount; None if not named), `fs_name: str | None` (None: the cluster's default filesystem), `fs_path: str` (absolute and normalized; `"/"` is the filesystem root).
  - `class MountError(Exception)`: ROOT's mount is not an identifiable CephFS kernel mount. The message is a lowercase clause without a final period, so callers can embed it.
  - `parse_mountinfo_line(line: bytes) -> MountEntry`. Raises `MountError` on a malformed line.
  - `ceph_mount(entry: MountEntry, resolved_root: str) -> CephMount`. Raises `MountError`.
  - `fd_mount_id(fd: int) -> int`. Reads the `mnt_id:` line of `/proc/self/fdinfo/<fd>`.
  - `identify(fd: int, resolved_root: str) -> CephMount`. Finds the `/proc/self/mountinfo` line with `fd_mount_id(fd)` and applies `ceph_mount`. Raises `MountError` (no such line, not ceph) or `OSError`.

- [ ] **Step 1: Write the failing tests** in `tests/test_mount.py`. Use these lines (the real dev mounts, secrets elided):

```python
CEPH1 = (b"309 41 0:58 / /mnt/ceph1 ro,relatime shared:265 - ceph "
         b"192.0.2.192,192.0.2.191:/ ro,name=admin,secret=<hidden>,"
         b"fsid=11111111-2222-4333-8444-555555555555,dirstat,acl")
CEPH2 = (b"464 41 0:62 / /mnt/ceph2 ro,relatime shared:275 - ceph "
         b"admin@aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.default=/ "
         b"ro,name=admin,secret=<hidden>,ms_mode=prefer-crc,dirstat,acl,"
         b"mon_addr=192.0.2.219:3300/192.0.2.220:3300")
```

Tests, named, with `ceph_mount(parse_mountinfo_line(line), root)` unless noted:
- `test_parse_ceph1_line`: `mount_id == 309`, `root == "/"`, `mount_point == "/mnt/ceph1"`, `fstype == "ceph"`, `"dirstat" in super_options`.
- `test_old_syntax_without_fs_option_is_default_fs`: CEPH1 with root `/mnt/ceph1/proj/sub` gives `CephMount("11111111-2222-4333-8444-555555555555", None, "/proj/sub")`.
- `test_new_syntax`: CEPH2 with root `/mnt/ceph2` gives `CephMount("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "default", "/")`.
- `test_new_syntax_without_fsid`: source `admin@.cephfs=/projects`, mount point `/mnt/x`, root `/mnt/x/a` gives `CephMount(None, "cephfs", "/projects/a")`.
- `test_old_syntax_fs_options`, parametrized over `fs=other` and `mds_namespace=other`: `fs_name == "other"`; `fsid is None` when the options have no `fsid=`.
- `test_old_syntax_submount`: source `mon1:6789:/projects`, root `/mnt/x/a/b` gives `fs_path == "/projects/a/b"`.
- `test_bind_mount_root_field`: field 4 `/sub/dir`, source `mon1:/`, mount point `/mnt/b`, root `/mnt/b/c` gives `"/sub/dir/c"`.
- `test_octal_escapes`: mount point `/mnt/my\040ceph` parses to `/mnt/my ceph`; root `/mnt/my ceph/x` gives `"/x"`.
- `test_ipv6_monitors`: source `[2001:db8::1]:6789,[2001:db8::2]:6789:/vol` gives `fs_path == "/vol"` at the mount point.
- `test_mounted_at_slash`: mount point `/`, source `mon1:/`, root `/a` gives `"/a"`.
- `test_optional_fields_vary`: a line with zero and with two optional fields (`shared:1 master:2`) parses to the same fstype and source.
- `test_not_ceph_is_rejected`: fstype `fuse.ceph-fuse` raises `MountError`, whose message contains `fuse.ceph-fuse`.
- `test_root_outside_mount_point_is_rejected`: root `/mnt/other` for mount point `/mnt/x` raises `MountError`.
- `test_unparsable_source_is_rejected`: fstype `ceph`, source `nonsense` raises `MountError`.
- `test_malformed_line_is_rejected`: `b"1 2 3"` raises `MountError`.
- `test_fd_mount_id_matches_mountinfo` (real `/proc`): on an fd of `tmp_path`, the result is the id of some line of `/proc/self/mountinfo`.
- `test_identify_local_dir_is_not_ceph` (real `/proc`): `identify(fd, str(tmp_path))` raises `MountError`.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_mount.py -q` (or system `pytest` until Task 5's install). Expected: FAIL, module not found.

- [ ] **Step 3: Implement `src/trackdir/mount.py`** with the interfaces above and these rules:
  - Fields are split on `b" "`. Fields 1–6 are fixed, then optional fields up to a lone `b"-"`, then fstype, source and super options. Unescape `\NNN` octal in every field before `os.fsdecode`.
  - Parsing the ceph source, in this order:
    - New syntax: `re.fullmatch(r"(?:[^@]*@)?(?P<fsid>[0-9a-f-]*)\.(?P<name>[^=]+)=(?P<path>/.*)", source)`. An empty fsid means None.
    - Otherwise old syntax: the subpath starts at the first `":/"` (the `/` is kept), and the fs name is the value of super option `fs=` or `mds_namespace=`.
    - In both, an `fsid=` super option gives the fsid if the source did not.
  - `fs_path`: let `rel = posixpath.relpath(resolved_root, mount_point)`. If it is `..` or starts with `../`, raise `MountError`. Join the non-empty components of subpath, entry.root and rel (dropping `.`), then prefix one `/`. The result never starts with `//`.

- [ ] **Step 4: Run** the tests again. Expected: all pass. Then run `ruff format . && ruff check .`; expected clean.

- [ ] **Step 5: Propose commit.** Subject: `Add mount.py: identify ROOT's CephFS filesystem and path from mountinfo`. The body says why the cluster fsid is not enough: it names the cluster, not the filesystem or the mounted subdirectory. It also says why the mount id is taken from fdinfo: exact with stacked and bind mounts.

---

### Task 2: Capture format v2 (header carries the tree key)

**Files:**
- Modify: `src/trackdir/capture.py` (`CaptureHeader`, `VERSION`, `_header_from_json`)
- Modify: `src/trackdir/anonymize.py` (`anonymize`)
- Test: `tests/test_capture.py`, `tests/test_anonymize.py`

**Interfaces:**
- Produces: `VERSION = 2`. `CaptureHeader` fields, in this order: `root: str`, `max_depth: int`, `cluster_fsid: str`, `fs_name: str | None`, `fs_path: str`, then the rest unchanged (`rstat_mode` … `anonymized`). The JSON key order follows.
- `anonymize()` additionally maps `fs_name` to `an.name(os.fsencode(fs_name)).decode("ascii")` (None stays None) and `fs_path` to `an.abs_path(fs_path)`. `cluster_fsid` is always tokenized.

- [ ] **Step 1: Update the tests.**
  - In `tests/test_capture.py`, `HEADER` gains `fs_name=None, fs_path="/x"` and keeps `root="/mnt/cephfs/x"`. `header_json` writes `"version": 2`. Change the parametrized case `"version": 2` to `"version": 1`, still expecting `"version"`. Add these cases:
    - `{**header_json(), "cluster_fsid": None}` → `"bad header"`
    - `{**header_json(), "fs_path": None}` → `"bad header"`
    - `{**header_json(), "fs_path": "x"}` → `"bad header"`
    - `{**header_json(), "fs_name": 3}` → `"bad header"`
  - In `tests/test_anonymize.py`, `HEADER` gains `fs_name="default", fs_path="/proj/sub"` with `root="/mnt/ceph1/proj/sub"`. Add:
    - `test_fs_path_tokens_are_a_suffix_of_root`: `h.root.endswith(h.fs_path)` and `h.fs_path.count("/") == 2`.
    - `test_fs_name_is_tokenized_stably`: same key gives the same token, which has 16 hex chars and is not `"default"`.
    - `test_unnamed_fs_stays_none`: `fs_name=None` stays None.

- [ ] **Step 2: Run** `pytest tests/test_capture.py tests/test_anonymize.py -q`. Expected: FAIL (unexpected keyword `fs_name`).

- [ ] **Step 3: Implement.** In `_header_from_json`, after constructing the header, raise `CaptureFormatError("bad header: …")` unless:
  - `type(cluster_fsid) is str`
  - `type(fs_path) is str` and it starts with `/`
  - `fs_name is None` or `type(fs_name) is str`

  In `anonymize`, drop the `cluster_fsid is None` branch.

- [ ] **Step 4: Run** `pytest -q --ignore=tests/test_fixtures.py`. Expected: everything passes except `tests/test_cli.py` capture tests, which fail because `run_capture` does not yet fill `fs_name` and `fs_path`. Task 3 fixes those. `tests/test_fixtures.py` fails on the v1 fixtures until Task 4. List the failing tests in the report; they must be exactly these. Run ruff; expected clean.

- [ ] **Step 5: Propose commit.** Subject: `Capture format v2: header carries the tree key (fsid, fs name, fs path)`. The body says v1 is dropped by the user's decision (old captures had little value), and that every capture can now be replayed into the database.

---

### Task 3: Shared ROOT inspection and live walk (`live.py`); capture requires ROOT identity

**Files:**
- Create: `src/trackdir/messages.py`, `src/trackdir/live.py`
- Modify: `src/trackdir/model.py`, `src/trackdir/cli.py`
- Test: `tests/test_live.py` (create), `tests/test_cli.py`, `tests/test_model.py`

**Interfaces:**
- Consumes: `mount.identify`, `mount.CephMount`, `mount.MountError` (Task 1); `CaptureHeader` v2 (Task 2).
- Produces:
  - `trackdir.model`: `list_above(max_depth: int) -> ShouldList` (moved from `cli._list_above`); `error_counts(probes: Iterable[DirProbe]) -> Counter[tuple[str, str]]`, which maps (stage, code) to a number of dirs.
  - `trackdir.messages`: `class FatalError(Exception)` (moved from cli; `cli` re-imports it so `cli.FatalError` keeps working); `WRAP_WIDTH = 79`; `paragraph(text: str) -> str` (moved from `cli._paragraph`); `describe_errors(errors: Counter[tuple[str, str]]) -> str`, which returns `"none had errors"` or `"N had errors: open EACCES 3, …"` (sorted, the text `render_summary` builds today).
  - `trackdir.live`:
    - `DEFAULT_MAX_DIRS`, `DEFAULT_JOBS`, `MSG_NO_OPENAT2_AS_ROOT`, `MSG_NO_OPENAT2`, `MSG_NO_DIRSTAT` (moved from cli).
    - `@dataclass(frozen=True, slots=True) class LiveRoot`: `safe_root: SafeRoot`, `path: str` (resolved), `cluster_fsid: str`, `fs_name: str | None`, `fs_path: str`, `rstat_mode: RStatMode`, `openat2: bool`.
    - `@contextlib.contextmanager def open_live_root(path: Path, warn: Callable[[str], None]) -> Iterator[LiveRoot]`. It applies the openat2 policy, opens `SafeRoot`, then runs `detect_mode`, `cluster_fsid` and `identify(fd, resolved_path)`, and checks the fsid. It warns (openat2 fallback, getxattr mode) before yielding and raises `FatalError`.
    - `walk_within(backend: Backend, *, max_depth: int, max_dirs: int, jobs: int) -> list[DirProbe]`: `walk` with `list_above(max_depth)`; `DirLimitExceeded` becomes `FatalError` with capture's current message.
    - `@dataclass(frozen=True, slots=True) class LiveWalk`: `header: CaptureHeader`, `probes: list[DirProbe]`, `elapsed: float`.
    - `walk_live(live: LiveRoot, *, max_depth: int, max_dirs: int, jobs: int) -> LiveWalk`: builds `LiveBackend`, walks, and builds the header as `run_capture` does today, plus `fs_name` and `fs_path`, with `anonymized=False`.
  - `cli.run_capture` and `CaptureResult` keep their signatures. `CaptureResult` gains nothing.

New fatal messages (exact copy, in `live.py`; `{path}` is ROOT as given):
- fsid None: `f"ROOT {path} reports no Ceph cluster fsid (ceph.cluster_fsid), so trackdir cannot tell which cluster it belongs to."`
- `MountError e`: `f"cannot identify the CephFS filesystem of ROOT {path}: {e}. trackdir needs the CephFS kernel client."`
- fsid mismatch: `f"ROOT {path} reports cluster fsid {xattr_fsid}, but its mount names {mount_fsid}. Check the mount."`

- [ ] **Step 1: Write and update tests.**
  - `tests/test_model.py`: `test_list_above` (depth 0 and 1 against `max_depth=1`); `test_error_counts`.
  - `tests/test_cli.py`: patch targets move from `cli` to `live` (`detect_mode`, `cluster_fsid`, `read_rstats`, `walk`, `openat2_supported`). `fake_ceph` also patches `live.identify` with `lambda fd, root: CephMount(None, None, "/x")`. `test_capture_writes_the_file_and_reports_counts` additionally asserts `header.fs_name is None and header.fs_path == "/x"`. No other assertion changes.
  - Move `collapse`, `must_not_walk` and the `tree` and `fake_ceph` fixtures from `tests/test_cli.py` to `tests/conftest.py`; Tasks 3 and 6 share them. `fake_ceph` patches `live.*` as above.
  - `tests/test_live.py`:
    - `test_missing_fsid_is_fatal` (`cluster_fsid` returns None; message contains `ceph.cluster_fsid`)
    - `test_unidentifiable_mount_is_fatal` (`identify` raises `MountError("filesystem type is ext4, not ceph")`; message contains `kernel client`)
    - `test_fsid_mismatch_is_fatal` (mount names `"other"`, xattr `"fsid"`)
    - `test_identity_uses_the_resolved_root`: ROOT given as a relative path with `./` and a trailing slash (`monkeypatch.chdir(tree.parent)`, path `Path("./root/")`); the fake `identify` records its `root` argument, which equals `str(tree.resolve())`.
    - `test_live_root_carries_identity` (fields copied from the fakes).
    - In `tests/test_cli.py`: `test_capture_fails_before_walking_without_identity`, with `walk` patched to `must_not_walk` and `identify` raising; exit 1 and no output file.

- [ ] **Step 2: Run** `pytest tests/test_live.py tests/test_cli.py tests/test_model.py -q`. Expected: FAIL (no module `trackdir.live`).

- [ ] **Step 3: Implement.** Move the code; don't copy it. `run_capture` becomes: `_check_output`, load key, `with open_live_root(opts.root, warn) as live:`, `_create_writer`, `walk_live`, anonymize, commit. The order of checks and all existing messages stay the same. `render_summary` uses `describe_errors`. `cli.py` keeps `_warn`, signal handling, `_check_output`, `_load_key`, `_create_writer`, `_anonymize`, `_commit` and the parser.

- [ ] **Step 4: Run** `pytest -q --ignore=tests/test_fixtures.py`. Expected: all pass. Then run ruff; expected clean.

- [ ] **Step 5: Propose commit.** Subject: `Share ROOT inspection and live walk between commands; capture requires ROOT identity`. The body says why the split into open-and-inspect plus walk: capture creates its writer, and sample runs its database pre-check, between the two. It also says why capture now fails without an identity: every capture must be replayable into the database.

---

### Task 4: Retake the real fixtures as v2

Reads production mounts read-only. Run as uid 1000. If `/mnt/ceph1` or `/mnt/ceph2` is not mounted, stop and report to the user; do not improvise fixtures.

**Files:**
- Replace: `tests/data/ceph1-depth3.jsonl.gz`, `tests/data/ceph2-depth3.jsonl.gz`
- Create: `tests/data/ceph1-sub-depth4.jsonl.gz`
- Modify: `tests/test_fixtures.py`

**Interfaces:**
- Consumes: the `trackdir capture` CLI from Task 3.
- Produces: three v2 anonymized fixtures, which Tasks 5 and 6 use. Tasks 5–6 refer to `tests/data/ceph1-sub-depth4.jsonl.gz` as `SUBTREE`.

- [ ] **Step 1: Check the key.** Run `test -s .keys/fixtures.key && stat -c %a .keys/fixtures.key`. Expected: `600`. Never create or regenerate it; if it is missing, stop and report.

- [ ] **Step 2: Capture** into `captures/` (gitignored; capture never overwrites):

```bash
K=.keys/fixtures.key
t() { PYTHONPATH=src venv/bin/python -m trackdir capture "$@"; }
t /mnt/ceph1 --max-depth 3 --max-dirs 5000 --anonymize $K -o captures/ceph1-depth3.jsonl.gz
t /mnt/ceph2 --max-depth 3 --max-dirs 5000 --anonymize $K -o captures/ceph2-depth3.jsonl.gz
t /mnt/ceph1/proj/sub --max-depth 4 --max-dirs 100000 --jobs 32 --anonymize $K \
    -o captures/ceph1-sub-depth4.jsonl.gz
t /mnt/ceph2 --max-depth 0 -o captures/ceph2-depth0-raw.jsonl.gz
```

Expected: four `Captured …` summaries and no `WARNING:`. The previous fixtures had 3,184 and 242 dirs; the subtree had about 72k dirs and 41 `open EACCES`.

- [ ] **Step 3: Check identity.** Run `zcat FILE | head -1 | jq '{root, cluster_fsid, fs_name, fs_path}'` on each file.
  - The raw ceph2 file has `fs_name "default"`, `fs_path "/"` and `cluster_fsid "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"`.
  - The the subtree file has `fs_name null`, and `fs_path` equals the last two `/`-components of `root`.
  - The ceph1 files have `fs_name null`, and `fs_path` is `/` for depth3.

  Delete `captures/ceph2-depth0-raw.jsonl.gz` once checked.

- [ ] **Step 4: Update `tests/test_fixtures.py`.** Set `DATA = Path(os.environ.get("TRACKDIR_FIXTURES_DIR", Path(__file__).parent / "data"))`, so new captures can be checked before they replace the old ones. Add `test_headers_carry_tree_key`, which asserts `len(header.cluster_fsid) == 32`, `header.fs_path.startswith("/")`, and `header.fs_path == "/" or header.root.endswith(header.fs_path)`.

- [ ] **Step 5: Validate before replacing.** Run `TRACKDIR_FIXTURES_DIR=captures pytest tests/test_fixtures.py -q`. Expected: all pass, including `test_fixtures_cover_pinned_rctime_and_permission_errors`. Then `mv captures/*.jsonl.gz tests/data/`.

- [ ] **Step 6: Run** `pytest -q`. Expected: all pass. Run `git status --short tests/data`. Expected: two modified and one new file, the new one about 2 MB.

- [ ] **Step 7: Propose commit.** Subject: `Retake real fixtures as capture v2; add subtree depth-4 fixture`. The body states the commands, that the fixtures key is unchanged (tokens line up with the old fixtures), and that the dev mounts are temporary.

---

### Task 5: Database module (`db.py`)

**Files:**
- Modify: `pyproject.toml` (extra `postgres`)
- Create: `src/trackdir/db.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `CaptureHeader` v2, `DirProbe`, `load_capture`, `SUBTREE`.
- Produces (`trackdir.db`):
  - `SCHEMA = "trackdir"`; `SCHEMA_VERSION = 1`; `DDL: str` (spec §5 verbatim, including the view); `DIR_STATE_COLUMNS: tuple[str, ...]` = `("sample_id", "path", "ino", "mode", "uid", "gid", "nlink", "rctime", "rbytes", "rfiles", "rsubdirs", "error_stage", "error_code")`.
  - Exceptions: `class DbError(Exception)`, the base, which also wraps any `psycopg.Error` raised inside this module's functions (message `str(e)`, chained). Its subclasses:
    - `PsycopgMissing(DbError)`
    - `SchemaMissing(DbError)`
    - `SchemaVersionMismatch(DbError)`, with attribute `found: int | None` (None: schema without a meta row)
    - `SamplesExist(DbError)`, with attribute `key: TreeKey`
  - `@dataclass(frozen=True, slots=True) class TreeKey`: `cluster_fsid: str`, `fs_name: str | None`, `fs_path: bytes`. Also `tree_key_of(header: CaptureHeader) -> TreeKey` (not `tree_key`: that name is `model.tree_key`, the sort key).
  - `dir_state_row(probe: DirProbe) -> tuple`: the values for `DIR_STATE_COLUMNS[1:]`. The path stays bytes, the error is split into `str(stage)` and code, and None stays None.
  - `connect(dsn: str) -> "psycopg.Connection"`: psycopg imported here, inside the function; `ImportError` raises `PsycopgMissing`. The connection uses `autocommit=True`; functions open their own `conn.transaction()`.
  - `init_schema(conn) -> bool`: True if created, False if already at `SCHEMA_VERSION`. Raises `SchemaVersionMismatch`.
  - `check_schema(conn) -> None`: raises `SchemaMissing` or `SchemaVersionMismatch`.
  - `has_samples(conn, key: TreeKey) -> bool`.
  - `store_initial_sample(conn, header: CaptureHeader, probes: Iterable[DirProbe], replayed_from: str | None) -> int`: one transaction, returning the new sample id. Raises `SamplesExist`.
- `tests/conftest.py` fixture `db`:
  - If `TRACKDIR_TEST_DSN` is unset, skip.
  - Otherwise `pytest.importorskip("psycopg")`.
  - Then `pytest.fail` unless `psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]` ends with `_test`.
  - Run `DROP SCHEMA IF EXISTS trackdir CASCADE` and yield `connect(dsn)`; drop again after.

  Test helper in `tests/test_db.py`: `read_sample(conn, sample_id) -> list[DirProbe]` (tree order, `children=None`).

SQL rules (from the spec, easy to get wrong):
- Every tree lookup is `cluster_fsid = %s AND fs_name IS NOT DISTINCT FROM %s AND fs_path = %s`.
- Store steps:
  1. `INSERT INTO trackdir.tree (…) VALUES (…) ON CONFLICT DO NOTHING`
  2. `SELECT id … FOR UPDATE`
  3. check for an existing `sample` with that `tree_id`
  4. `INSERT INTO trackdir.sample (…) RETURNING id`, with `root = os.fsencode(header.root)`, `started` and `finished` from `datetime.fromisoformat`, `dirs` and `errors` from the header
  5. `COPY trackdir.dir_state (DIR_STATE_COLUMNS) FROM STDIN`, `write_row((sample_id, *dir_state_row(p)))`
- Schema detection: `to_regnamespace('trackdir')` and `to_regclass('trackdir.meta')`.

- [ ] **Step 1: Install** (precondition, user-approved): `venv/bin/pip install pytest 'psycopg[binary]>=3.2'`. Expected: psycopg 3.3.x and psycopg-binary install. Add the `postgres` extra to `pyproject.toml`.

- [ ] **Step 2: Write tests in `tests/test_db.py`.**

  Pure tests, which run without a database:
  - `test_dir_state_row_full`
  - `test_dir_state_row_missing_fields_are_none`
  - `test_dir_state_row_splits_error` (`ProbeError(Stage.OPEN, "EACCES")` gives `"open"`, `"EACCES"`)
  - `test_dir_state_row_keeps_non_utf8_path` (`b"caf\xe9"`)
  - `test_tree_key_of_encodes_fs_path`
  - `test_connect_without_psycopg`: `monkeypatch.setitem(sys.modules, "psycopg", None)` makes `connect("x")` raise `PsycopgMissing`.

  Tests that use the `db` fixture:
  - `test_init_creates_and_is_idempotent` (True, then False; meta has 1)
  - `test_init_rejects_other_version` (`UPDATE trackdir.meta SET schema_version = 99` gives `found == 99`)
  - `test_init_rejects_schema_without_meta` (`CREATE SCHEMA trackdir` only; `found is None`)
  - `test_check_schema_missing`
  - `test_store_round_trip`: `SUBTREE` loaded; `store_initial_sample(conn, c.header, list(c.probes.values()), "/abs/sub")`; then:
    - `read_sample` equals the probes with `children=None`
    - the sample row's `started`/`finished` equal the header's
    - `dirs` and `errors` equal the header's
    - `replayed_from == "/abs/sub"`
    - `SELECT count(*) FROM trackdir.current_dir_state` equals `len(c.probes)`
  - `test_second_sample_of_default_fs_tree_is_refused`: header with `fs_name=None` stored twice; the second raises `SamplesExist`, and `has_samples` is True.
  - `test_trees_differ_by_fs_name_and_path`: three headers (`None`, `"a"`, and `fs_path` `/other`) are all stored without error, giving three trees.
  - `test_awkward_values_round_trip`: path `b"caf\xe9"`, `rctime = 4294967295 * 10**9`, `ino = 2**63 - 1`.
  - `test_out_of_range_value_stores_nothing`: a probe with `rbytes = 2**63` raises `DbError`, and the `tree`, `sample` and `dir_state` counts are all 0.
  - `test_interrupt_during_copy_stores_nothing`: `probes` is a generator that raises `KeyboardInterrupt` after 10 probes; `KeyboardInterrupt` propagates, and all counts are 0.

- [ ] **Step 3: Run without a database.** `venv/bin/python -m pytest tests/test_db.py -q`. Expected: the pure tests FAIL (no module) and the DB tests are skipped.

- [ ] **Step 4: Implement `src/trackdir/db.py`.**

- [ ] **Step 5: Run with the database.**
  - `scripts/postgres.sh start --ephemeral`
  - `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest tests/test_db.py -q`. Expected: all pass, none skipped.
  - Then `venv/bin/python -m pytest -q` with the variable unset. Expected: all pass, DB tests skipped.
  - Run ruff; expected clean.

- [ ] **Step 6: Propose commit.** Subject: `Add db.py: PostgreSQL schema and atomic initial-sample store`. The body covers:
  - bytea paths
  - `NULLS NOT DISTINCT` and `IS NOT DISTINCT FROM` for the default-fs NULL
  - the row lock that serializes concurrent initial samples
  - COPY in one transaction
  - the lazy import that keeps capture dependency-free

---

### Task 6: `trackdir sample` and `trackdir db init`

**Files:**
- Create: `src/trackdir/sample.py`
- Modify: `src/trackdir/cli.py` (parser, `main` dispatch)
- Modify: `README.md`, `docs/terms.md`, `docs/conventions.md`, `docs/superpowers/specs/2026-10-15-trackdir-capture-design.md` (amendment note under the Status line: format v2 and the ROOT identity requirement, pointing to the new spec)
- Test: `tests/test_sample.py`

**Interfaces:**
- Consumes: `live.open_live_root`, `live.walk_live`, `live.walk_within`, `live.DEFAULT_*`; `messages.*`; `db.*`; `capture.load_capture`, `ReplayBackend`, `CaptureFormatError`.
- Produces (`trackdir.sample`):
  - `DSN_ENV = "TRACKDIR_DB"`.
  - `@dataclass(frozen=True, slots=True) class SampleOptions`: `dsn: str`, `root: Path | None = None`, `replay: Path | None = None`, `max_depth: int | None = None` (required when live; replay defaults to the capture's), `max_dirs: int = DEFAULT_MAX_DIRS`, `jobs: int = DEFAULT_JOBS`.
  - `@dataclass(frozen=True, slots=True) class SampleResult`: `sample_id: int`, `root: str`, `cluster_fsid: str`, `fs_name: str | None`, `fs_path: str`, `max_depth: int`, `dirs: int`, `errors: Counter[tuple[str, str]]`, `walk_seconds: float`, `store_seconds: float`, `replayed_from: str | None`.
  - `replay_walk(capture: Capture, max_depth: int, max_dirs: int) -> tuple[CaptureHeader, list[DirProbe]]`. It uses `walk_within(ReplayBackend(capture, max_depth), …, jobs=1)`. The header is `replace(capture.header, max_depth=…, dirs=len(probes), errors=<dirs with error>)`.
  - `run_sample(opts: SampleOptions, warn: Callable[[str], None]) -> SampleResult`. It raises `FatalError`; `KeyboardInterrupt` propagates.
  - `render_sample_summary(result: SampleResult) -> str`.
  - `run_db_init(dsn: str) -> bool`, which returns whether the schema was created. Also `render_db_init(created: bool) -> str`.

`run_sample` order:
1. **Live:**
   1. `with open_live_root(...)`
   2. pre-check: `connect` → `check_schema` → `has_samples(key)` with `key = TreeKey(live.cluster_fsid, live.fs_name, os.fsencode(live.fs_path))`
   3. `walk_live`
   4. a new `connect` → `store_initial_sample(..., replayed_from=None)`

   **Replay:**
   1. `load_capture`
   2. depth check (`ReplayBackend` raises `ValueError` → `FatalError`)
   3. the same pre-check with `tree_key_of(header)`
   4. `replay_walk`
   5. store with `replayed_from=str(opts.replay.resolve())`
2. `DbError` subclasses become `FatalError` with this exact copy:
   - `PsycopgMissing`: `"the PostgreSQL driver psycopg is not installed. Install it with: pip install 'trackdir[postgres]'"`
   - `SchemaMissing`: `"the database has no trackdir schema. Create it with: trackdir db init"`
   - `SchemaVersionMismatch`: `f"the database's trackdir schema is version {found}, but this trackdir needs version {SCHEMA_VERSION}"` (`found` None → `"incomplete (no version recorded)"`)
   - `SamplesExist`: `f"the tree (cluster {fsid}, {fs_name or 'default filesystem'}, path {fs_path}) already has samples. This version of trackdir stores only a tree's first sample; incremental sampling is not implemented yet"`
   - other `DbError`: `f"database error: {e}. Nothing was stored"`
   - `CaptureFormatError` or `OSError` on the capture: `f"cannot read the capture {path}: {e}"`

CLI (`cli.py`):
- `sample [ROOT] [--replay CAPTURE] [--max-depth N] [--max-dirs N] [--jobs N] [--db DSN]`.
- `db init [--db DSN]`, as a nested subparser `db` with the command `init`.
- `--max-depth`, `--max-dirs`, `--jobs` and `--db` are defined once, by helper functions shared by the subparsers. `--jobs` uses `default=None`; the effective default is applied after parsing.
- Usage errors (`parser.error`, exit 2):
  - both or neither of ROOT and `--replay`
  - `--jobs` with `--replay`
  - live without `--max-depth`
  - no DSN: `"no database given: pass --db DSN or set TRACKDIR_DB"`
- Interrupt and signal messages say `"nothing was stored"` for `sample`, keeping `"no capture was written"` for capture. One dispatch path in `main`, parameterized by that phrase.
- `--help` text for `sample` (exact):

```
Store the first sample of a tree in PostgreSQL: every directory under ROOT,
down to --max-depth, with owner, mode, nlink, CephFS rstats and
per-directory errors. With --replay, read the directories from a capture
instead of CephFS. Only a tree's first sample is supported so far.

Read-only on ROOT; writes to the database given by --db or TRACKDIR_DB.
The database schema must exist: run `trackdir db init` once.
```

  The epilog covers: run as root for a complete sample; the tree is (cluster fsid, CephFS filesystem, path inside it); put passwords in `~/.pgpass` or `PGPASSWORD`, not in the DSN; exit codes. `db init` help: `"Create trackdir's tables in the database given by --db or TRACKDIR_DB. Changes the database; safe to repeat."`
- Summary (stderr):
  - `f"Stored sample {id}: {dirs} directories under {root} to depth {max_depth}, tree (cluster {fsid}, {fs_name or 'default filesystem'}, path {fs_path}); {describe_errors(errors)}. Walk {w:.1f} s, store {s:.1f} s."`
  - Replay adds `f" Replayed from {replayed_from}."`
  - db init: `"Created the trackdir schema (version 1) in the database."` or `"The trackdir schema (version 1) already exists; nothing changed."`

- [ ] **Step 1: Write tests in `tests/test_sample.py`.**

  Without a database:
  - `test_replay_walk_at_capture_depth` (SUBTREE: probes equal the capture's, minus children; `dirs` and `errors` equal the header's)
  - `test_replay_walk_at_smaller_depth` (depth 2: every probe has depth ≤ 2; `dirs == len(probes)`; `errors` recomputed)
  - `test_usage_errors`, parametrized over the four usage errors above: `main([...])` raises `SystemExit(2)`, and stderr names the problem
  - `test_dsn_from_environment` (`monkeypatch.setenv("TRACKDIR_DB", "x")`, with `sample.run_sample` patched to capture `opts.dsn`)
  - `test_psycopg_missing_message` (`sys.modules["psycopg"] = None`; exit 1; message contains `trackdir[postgres]`)
  - `test_replay_deeper_than_capture_fails` (`--max-depth 5` on SUBTREE; exit 1; message contains `depth`)
  - `test_replay_of_bad_capture_fails` (a gzip file with `{"format":"x"}`; exit 1; message contains `cannot read the capture`)
  - `test_render_sample_summary` (whitespace collapsed; `default filesystem` when `fs_name` is None)

  Needing psycopg but no server:
  - `test_unreachable_database_fails_before_walking` (DSN `postgresql://trackdir@127.0.0.1:1/x_test`; live path with the `fake_ceph` fakes and `live.walk` patched to `must_not_walk`; exit 1; one `ERROR:` paragraph containing `database error`, no traceback)

  With the `db` fixture:
  - `test_db_init_cli` (exit 0 twice; messages `Created`, then `already exists`)
  - `test_sample_without_schema_fails` (message contains `trackdir db init`)
  - `test_replay_stores_initial_sample` (`run_sample` on SUBTREE: `result.dirs` equals the header's; `replayed_from` is absolute; `SELECT count(*) FROM trackdir.dir_state` equals `dirs`)
  - `test_second_replay_fails_before_walking` (second `run_sample` with `sample.replay_walk` patched to `must_not_walk`; message contains `incremental`)
  - `test_live_sample_with_fakes` (fake live ROOT from `fake_ceph`; stored tree's `fs_path` is `b"/x"`, sample `replayed_from` is NULL)

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_sample.py -q`. Expected: FAIL (no module).

- [ ] **Step 3: Implement** `sample.py` and the CLI changes.

- [ ] **Step 4: Update docs** (spec §9):
  - **README:**
    - status line
    - a "Sample into PostgreSQL" section with `db init` and `sample`, live and `--replay` examples
    - the `trackdir[postgres]` extra
    - `TRACKDIR_DB`, and the password note
    - the capture header fields `cluster_fsid`, `fs_name`, `fs_path`, and the requirement for the kernel client
    - under Development: `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest`, and that the test database name must end in `_test`
  - **`docs/terms.md`:**
    - tree
    - filesystem path (`fs_path`)
    - fs name (NULL = the cluster's default filesystem)
    - sample (drop "Planned")
    - initial sample
    - dir state
    - current state
  - **`docs/conventions.md`:**
    - paths are `bytea` in the database
    - DB tests opt in with `TRACKDIR_TEST_DSN`
    - database errors are `db.DbError` subclasses, turned into messages by `sample.py`

- [ ] **Step 5: Run.**
  - With the DB: `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest -q`. Expected: all pass.
  - Without it: `venv/bin/python -m pytest -q`. Expected: all pass, DB tests skipped.
  - `ruff format . && ruff check .`; expected clean.

- [ ] **Step 6: Propose commit.** Subject: `Add trackdir sample and db init: store a tree's first sample in PostgreSQL`. The body covers:
  - the pre-check before walking (fail early)
  - no connection held through the walk
  - replay keeps the capture's times and provenance
  - only the first sample, refused otherwise

---

### Task 7: Manual acceptance against the dev container

No code changes unless a check fails. If one fails, fix it in the owning task's files and rerun its tests.

- [ ] **Step 1:** `scripts/postgres.sh start --ephemeral`, then `export TRACKDIR_DB=$(scripts/postgres.sh url)`.
- [ ] **Step 2:** `PYTHONPATH=src venv/bin/python -m trackdir db init`. Expected: `Created the trackdir schema (version 1)…`, exit 0. Running it again gives `already exists`, exit 0.
- [ ] **Step 3:** `PYTHONPATH=src venv/bin/python -m trackdir sample --replay tests/data/ceph1-sub-depth4.jsonl.gz`. Expected: `Stored sample 1: N directories … default filesystem …`, with N equal to the header's `dirs`.
- [ ] **Step 4:** Check with `scripts/postgres.sh psql -c "…"`:
  - `SELECT count(*), count(error_code) FROM trackdir.dir_state` equals the header's `dirs` and `errors`.
  - `SELECT started, finished, replayed_from FROM trackdir.sample` gives the capture's times and an absolute path.
  - `SELECT cluster_fsid, fs_name, convert_from(fs_path, 'UTF8') FROM trackdir.tree` gives the ceph1 fsid token, NULL, and `/<token>/<token>`.
  - `SELECT count(*) FROM trackdir.current_dir_state` equals `dirs`.
- [ ] **Step 5:** Repeat Step 3. Expected: `ERROR: … already has samples … incremental sampling is not implemented yet`, exit 1, and the sample count is still 1.
- [ ] **Step 6:** `scripts/postgres.sh stop`. Report the observed numbers to the user. Propose no commit; this task changes nothing.
