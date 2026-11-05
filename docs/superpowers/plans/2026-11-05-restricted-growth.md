# Restricted own growth for `db summary` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trackdir db summary`'s growth costs time and memory in proportion to what changed in each window, not to the size of the tree, and its output stays the same byte for byte. `own_growth` moves out of `db.py` into a pure module that is tested without PostgreSQL.

**Architecture:** today `db._growth` reads every current row of the tree (`_CURRENT_ENDS`) and `db.own_growth` walks all of them once per window. Instead, `_growth` reads only the paths that can have a non-zero own growth or a `(subtree)` mark: the paths stored in the window, the error paths, one witness below each unlisted directory, and the ancestors of all of these. `own_growth` itself is unchanged and moves to `growth.py`.

**Tech Stack:** Python ≥ 3.12 stdlib; psycopg 3 (optional extra); pytest, ruff. No schema change (version stays 4).

**Origin:** the DB review of 2026-11-05. It measured, on a replayed 855k-directory depth-7 tree with 10 samples (`captures/`, gitignored):

| | today |
|---|---|
| `db summary`, all trees | 7.0 s, 593 MB peak |
| `db summary --top 0` | 1.16 s, 43 MB |
| `_CURRENT_ENDS` on the 855k tree | 1.1-1.4 s, 855k rows to the client |
| `own_growth` over 855k paths | ~1.5 s per window, two windows |
| paths stored per window | 838-897 |
| current rows for 897 named paths via the view | 7 ms (`path = ANY(...)` is pushed into `DISTINCT ON`) |

A prototype restricted to the set below gave results identical to the full pass on all 8 windows of the 4 replayed trees, keeping 948 of 854,883 paths. **Not exercised there:** stale rows below a directory that was listed once and is unlisted now (every error in those captures is an `open EACCES` directory that was never listed). Task 2's property test and Task 3's DB test must cover it.

## Why the restriction is exact

Let W be the paths with a `dir_state` row in the window's later samples (the rows `_START_ENDS` returns). A path not in W has the same state at the window's start as now. In `own_growth`, a path p can matter only by:

1. a non-zero growth, which needs a start state different from the end state, so p ∈ W;
2. an unknown growth (no rstats at either end, or unread below an unlisted ancestor), which marks its nearest ancestor with a known growth as `(subtree)`;
3. being that nearest ancestor of a path of kind 1 or 2: a non-zero `below`, a hidden mark, or its own `unread` flag, which depends only on its ancestors.

Every other path has growth 0, own growth 0 - 0 = 0, no mark and no effect on any other path, so it is neither listed nor counted in `positive_own`. So keep:

- **W**, with start states from `_START_ENDS` (as today);
- **E**: current paths without rstats or with an error. Every error at the window start of a path not in W is also a current error;
- **witnesses**: for each directory U in W ∪ E that is unlisted (`open`, `stat`, `readdir`) at either end, one current path below U, if any. Every path below U is unread, so the hidden mark goes to the same ancestor whichever one is chosen. Paths below U present at the start but gone now have tombstones, so they are in W already;
- **the ancestors** of all of the above.

Then `ends` is built for exactly those paths: start state from W if the path is in W, else the current state; end state the current state, or None. `own_growth` runs on that.

## Global Constraints

- Run tests as `venv/bin/python -m pytest`; database tests need `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)` with the server up (`python3 scripts/postgres.py start --ephemeral`). Run `ruff format . && ruff check .` before each commit proposal.
- Don't edit `scripts/` or `tests/test_postgres_script.py`. Never write to `/mnt/ceph*`.
- Another session may be editing `db.py`, `sample.py`, `baseline.py` and `daemon.py`; run `git status` and `git log -3` before each task and rebase your edits on what you find.
- Do not `git commit` or `git push` unless the user says "commit". Each task ends with a proposed commit subject and body that include the design choices and intentions (AGENTS.md.d/GIT-v1.md), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Public repository: no real fsids, host names, or real directory paths in code, tests, docs or commit messages. The capture series names in `captures/` include real directory names; describe trees by size and depth ("the 855k-directory depth-7 tree").
- `db summary` output (text and JSON) stays byte-identical for the same database; existing tests pass unchanged except imports.

## Review Focus

1. **A directory listed at the window start and failing `readdir` (with rstats) at the end, with stale children below it.** Expected: it is marked `(subtree)`, its children are not listed. → Task 2 property test, Task 3 `test_growth_restricted_marks_a_dir_unlisted_only_at_the_end`.
2. **ROOT unlisted at an end.** Expected: every other path is unread; ROOT gets the hidden mark if it has a known growth and any other path exists. → Task 2 `test_restricted_root_unlisted`.
3. **A path deleted in the window whose parent survives.** Expected: its negative growth lowers its parent's `below` (parent's own growth unchanged from today). → covered by W; Task 2 property test.
4. **A tree with errors in many samples but none now.** Expected: E is empty; the candidate query returns those paths and the current-row fetch drops them. → Task 3.
5. **Two windows in one summary.** Each window builds its own kept set; the current-row cache is shared. → Task 3 differential test.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/trackdir/model.py` | modify | `UNLISTING_STAGES`: the error stages after which a directory's subdirectories were not read |
| `src/trackdir/capture.py` | modify | `is_listed` uses `UNLISTING_STAGES` |
| `src/trackdir/growth.py` | create | `End`, `DirGrowth`, `own_growth` (moved unchanged), `kept_paths`, `window_ends` |
| `src/trackdir/db.py` | modify | `_growth` uses the restricted queries; `_CURRENT_ENDS` and `_UNLISTING` go; imports `DirGrowth` from `growth` |
| `tests/test_growth.py` | create | pure tests of `own_growth`, `kept_paths`, `window_ends`, and the property test |
| `tests/test_summary.py` | modify | imports; two DB tests (Task 3) |

`Growth` stays in `db.py` with the other summary dataclasses; `summary.py` keeps importing it from there.

---

## Task 1: Move `own_growth` into `growth.py`, test it without PostgreSQL

No behavior change.

- [ ] Add to `model.py`, after `Stage`:

  ```python
  # Error stages that leave a directory's subdirectories unread; an rstats error
  # still lists them.
  UNLISTING_STAGES = frozenset({Stage.OPEN, Stage.STAT, Stage.READDIR})
  ```

  `Stage` is a `StrEnum`, so `"open" in UNLISTING_STAGES` holds for the strings read from the database. Add a one-line test in `tests/test_model.py` asserting exactly that.
- [ ] `capture.is_listed`: `return probe.error is None or probe.error.stage not in UNLISTING_STAGES` (same truth table; `tests/test_capture.py` covers it).
- [ ] Create `growth.py` with the module docstring "Own growth of directories between two states of a tree; pure: no database." Move `End`, `DirGrowth` and `own_growth` from `db.py` verbatim, replacing `_UNLISTING` with `UNLISTING_STAGES`. Write `End` as `type End = tuple[int | None, str | None] | None`, like `baseline.Row`. Fix `Growth.positive_own`'s comment while touching it: "listed or not" → "shown or not" ("listed" is a defined term).
- [ ] `db.py` imports `DirGrowth` and `own_growth` from `growth`; delete `_UNLISTING` and `End` there. `tests/test_summary.py` imports `DirGrowth` from wherever it does today, adjusted.
- [ ] Create `tests/test_growth.py` with hand-built `ends` dicts (a few paths each) for every rule in `own_growth`'s docstring:
  - a new path grows from 0; a deleted path by minus its start rbytes, and it is not shown;
  - unknown growth without rstats at the start, and at the end;
  - unread below a directory unlisted at the start, and at the end; its nearest known ancestor is marked `subtree`;
  - `subtree` for a directory at `max_depth` and for one unlisted at the end;
  - ROOT with unknown growth: children's growth goes to nobody;
  - `positive_own` sums every positive own growth, also those beyond `top`;
  - `top` and ties by path; own growth ≤ 0 is left out.
- [ ] Without `TRACKDIR_TEST_DSN`, `pytest tests/test_growth.py` runs (no skips). Full suite with the DSN passes.
- [ ] Propose a commit, e.g. "growth: move own_growth out of db.py and test it without PostgreSQL".

## Task 2: The kept set and the window's ends, as pure functions

- [ ] In `growth.py`:

  ```python
  def kept_paths(seeds: Iterable[bytes]) -> set[bytes]:
      """Return SEEDS and all their ancestors, ROOT included."""

  def window_ends(
      start: Mapping[bytes, End],    # W: path -> state at the window start (None: absent)
      current: Mapping[bytes, End],  # current state of at least the kept paths
      kept: Iterable[bytes],
  ) -> dict[bytes, tuple[End, End]]:
      """Return own_growth's ENDS for the KEPT paths.

      A kept path's start state is START's if it has one, else its current
      state; its end state is its current state, or None. A path absent at
      both ends is left out.
      """
  ```

  `kept_paths` walks up with `split` and stops at an already-kept path, as `Baseline.build` does for the retry set.
- [ ] Property test in `tests/test_growth.py`, seeded (`random.Random(seed)` for seed in `range(300)`). Each case:
  - builds a random parent-closed tree of 1-40 paths, depth ≤ 4, as the start state, with random rbytes and some error rows (stages `open`, `stat`, `readdir` with or without rstats, `rstats`);
  - derives the end state: change some rbytes, delete some subtrees (all descendants too), add new children only under directories listed at the start, turn some listed directories into unlisted ones while keeping their children as stale rows, and fix some errors;
  - W = paths whose state differs between start and end, or that appear in only one, plus a few unchanged ones (a stored row can differ only in columns growth ignores, such as rctime);
  - computes `own_growth` on the full `ends` and on `window_ends(start of W, end, kept_paths(W ∪ E ∪ witnesses))`, where E and the witnesses are computed from the end state as the queries in Task 3 would;
  - asserts both results are equal.

  Build the case so the "listed at start, unlisted at end with stale children" branch occurs in at least a third of the seeds; assert that count, so the test cannot silently stop covering it.
- [ ] `test_restricted_root_unlisted`: ROOT with a `readdir` error with rstats at the end; restricted result equals full.
- [ ] Propose a commit.

## Task 3: Use the restricted read in `db._growth`

- [ ] Replace `_CURRENT_ENDS` with three queries:

  ```python
  # Paths of tree %(tree)s whose stored row has an error, in any sample:
  # candidates for its current error paths. No sort, unlike the view.
  _ERROR_CANDIDATES = """SELECT DISTINCT d.path
  FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
  WHERE s.tree_id = %(tree)s AND NOT d.deleted
    AND (d.error_stage IS NOT NULL OR d.rbytes IS NULL)
  """
  # The current rows of the paths %(paths)s of tree %(tree)s; the condition on
  # path is applied before the view's sort.
  _CURRENT_OF = """SELECT path, rbytes, error_stage FROM trackdir.current_dir_state
  WHERE tree_id = %(tree)s AND path = ANY(%(paths)s)
  """
  # One current path strictly below %(dir)s: path > dir/ and < dir0, as "0"
  # follows "/" in byte order. For ROOT, any path but ROOT.
  _WITNESS = """SELECT path FROM trackdir.current_dir_state
  WHERE tree_id = %(tree)s AND path > %(lo)s AND path < %(hi)s LIMIT 1
  """
  ```

  For ROOT use `path <> '.'` instead of the range (a separate constant).
- [ ] `_growth` per tree: run `_ERROR_CANDIDATES` once, then `_CURRENT_OF` for those paths to get E (current rows with an error or without rbytes). Per window: W from `_START_ENDS` (unchanged); witnesses for each unlisted directory in W ∪ E; `kept = kept_paths(W ∪ E ∪ witnesses)`; fetch the current rows of kept paths not fetched yet (one `_CURRENT_OF` per window, cache across windows); `ends = window_ends(...)`; `own_growth(ends, max_depth, top)`.
- [ ] Update `summarize`'s docstring: "growth reads each tree's current state, and for each window the earlier rows of the paths changed since" → "growth reads, for each window, the rows of the paths changed since its start, the tree's error rows, and the current rows of those paths and their ancestors".
- [ ] Every growth test in `tests/test_summary.py` passes unchanged.
- [ ] New DB test `test_growth_restricted_marks_a_dir_unlisted_only_at_the_end`: from the `subtree` fixture, an initial sample, then a capture where one listed depth-1 directory with children fails `readdir` but keeps rstats and grows (`readdir_failed` and `with_rbytes` helpers exist). Expected: it is listed with `subtree=True`, none of its children are.
- [ ] New DB differential test: keep the old full computation in the test module as `full_growth(db, tree_id, max_depth, top)` (the current `_growth` body with `_CURRENT_ENDS`). After an initial sample and four edited captures (touch, remove, add, fix, and one `readdir_failed`), `summarize(...).tree_stats[0].growth` equals `full_growth`'s for both windows.
- [ ] Measure the error-candidate query on the 855k-directory replay: if it exceeds ~0.5 s, report it with `EXPLAIN (ANALYZE, BUFFERS)` and stop for a decision (a partial index on `dir_state (sample_id) WHERE error_stage IS NOT NULL` is a schema change, version 5; the current-state-table proposal would also make it an index lookup).
- [ ] Propose a commit.

## Task 4: Measure and record

- [ ] Replay the four capture series in `captures/` into a fresh ephemeral database with `trackdir sample --replay`, oldest first per series (the epoch after `date=` in each file name sorts them: `sort -t= -k2 -n`). Record, before (on master) and after: `/usr/bin/time -f "%e s %M KiB" venv/bin/trackdir db summary`, with and without `--top 0`, and `--tree` for the 855k tree. Check `db summary --json` output is byte-identical before and after.
- [ ] Add the numbers to the proposal `docs/superpowers/proposals/2026-10-22-current-state-table.md` under a dated note: growth no longer reads the whole current state, so of that proposal's readers only `load_baseline` and `_CURRENT_ERRORS` remain.
- [ ] Propose a commit.

## Open questions for the user

1. If `_ERROR_CANDIDATES` is slow on a large history, add a partial index (schema version 5, no migration) or wait for the current-state table? [wait; measure first]
2. Keep the old full computation only as a test reference (`full_growth` in tests)? [yes]
