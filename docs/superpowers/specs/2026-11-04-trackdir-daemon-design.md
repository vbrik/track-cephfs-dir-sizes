# trackdir daemon — design

Date: 2026-11-04. Status: draft; the decisions in section 2 were agreed in
brainstorming on 2026-11-04, the document itself awaits approval.

## 1. Goal and scope

`trackdir daemon --config FILE` runs continuously and samples the trees listed
in a TOML config file, one cycle per interval. Logs are not routinely
monitored, so the daemon exits on any error that is not routine; its exit
status, not its log, is the alarm.

In scope:

- the config file (daemon only)
- the schedule: cycles, the first cycle after a start, overruns
- logging: one line per event, on stdout
- startup checks, error handling, signals and exit statuses
- `db.latest_started`, a light query for the first cycle's time
- documentation: `--help`, README, conventions, terms

Unchanged: `trackdir sample`, `capture` and `db` behave and print as today
(messages on stderr as wrapped paragraphs, results on stdout). They do not
read the config file.

Out of scope: replayed trees in the config, a `--once` flag, a summary run by
the daemon, PID files, forking into the background, config reload (SIGHUP
stops the daemon like SIGTERM), per-tree intervals, running trees in
parallel.

## 2. Decisions

| Topic | Decision |
|---|---|
| Mode | A subcommand, `trackdir daemon --config FILE`, in the foreground (systemd `Type=simple`). `--config` is required; there is no default path. |
| Config format | TOML read with `tomllib` (stdlib): no new dependency. Daemon only. |
| Trees | One config lists any number of trees, sampled one at a time in config order. Live trees only. |
| Interval | One global `interval`, start to start. Written `<integer><unit>`, unit `s`, `m`, `h` or `d`, e.g. `"6h"`; greater than 0. |
| Cycle | Every interval the daemon samples every tree once, in config order. |
| Overrun | A cycle that has not finished one interval after it started. The running sample is aborted (it stores nothing), the daemon logs an ERROR and exits 1. Missed cycles are never queued. |
| First cycle | Decided by the database: one interval after the oldest of the trees' latest samples, or immediately if a tree has no sample or that time is past. A restart never causes an extra sample. |
| Routine errors | Per-directory errors (a directory deleted during the walk: ENOENT; unreadable: EACCES; ...) are stored in the sample as today and counted in its log line; the daemon carries on. |
| Non-routine errors | Any `FatalError`, at startup or in a cycle, is logged as one ERROR line and the daemon exits 1: bad config, psycopg missing, schema missing or of another version, database down, `max_dirs` exceeded, ROOT unreadable, a concurrent or out-of-order sample, an overrun. A bug (any other exception) is logged with its traceback and exits 1. |
| Log stream | Always stdout. The daemon has no results, so the stdout-for-results rule does not apply; `docs/conventions.md` records the exception. |
| Log format | `2026-11-04T12:00:00Z LEVEL message`: UTC timestamp always (journald's duplicate stamp is harmless), one line per event, no wrapping, no progress line. |
| Log level | `log_level` in the config: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`. |
| Warnings | Degraded-mode warnings (no `dirstat`, no openat2) are logged once per daemon run, not once per tree and cycle. |
| Database | `db` in the config, else `$TRACKDIR_DB`. Passwords stay in `~/.pgpass` or `PGPASSWORD`. |
| Signals | SIGTERM, SIGHUP and SIGINT abort a sample in progress (nothing stored for that tree), log INFO and exit **0**, so systemd counts it as a clean stop. Manual commands keep 128+N and 130. |
| Implementation | In process: the daemon calls `sample.run_sample` (section 9 gives the alternatives). |

## 3. Config file

```toml
interval = "6h"
db = "postgresql://trackdir@db.example.org/trackdir"   # optional; else $TRACKDIR_DB
log_level = "INFO"                                     # optional

[[tree]]
root = "/mnt/cephfs/projects"
max_depth = 4
max_dirs = 10000000   # optional; default as `trackdir sample`
jobs = 16             # optional; default as `trackdir sample`

[[tree]]
root = "/mnt/cephfs/home"
max_depth = 2
```

Validation, by `config.load(path) -> DaemonConfig`; each failure is a
`FatalError` naming the file and the key (with the tree's index for `tree`
keys):

- the file cannot be read, or is not valid TOML (tomllib's message, with its
  line and column)
- an unknown key, at the top level or in a tree: catches typos such as
  `max_dirs` spelled `max-dirs`
- a missing required key: `interval`, at least one `tree`, `root`,
  `max_depth`
- a wrong type, e.g. `max_depth = "4"`; TOML booleans are not integers
- `interval` not matching `<integer><unit>`, not greater than 0, or longer
  than 365 days (amended after the final review: `setitimer` overflows)
- `root` containing a NUL character
- `root` not absolute: a relative path would depend on the daemon's working
  directory
- `max_depth`, `max_dirs`, `jobs` out of range: the same limits as the CLI
  options (`MAX_DEPTH_LIMIT`, `MAX_DIRS_LIMIT`, at least 1), from the same
  constants
- `log_level` not one of the four names
- neither `db` nor `$TRACKDIR_DB` set

The result is frozen dataclasses:

```python
@dataclass(frozen=True, slots=True)
class TreeConfig:
    root: Path
    max_depth: int
    max_dirs: int = DEFAULT_MAX_DIRS
    jobs: int = DEFAULT_JOBS

@dataclass(frozen=True, slots=True)
class DaemonConfig:
    path: Path              # the config file, for messages
    interval: Interval      # seconds plus the text as written, e.g. "6h"
    dsn: str
    log_level: int          # a logging level
    trees: tuple[TreeConfig, ...]
```

`TreeConfig` plus the DSN make a `sample.SampleOptions`; the defaults are the
CLI's constants, not copies. Two entries that name the same tree are found at
startup (section 5), not here: only the filesystem knows a `TreeKey`.

## 4. Logging

- The stdlib `logging` module: logger `trackdir`; one
  `logging.StreamHandler(sys.stdout)` on the root logger, so libraries'
  records (psycopg's warnings) share the stream instead of reaching stderr
  through `logging.lastResort` (amended after the final review); formatter
  `%(asctime)s %(levelname)s %(message)s` with `time.gmtime` and
  `%Y-%m-%dT%H:%M:%SZ`. It is set up before the config is loaded, at INFO, so
  config errors are logged too; the configured level applies after loading.
- `StreamHandler` flushes after every record, so stdout on a pipe (journald)
  shows each line at once.
- stdout is reconfigured with `errors="backslashreplace"`, so a path with
  surrogates (see AGENT-LESSONS-LEARNED) can never crash the daemon. Paths in
  messages already go through `dbcli.display_path`.
- Message text is shared with the manual commands. `sample.render_sample_summary`
  is split: `describe_sample(result) -> str` returns the one-line text, and
  `render_sample_summary` wraps it with `paragraph()`. `FatalError` messages
  and warnings are already unwrapped text.
- The daemon passes each tree's sample a `progress.LogProgress`: no status
  line, but the walk's counts (the status line's text, `progress.walk_text`)
  logged at DEBUG every `PROGRESS_INTERVAL` (5 min) of the walk.

Events and levels:

| Level | Event | Example |
|---|---|---|
| INFO | start | `trackdir 0.1.0 daemon started: config /etc/trackdir.toml, 2 trees, interval 6h` |
| INFO | schedule | `next cycle at 2026-11-04T18:00:00Z` |
| INFO | sample stored | `Stored sample 42 (incremental): probed 962 directories under /mnt/cephfs/projects ...; 41 had errors: open EACCES 41. Walk 0.1 s, store 0.1 s.` |
| INFO | cycle done | `cycle finished in 812.4 s; next cycle at 2026-11-04T18:00:00Z` |
| INFO | stop | `SIGTERM received while sampling /mnt/cephfs/home; nothing was stored for it. Stopped.` |
| WARNING | degraded mode, once | the existing `MSG_NO_DIRSTAT`, `MSG_NO_OPENAT2` |
| ERROR | fatal | `the database has no trackdir schema. Create it with: trackdir db init` |
| ERROR | overrun | `cycle did not finish within the interval 6h; sample of /mnt/cephfs/home aborted, nothing was stored for it. Raise interval or sample fewer or smaller trees.` |
| ERROR | bug | `unexpected error`, followed by the traceback |
| DEBUG | tree start, end | `sampling /mnt/cephfs/projects`, `sampled /mnt/cephfs/projects in 812.4 s` |
| DEBUG | walk progress, every 5 min | `progress of /mnt/cephfs/projects: probed 412,331 dirs, 1,204 queued, 3 errors, 2,148 dirs/s, 3m12s` |

The walk's elapsed time counts from its first probe, after the baseline is
loaded; a walk shorter than 5 minutes logs no progress line.

## 5. Startup

Each step fails fast: a `FatalError` logs ERROR and exits 1.

1. Set up logging; load and validate the config (section 3).
2. Connect to the database; `check_schema` (also reports a missing psycopg).
3. For each tree, `open_live_root(root, warn)` to check that ROOT exists, is
   CephFS and can be opened, and to get its `TreeKey`; close it. Two trees
   with the same `TreeKey` (e.g. a directory and a bind mount of it) are a
   `FatalError` naming both entries: they would fail every cycle on the
   order guard.
4. `db.latest_started(conn, key) -> datetime | None` for each tree: the
   `started` of its latest sample (`_TREE_MATCH`, `_LATEST_SAMPLE`), None for
   an unknown tree or one without samples. It reads no `dir_state` rows,
   unlike `load_baseline`.
5. Schedule the first cycle (section 6), log the start and the schedule.

## 6. Schedule

Pure functions, testable without a clock:

- `first_start(latest: Sequence[datetime | None], interval, now: datetime) -> float`:
  seconds from now until the first cycle. 0 if any entry is None, else
  `max(0, min(latest) + interval - now)`.
- The loop keeps cycle times in `time.monotonic()`. The wall clock is used
  only to turn the database's `started` times into the first delay, and to
  print the next start in logs.

The loop:

1. Sleep until the cycle's scheduled start. A cycle starts at
   `start = max(scheduled, now)`; the next is due at `start + interval`.
2. Arm the deadline for `start + interval` with
   `signal.setitimer(signal.ITIMER_REAL, ...)`. Its SIGALRM handler raises
   `Overrun`, a `BaseException` so no `except Exception` swallows it, in the
   main thread.
3. For each tree in config order: `run_sample(opts, warn)`, log
   `describe_sample(result)`.
4. Disarm the deadline (`setitimer(ITIMER_REAL, 0)`); log the cycle's
   duration and the next start.

An aborted walk unwinds as on SIGTERM today: `walker.walk` cancels pending
probes and waits for running ones, and the database transaction rolls back.
Samples stored earlier in an overrun cycle stay stored. A probe stuck in the
kernel on an unresponsive MDS delays the exit until it returns; nothing in
user space can cut that short.

## 7. Errors, signals, exit statuses

`daemon.run(config_path) -> int` owns its logging and exit status. `main()`
runs it directly instead of through `_Command`, whose handlers print stderr
paragraphs and return 128+N.

| Status | Cause |
|---|---|
| 0 | stopped by SIGTERM, SIGHUP or SIGINT |
| 1 | any `FatalError` (startup or cycle), an overrun, a bug |
| 2 | a usage error (argparse, on stderr as for every command) |

`_Terminated` and `_terminate_on_signals` move from `cli.py` to a new module,
`trackdir/interrupts.py`, shared by `cli.py` and `daemon.py`, which adds
SIGINT and the SIGALRM handler. Signal handlers are restored on return, so
tests can run the daemon in process.

For unattended use the exit status is the alarm: the README's systemd unit
has no `Restart=always`, so a failed daemon stays visible as a failed unit
(`systemctl --failed`, unit-state monitoring, `OnFailure=`).

## 8. Code layout

| File | Change |
|---|---|
| `src/trackdir/config.py` | new: `load`, `DaemonConfig`, `TreeConfig`, `Interval`, `parse_interval` |
| `src/trackdir/daemon.py` | new: logging setup, startup, `first_start`, the loop, `run` |
| `src/trackdir/interrupts.py` | new: `Terminated`, `terminate_on_signals`, moved from `cli.py` |
| `src/trackdir/db.py` | `latest_started` |
| `src/trackdir/sample.py` | `describe_sample`; `render_sample_summary` wraps it |
| `src/trackdir/cli.py` | `daemon` subcommand, its `--help` description and epilog (config keys, schedule, exit statuses including SIGTERM 0); dispatch for a command that owns its exit status; shared range constants with `config.py` |

`daemon.run` takes seams for tests: the sampler (default `sample.run_sample`),
a monotonic clock and a sleep function. Startup's root inspection and database
lookups go through one injectable function, so loop tests need neither Ceph
nor PostgreSQL.

## 9. Alternatives considered

- **A subprocess per sample** (`trackdir sample` as a child, killed at the
  deadline): memory is returned to the system after every sample, but the
  daemon would translate config into argv and parse stderr paragraphs into
  log lines, or need a machine-readable output mode. Rejected: more moving
  parts for little gain.
- **A systemd timer running `trackdir sample`**: works today without code,
  but has no overrun rule, no shared config for several trees, and no single
  log stream. Rejected: a daemon was asked for.

Accepted risk of the in-process design: after a large sample, CPython may keep
memory it freed, so the daemon's resident size can stay near its largest
sample's peak between cycles. The README says so next to the memory figures.

## 10. Tests

None need Ceph or root.

- Config: a valid file with and without optional keys; every rejection of
  section 3, each naming its key; `$TRACKDIR_DB` fallback; `parse_interval`
  (`"90m"`, `"1d"`; rejected `"0h"`, `"6"`, `"1.5h"`, `"6 h"`, `"6H"`).
- `first_start`: empty database (None), all latest samples older than one
  interval, the oldest within it, a latest sample in the future.
- The loop with a fake clock, sleep and sampler: trees in config order every
  cycle; start-to-start timing; a `FatalError` from the sampler → one ERROR
  line, status 1; a warning repeated over trees and cycles → logged once.
- Overrun with a real SIGALRM: interval 0.05 s, a sampler that sleeps longer
  → ERROR line, status 1, signal handlers restored.
- Signals: SIGTERM during a sample → INFO line, status 0 (as
  `test_termination_signal_leaves_nothing` does for manual runs).
- Startup: duplicate `TreeKey` → status 1 naming both entries.
- Logging: each record is one line on stdout, `YYYY-MM-DDTHH:MM:SSZ LEVEL`,
  nothing on stderr; a surrogate in a message does not raise.
- `describe_sample`: `render_sample_summary(r) == paragraph(describe_sample(r))`.
- `db.latest_started` against the test database (`TRACKDIR_TEST_DSN`):
  unknown tree, tree without samples, the greatest `(started, id)`.
- CLI: `trackdir daemon` without `--config` is a usage error (2);
  `--help` names the exit statuses.

Manual end-to-end check: the daemon on a small tree under `/mnt/ceph1`
(read-only) with `interval = "1m"` against an ephemeral dev PostgreSQL, for a
few cycles, then SIGTERM; and an overrun forced with a short interval on a
larger tree.

## 11. Documentation

- README: "Run as a daemon": the config example, an example systemd unit
  (`Type=simple`, no `Restart=always`, `ExecStart=.../trackdir daemon --config
  /etc/trackdir.toml`), the exit-status-as-alarm advice, the memory note, and
  a warning that a manual `trackdir sample` of a daemon's tree that overlaps
  one of its samples stops the daemon (concurrent sample); one between
  cycles is harmless.
- `docs/conventions.md`: the daemon logs on stdout, one line per event with a
  UTC timestamp, no progress line; manual commands are unchanged.
- `docs/terms.md`: cycle, interval, overrun.
- `trackdir daemon --help`: what it does, the config keys, the schedule, the
  exit statuses.
