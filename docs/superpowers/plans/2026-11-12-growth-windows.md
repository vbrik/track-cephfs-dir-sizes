# Growth over time windows, read from the window's rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `trackdir db summary` shows growth over time windows (`--since 1d`, `--since 7d`, …) instead of "since the first sample" and "since the second-to-last". Each window costs time in proportion to the rows stored in it, plus the current rows of the directories they touch and of the tree's directories without rstats, not to the size of the tree or to older history.

**Architecture:** today `db._growth` reads every current row of the tree (`_CURRENT_ENDS`) and walks all of them once per window in `own_growth`. Instead:
- a new `dir_state.prev_error_stage` completes what each row says it replaced;
- a window's start and end states come from the window's rows and the current rows of the paths they name;
- `own_growth` runs unchanged on those paths, plus a few others its result can depend on (Design, below).

`own_growth` and the new pure helpers move into `growth.py`, which is tested without PostgreSQL.

**Tech Stack:** Python ≥ 3.12 stdlib; psycopg 3; PostgreSQL 18; pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-schema-v5-design.md`, the **`db summary`** paragraph of section 5 (amended by this plan's Task 6) and section 4 for `prev_*`. The plan replaces `docs/superpowers/plans/2026-11-05-restricted-growth.md`, written for version 4. Decisions agreed on 2026-11-12:
1. Time windows replace both sample windows: `--since DURATION`, repeatable, default `1d` and `7d`. A window that reaches before the tree's first sample starts at that sample.
2. Add `prev_error_stage`.
3. Cost as in the Goal.
4. Keep the old plan's pure module and its reference test.

## Design

**A window** `since D` of a tree ends at the tree's latest sample L. It starts at the latest sample S whose `started` is at most `L.started − D`. If no sample qualifies, it starts at the tree's first sample, and the window is *clipped*: it covers all of the tree's history, which is shorter than D.

The anchor is L's `started`, not the clock. The summary therefore does not depend on when it is run, and a stopped daemon shows as an old "last" sample in the samples table, not as empty windows. A tree with one sample has no windows. Windows that resolve to the same S are computed once and shown each.

**The window's rows** are the `dir_state` rows of the tree's samples after S, up to L. A tree's sample ids are in `(started, id)` order (`check_order`), so these rows lie in the id range `(S, L]`. That range is what the BRIN index finds. It also contains other trees' rows from the same span, filtered out by the sample id list, so the read is bounded by the window across all trees, never by older history.

**W** is the set of paths with a row in the window. For each one:
- **start state:** taken from its first row in the window.
  - If that row is `created`, the path was absent at S.
  - Otherwise the start state is `(prev_rbytes, prev_error_stage)`. `prev_*` are the path's current row before that sample, which is its state at S, because it had no row between S and that sample.
- **end state:** its current row, `(rbytes, error_stage)`, or absent without one. L is the latest sample, so the current state is the state at L.
- **both ends absent:** a path created and deleted in the window. It is left out.

**Why a restricted set gives `own_growth`'s exact result.** A path q outside W has the same state at S and at L. Keep:
- **W**;
- **E**, the current paths without rbytes;
- **witnesses**: for each U in W that is present at both ends, unlisted at the start (stage `open`, `stat` or `readdir`) but not at the end, and has rbytes at both ends, one current path strictly below U, if there is one;
- **the ancestors** of all of these, ROOT included.

Any other path q is unchanged and has rbytes. There are two cases.

- **q is read** (no unlisted ancestor): its growth is 0.
  - Its own growth is 0 minus the growth of the descendants it is the nearest known ancestor of.
  - That subtraction is 0: a descendant with a non-zero or unknown growth would be in W or E, and q, as its ancestor, would be kept.
  - So q is not shown, adds 0 to its owner's `below`, and marks nothing.
- **q is unread**: some ancestor V is unlisted at an end, and q's unknown growth marks q's nearest ancestor with a known growth. That ancestor gets the same mark without q:
  - **V in W, unlisted at the end:** if V's growth is known, V is that ancestor and is marked through `unlisting(end)` anyway. If V's growth is unknown, V marks the same ancestor.
  - **V in W, unlisted only at the start:** V's growth is either unknown, and V marks that ancestor itself, or known, and then V is a witness case. The witness is unread and marks V, as q would.
  - **V not in W:** V is unlisted at both ends.
    - Without rbytes, V is in E and marks the same ancestor as q.
    - With rbytes, V's growth is 0 and it owns nothing below (everything below it is unread), so a mark on V changes nothing shown.

So `own_growth` on the kept paths equals `own_growth` on all paths. The property test in Task 3 checks this against the full computation.

The cost per window is:
- the window's rows;
- one current-row lookup per kept path;
- E, read once per summary from the partial index;
- one witness probe per witness case, each a range scan of `path`'s index that may step over dead paths below U before it reaches a live one.

`current_dir_state.path` has no index, so every lookup by path goes through `path`'s `UNIQUE (tree_id, path)` index and then the primary key `(tree_id, path_id)`.

## Global Constraints

- Read `AGENTS.md`, `AGENTS.md.d/`, `AGENT-LESSONS-LEARNED.md` (in `AGENTS.md.d/`), `docs/conventions.md` and `docs/terms.md` first.
- Tests: `venv/bin/python -m pytest`. The full suite needs `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)` with the server up (`python3 scripts/postgres.py start --ephemeral`). Never run `scripts/postgres.py reset` while a suite runs.
- `ruff format . && ruff check .` before each commit.
- No branches, never push. Commit each task when its tests pass. The body states the intention and the design choices and ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Public repository: no real fsids, host names, IPs or real directory paths in code, tests, docs or commit messages. The capture series in `captures/` have real directory names, so describe them by size ("the 855k-directory tree").
- Never write to `/mnt/ceph*`. Never connect to the ceph1-admin database.
- Schema version stays 5. Its DDL may still change in place: production starts with the user's `db init` on the ceph1-admin database, after this plan. Development databases are created again after a DDL change.
- Every query added here that joins `path` or `current_dir_state` by tree, or `dir_state` by sample, is tested on a database whose statistics predate the rows under test (`test_db.stale_tree`). See the planner-statistics rule in `docs/conventions.md`.
- Output changes only in the growth part of `db summary`; the rest of the text and JSON stays byte-identical.

## Review Focus

1. **A window clipped to the first sample**, e.g. `7d` on a tree two days old. Expected: the window starts at sample 1, the text heading says `(all history)` and the JSON has `clipped: true`. → Task 3 `test_window_start_*`, Task 5 `test_text_heading_of_a_clipped_window`.
2. **A directory unlisted only at the start, with stale children below it that did not change in the window** (the case `prev_error_stage` exists for). Expected: it is marked `(subtree)` and its children are not shown. → Task 3 property test, Task 4 `test_growth_marks_a_dir_unlisted_only_at_the_start`.
3. **A path deleted before the window and created again in it.** Expected: it grows from 0 and is marked `(new)`, even though the path id is old. → Task 4 `test_growth_of_a_path_recreated_in_the_window_is_new`.
4. **A path created and deleted inside the window.** Expected: it is absent from the result and changes nobody's own growth. → Task 3 property test, Task 4 differential test.
5. **A window in which nothing was stored**, e.g. `--since 1s` when the latest sample stored no rows. Expected: `grew most in 1s, since sample N (…), by own growth: none`. → Task 4 `test_growth_of_a_window_without_rows_is_empty`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/trackdir/db.py` | modify | DDL (`prev_error_stage`, partial index predicate); `_INSERT_STATES`; `Growth`; `summarize(..., since)`; `_growth` and its queries; `_CURRENT_ENDS`, `_START_ENDS`, `_UNLISTING`, `End`, `own_growth` and `DirGrowth` leave |
| `src/trackdir/model.py` | modify | `UNLISTING_STAGES` |
| `src/trackdir/capture.py` | modify | `is_listed` uses `UNLISTING_STAGES` |
| `src/trackdir/growth.py` | create | pure: `End`, `DirGrowth`, `own_growth`, `Window`, `DEFAULT_WINDOWS`, `window_start`, `witness_dirs`, `kept_paths`, `window_ends` |
| `src/trackdir/config.py` | modify | `duration_seconds`, shared by `parse_interval` and `--since` |
| `src/trackdir/summary.py`, `src/trackdir/cli.py` | modify | `--since`; the heading of each window |
| `tests/test_growth.py` | create | the pure tests and the property test |
| `tests/test_db.py`, `tests/test_summary.py`, `tests/test_config.py`, `tests/test_cli.py`, `tests/conftest.py` | modify | as each task says |
| `docs/schema.md`, `docs/terms.md`, `docs/conventions.md`, `README.md`, the v5 spec, the 2026-11-05 plan | modify | Tasks 1, 5, 6 |

`Growth` stays in `db.py` with the other summary dataclasses. `DirGrowth` moves to `growth.py`, and `db.py` re-exports nothing: importers change.

---

### Task 1: `prev_error_stage`

**Files:**
- Modify: `src/trackdir/db.py` (DDL, its comment, `_INSERT_STATES`), `tests/conftest.py` (`_BROKEN_CHAIN`, `assert_invariants` docstring), `docs/schema.md`, `docs/superpowers/specs/2026-11-11-schema-v5-design.md` (section 4's `prev_*` bullet)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - **`dir_state.prev_error_stage text CHECK (prev_error_stage IN ('open', 'stat', 'rstats', 'readdir'))`**, after `prev_rsubdirs`. It holds the `error_stage` of the path's current row before the sample. It is NULL without one (`created`) or when that row had no error. A tombstone carries the last known value.
  - **The partial index `current_dir_state_errors`** gets the predicate `WHERE error_stage IS NOT NULL OR rbytes IS NULL`. It serves both `_CURRENT_ERRORS` and Task 4's `_CURRENT_UNKNOWN`. A live probe never has NULL rbytes without an error, but a replayed capture is not checked for that.

- [ ] **Step 1: Write the failing tests** in `tests/test_db.py`

```python
def test_prev_error_stage_is_the_stage_the_row_replaced(db):
    # sample 1: ROOT, a (readdir EACCES with rstats), b (no error)
    # sample 2: a fixed, b open EACCES        -> a.prev_error_stage == "readdir",
    #                                             b.prev_error_stage is None
    # sample 3: b deleted (tombstone)          -> its prev_error_stage == "open"
    # sample 1 rows: created, prev_error_stage None
    assert_invariants(db)

def test_partial_index_finds_current_rows_without_rbytes(db):
    # EXPLAIN "SELECT path_id FROM trackdir.current_dir_state
    #          WHERE tree_id = 1 AND rbytes IS NULL" with enable_seqscan off:
    # the plan names current_dir_state_errors
```

Extend `_BROKEN_CHAIN` in `conftest.py` with `lag(d.error_stage) OVER w AS was_stage` and `OR prev_error_stage IS DISTINCT FROM was_stage`.

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_db.py -q -k "prev_error_stage or without_rbytes"`
Expected: FAIL (no column; the index predicate does not imply `rbytes IS NULL`).

- [ ] **Step 3: Change the DDL and `_INSERT_STATES`**

`_INSERT_STATES` adds `prev_error_stage` from `c.error_stage`. In the DDL comment, change "a row's prev_* are the rstats of its path's row before the sample" to "the rstats and error stage of …".

- [ ] **Step 4: Update the docs**

`docs/schema.md`:
- add the column to the table;
- in the "Each row says what it replaced" bullet, say the error stage too, and why: growth over a window needs to know whether a directory was listed at its start;
- change the index row's predicate.

The spec's section 4 `prev_*` bullet gets the same addition. `test_schema_doc_covers_the_ddl` must pass.

- [ ] **Step 5: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass. `assert_invariants` now checks the new column in every test that calls it.

- [ ] **Step 6: Commit**

Subject: `db: store each row's previous error stage`. Body: a window's start state must be fully in the window's rows, and whether a directory was listed at the start is part of it. This is a change in place, before the production freeze.

---

### Task 2: Move `own_growth` into `growth.py`

No change in behavior.

**Files:**
- Create: `src/trackdir/growth.py`, `tests/test_growth.py`
- Modify: `src/trackdir/model.py`, `src/trackdir/capture.py`, `src/trackdir/db.py`, `tests/test_summary.py` (imports), `tests/test_model.py`

**Interfaces:**
- Produces:
  - `model.UNLISTING_STAGES = frozenset({Stage.OPEN, Stage.STAT, Stage.READDIR})`, with the comment "error stages that leave a directory's subdirectories unread; an rstats error still lists them". `Stage` is a `StrEnum`, so `"open" in UNLISTING_STAGES` holds for strings read from the database.
  - `growth.End`: `type End = tuple[int | None, str | None] | None`, a path's (rbytes, error stage) at one end of a window, or None if absent.
  - `growth.DirGrowth`, moved unchanged.
  - `growth.own_growth(ends: Mapping[bytes, tuple[End, End]], max_depth: int, top: int) -> tuple[int, tuple[DirGrowth, ...]]`, moved unchanged except that `_UNLISTING` becomes `UNLISTING_STAGES`.

- [ ] **Step 1: Write the tests** in `tests/test_growth.py`, on hand-built `ends` of a few paths each, one per rule of `own_growth`'s docstring:
  - **new and deleted paths:** a new path grows from 0; a deleted path grows by minus its start rbytes and is not shown;
  - **missing rstats:** growth is unknown without rstats at the start, and at the end;
  - **unlisted directories:** below a directory unlisted at the start, and below one unlisted at the end, growth is unknown, and the nearest known ancestor is marked `subtree`;
  - **`subtree` marks:** on a directory at `max_depth`, and on one unlisted at the end;
  - **ROOT:** with an unknown growth, its children's growth goes to nobody;
  - **`positive_own`:** sums every positive own growth, including those beyond `top`;
  - **`top`:** ties are broken by path, and an own growth of 0 or less is left out.

  Add `test_unlisting_stages_hold_database_strings` to `tests/test_model.py`.

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_growth.py -q`
Expected: FAIL (`trackdir.growth` does not exist).

- [ ] **Step 3: Move the code**

The module docstring is "Own growth of directories between two states of a tree; pure: no database." `capture.is_listed` becomes `probe.error is None or probe.error.stage not in UNLISTING_STAGES`, which has the same truth table. `db.py` imports `DirGrowth` and `own_growth` from `growth`.

- [ ] **Step 4: Run the suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass. Without `TRACKDIR_TEST_DSN`, `tests/test_growth.py` runs with no skips.

- [ ] **Step 5: Commit**

Subject: `growth: move own_growth out of db.py and test it without PostgreSQL`.

---

### Task 3: Windows, the kept set and the window's ends, as pure functions

**Files:**
- Modify: `src/trackdir/growth.py`
- Test: `tests/test_growth.py`

**Interfaces:**
- Consumes: `End`, `own_growth`, `UNLISTING_STAGES` (Task 2).
- Produces, in `growth.py`:
  - **`Window(text: str, seconds: int)`**, a frozen slotted dataclass: a window's length, and the text it was given as, e.g. `Window("7d", 604800)`.
  - **`DEFAULT_WINDOWS = (Window("1d", 86_400), Window("7d", 604_800))`**.
  - **`window_start(started: Sequence[datetime], seconds: int) -> tuple[int, bool] | None`**
    - STARTED holds a tree's sample start times, oldest first.
    - It returns the index of the window's start sample and whether the window is clipped (Design).
    - It returns None with fewer than 2 samples.
  - **`witness_dirs(changed: Mapping[bytes, tuple[End, End]]) -> list[bytes]`**: the paths of CHANGED that need a witness (Design), sorted.
  - **`kept_paths(seeds: Iterable[bytes]) -> set[bytes]`**: SEEDS and all their ancestors, ROOT included. It walks up with `model.split` and stops at an already-kept path.
  - **`window_ends(changed: Mapping[bytes, tuple[End, End]], current: Mapping[bytes, End], kept: Iterable[bytes]) -> dict[bytes, tuple[End, End]]`**: `own_growth`'s ENDS for the KEPT paths.
    - A path in CHANGED has CHANGED's ends.
    - Any other path is unchanged, `(current, current)`.
    - A path absent at both ends is left out, as is a kept path in neither mapping.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(
    "hours_ago, seconds, expected",
    # samples started 48 h, 24 h, 1 h and 0 h before the latest
    [((48, 24, 1, 0), 3600, (2, False)),      # exactly 1 h before: that sample
     ((48, 24, 1, 0), 86_400, (1, False)),
     ((48, 24, 1, 0), 7 * 86_400, (0, True)),  # clipped to the first
     ((48, 24, 1, 0), 48 * 3600, (0, False)),  # the first, exactly: not clipped
     ((48, 24, 1, 0), 1, (2, False)),
     ((0,), 86_400, None)],
)
def test_window_start(hours_ago, seconds, expected): ...

def test_witness_dirs():
    # needs one: start ("readdir" with rbytes) -> end listed with rbytes
    # needs none: unlisted at both ends; unlisted only at the end; no rbytes at
    # either end; absent at the start; absent at the end; "rstats" at the start
    # ROOT can need one

def test_kept_paths_adds_every_ancestor_once():
    assert kept_paths([b"a/b/c", b"a/d"]) == {ROOT, b"a", b"a/b", b"a/b/c", b"a/d"}
    assert kept_paths([]) == set()

def test_window_ends():
    # a changed path keeps its ends; a kept unchanged one gets (current, current);
    # a changed path absent at both ends, and a kept path in neither mapping,
    # are left out
```

Then the property test. It is seeded: `random.Random(seed)` for each seed in `range(300)`.
- **The start state:** a random parent-closed tree of 1 to 40 paths, depth at most 4, with random rbytes and some error rows. The stages are `open`/`stat` without rbytes, `readdir` with or without rbytes, and `rstats` without rbytes.
- **The end state:** derived from the start.
  - Change some rbytes and fix some errors.
  - Delete some subtrees, every descendant included.
  - Add new children only below directories listed at the start.
  - Make some listed directories unlisted, keeping their children as unchanged stale rows.
  - Make some unlisted directories listed again, keeping their stale children unchanged.
  - Add some paths that are absent at both ends (created and deleted in the window).
- **CHANGED:** every path whose state differs between the ends or that exists at only one end, plus a few unchanged paths, as a stored row may differ only in columns growth ignores (rctime).
- **CURRENT:** the end state.
- **E:** the current paths without rbytes, outside CHANGED.
- **Witnesses:** for each of `witness_dirs(CHANGED)`, the smallest current path strictly below it, if any.
- **The comparison:**
  - The full ends are every path of either state.
  - The restricted ones are `window_ends(CHANGED, CURRENT, kept_paths(CHANGED ∪ E ∪ witnesses))`.
  - For `top` in `(3, 1000)`, `own_growth` on both gives equal results.
- **Coverage:** count the seeds in which a witness changed the result; computing without witnesses gives a different `own_growth`. Assert there are at least 30, so that the test cannot silently stop covering Review Focus 2.

`test_restricted_root_unlisted_only_at_the_start`: ROOT goes from `readdir` with rbytes to listed, and has unchanged children. The restricted result equals the full one, and ROOT is marked `subtree`.

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_growth.py -q`
Expected: FAIL (names not defined).

- [ ] **Step 3: Implement the five names in `growth.py`**

`window_start` uses `bisect` on STARTED. It compares datetimes: `started[-1] - timedelta(seconds=seconds)`.

- [ ] **Step 4: Run them to verify they pass**

Run: `venv/bin/python -m pytest tests/test_growth.py -q`, then the whole suite with ruff.
Expected: all pass, including the coverage assertion.

- [ ] **Step 5: Commit**

Subject: `growth: time windows and the paths a window's growth depends on`. The body gives the exactness argument in short, and the witness rule.

---

### Task 4: `summarize` reads windows from their rows

**Files:**
- Modify: `src/trackdir/db.py`, `src/trackdir/summary.py` (only what the new `Growth` fields break)
- Test: `tests/test_summary.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: Task 3's names and Task 1's column.
- Produces:
  - **`Growth`**, a frozen slotted dataclass with these fields in this order:
    - `window: str`: the `Window`'s text;
    - `since: int`: the start sample's id;
    - `since_started: datetime`;
    - `clipped: bool`;
    - `positive_own: int`;
    - `dirs: tuple[DirGrowth, ...]`.

    Its docstring: "the directories whose own growth was the greatest in a window: from sample SINCE to the tree's latest sample". Its `positive_own` comment changes from "listed or not" to "shown or not", since "listed" is a defined term.
  - **`summarize(conn, tree=None, recent=10, top=3, since: Sequence[Window] = DEFAULT_WINDOWS) -> DbStats`**. `TreeStats.growth` holds one `Growth` per window of SINCE that the tree has, in SINCE's order. It is empty with `top=0` or fewer than 2 samples.
- The queries, replacing `_CURRENT_ENDS` and `_START_ENDS`:

```python
# W of a window of tree %(tree)s: each path with a row in samples %(samples)s,
# which are the tree's samples in the id range (%(after)s, %(last)s], with
# its first row's created, prev_rbytes and prev_error_stage, and its current
# row's rbytes and error_stage (NULLs if it has none). The range lets the
# BRIN index find the rows; the list drops other trees' rows in it.
_WINDOW_ROWS = """SELECT p.path, f.created, f.prev_rbytes, f.prev_error_stage,
       c.path_id IS NOT NULL, c.rbytes, c.error_stage
FROM (
    SELECT DISTINCT ON (d.path_id) d.path_id, d.created, d.prev_rbytes,
           d.prev_error_stage
    FROM trackdir.dir_state d
    WHERE d.sample_id > %(after)s AND d.sample_id <= %(last)s
      AND d.sample_id = ANY(%(samples)s)
    ORDER BY d.path_id, d.sample_id
) f
JOIN trackdir.path p ON p.id = f.path_id
LEFT JOIN trackdir.current_dir_state c
       ON c.tree_id = %(tree)s AND c.path_id = f.path_id
"""
# E: tree %(tree)s's current paths without rbytes, from the partial index.
_CURRENT_UNKNOWN = """SELECT p.path, c.error_stage
FROM trackdir.current_dir_state c JOIN trackdir.path p ON p.id = c.path_id
WHERE c.tree_id = %(tree)s AND c.rbytes IS NULL
"""
# The current rows of the paths %(paths)s of tree %(tree)s, found through
# path's index; current_dir_state.path has none.
_CURRENT_OF = """SELECT p.path, c.rbytes, c.error_stage
FROM trackdir.path p
JOIN trackdir.current_dir_state c ON c.tree_id = p.tree_id AND c.path_id = p.id
WHERE p.tree_id = %(tree)s AND p.path = ANY(%(paths)s)
"""
# One current path of tree %(tree)s strictly below a directory: in bytes
# (%(dir)s || '/', %(dir)s || '0'), as '0' follows '/'. _WITNESS_ROOT: any
# current path but ROOT.
_WITNESS = """SELECT p.path FROM trackdir.path p
JOIN trackdir.current_dir_state c ON c.tree_id = p.tree_id AND c.path_id = p.id
WHERE p.tree_id = %(tree)s AND p.path > %(lo)s AND p.path < %(hi)s
ORDER BY p.path LIMIT 1
"""
```

`_WITNESS_ROOT` is the same with `p.path <> %(root)s`. In `_growth`:
- **once per tree:** read E (`_CURRENT_UNKNOWN`) and seed the current-row cache with it.
- **per distinct start index from `window_start`:**
  - read W: start `None` if created, else `(prev_rbytes, prev_error_stage)`; end `(rbytes, error_stage)` if current, else None;
  - find a witness for each of `witness_dirs(W)`;
  - compute `kept = kept_paths(W ∪ E ∪ witnesses)`;
  - read with `_CURRENT_OF` the current rows of the kept paths that are neither in W nor in the cache;
  - run `own_growth(window_ends(W, cache, kept), max_depth, top)`.

  `summarize`'s docstring replaces "Growth reads each tree's current state, and for each window the earlier rows of the paths changed since" with "Growth reads, for each window, the rows stored in it, the tree's current directories without rstats, and the current rows of the directories these name and of their ancestors".

- [ ] **Step 1: Write the failing tests**

In `tests/test_summary.py`, keep the old computation as the test reference:

```python
def full_growth(db, tree_id, max_depth, start_id, top):
    """own_growth over every path of the tree, from each path's latest row up
    to sample START_ID and its current row: what _growth computed before."""
```

It reads all of `current_dir_state` and each path's latest row at or before START_ID (`DISTINCT ON … ORDER BY sample_id DESC`).

The existing growth tests are rewritten to name their windows. Captures made with `edit(..., hours)` start hours apart, so `since=[Window("x", 3600 * h)]` selects a start. Each keeps its expected `DirGrowth`s. `test_growth_since_first_and_previous_sample` becomes `test_growth_over_two_windows`, which uses one window clipped to the first sample and one that starts at the second-to-last. New tests:

```python
def test_growth_matches_the_full_computation_for_every_start(db, dsn, subtree, tmp_path):
    # initial, then edits: touch, remove, add, fix, readdir_failed on a listed
    # depth-1 dir with children, then that dir listed again, then a removed dir
    # added back. For every start index s < last, with a window resolving to s:
    # summarize(...).growth == full_growth(...) for top 1000

def test_growth_marks_a_dir_unlisted_only_at_the_start(db, dsn, subtree, tmp_path):
    # depth-1 dir with children: readdir_failed with rbytes in sample 2, listed
    # and grown in sample 3, children unchanged; window from sample 2:
    # it is shown with subtree=True and none of its children are

def test_growth_of_a_path_recreated_in_the_window_is_new(db, dsn, subtree, tmp_path):
    # removed in sample 2, added back in sample 3; window from sample 2:
    # DirGrowth(path, None, size, size, False)

def test_growth_of_a_window_without_rows_is_empty(db, dsn, subtree, initial, tmp_path):
    # sample 2 stores nothing (a capture identical to the first):
    # Growth(window, since=initial id, clipped=False, positive_own=0, dirs=())

def test_windows_resolving_to_one_start_are_read_once(db, ..., monkeypatch):
    # since=[1d, 7d] on two samples 1 h apart: both Growths equal but for
    # window; _WINDOW_ROWS executed once (record Cursor.execute as in
    # test_summary_without_growth_reads_no_history)
```

In `tests/test_db.py`, beside `test_growth_after_a_large_sample_reads_fresh_statistics`:

```python
def test_growth_queries_use_indexes_on_stale_statistics(db, monkeypatch):
    # other = stale_tree(db, monkeypatch, 10_000), then store a sample of
    # `other` changing 5 directories (below ANALYZE_AFTER, so no ANALYZE).
    # EXPLAIN each of _WINDOW_ROWS (that sample's window), _CURRENT_UNKNOWN,
    # _CURRENT_OF (5 paths), _WITNESS and _WITNESS_ROOT with real parameters:
    # no "Seq Scan on path" or "Seq Scan on current_dir_state", and the
    # _WINDOW_ROWS plan names dir_state_sample_id. If the planner picks a seq
    # scan legitimately at this size, raise the size; never loosen the
    # assertion.
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_summary.py tests/test_db.py -q -k "growth or window"`
Expected: FAIL (`summarize` takes no `since`; `Growth` has no `window`).

- [ ] **Step 3: Implement**

Do what the Interfaces block gives. Delete `_CURRENT_ENDS`, `_START_ENDS`, `_UNLISTING`, `End` and the moved code from `db.py`. `summary._growth_lines` keeps its present heading for now, as Task 5 rewrites it.

- [ ] **Step 4: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass. Also run `test_growth_queries_use_indexes_on_stale_statistics` three more times, and it must pass every time.

- [ ] **Step 5: Commit**

Subject: `db: growth over time windows, read from the window's rows`. The body states:
- the cost per window (Design);
- the anchor at the latest sample;
- that the old sample windows are gone;
- that the full computation survives only as a test reference.

---

### Task 5: `--since`, the headings and the texts

**Files:**
- Modify: `src/trackdir/config.py`, `src/trackdir/cli.py`, `src/trackdir/summary.py`, `README.md`, `docs/terms.md`
- Test: `tests/test_config.py`, `tests/test_cli.py`, `tests/test_summary.py`

**Interfaces:**
- Consumes: `Window`, `DEFAULT_WINDOWS` (Task 3), `summarize(..., since)`, `Growth` (Task 4).
- Produces:
  - **`config.duration_seconds(text: str, what: str, max_days: int | None) -> int`**:
    - the grammar is `_INTERVAL`'s;
    - it raises `ValueError(f'{what} must be <integer><unit> with unit s, m, h or d, e.g. "6h": {text!r}')`, or `ValueError(f"{what} must be at most {max_days}d: {text!r}")`.
  - **`parse_interval`** becomes `Interval(duration_seconds(text, "interval", MAX_INTERVAL_DAYS), text)`. Its messages are unchanged.
  - **`summary.run_summary(dsn, tree, recent, top, since: Sequence[Window])`**.
  - **`db summary --since DURATION`**:
    - `action="append"`, metavar `DURATION`, default None, which becomes `DEFAULT_WINDOWS`;
    - type `_window(text) -> Window`, via `duration_seconds(text, "a duration", None)`. There is no upper bound, because history is meant to reach years;
    - help: `show growth in the DURATION (e.g. 6h, 7d) up to each tree's latest sample; repeatable (default: 1d and 7d)`.
- The headings:
  - `  grew most in 7d, since sample 12 (2026-11-05T14:25:00Z), by own growth:`
  - clipped: `  grew most in 7d (all history), since sample 1 (2026-10-20T20:25:00Z), by own growth:`
  - with no directory, the same followed by ` none` on one line, as today.

  Times use `_moment`.

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
def test_duration_seconds_has_no_bound_without_max_days():
    assert duration_seconds("3650d", "a duration", None) == 3650 * 86_400
def test_parse_interval_messages_are_unchanged(): ...   # the two existing texts

# test_cli.py
def test_since_is_repeatable_and_defaults_to_1d_and_7d(): ...
def test_since_rejects_bad_durations(capsys):  # "7", "0d", "1w": exit 2, message

# test_summary.py
def test_text_heading_of_a_window(): ...          # stats_of(...) with a Growth
def test_text_heading_of_a_clipped_window(): ...  # "(all history)"
def test_json_growth_has_window_fields(...):      # window, since, since_started
                                                  # (ISO UTC), clipped
def test_cli_since_selects_windows(initial, dsn, subtree, tmp_path, capsys):
    # two samples 1 h apart; --since 30m --since 2h: two headings in that
    # order, the second "(all history)"
```

Update `test_cli_top_selects_growth` and the full-text rendering test (around line 574) to the new headings.

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_config.py tests/test_cli.py tests/test_summary.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement, then rewrite the texts**

- **`DB_SUMMARY_DESCRIPTION`**: "…and the directories, at any depth, with the most own growth in each window given by --since (default: the last day and the last week)".
- **`DB_SUMMARY_EPILOG`**: its first growth sentence becomes: "A window of --since D ends at the tree's latest sample and starts at the latest sample at least D before it; a tree with a shorter history is shown from its first sample, marked (all history). A window needs 2 samples." Then add: "Growth reads the rows stored in each window, so a long window over a long history takes longer." The rest stays.
- **`README.md`**, the `db summary` example: the new headings, from a real run on a dev database with anonymized fixture data.
- **`docs/terms.md`**: add the row "growth window | the span `db summary` shows growth over: from the latest sample at least `--since` before a tree's latest sample, or from its first, to its latest." Add this row next to "growth".

- [ ] **Step 4: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

Subject: `summary: --since selects growth windows`.

---

### Task 6: Measure, check on real data, record

**Files:**
- Modify: `docs/superpowers/specs/2026-11-11-schema-v5-design.md`, `docs/superpowers/plans/2026-11-05-restricted-growth.md`

- [ ] **Step 1: Rebuild the dev database**

Recreate the dev database, since the DDL changed in Task 1. Then replay the four series in `captures/` oldest first per series, the epoch after `date=` sorting them, with `trackdir sample --replay --tree NAME`. Use the names `ana`, `exp`, `sim` and `user`.

- [ ] **Step 2: Check against the full computation on real data**

Run a throwaway script in the scratchpad, not committed. It imports `full_growth`'s logic and compares `summarize`'s growth with it for every tree and every start index (9 per tree, 36 windows), with `top=1000`. Expected: all 36 are equal. Report the number of kept paths against the tree's size for the largest window of the 855k tree.

- [ ] **Step 3: Measure**

Run `/usr/bin/time -f "%e s %M KiB" venv/bin/trackdir db summary`:
- with the defaults;
- with `--top 0`;
- with `--tree sim`;
- with `--tree sim --since 30d`, which covers the whole history.

The baseline, from before Task 1 on the same data, is: 4.82 s and 593 MB; 0.15 s with `--top 0`; 4.36 s with `--tree sim`. Run `EXPLAIN (ANALYZE, BUFFERS)` for `_WINDOW_ROWS` of the sim tree's last window and keep the plan's top lines.

- [ ] **Step 4: Record**

In the spec:
- the status line names this plan;
- section 5's **`db summary`** paragraph says what growth reads now, and points to this plan;
- under "Measured on version 5", a dated paragraph gives the numbers of Step 3 and the result of Step 2.

The 2026-11-05 plan's warning becomes "Superseded by `2026-11-12-growth-windows.md` (2026-11-12)".

- [ ] **Step 5: Commit**

Subject: `docs: growth windows measured on the replayed captures`.
