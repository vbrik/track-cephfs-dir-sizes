# Non-fatal overrun — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a cycle of `trackdir daemon` that runs past the start of the next one is logged as an error and finishes; nothing is aborted and the daemon keeps running.

**Architecture:** the SIGALRM timer that today raises `Overrun` in the sampling thread becomes a timer that only calls a function, so the overrun is still logged the moment the interval passes. The loop's existing rule (the next cycle is due one interval after the last start, and starts at once if that is past) already gives the rest: missed cycles are skipped, and lasting overload runs cycles back to back.

**Tech Stack:** Python ≥ 3.12 stdlib (`signal.setitimer`); pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-schema-v5-design.md`, section 6 and the "Overrun" row of section 2. It supersedes the "Overrun" row and sections 6 and 7 of `docs/superpowers/specs/2026-11-04-trackdir-daemon-design.md`. This is plan 3 of 3.

## Global Constraints

- Read `AGENTS.md`, `AGENTS.md.d/`, `docs/conventions.md` and `docs/terms.md` first.
- Tests: `venv/bin/python -m pytest`; set `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)` for the full suite. The tests of this plan need no database.
- `ruff format . && ruff check .` before proposing a commit.
- No branches, never push. A "Propose the commit" step shows the message and waits for the user's go-ahead. Commit bodies state the intention and the design choices.
- No real cluster fsids, host names, IPs or real directory paths anywhere. Never write to `/mnt/ceph*`.
- An overrun never stops the daemon and never aborts a sample, however many cycles overrun in a row. Exit status 1 remains for a `FatalError` and a bug; 0 for SIGTERM, SIGHUP and SIGINT.
- Missed cycles are not queued: after an overrun the next cycle starts once, at once.
- One ERROR line per overrun cycle, logged when the interval passes, not when the cycle finishes.
- Log format, stream and the daemon's other messages do not change.

## Review Focus

1. A cycle that lasts several intervals logs one ERROR, not one per interval passed, and is followed by one cycle, not several (Task 1).
2. The interval passes between two trees of a cycle, when no tree is being sampled: the ERROR is still logged, without naming a tree (Task 1).
3. SIGTERM during an overrunning sample still stops the daemon with status 0 and the "nothing was stored for it" line; the SIGALRM handler and timer are restored when the daemon returns (Task 1).
4. Under lasting overload every cycle overruns: each logs its one ERROR, cycles run back to back, and nothing accumulates (Task 1).
5. Text that still promises the old behavior: "exits", "aborted", "stores nothing" and "status 1 (including an overrun)" in `--help`, the README, `docs/terms.md` and the daemon spec (Task 2).

---

### Task 1: The timer calls instead of raising; the loop carries on

**Files:**
- Modify: `src/trackdir/interrupts.py`, `src/trackdir/daemon.py`
- Test: `tests/test_interrupts.py`, `tests/test_daemon.py`

**Interfaces:**
- Produces:
  - `interrupts.call_after(seconds: float, on_expire: Callable[[], None]) -> ContextManager[None]`: runs `on_expire` once, in the main thread, if the body is still running after `seconds` (> 0, else `ValueError`); the body is not interrupted. SIGALRM's previous handler and the timer are restored on exit. It replaces `deadline` and `Overrun`, which are deleted.
  - `daemon._cycle(config, log, warn, sample, clock, seconds) -> bool`: True if the cycle was still running `seconds` after it began.
  - `_OverrunLogged` is deleted.
- Log lines:
  - at the interval's end, ERROR: `cycle still running after the interval 1h, sampling ROOT (tree NAME); it will finish, and the next cycle starts when it does. Raise interval or sample fewer or smaller trees if this repeats.` Between trees the clause from `, sampling` to `)` is left out.
  - after an overrun cycle, INFO: `cycle finished in 5400.0 s, 1800.0 s past the interval; next cycle starts now`. After a cycle within the interval the line is unchanged.

- [ ] **Step 1: Replace the tests of `deadline` and of the fatal overrun**

In `tests/test_interrupts.py`, in place of the four `deadline` tests:

```python
def test_call_after_calls_once_and_lets_the_body_finish():
    # body sleeps 0.2 s in 0.01 s steps with call_after(0.05, calls.append-like);
    # afterwards calls == [1] and the body ran to its end (a flag set last)

def test_call_after_does_not_call_a_body_that_finished_in_time():
    # body returns at once; after 0.1 s more, no call

def test_call_after_restores_the_handler_and_the_timer():
    # signal.getsignal(SIGALRM) and signal.getitimer(ITIMER_REAL) as before

@pytest.mark.parametrize("seconds", [0, -1])
def test_call_after_rejects_non_positive(seconds):   # ValueError
```

In `tests/test_daemon.py`, in place of `test_overrun_exits_1`, `test_overrun_is_logged_before_the_sample_unwinds` and `test_overrun_is_logged_once_and_exits_1`:

```python
def test_overrun_is_logged_when_the_interval_passes_and_the_sample_finishes(tmp_path):
    # real timer: interval "1s" is too long for a test, so call run_cycles with
    # Interval(0.05, "50ms") and a sampler that sleeps 0.2 s in real time;
    # stop after the first cycle. Lines in order: ERROR "cycle still running
    # after the interval 50ms, sampling ROOT (tree t1)", then INFO "Stored
    # sample", then INFO "cycle finished in … past the interval; next cycle
    # starts now". The ERROR is logged before the sampler returns.

def test_a_cycle_of_several_intervals_logs_one_error_and_is_followed_by_one_cycle(...):
    # real timer, sampler sleeps 4 intervals: exactly one ERROR for that cycle

def test_next_cycle_starts_at_once_after_an_overrun_and_missed_cycles_are_skipped(...):
    # fake clock: HOUR interval, first cycle takes 2.5 h. Sampler call times:
    # cycle 1 at 0, cycle 2 at 2.5 h (sleep 0), cycle 3 at 3.5 h; never 1 h or 2 h

def test_every_cycle_under_lasting_overload_runs_back_to_back(...):
    # fake clock, every cycle 1.5 h: starts at 0, 1.5 h, 3 h; sleeps all 0

def test_overrun_between_trees_names_no_tree(...):
    # real timer; the first sampler call returns at once, the test's sleep hook
    # is not involved: make the timer expire inside the logging of the first
    # result by patching describe_sample to sleep. ERROR has no ", sampling"

def test_sigterm_during_an_overrunning_sample_stops_with_status_0(tmp_path):
    # run() with a real short interval; the sampler raises Terminated(SIGTERM)
    # after the interval passed: status 0, the "nothing was stored for it" line,
    # and SIGALRM's handler and timer are as before run()

def test_overrun_does_not_stop_the_daemon(tmp_path):
    # run() over three overrunning cycles then SIGTERM between cycles: status 0,
    # three ERROR lines, three "cycle finished" lines
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_interrupts.py tests/test_daemon.py -q`
Expected: FAIL (`call_after` is not defined; the daemon exits 1).

- [ ] **Step 3: Implement `call_after`, delete `deadline` and `Overrun`**

Same mechanics as `deadline` (SIGALRM, `ITIMER_REAL`, one at a time per process), but the handler only calls `on_expire`. Its docstring says the call runs in the main thread between bytecodes, so `on_expire` must be short and must not raise.

- [ ] **Step 4: Change `_cycle` and `run_cycles`**

`_cycle` wraps its loop in `call_after(seconds, …)`; the callback logs the ERROR line and sets the flag that `_cycle` returns. `run_cycles` keeps its arithmetic; it logs the second form of the "cycle finished" line when `_cycle` returned True. `run` loses its `except _OverrunLogged`. Keep `_MIN_DEADLINE` under a name that fits (`_MIN_TIMER`): `call_after` needs a positive time even when a cycle starts late.

- [ ] **Step 5: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass. The real-timer tests must not be flaky: run `venv/bin/python -m pytest tests/test_daemon.py tests/test_interrupts.py -q` three more times; every run must pass.

- [ ] **Step 6: Propose the commit**

Subject: `daemon: an overrun is logged and the cycle finishes`. The body gives the reason (a walk took 1,881 s against 75 s on average with the database idle; a late sample loses only resolution, a stopped daemon loses everything until restarted) and the accepted risk (overruns show only in the log and as gaps between samples).

---

### Task 2: Help text and documentation

**Files:**
- Modify: `src/trackdir/cli.py` (`DAEMON_DESCRIPTION`, `DAEMON_EPILOG`), `README.md`, `docs/terms.md`, `docs/conventions.md`, `docs/superpowers/specs/2026-11-04-trackdir-daemon-design.md`, `docs/superpowers/specs/2026-11-11-schema-v5-design.md`
- Test: `tests/test_daemon.py` or `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_daemon_help_describes_an_overrun_as_logged_not_fatal(capsys):
    # `trackdir daemon --help`: contains "overrun", "logged" and "keeps running";
    # contains none of "aborted", "exits once", "(including an overrun)";
    # the exit status line reads "1 error" without an overrun
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/bin/python -m pytest tests -q -k overrun_as_logged`
Expected: FAIL.

- [ ] **Step 3: Rewrite the texts**

- `--help`: the schedule paragraph says that a cycle still running when the next is due is an overrun, logged at once as an error; the cycle finishes, the next starts when it does, and cycles that were due meanwhile are skipped. The paragraph on errors says that an overrun is not one of the errors that stop the daemon, and that logs or the gaps between samples (`trackdir db summary`) are where overruns show. The exit status line drops the overrun.
- `README.md`, "Run as a daemon": the same, in place of the sentences about the exit waiting for probes in flight; keep the sentence that a probe stuck on an unresponsive MDS holds the daemon until the MDS answers, now without an exit.
- `docs/terms.md`, "overrun": a cycle not finished one interval after it started; it is logged, it finishes, and the next cycle starts at once.
- `docs/conventions.md`: where signals-as-exceptions are described, `Overrun` goes; add that the daemon's only timer calls a function and interrupts nothing.
- Daemon spec of 2026-11-04: an "Amended 2026-11-12" paragraph at the top, as the incremental-sample spec does, pointing to section 6 of the schema-v5 spec; the document's body stays as history.
- Schema-v5 spec: the status line says section 6 is implemented.

- [ ] **Step 4: Run the whole suite, then try it by hand**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Then run the daemon against a dev database with `interval = "2s"` on one tree under `/mnt/ceph1` (read-only) of at most about 70,000 directories, for four cycles, and stop it with SIGTERM. Only the first cycle, an initial sample of some 20 s, overruns on a real mount: later cycles are incremental and take well under a second. Repeated overruns are the automated tests' job.
Expected: one ERROR when the interval passes in the first cycle, that cycle's "Stored sample" line and its "past the interval; next cycle starts now" line, then three cycles within the interval, exit status 0, and `trackdir db summary` showing four samples.

- [ ] **Step 5: Propose the commit**

Subject: `docs: an overrun no longer stops the daemon`.
