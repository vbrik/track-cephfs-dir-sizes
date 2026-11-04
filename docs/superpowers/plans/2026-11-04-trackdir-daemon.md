# trackdir daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trackdir daemon --config FILE` samples the trees of a TOML config once per interval, logs one line per event on stdout, and exits on any non-routine error.

**Architecture:** `config.py` parses and validates the TOML file into frozen dataclasses. `daemon.py` sets up logging, checks the trees at startup, computes the first cycle from the database, and loops calling `sample.run_sample` per tree; `interrupts.py` (moved out of `cli.py`) turns SIGTERM/SIGHUP/SIGINT into `Terminated` and a SIGALRM deadline into `Overrun`. `cli.py` adds the subcommand and hands it the exit status.

**Tech Stack:** Python ≥ 3.12 stdlib (`tomllib`, `logging`, `signal.setitimer`); psycopg 3 (optional extra); pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-04-trackdir-daemon-design.md` (read it first; this plan does not repeat its rationale).

## Global Constraints

- `requires-python = ">=3.12"`, ruff `target-version = "py312"`. No new dependencies. `import trackdir.cli`, `import trackdir.daemon` and every non-database test must work without psycopg.
- Run tests as `venv/bin/python -m pytest`; database tests need `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)` with the server up (`python3 scripts/postgres.py start --ephemeral`). Run `ruff format . && ruff check .` before each commit proposal. Run the tool as `venv/bin/trackdir`.
- Don't edit `scripts/` or `tests/test_postgres_script.py`.
- Never write to `/mnt/ceph1` or `/mnt/ceph2`; only Task 6's manual check reads them.
- Do not `git commit` or `git push` unless the user says "commit". Each task ends by displaying a proposed commit subject and body that include the design choices and the intentions behind them (AGENTS.md.d/GIT-v1.md), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- The repository is public: no real fsids, host names or real directory paths in code, tests, docs or commit messages; placeholders only (`/mnt/cephfs/...`).
- Manual commands (`capture`, `sample`, `db`) keep their behavior and output byte for byte; existing tests pass unchanged except where a task says otherwise.
- Daemon log line: `YYYY-MM-DDTHH:MM:SSZ LEVEL message`, UTC, one line, stdout only. Exit statuses: 0 stopped by SIGTERM/SIGHUP/SIGINT; 1 any `FatalError`, overrun or bug; 2 usage error.
- Interval syntax: fullmatch `([1-9][0-9]*)([smhd])`.

## Review Focus

1. **`--config` names a directory or an unreadable file.** Expected: one ERROR line `cannot read the config ...`, status 1, no traceback. → Task 1 `test_load_unreadable`.
2. **`[tree]` written as a table instead of `[[tree]]`, or `tree = []`.** Expected: an ERROR naming `tree`, not a `TypeError`. → Task 1 `test_load_rejects` cases `tree_table`, `tree_empty`.
3. **A `root` that is not CephFS or does not exist.** Expected: startup fails with `open_live_root`'s message, status 1, before any sleep. → Task 4 `test_startup_rejects_non_cephfs_root`.
4. **SIGTERM while sleeping between cycles.** Expected: `SIGTERM received. Stopped.`, status 0. → Task 4 `test_signal_while_idle_stops_cleanly`.
5. **`log_level = "ERROR"`.** Expected: start, schedule and sample lines suppressed, a fatal error still logged. → Task 4 `test_log_level_applies_after_loading`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/trackdir/config.py` | create | `Interval`, `parse_interval`, `TreeConfig`, `DaemonConfig`, `load` |
| `src/trackdir/db.py` | modify | `latest_started` |
| `src/trackdir/sample.py` | modify | `describe_sample`; `render_sample_summary` wraps it |
| `src/trackdir/interrupts.py` | create | `Terminated`, `terminate_on_signals`, `Overrun`, `deadline` |
| `src/trackdir/daemon.py` | create | `setup_logging`, `OnceWarner`, `first_start`, `inspect_trees`, `run_cycles`, `run` |
| `src/trackdir/cli.py` | modify | `daemon` subcommand and help; uses `interrupts` |
| `tests/conftest.py` | modify | `sample_result(**changes)` moved here from `test_sample.py` (`result`) |
| `tests/test_config.py`, `tests/test_interrupts.py`, `tests/test_daemon.py` | create | |
| `README.md`, `docs/conventions.md`, `docs/terms.md` | modify | Task 6 |

---

### Task 1: Config file

**Files:**
- Create: `src/trackdir/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `trackdir.live.DEFAULT_JOBS`, `DEFAULT_MAX_DIRS`; `trackdir.model.MAX_DEPTH_LIMIT`, `MAX_DIRS_LIMIT`; `trackdir.dbcli.DSN_ENV`; `trackdir.messages.FatalError`.
- Produces:
  - `@dataclass(frozen=True, slots=True) class Interval: seconds: float; text: str`
  - `parse_interval(text: str) -> Interval` — raises `ValueError` with the message below
  - `@dataclass(frozen=True, slots=True) class TreeConfig: root: Path; max_depth: int; max_dirs: int = DEFAULT_MAX_DIRS; jobs: int = DEFAULT_JOBS`
  - `@dataclass(frozen=True, slots=True) class DaemonConfig: path: Path; interval: Interval; dsn: str; log_level: int; trees: tuple[TreeConfig, ...]`
  - `load(path: Path) -> DaemonConfig` — raises `FatalError`

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py` (an autouse fixture deletes `TRACKDIR_DB` from the environment, as in `test_sample.py`):
  - `test_parse_interval`: `"30s"→30`, `"90m"→5400`, `"6h"→21600`, `"1d"→86400`, `.text` is the input; parametrized rejections `"0h"`, `"6"`, `"1.5h"`, `"6 h"`, `"6H"`, `""`, `"-1h"` raise `ValueError` whose text contains `<integer><unit>`.
  - `test_load_minimal`: file with `interval = "6h"`, `db = "postgresql://x/y"`, one `[[tree]]` with `root = "/mnt/cephfs/a"`, `max_depth = 2` → `DaemonConfig(path, Interval(21600, "6h"), "postgresql://x/y", logging.INFO, (TreeConfig(Path("/mnt/cephfs/a"), 2),))`; defaults equal `live.DEFAULT_MAX_DIRS`, `live.DEFAULT_JOBS`.
  - `test_load_full`: all optional keys set, two trees, `log_level = "DEBUG"` → `logging.DEBUG`, order kept.
  - `test_dsn_from_environment`: no `db`, `TRACKDIR_DB` set → its value; neither → `FatalError` mentioning `db` and `TRACKDIR_DB`.
  - `test_load_rejects`, parametrized `(id, toml_text, words)`; each asserts `FatalError` whose message contains `str(path)` and every word in `words`:
    - `bad_toml` (`interval = `) → `line`
    - `unknown_top` (`intervall = "6h"`) → `intervall`
    - `unknown_tree_key` (`max-dirs = 5` in tree 2) → `tree 2`, `max-dirs`
    - `no_interval` → `interval`; `no_root` → `tree 1`, `root`; `no_max_depth` → `max_depth`
    - `tree_table` (`[tree]` instead of `[[tree]]`) → `tree`; `tree_empty` (`tree = []`) → `tree`; `no_tree` → `tree`
    - `depth_string` (`max_depth = "4"`) → `max_depth`, `integer`; `depth_bool` (`max_depth = true`) → `max_depth`, `integer`
    - `depth_range` (`MAX_DEPTH_LIMIT + 1`), `dirs_range` (`MAX_DIRS_LIMIT + 1`), `jobs_zero` (`jobs = 0`) → the key name and `at most`/`at least`
    - `relative_root` (`root = "a/b"`) → `root`, `absolute`
    - `bad_interval` (`interval = "6"`) → `interval`, `<integer><unit>`; `interval_int` (`interval = 6`) → `interval`, `string`
    - `bad_level` (`log_level = "LOUD"`) → `log_level`, `DEBUG, INFO, WARNING, ERROR`
  - `test_load_unreadable`: `load(tmp_path)` (a directory) and `load(tmp_path / "missing.toml")` → `FatalError` starting `cannot read the config`.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_config.py -q` — Expected: collection error, `No module named 'trackdir.config'`.

- [ ] **Step 3: Implement `config.py`.** Read bytes and `tomllib.loads`; check keys against explicit allowed sets (`{"interval", "db", "log_level", "tree"}`, `{"root", "max_depth", "max_dirs", "jobs"}`), then types and ranges. Message forms (each ends with a period; `where` is `""` or `"tree N: "`, N 1-based):
  - `f"cannot read the config {path}: {e.strerror}."` (OSError)
  - `f"{path}: {e}."` (TOMLDecodeError; its text carries line and column)
  - `f"{path}: {where}unknown key {key!r}."`, `f"{path}: {where}{key} is required."`
  - `f"{path}: {where}{key} must be an integer: {value!r}."` (bool rejected), `... must be at least {lo}: {v}.`, `... must be at most {hi}: {v}.`
  - `f"{path}: tree {n}: root must be an absolute path: {root!r}."`
  - `f"{path}: tree must be a list of [[tree]] tables with at least one entry."`
  - `parse_interval` error: `f"interval must be <integer><unit> with unit s, m, h or d, e.g. \"6h\": {text!r}"`; `load` prefixes `f"{path}: "`; a non-string interval is `f"{path}: interval must be a string such as \"6h\": {value!r}."`
  - `f"{path}: log_level must be one of DEBUG, INFO, WARNING, ERROR: {value!r}."`
  - no DSN: `f"{path}: no database given: set db or {DSN_ENV}."`

- [ ] **Step 4: Run** `venv/bin/python -m pytest tests/test_config.py -q` — Expected: all pass. `ruff format . && ruff check .` clean.

- [ ] **Step 5: Propose commit** `config: TOML config file for the daemon` (body: daemon-only config, tomllib to avoid dependencies, unknown keys rejected to catch typos, limits shared with the CLI constants).

---

### Task 2: `db.latest_started` and `describe_sample`

Two small producers the daemon needs; one task because neither is worth a separate review.

**Files:**
- Modify: `src/trackdir/db.py` (next to `load_baseline`), `src/trackdir/sample.py` (`render_sample_summary`)
- Modify: `tests/conftest.py`, `tests/test_sample.py` (move `result` → `conftest.sample_result`), `tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.latest_started(conn: "psycopg.Connection", key: TreeKey) -> datetime | None` — `started` of the tree's latest sample by `(started, id)`; None for an unknown tree or one without samples. Uses `_TREE_MATCH`, `_LATEST_SAMPLE`, `_db_errors()`.
  - `sample.describe_sample(result: SampleResult) -> str` — the summary text, one line, no trailing newline. `render_sample_summary(result) == paragraph(describe_sample(result))`.
  - `conftest.sample_result(**changes) -> SampleResult` — the existing `result()` helper, unchanged fields.

- [ ] **Step 1: Write the failing tests.**
  - `test_sample.py`: replace `result(` with `sample_result(` (imported from conftest); add `test_describe_sample_is_one_line`: for the three results of `test_render_sample_summary`, `"\n" not in describe_sample(r)` and `render_sample_summary(r) == paragraph(describe_sample(r))`.
  - `test_db.py` (database tests): `test_latest_started` — `latest_started(db, tree_key_of(HEADER))` is None after `init_schema` and after inserting the tree row without samples (as in `test_load_baseline_empty_for_unknown_tree_and_for_tree_without_samples`); after `store(db, HEADER)` and `store(db, later(HEADER, 60))` it equals `T0 + timedelta(seconds=60)`.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_sample.py tests/test_db.py -q` (with `TRACKDIR_TEST_DSN`) — Expected: ImportError for `describe_sample` / `latest_started`.

- [ ] **Step 3: Implement** both functions; `render_sample_summary` becomes `return paragraph(describe_sample(result))`.

- [ ] **Step 4: Run** the full suite with `TRACKDIR_TEST_DSN` — Expected: all pass.

- [ ] **Step 5: Propose commit** `db, sample: latest sample time and one-line summary for the daemon` (body: `latest_started` reads no dir_state rows so startup stays cheap; one-line text shared by both renderings so message copy lives once).

---

### Task 3: `interrupts.py` — termination signals and the deadline

**Files:**
- Create: `src/trackdir/interrupts.py`
- Modify: `src/trackdir/cli.py` (delete `_Terminated`, `_terminate_on_signals`; import from `interrupts`)
- Test: `tests/test_interrupts.py`

**Interfaces:**
- Produces:
  - `class Terminated(BaseException)` with attribute `signum: int` (was `cli._Terminated`).
  - `terminate_on_signals(signums: Iterable[int] = (signal.SIGTERM, signal.SIGHUP)) -> ContextManager[None]` — while the body runs, each signal raises `Terminated(signum)`; previous handlers restored on exit. Keep the existing docstring's rationale.
  - `class Overrun(BaseException)` — the deadline passed.
  - `deadline(seconds: float) -> ContextManager[None]` — installs a SIGALRM handler raising `Overrun`, arms `signal.setitimer(signal.ITIMER_REAL, seconds)`; on exit disarms (`setitimer(ITIMER_REAL, 0)`) and restores the previous SIGALRM handler. `seconds` must be > 0 (`ValueError` otherwise: `setitimer` with 0 would mean "never").

- [ ] **Step 1: Write the failing tests** in `tests/test_interrupts.py`:
  - `test_deadline_raises_overrun`: `with pytest.raises(Overrun), deadline(0.05): time.sleep(2)`; afterwards `signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)` and `signal.getsignal(signal.SIGALRM)` is the handler from before.
  - `test_deadline_disarms_on_normal_exit`: `with deadline(5): pass` → timer `(0.0, 0.0)`, handler restored.
  - `test_deadline_rejects_non_positive`: `deadline(0)` raises `ValueError`.
  - `test_terminate_on_signals_includes_sigint`: with `terminate_on_signals((signal.SIGINT,))`, `os.kill(os.getpid(), signal.SIGINT)` raises `Terminated` with `signum == signal.SIGINT`; default handler restored afterwards.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_interrupts.py -q` — Expected: `No module named 'trackdir.interrupts'`.

- [ ] **Step 3: Implement** `interrupts.py`; update `cli.py` to use `Terminated` and `terminate_on_signals()`.

- [ ] **Step 4: Run** the full suite — Expected: all pass, including `test_termination_signal_leaves_nothing`.

- [ ] **Step 5: Propose commit** `interrupts: share termination handling; add a SIGALRM deadline` (body: moved out of cli so the daemon reuses the same unwinding; BaseException so no `except Exception` swallows them; deadline aborts a walk the way SIGTERM does).

---

### Task 4: `daemon.py`

**Files:**
- Create: `src/trackdir/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: Task 1 `load`, `DaemonConfig`, `TreeConfig`, `Interval`; Task 2 `latest_started`, `describe_sample`; Task 3 `Terminated`, `terminate_on_signals`, `Overrun`, `deadline`; `sample.run_sample`, `sample.SampleOptions`; `live.open_live_root`; `db.TreeKey`, `db.check_schema`; `dbcli.database`, `dbcli.DB_UNCHANGED`, `dbcli.describe_tree`, `dbcli.utc`.
- Produces:
  - `setup_logging(stream: TextIO) -> logging.Logger` — logger `"trackdir"`, level INFO, `propagate = False`, exactly one `StreamHandler(stream)` (replaces any previous handler, so repeated calls in tests do not duplicate lines), formatter `"%(asctime)s %(levelname)s %(message)s"`, `datefmt="%Y-%m-%dT%H:%M:%SZ"`, `converter = time.gmtime`. If `stream` has `reconfigure`, call `stream.reconfigure(errors="backslashreplace")`.
  - `class OnceWarner: __init__(self, log: logging.Logger); __call__(self, message: str) -> None` — logs WARNING the first time each distinct text is seen.
  - `first_start(latest: Sequence[datetime | None], interval: Interval, now: datetime) -> float` — seconds to wait: 0 if any None, else `max(0.0, (min(latest) + timedelta(seconds=interval.seconds) - now).total_seconds())`.
  - `inspect_trees(config: DaemonConfig, warn: Callable[[str], None]) -> tuple[datetime | None, ...]` — in one `database(config.dsn, DB_UNCHANGED)` block: `check_schema`; per tree `open_live_root(tree.root, warn)` → `TreeKey.of(live.cluster_fsid, live.fs_name, live.fs_path, tree.max_depth)`; duplicate keys → `FatalError(f"trees {i} and {j} in {config.path} ({root_i}, {root_j}) are the same {describe_tree(key)}; remove one.")` (1-based); then `latest_started` per key, in config order.
  - `run_cycles(config: DaemonConfig, first_delay: float, log: logging.Logger, warn: Callable[[str], None], *, sample=run_sample, clock=time.monotonic, sleep=time.sleep) -> NoReturn` — the loop of spec §6. Wraps each tree's sample: `FatalError` → `FatalError(f"sample of {root} failed: {e}")`; `Overrun` → `FatalError(f"cycle did not finish within the interval {interval.text}; sample of {root} aborted, nothing was stored for it. Raise interval or sample fewer or smaller trees.")`; `Terminated` while sampling → re-raised with the root recorded so `run` can say which tree. Calls `sample(opts, warn)` with `progress` left at its default.
  - `run(config_path: Path, *, stream: TextIO | None = None, inspect=inspect_trees, sample=run_sample, clock=time.monotonic, sleep=time.sleep) -> int` — spec §5/§7: `setup_logging(stream or sys.stdout)`; inside `terminate_on_signals((SIGTERM, SIGHUP, SIGINT))`: `load`, `log.setLevel(config.log_level)`, `inspect`, log start and schedule, `run_cycles`. `FatalError` → `log.error(str(e))`, 1. `Terminated` → INFO `f"{name} received while sampling {root}; nothing was stored for it. Stopped."` or `f"{name} received. Stopped."`, 0. Any other `Exception` → `log.exception("unexpected error")`, 1.
  - Log copy (INFO): `f"trackdir {__version__} daemon started: config {path}, {n} {'tree' if n == 1 else 'trees'}, interval {interval.text}"`; `f"next cycle at {when}"`; per tree `describe_sample(result)`; `f"cycle finished in {seconds:.1f} s; next cycle at {when}"`. `when` = wall-clock now plus the remaining delay, `%Y-%m-%dT%H:%M:%SZ` UTC. DEBUG: `f"sampling {root}"` before and `f"sampled {root} in {seconds:.1f} s"` after each tree.

Test helpers (in `tests/test_daemon.py`): `write_config(tmp_path, trees=1, **top) -> Path`; a `FakeTime` with `clock()` and `sleep(s)` that advances its own time and, after a given number of sleeps, raises `Terminated(signal.SIGTERM)` (ends a run cleanly); a sampler recording `(opts.root, fake_time.now)` and returning `conftest.sample_result(root=str(opts.root))`, optionally advancing the fake clock by a given duration per sample; `lines(stream)` splits the captured output.

- [ ] **Step 1: Write the failing tests** in `tests/test_daemon.py`:
  - `test_first_start`: `[None, t]` → 0; `[t0 - 7h, t0 - 1h]`, interval 6h, now t0 → 0; `[t0 - 5h, t0 - 1h]` → 3600; `[t0 + 1h]` → 25200.
  - `test_log_line_format`: `setup_logging(io.StringIO())`, `log.info("a b")` → the output matches `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO a b\n$`; the timestamp is within 5 s of `datetime.now(UTC)`; calling `setup_logging` twice still yields one line per record.
  - `test_log_survives_surrogates`: a `io.TextIOWrapper(io.BytesIO(), encoding="utf-8")` stream; `log.info("caf\udce9")` does not raise and the bytes contain `caf\\udce9`.
  - `test_once_warner`: same text twice and another once → two WARNING lines.
  - `test_cycles_sample_trees_in_order_start_to_start`: 2 trees, interval 1h, `inspect` returning `(None, None)`, sampler advancing 10 s per sample, sleeps limited so 3 cycles run → sampled roots `[a, b, a, b, a, b]`; sample start times `0, 10, 3600, 3610, 7200, 7210`; sleep durations `3580` between cycles (starts are start-to-start); status 0; lines include `daemon started`, `next cycle at`, three `cycle finished in 20.0 s`, `SIGTERM received. Stopped.`.
  - `test_first_cycle_waits_for_the_database`: `inspect` returns `(now_wall - 1h, now_wall - 2h)` with interval 6h → first sleep ≈ 14400 (± 1 s, wall clock moves).
  - `test_fatal_sample_error_exits_1`: sampler raises `FatalError("database error: x.")` on tree 2 → status 1; last line `ERROR sample of /mnt/cephfs/b failed: database error: x.`; nothing after it.
  - `test_bug_is_logged_with_traceback`: sampler raises `RuntimeError("boom")` → status 1; output contains `ERROR unexpected error` and `RuntimeError: boom`; stderr empty (`capsys`).
  - `test_warnings_logged_once`: sampler calls `warn(MSG_NO_DIRSTAT)` every sample over 2 trees × 2 cycles → exactly one WARNING line.
  - `test_overrun_exits_1`: `run_cycles` with a `DaemonConfig` built in code, `Interval(0.2, "0.2s")`, real clock and sleep, a sampler sleeping 2 s → `FatalError` whose message starts `cycle did not finish within the interval 0.2s; sample of`; afterwards the timer is `(0.0, 0.0)`.
  - `test_signal_during_sample_stops_cleanly`: sampler does `os.kill(os.getpid(), signal.SIGTERM); time.sleep(5)` (guard handler as in `test_termination_signal_leaves_nothing`) → status 0, last line `SIGTERM received while sampling /mnt/cephfs/a; nothing was stored for it. Stopped.`; SIGTERM handler restored.
  - `test_signal_while_idle_stops_cleanly` (Review Focus 4): FakeTime raising `Terminated(SIGTERM)` on the first sleep, `inspect` returning a recent latest → no sample, last line `SIGTERM received. Stopped.`, status 0.
  - `test_bad_config_exits_1`: a config with `intervall` → status 1, one `ERROR` line naming `intervall`; `inspect` never called.
  - `test_log_level_applies_after_loading` (Review Focus 5): `log_level = "ERROR"`, sampler raising `FatalError` → output is exactly one ERROR line.
  - `test_startup_rejects_duplicate_trees`: `fake_ceph` fixture plus `live.identify` monkeypatched to `lambda fd, root: CephMount(None, None, root)` (so `fs_path` follows the resolved root; `fake_ceph` alone gives every root `/x`), `daemon.database` monkeypatched to a `contextlib.nullcontext(object())`, `daemon.check_schema` and `daemon.latest_started` to no-ops. Roots `tmp_path/"a"` and `tmp_path/"a"/"."` → `inspect_trees` raises `FatalError` containing `trees 1 and 2` and `remove one`; roots `a` and `b` at the same depth, and `a` twice with different `max_depth`, pass.
  - `test_startup_rejects_non_cephfs_root` (Review Focus 3): as above without `fake_ceph`, one tree at a plain `tmp_path` dir → `run(...)` status 1, one ERROR line; the sampler is never called.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_daemon.py -q` — Expected: `No module named 'trackdir.daemon'`.

- [ ] **Step 3: Implement `daemon.py`.** Cycle loop, in monotonic time:

```python
scheduled = clock() + first_delay
while True:
    sleep(max(0.0, scheduled - clock()))
    start = max(scheduled, clock())
    scheduled = start + config.interval.seconds
    with deadline(scheduled - clock()):
        for tree in config.trees:
            ...  # sample, log describe_sample
    log.info("cycle finished in %.1f s; next cycle at %s", ...)
```

  `scheduled - clock()` is > 0 when the deadline is armed because `start ≥ clock()` only by the time spent between the two calls; guard with `max(..., 1e-3)`. Module docstring: the design rationale in short (exit status is the alarm; monotonic for cycles, wall clock only for the first delay).

- [ ] **Step 4: Run** the full suite — Expected: all pass. `ruff format . && ruff check .` clean.

- [ ] **Step 5: Propose commit** `daemon: sample configured trees once per interval` (body: in-process loop reusing run_sample; FatalError and overrun exit 1 because logs are unwatched; SIGTERM exits 0 for systemd; first cycle from the database so restarts add no samples; warnings deduplicated per run).

---

### Task 5: CLI subcommand

**Files:**
- Modify: `src/trackdir/cli.py` (module docstring, `main`, `_parser`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4 `daemon.run(config_path: Path) -> int`.
- Produces: `trackdir daemon --config FILE`. The parser sets `set_defaults(main=lambda args: daemon.run(args.config))`; `main()` checks `getattr(args, "main", None)` right after parsing and returns its result, bypassing `_Command` and its stderr handlers.

- [ ] **Step 1: Write the failing tests** in `tests/test_cli.py`:
  - `test_daemon_requires_config`: `main(["daemon"])` raises `SystemExit(2)`; stderr names `--config`.
  - `test_daemon_runs_with_its_own_status`: monkeypatch `daemon.run` to record its argument and return 0 → `main(["daemon", "--config", "c.toml"]) == 0`, called with `Path("c.toml")`.
  - `test_daemon_help`: `main(["daemon", "--help"])` exits 0; collapsed stdout contains `interval`, `[[tree]]`, `max_depth`, `log_level`, `stdout`, `0 stopped by SIGTERM, SIGHUP or SIGINT`, `1 error`, `overrun`.

- [ ] **Step 2: Run** `venv/bin/python -m pytest tests/test_cli.py -q` — Expected: the three tests fail (`invalid choice: 'daemon'`).

- [ ] **Step 3: Implement.** Help text, `RawDescriptionHelpFormatter`, in the style of `SAMPLE_DESCRIPTION`/`SAMPLE_EPILOG` (docs/CLI-WRITING-STYLE). `help="sample the trees of a config file once per interval"`. Description: what it does (every interval, every tree in config order, as `trackdir sample` would; one log line per event on stdout; manual commands are unaffected), the database prerequisite (`trackdir db init`). Epilog: a short config example with every key and its default; the schedule (start to start; first cycle one interval after the oldest of the trees' latest samples, or at once); routine per-directory errors are stored and counted, any other error stops the daemon; overrun; run it under a service manager that does not restart it, so a stop stays visible; exit statuses `Exit status: 0 stopped by SIGTERM, SIGHUP or SIGINT, 1 error (including an overrun), 2 usage error.` Update the module docstring to name `daemon`.

- [ ] **Step 4: Run** the full suite — Expected: all pass.

- [ ] **Step 5: Propose commit** `cli: trackdir daemon subcommand` (body: the daemon owns its exit status and logging, so main() hands it off instead of wrapping it in the manual commands' stderr handlers).

---

### Task 6: Documentation and end-to-end check

**Files:**
- Modify: `README.md`, `docs/conventions.md`, `docs/terms.md`

- [ ] **Step 1: README.** New section "Run as a daemon" after "Sample into PostgreSQL": a config example (placeholder paths), `trackdir daemon --config /etc/trackdir.toml`, an example systemd unit (`Type=simple`, `ExecStart=/opt/trackdir/venv/bin/trackdir daemon --config /etc/trackdir.toml`, no `Restart=`), the exit-status-as-alarm advice (`systemctl --failed`, `OnFailure=`), the concurrent manual sample warning (spec §11), and the memory note (spec §9). Intro bullet list gains `trackdir daemon`. Keep it short; details live in `--help`.

- [ ] **Step 2: `docs/conventions.md`.** Amend the messages bullet: the daemon logs instead, on stdout, one line per event (`YYYY-MM-DDTHH:MM:SSZ LEVEL message`), no progress line; message text is shared with the manual commands through one-line `describe_*` functions. `docs/terms.md`: rows for cycle, interval, overrun.

- [ ] **Step 3: End-to-end check (manual, not committed).** With `python3 scripts/postgres.py start --ephemeral` and `trackdir db init`, a config in the scratchpad with `interval = "1m"` and one small tree under `/mnt/ceph1` (`max_depth = 1`). Run `venv/bin/trackdir daemon --config CONFIG` for three cycles, then `kill -TERM`. Expected: the first cycle at once, later cycles 60 s apart, one INFO line per sample, status 0. Restart: the first cycle waits for the remainder of the minute. Then a deeper tree under `/mnt/ceph2` with `interval = "1s"` → overrun ERROR, status 1, and `trackdir db summary` shows no sample from the aborted run. Report the log excerpts with real paths replaced by placeholders.

- [ ] **Step 4: Run** the full suite with `TRACKDIR_TEST_DSN` and `ruff format . && ruff check .` — Expected: all pass, clean.

- [ ] **Step 5: Propose commit** `docs: the daemon` (body: README for operators, conventions for the stdout exception).
