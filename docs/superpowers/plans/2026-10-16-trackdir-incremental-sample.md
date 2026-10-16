# trackdir incremental sample Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trackdir sample` on a tree that already has samples reads the stored state as a baseline, skips unchanged subtrees, and stores only changed directories plus deletion records.

**Architecture:** A new pure module `baseline.py` decides what to list (`should_list_changed`) and what to store (`diff`). `db.py` moves to schema version 2 (tree identity gains `max_depth` and `max_dirs`, `dir_state.deleted`, a new view), loads the baseline, and stores a sample with stale-baseline and order guards. `sample.py` wires pre-check, pruned walk, diff and store; live and replay share one path. The walker is unchanged: pruning is a `ShouldList` callback.

**Tech Stack:** Python ≥ 3.12 stdlib; psycopg 3 (optional extra); PostgreSQL 18 dev container; pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-16-trackdir-incremental-sample-design.md` (read it first; this plan does not repeat its rationale). Background: the initial-sample spec and plan in the same directories.

## Global Constraints

- `requires-python = ">=3.12"`, ruff `target-version = "py312"`; no 3.13+ APIs. No new dependencies. `import trackdir.cli` and every non-database test must work without psycopg.
- Run tests as `venv/bin/python -m pytest`; database tests need `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test)` with the container up (`scripts/postgres.sh start --ephemeral`). Run `ruff format . && ruff check .` before each commit proposal. Run the tool as `PYTHONPATH=src venv/bin/python -m trackdir`.
- Never write to `/mnt/ceph1` or `/mnt/ceph2`; this plan needs neither. Tests use only `tests/data/*.jsonl.gz`.
- Do not `git commit` or `git push`. Each task ends by displaying a proposed commit subject and body that include the design choices and the intentions behind them (AGENTS.md.d/GIT-v1.md).
- The repository is public: no real fsids, IPs or mount paths in code, tests, docs or commit messages; placeholders only.
- `SCHEMA_VERSION = 2`. Paths are `bytes` internally and `bytea` in the database. A directory's *row* is the 11 values `ROW_COLUMNS` (below); a directory is *unchanged* iff its probe's row equals its baseline row.
- A tree is `(cluster_fsid, fs_name, fs_path, max_depth, max_dirs)`; `fs_name` NULL is a value (`IS NOT DISTINCT FROM`).
- Messages go to stderr as one wrapped paragraph each; stdout stays empty. Exit codes unchanged (0, 1, 2, 130, 128+N). Tests compare messages with whitespace collapsed.
- Comments and docstrings stay in step with the code; the initial-sample docstrings that say "first sample" or "initial" are updated where the behavior changes.

## Review Focus

1. **A directory deleted while the walk runs** (listed by its parent, then `open ENOENT`). Expected: a tombstone, no error row, no crash. → Task 2 `test_open_enoent_tombstones_without_error_row`, Task 4 `test_deleted_subtree_is_tombstoned_and_hidden`.
2. **A non-root sample followed by a root sample.** Expected: the root run restores the directories the non-root run could not read, even though their parents are unchanged. → Task 2 `test_error_baseline_forces_listing_of_ancestors`, Task 4 `test_error_directory_is_retried_when_readable`.
3. **Two samplers of one tree, or a replay of an older capture.** Expected: the loser fails with `ERROR:` and stores nothing. → Task 3 `test_stale_baseline_is_refused`, Task 4 `test_concurrent_sample_is_refused_at_store`, `test_replay_dated_before_latest_sample_is_refused_before_walking`.
4. **A directory with `nlink == 2` whose `rsubdirs` says it has subdirectories.** Expected: it is read, so its subdirectories are not tombstoned. → Task 1.
5. **The same ROOT with another `--max-depth` or `--max-dirs`.** Expected: a different tree with a full initial sample; the dir-limit message says so. → Task 3 `test_trees_differ_by_depth_and_max_dirs` and `test_dir_limit_message_names_the_tree_consequence`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/trackdir/probe.py` | modify | `nlink == 2` shortcut guarded by `rsubdirs` |
| `src/trackdir/baseline.py` | create | `ROW_COLUMNS`, `row_of`, `Baseline`, `should_list_changed`, `SampleDiff`, `diff` (pure, no I/O) |
| `src/trackdir/db.py` | modify | schema v2, `TreeKey` with depth and dirs, `load_baseline`, `store_sample`, `check_order`, conflict errors; drops `store_initial_sample`, `has_samples`, `SamplesExist` |
| `src/trackdir/live.py` | modify | `should_list` and `limit_advice` parameters on `walk_within`/`walk_live` |
| `src/trackdir/sample.py` | modify | pre-check loads baseline, pruned walk, diff, store; result and summary; messages |
| `src/trackdir/cli.py` | modify | `sample` help text only |
| `tests/test_probe.py`, `tests/test_baseline.py` (new), `tests/test_db.py`, `tests/test_live.py`, `tests/test_sample.py`, `tests/test_incremental.py` (new), `tests/conftest.py` | modify/create | per task |
| `README.md`, `docs/terms.md`, `docs/conventions.md`, both earlier specs | modify | spec §9 |

---

### Task 1: Trust the `nlink == 2` shortcut only when `rsubdirs` agrees

**Files:**
- Modify: `src/trackdir/probe.py` (`LiveBackend._probe_open`)
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: existing `LiveBackend`, `NLINK_NO_SUBDIRS`.
- Produces: no new names. Behavior: `children=()` without a readdir only if `probe.nlink == NLINK_NO_SUBDIRS` and `probe.rsubdirs in (None, 1)` (`rsubdirs` counts the directory itself; None means the rstats read failed).

- [ ] **Step 1: Write the failing tests** in `tests/test_probe.py`. The existing `test_nlink_two_means_no_subdirs_without_readdir` uses `FAKE_RSTATS` with `rsubdirs=3`, which now contradicts nlink 2: change it to use a backend whose `read_rstats` returns `replace(FAKE_RSTATS, rsubdirs=1)`. Add:
  - `test_nlink_two_with_subdirs_in_rstats_is_read`: `set_nlink(monkeypatch, 2)`, rstats `rsubdirs=3`, and `monkeypatch.setattr(probe_module, "_subdir_names", recorder)` where `recorder(fd)` appends to a list and returns `(b"x",)`. Assert the recorder was called once and `probe.children == (b"x",)`.
  - `test_nlink_two_with_failed_rstats_still_skips_readdir`: `set_nlink(monkeypatch, 2)`, `read_rstats=failing_read_rstats`; the recorder is never called and `children == ()` (as before the change).

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_probe.py -q`. Expected: the first new test FAILS (recorder not called).

- [ ] **Step 3: Modify `_probe_open`** so the shortcut condition is `probe.nlink == NLINK_NO_SUBDIRS and probe.rsubdirs in (None, 1)`. Extend the comment at `NLINK_NO_SUBDIRS`: a contradicting `rsubdirs` means a wrong empty listing would tombstone a subtree, so the directory is read.

- [ ] **Step 4: Run** the whole suite (`venv/bin/python -m pytest -q`) and ruff. Expected: pass, clean (the fixtures never trigger the guard: 34,933 directories with nlink 2, all `rsubdirs == 1`).

- [ ] **Step 5: Propose commit.** Subject: `probe: read a directory whose rsubdirs contradicts nlink == 2`. Body: incremental samples turn "absent from a complete listing" into a deletion, so a false empty listing would tombstone a subtree; the guard costs no extra reads on the fixtures.

---

### Task 2: The baseline: pruning predicate and diff (`baseline.py`)

**Files:**
- Create: `src/trackdir/baseline.py`
- Test: `tests/test_baseline.py`

**Interfaces:**
- Consumes: `trackdir.model` (`DirProbe`, `ProbeError`, `Stage`, `ShouldList`, `ROOT`, `join`, `split`, `tree_key`).
- Produces (`trackdir.baseline`):
  - `ROW_COLUMNS = ("ino", "mode", "uid", "gid", "nlink", "rctime", "rbytes", "rfiles", "rsubdirs", "error_stage", "error_code")`; `type Row = tuple[int | str | None, ...]`; `row_of(probe: DirProbe) -> Row` (the 11 values in that order; `error_stage` as `str`).
  - `@dataclass(frozen=True, slots=True) class Baseline`: `sample_id: int | None` (the tree's latest sample; None: no samples), `started: datetime | None` (that sample's), `rows: Mapping[bytes, Row]`, `retry: frozenset[bytes]`, `children: Mapping[bytes, tuple[bytes, ...]]` (parent path to full child paths). Classmethods `Baseline.empty() -> Baseline` and `Baseline.build(sample_id: int, started: datetime, rows: Mapping[bytes, Row]) -> Baseline`; `build` computes `retry` (every path whose row has an `error_stage`, plus all its ancestors up to ROOT) and `children` (from `split`).
  - `should_list_changed(baseline: Baseline, max_depth: int) -> ShouldList`: `probe.depth < max_depth and (row_of(probe) != baseline.rows.get(probe.path) or probe.path in baseline.retry)`. Reads only; thread-safe.
  - `@dataclass(frozen=True, slots=True) class SampleDiff`: `changed: list[DirProbe]` (tree order), `deleted: list[bytes]` (tombstone paths, tree order), `skipped: int`.
  - `diff(baseline: Baseline, probes: Sequence[DirProbe], max_depth: int) -> SampleDiff` per spec §3.2: `changed` = probes whose row differs from the baseline row (a missing baseline row differs), except `open ENOENT` probes; `deleted` = (baseline children absent from the `children` of a probe whose `children is not None`) plus (paths of `open ENOENT` probes that have a baseline row), each together with every baseline descendant (through `baseline.children`); `skipped` = probes with `children is None`, no error and `depth < max_depth`.

- [ ] **Step 1: Write the failing tests** in `tests/test_baseline.py`, building `Baseline.build(1, T, rows)` from small `DirProbe` lists via `row_of`:
  - `test_row_of_matches_columns_and_splits_error`: `len(row_of(p)) == len(ROW_COLUMNS)`; an error probe gives `("open", "EACCES")` as the last two items and `type(row[-2]) is str`.
  - `test_build_retry_set_is_error_paths_and_ancestors`: error at `a/b/c` gives `retry == {b"a/b/c", b"a/b", b"a", b"."}`; no errors gives an empty set.
  - `test_empty_baseline_lists_everything_above_max_depth`: with `Baseline.empty()`, `should_list_changed(b, 2)` is True for depth 0 and 1, False for depth 2.
  - `test_unchanged_directory_is_pruned`, `test_changed_rstat_is_listed` (each of `rctime`, `rbytes`, `rfiles`, `rsubdirs`, `ino`, `uid` changed, parametrized), `test_new_directory_is_listed`.
  - `test_error_baseline_forces_listing_of_ancestors`: baseline with an error row at `a/b`; an unchanged probe of `a` and of `.` is listed; an unchanged probe of `z` is not.
  - `test_diff_unchanged_stores_nothing`; `test_diff_changed_and_new_are_stored_in_tree_order`; `test_diff_stores_error_rows` (a probe that newly has `open EACCES`).
  - `test_listed_parent_missing_a_child_tombstones_it_and_its_descendants`: baseline has `a`, `a/b`, `a/b/c`, `a/z`; the probe of `a` is listed with children `(b"z",)`; `deleted == [b"a/b", b"a/b/c"]`.
  - `test_open_enoent_tombstones_without_error_row`: probe `DirProbe(b"a", error=ProbeError(Stage.OPEN, "ENOENT"))` with baseline rows `a` and `a/b`: `deleted == [b"a", b"a/b"]` and `a` is not in `changed`.
  - `test_enoent_without_baseline_stores_nothing`.
  - `test_nothing_is_tombstoned_below_unlisted_directories`: baseline `a/b/c`; probes `a` pruned (`children=None`), or with `error=ProbeError(Stage.READDIR, "EIO")`, or `open EACCES`: `deleted == []` each time. Also a directory at max depth.
  - `test_listing_with_an_rstats_error_still_tombstones`: a probe with `error=ProbeError(Stage.RSTATS, "EIO")` and `children=()` tombstones its baseline children (it was listed).
  - `test_recreated_path_after_tombstone_is_stored`: baseline without the path (the view hides tombstones); a probe of it is in `changed`.
  - `test_skipped_counts_pruned_directories_only`: pruned (`children None`, no error, above max depth) counts; at max depth, errored, and listed ones do not.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_baseline.py -q`. Expected: FAIL, module not found.

- [ ] **Step 3: Implement `src/trackdir/baseline.py`** as specified. Compute `retry` by walking up with `split`, stopping at a path already in the set. Descendants of a deleted path: depth-first through `baseline.children`. Sort `deleted` with `tree_key`. Docstrings state the contracts (what `changed` and `deleted` mean, thread-safety of the predicate).

- [ ] **Step 4: Run** the tests; then `ruff format . && ruff check .`. Expected: pass, clean.

- [ ] **Step 5: Propose commit.** Subject: `Add baseline.py: prune unchanged subtrees and diff a walk against stored state`. Body: one change rule (whole row equal) replaces "monitored attributes" and "future rctime"; error rows put their ancestors in a retry set so errors are retried (otherwise a non-root run's error rows would outlive any root run because unchanged parents are pruned); tombstones cover baseline descendants without walking; the module is pure so initial and incremental samples share one path (initial = empty baseline).

---

### Task 3: Schema v2, tree identity and the incremental flow (`db.py`, `live.py`, `sample.py`, `cli.py`)

One task because removing `store_initial_sample`, `has_samples`, `SamplesExist` and the three-field `TreeKey` breaks `sample.py` and therefore `import trackdir.cli`; the package must import and the suite pass at the task's end. Part A is the database layer, Part B the flow that uses it.

**Files:**
- Modify: `src/trackdir/db.py`, `src/trackdir/live.py`, `src/trackdir/sample.py`, `src/trackdir/cli.py` (help text only)
- Test: `tests/test_db.py`, `tests/test_live.py`, `tests/test_sample.py`

**Part A interfaces (`trackdir.db`):**
- Consumes: Task 2's `ROW_COLUMNS`, `Row`, `row_of`, `Baseline`, `SampleDiff` (db imports baseline, never the reverse).
- Produces (`trackdir.db`):
  - `SCHEMA_VERSION = 2`; `DDL` per spec §4 (tree gains `max_depth integer NOT NULL CHECK (max_depth >= 0)` and `max_dirs bigint NOT NULL CHECK (max_dirs > 0)` with `UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path, max_depth, max_dirs)`; sample gains `changed bigint NOT NULL` and `deleted bigint NOT NULL` after `errors`; `dir_state` gains `deleted boolean NOT NULL DEFAULT false` with the tombstone CHECK; the view as in spec §4). Fix the comment above `DDL` and the `dir_state` comments to say rows are stored changes.
  - `DIR_STATE_COLUMNS = ("sample_id", "path", *ROW_COLUMNS, "deleted")`. `dir_state_row(probe) -> tuple` is unchanged: `(probe.path, *row_of(probe))`.
  - `TreeKey(cluster_fsid: str, fs_name: str | None, fs_path: bytes, max_depth: int, max_dirs: int)`; `TreeKey.of(cluster_fsid, fs_name, fs_path: str, max_depth, max_dirs)`; `tree_key_of(header: CaptureHeader, max_dirs: int) -> TreeKey` (uses `header.max_depth`).
  - `class SampleConflict(DbError)` with `key: TreeKey`; `class ConcurrentSample(SampleConflict)`; `class OutOfOrderSample(SampleConflict)` with `latest_started: datetime`.
  - `check_order(baseline: Baseline, key: TreeKey, started: datetime) -> None`: raises `OutOfOrderSample` if `baseline.started is not None and started <= baseline.started`.
  - `load_baseline(conn, key: TreeKey) -> Baseline`: in one `REPEATABLE READ` transaction (first statement `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`): the tree's latest sample `(id, started)` by `ORDER BY started DESC, id DESC LIMIT 1`, then `path` and `ROW_COLUMNS` from `trackdir.current_dir_state` for the tree, read through a named (server-side) cursor so the result is not buffered twice. No tree or no sample gives `Baseline.empty()`.
  - `store_sample(conn, header: CaptureHeader, key: TreeKey, baseline: Baseline, diff: SampleDiff, replayed_from: str | None) -> int`: in one transaction, as `store_initial_sample` did (insert tree `ON CONFLICT DO NOTHING`, `SELECT ... FOR UPDATE`), then: the tree's latest sample id must equal `baseline.sample_id` else `ConcurrentSample(key)`; then `check_order(baseline, key, started)`; insert the `sample` row (`dirs`, `errors` from the header; `changed = len(diff.changed)`, `deleted = len(diff.deleted)`); COPY one row per changed probe `(sample_id, *dir_state_row(p), False)` and one tombstone per deleted path `(sample_id, path, *(None,) * 11, True)`.
  - Removed: `store_initial_sample`, `has_samples`, `SamplesExist`.

**Part B interfaces:**
- Produces:
  - `walk_within(backend, *, max_depth, max_dirs, jobs, should_list: ShouldList | None = None, limit_advice: str = DEFAULT_LIMIT_ADVICE) -> list[DirProbe]` and `walk_live(live, *, max_depth, max_dirs, jobs, should_list=None, limit_advice=DEFAULT_LIMIT_ADVICE)`; `should_list=None` means `list_above(max_depth)`, so `capture` is untouched. `DEFAULT_LIMIT_ADVICE = "Raise --max-dirs if memory allows, or lower --max-depth."` replaces the literal in the message.
  - `sample.replay_walk(capture, max_depth, max_dirs, should_list=None)`; same default.
  - `SampleResult` fields: `sample_id`, `root`, `tree: TreeKey`, `max_depth`, `dirs` (probed), `changed`, `deleted`, `skipped`, `errors`, `walk_seconds`, `store_seconds`, `replayed_from`, `kind: Literal["initial", "incremental"]` (initial iff the baseline had no sample).
  - Messages: `ConcurrentSample` → "another sample of the TREE was stored while this one ran; nothing was stored. Run trackdir sample again."; `OutOfOrderSample` → "this sample started at T, which is not later than the latest sample of the TREE (LATEST); nothing was stored. A replayed capture must be newer than the tree's latest sample." (`SamplesExist` message removed.) `describe_tree(key)` now reads `tree (cluster FSID, FILESYSTEM, path PATH, depth N, max dirs M)`. `SAMPLE_LIMIT_ADVICE = "--max-dirs and --max-depth are part of a tree's identity: a higher --max-dirs or another --max-depth starts a new tree with a full initial sample."` passed as `limit_advice` by sample.
  - Summary: initial `Stored sample 7 (initial): 5 directories under ROOT to depth 2, TREE; E. Walk 1.2 s, store 0.5 s.`; incremental `Stored sample 7 (incremental): probed 1204 directories under ROOT to depth 4, TREE; 18 changed, 2 deleted, 31 unchanged subtrees skipped; E. Walk 3.2 s, store 0.1 s.` (`E` is `describe_errors`; `Replayed from PATH.` appended as now).

- [ ] **Step 1: Update and add tests** in `tests/test_db.py`. Without a database: `test_tree_key_of_encodes_fs_path` expects all five fields (`max_dirs` passed in); `test_check_order` (None baseline passes; equal and earlier `started` raise `OutOfOrderSample` carrying `latest_started`; later passes); `test_dir_state_columns_end_with_deleted`. With the `db` fixture: `test_init_creates_and_is_idempotent` expects version 2; replace `store_initial_sample(db, h, probes, r)` with a helper `store(db, header=HEADER, probes=(), *, baseline=None, replayed_from=None, max_dirs=10**6)` that builds the key, loads the baseline when none is given, computes `diff(baseline, probes, header.max_depth)`, calls `store_sample`, and returns the id. Then:
  - `test_store_round_trip`: as before (read back equals the probes), plus the `sample` row's `changed == dirs`, `deleted == 0`, and the view's row count.
  - `test_load_baseline_empty_for_unknown_tree_and_for_tree_without_samples`.
  - `test_load_baseline_returns_latest_state_per_path`: store an initial sample, then a later one (later `started`) that changes `a` and tombstones `a/b`; the baseline has the new row for `a`, no row for `a/b`, `sample_id` of the second sample, `started` of the second.
  - `test_tombstone_rows_satisfy_the_check_and_the_view_hides_them`: a tombstone row has `deleted` true and all values NULL; `current_dir_state` has no row for it; inserting a row with `deleted` true and `ino` set raises `psycopg.errors.CheckViolation`.
  - `test_stale_baseline_is_refused`: load the baseline, store another sample for the tree, then `store_sample` with the old baseline raises `ConcurrentSample` and `counts` are unchanged from after the second sample. The same for an initial sample racing an initial sample (replaces `test_second_sample_of_default_fs_tree_is_refused`, keeping the NULL `fs_name` case).
  - `test_older_started_is_refused`: a second sample whose header `started` equals or precedes the first's raises `OutOfOrderSample`; nothing stored.
  - `test_trees_differ_by_depth_and_max_dirs`: headers/keys differing only in `max_depth`, only in `max_dirs`, only in `fs_name` (including NULL vs a name) or `fs_path` give separate trees; two NULL `fs_name`s with equal other fields are one tree; a baseline loaded for one key does not see another's rows.
  - Keep `test_awkward_values_round_trip`, `test_out_of_range_value_stores_nothing`, `test_interrupt_during_copy_stores_nothing` on the new helper (rows with `rctime = 4294967295 * 10**9`, `ino = 2**63 - 1`, a non-UTF-8 path).

- [ ] **Step 2: Run** `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest tests/test_db.py -q`. Expected: FAIL (names missing).

- [ ] **Step 3: Implement the changes** in `db.py` as specified. `_TREE_MATCH` and `_key_params` take all five key fields. Check order and race in the locked transaction exactly as above; compare `baseline.sample_id` with `None` for initial samples.

- [ ] **Step 4: Run** the DB tests with `TRACKDIR_TEST_DSN` set. Expected: pass. (`tests/test_sample.py` and `tests/test_cli.py` fail until Part B; ignore them here.)

- [ ] **Step 5: Write the failing tests for Part B.**
  - `tests/test_live.py`: `test_walk_within_honors_a_custom_should_list` (a predicate that lists only ROOT stops after ROOT's children); `test_dir_limit_message_uses_the_given_advice` (`limit_advice="X"` appears in the `FatalError`).
  - `tests/test_sample.py`, no database: update `test_render_sample_summary` to the two formats above (initial and incremental, with `kind`, `changed`, `deleted`, `skipped` set); replace `test_samples_exist_names_the_tree_as_the_summary_does` with the same check for `ConcurrentSample` and `OutOfOrderSample` (the message contains `describe_tree(key)` and `nothing was stored`, parametrized on `fs_name` None and `"cephfs"`); `test_describe_tree_includes_depth_and_max_dirs`; `test_dir_limit_message_names_the_tree_consequence` (a replay with `max_dirs=1` fails with a message containing `part of a tree's identity`); `test_schema_version_mismatch_says_what_to_do` uses found `1` (2 is current now); `test_replay_walk_with_a_should_list_reads_only_what_it_accepts`.
  - With the `db` fixture: `test_second_replay_fails_before_walking` becomes `test_older_replay_is_refused_before_walking`: the same capture twice; `sample.replay_walk` patched to `must_not_walk` for the second; the message contains `not later than`.
  - `test_replay_stores_initial_sample` also asserts `res.kind == "initial"`, `res.changed == res.dirs`, `res.deleted == 0`, and the tree row's `max_depth`/`max_dirs`.

- [ ] **Step 6: Run** `venv/bin/python -m pytest tests/test_live.py tests/test_sample.py -q` (with and without `TRACKDIR_TEST_DSN`). Expected: failures for the new and updated tests.

- [ ] **Step 7: Implement Part B.**
  - `live.py`: the two parameters; keep `capture`'s call sites unchanged.
  - `sample.py` flow in both modes: (1) obtain the key (live: from `open_live_root` plus `opts.max_depth`, `opts.max_dirs`; replay: `tree_key_of(replace(capture.header, max_depth=max_depth), opts.max_dirs)`, after the existing depth check); (2) pre-check on its own connection: `check_schema`, `load_baseline`, and for replay `check_order(baseline, key, started-of-capture)`; (3) walk with `should_list_changed(baseline, max_depth)`; (4) `diff(baseline, probes, max_depth)`; (5) `store_sample` on a new connection. Delete `_precheck`'s samples-exist branch and the `store_initial_sample` import. Replay's header keeps `replay_walk`'s recomputed `max_depth`, `dirs`, `errors`. Update module and function docstrings (no longer "first sample").
  - `cli.py`: `SAMPLE_DESCRIPTION` and `SAMPLE_EPILOG` say what spec §5 lists: the first sample of a tree stores every directory, later samples only changed directories and deletions; non-root runs are partial and their unreadable directories are retried by later samples; `--max-depth` and `--max-dirs` are part of a tree's identity.

- [ ] **Step 8: Run** the whole suite with `TRACKDIR_TEST_DSN` set and again without, then `ruff format . && ruff check .`. Expected: pass, clean; `PYTHONPATH=src venv/bin/python -c 'import trackdir.cli'` works without psycopg.

- [ ] **Step 9: Propose commit.** Subject: `sample: incremental samples against schema v2`. Body: the user decided that `max_depth` and `max_dirs` are part of a tree's identity (so deepening is a new tree and there is no depth guard); tombstones (`deleted`) because v1 could not say "gone"; the stale-baseline lock check and the `started` order guard because the baseline is read before an hours-long walk and the view orders by `started`; no migration because no real database exists; pruning is a `ShouldList` callback so the walker, capture and replay are untouched (replay therefore gets incremental samples, which is how they are tested offline); live and replay share one pre-check/walk/diff/store path; the dir-limit message names the tree-identity consequence because raising `--max-dirs` now starts a new tree, the recorded risk that this splits history.

---

### Task 4: End-to-end incremental tests on edited captures

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/test_incremental.py`

**Interfaces:**
- Produces (`tests/conftest.py`): `edited_capture(capture: Capture, out: Path, *, started: str, touch: Sequence[bytes] = (), remove: Sequence[bytes] = (), add: Sequence[bytes] = (), fix: Sequence[bytes] = ()) -> Path` writes a new capture file with `write_capture` and returns `out`. The header is `capture.header` with `started`/`finished` set to `started` and `dirs`/`errors` recomputed. `touch(p)`: for `p` and each ancestor up to ROOT, `rctime += 10**9` and `rbytes += 1`. `remove(p)`: drop `p` and every descendant, and `touch` its parent chain. `add(p)`: a new probe (`nlink=2`, `rsubdirs=1`, `rfiles=0`, `rbytes=0`, fresh `ino`, a plausible `rctime`) at depth ≤ the capture's, and `touch` its parent chain. `fix(p)`: replace the errored probe at `p` with an error-free one (`nlink=2`, `rsubdirs=1`, rstats set, same `ino`) without touching ancestors. Records are in `tree_key` order.
- Consumes: `SUBTREE` fixture (depth 4, 41 `open EACCES` directories), `sample.run_sample`, `SampleOptions`, the `db`/`dsn` fixtures.

- [ ] **Step 1: Write `tests/test_incremental.py`** (all opt-in through the `db` fixture; each calls `sample.run_db_init(dsn)` and an initial `run_sample` of `SUBTREE`, then replays edited captures dated after the fixture's `started`). Pick paths from the loaded capture in the test (a depth-4 directory without an error; a depth-2 directory with at least 3 descendants; one of the 41 error directories):
  - `test_unchanged_later_capture_stores_nothing`: `edited_capture` with no edits; `res.kind == "incremental"`, `changed == 0`, `deleted == 0`, `dirs < header.dirs`, `skipped >= 1`; the view's row count is unchanged.
  - `test_changed_chain_is_stored_and_nothing_else`: `touch=[deep]`; `changed == depth + 1`; each stored row equals the edited capture's probe; the view equals the edited capture's state for those paths and the initial state elsewhere.
  - `test_deleted_subtree_is_tombstoned_and_hidden`: `remove=[mid]`; `deleted == 1 + descendants`; the view has no row under `mid`; `dir_state` keeps the old rows (history).
  - `test_recreated_path_gets_an_ordinary_row`: after the removal, a third sample with `add=[mid]`; the view has the new row; the count of `dir_state` rows for `mid` is 3.
  - `test_error_directory_is_retried_when_readable`: `fix=[err_dir]` (ancestors untouched); `changed == 1` and the view's row for `err_dir` has no error; a later unchanged sample also skips it (retry set shrank).
  - `test_new_directory_is_stored`: `add` a path under an existing directory.
  - `test_concurrent_sample_is_refused_at_store`: patch `sample.replay_walk` with a wrapper that, before returning the real result, runs another `run_sample` (a later capture) with the original function; the outer run raises `FatalError` containing `another sample`; the sample count is that of the inner run only.
  - `test_replay_dated_before_latest_sample_is_refused_before_walking`: replay a capture dated before the latest sample's `started` with `sample.replay_walk` patched to `must_not_walk`; the message contains `not later than`.
  - `test_depth_or_max_dirs_starts_a_new_tree`: replay `SUBTREE` with `max_depth=2` and again with `max_dirs=10**6 + 1`; `trackdir.tree` has 3 rows, both new samples are `initial`.
  - `test_failed_store_leaves_nothing`: an edited capture with `rbytes = 2**63` in a changed directory raises `FatalError` (`database error`); `sample` count unchanged.
  - A CLI-level check `test_second_sample_prints_incremental_summary` through `main([...])`: stderr starts with `Stored sample 2 (incremental): probed`.

- [ ] **Step 2: Run** `TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest tests/test_incremental.py -q`. Expected: FAIL until `edited_capture` exists; then pass against Task 3's code. Fix the code (not the tests) for any real defect found, with a regression test.

- [ ] **Step 3: Run** the whole suite and ruff. Expected: pass, clean; without `TRACKDIR_TEST_DSN` the new tests skip.

- [ ] **Step 4: Propose commit.** Subject: `tests: end-to-end incremental samples on edited captures`. Body: edited copies of the real fixture (touched chains, removed and added subtrees, fixed error directories) exercise pruning, tombstones, retry and guards offline, since the dev mounts are temporary.

---

### Task 5: Documentation, acceptance and measurements

**Files:**
- Modify: `README.md`, `docs/terms.md`, `docs/conventions.md`, `docs/superpowers/specs/2026-10-15-trackdir-capture-design.md`, `docs/superpowers/specs/2026-10-16-trackdir-initial-sample-design.md`, `docs/superpowers/specs/2026-10-16-trackdir-incremental-sample-design.md` (§8 results)

- [ ] **Step 1: README.** Status line (incremental samples work); `sample` section: initial vs incremental, what an incremental sample stores, the identity rule (depth and max dirs; another value is a new tree, so one ROOT at two depths is stored twice), non-root samples (partial; unreadable directories retried), limits from spec §10 (rstat propagation delay; pinned rctime and same-size rewrites; a rename is a deletion plus a creation). No real names.
- [ ] **Step 2: `docs/terms.md`:** tree (now with `max_depth`, `max_dirs`), baseline, incremental sample, skipped (pruned) subtree, tombstone, retry set; "sample" no longer says "later". **`docs/conventions.md`:** `dir_state` rows are stored changes; the latest sample is by `(started, id)`; tombstones.
- [ ] **Step 3: Amendment notes** at the top of the capture spec (§14 settled: change rule, tombstones, error retry; "future rctime" dropped) and the initial-sample spec (tree identity extended, view revisited, `SamplesExist` replaced, schema version 2), each linking the incremental spec.
- [ ] **Step 4: Acceptance** (dev container, replay only, no real names in the dev database): `scripts/postgres.sh start --ephemeral`; `DSN=$(scripts/postgres.sh url)`; `PYTHONPATH=src venv/bin/python -m trackdir db init --db $DSN`; `... sample --replay tests/data/ceph1-sub-depth4.jsonl.gz --db $DSN` (initial). Then take a second sample from a throwaway copy made in the scratchpad (not product code) with `edited_capture`: a later `started`, a few `touch`, one `remove`, one `add`. Expected: `(incremental)`, `changed`/`deleted` equal the edits, and `SELECT count(*) FROM trackdir.current_dir_state` equals the edited capture's directory count. Replaying the unedited fixture a second time must fail with `not later than`. Report the output; `scripts/postgres.sh stop` afterwards.
- [ ] **Step 5: Measure** (spec §8) with a throwaway script in the scratchpad: after the initial and about five incremental samples of the fixture, time and peak RSS (`resource.getrusage`) of `load_baseline` and of `diff`; extrapolate to 1,000,000 directories. Add the numbers and the verdict (index needed or not, against the spec's thresholds) to spec §8. Do not add an index in this plan.
- [ ] **Step 6: Run** the whole suite with `TRACKDIR_TEST_DSN`, ruff, and `git status` (no stray files; the scratchpad scripts stay out of the repo).
- [ ] **Step 7: Propose commit.** Subject: `docs: incremental samples (README, terms, conventions, spec amendments)`. Body: records the settled capture-spec §14 items, the tree-identity amendment and the measurements, with the reasoning for each.

---

## Self-review notes

- Spec coverage: §3.1 and §3.2 (Task 2; the `nlink` guard Task 1), §3.3 and §4 and §5 (Task 3, store-time guards also exercised in Task 4), §6 (Tasks 1–3; `walk_*` take an optional `should_list` instead of a required one, so `capture` is untouched), §7 (each task; manual acceptance Task 5), §8 and §9 (Task 5), §10 (README, Task 5).
- Names used across tasks: `ROW_COLUMNS`, `row_of`, `Baseline.empty/build`, `should_list_changed`, `SampleDiff(changed, deleted, skipped)`, `diff`, `load_baseline`, `store_sample`, `check_order`, `TreeKey` (five fields), `tree_key_of(header, max_dirs)`, `ConcurrentSample`, `OutOfOrderSample`, `SampleResult.kind`.
