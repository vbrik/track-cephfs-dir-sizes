# trackdir incremental sample — design

Date: 2026-10-16. Status: draft; the decisions in section 2 were agreed in
brainstorming on 2026-10-16 (the user accepted all proposed defaults), the
document itself awaits approval.

Amended 2026-10-20: `max_dirs` is no longer part of a tree's identity, and the
schema is version 4 (`tree.max_dirs` dropped, no migration). A run over
`--max-dirs` stores nothing, so the limit never shapes what a tree holds; in
the key it only split a tree's history whenever the limit (or its default)
was raised. See the "`max_dirs` in the identity" item of section 10. Where
this spec still lists `max_dirs` as a key field, that is superseded.

## 1. Goal and scope

`trackdir sample` stores a tree's first sample (initial-sample spec). This
spec covers every later sample: **an incremental sample** records only the
directories whose state changed since their last known state, and skips
unchanged subtrees without reading them. That is the point of the project:
rstats make a whole subtree's change visible in one directory read.

In scope:

- the baseline: each directory's last known state, read from the database
- pruning the walk with the baseline, live and in `--replay`
- storing changed directories and recording deleted ones
- retrying directories that had errors
- schema version 2 (amended to 3 after review, section 4): deletion records, a
  new current-state view
- tree identity extended with `max_depth` and `max_dirs` (section 2)
- guards: sample order, a baseline that went stale during the walk
- the CLI summary and documentation changes

Out of scope (later): an unpruned `--full` sample (no longer needed to
deepen: a deeper run is a new tree), overlapping ROOTs (they are separate
trees), inode tracking (a rename is a
deletion plus a creation), indexes for current-state queries unless the
measurement in section 8 says otherwise, readers of the database.

The decisions of capture spec §14 that this spec keeps: the baseline is each
directory's last known state, possibly many samples old; ENOENT means deleted;
pinned directories get no special store rule. Two of its open items are
settled here: which attributes are monitored (section 3: all stored columns)
and the "future rctime acts as pinned" rule (dropped: unnecessary, section 3).

## 2. Decisions

| Topic | Decision |
|---|---|
| Change rule | A directory is *unchanged* iff its probe's row equals its baseline row in every `dir_state` column except `sample_id` and `path` (ino, mode, uid, gid, nlink, rctime, rbytes, rfiles, rsubdirs, error stage and code). Changed directories are stored and listed; unchanged ones are neither (their subtree is pruned). |
| No separate "monitored" set | One rule replaces "monitored attributes" and "future rctime counts as pinned". `getxattr` mode already reads all four rstats, so comparing them costs nothing extra. |
| Deletions | New column `dir_state.deleted boolean`; schema version 2 (3 after the review amendment of section 4), no migration code (no real database exists). One tombstone row per deleted directory, including every baseline descendant. The current-state view hides tombstoned paths. A path created again later gets an ordinary row. |
| Errors | Error rows are stored as changes. A directory whose baseline row, or any baseline descendant's row, has an error is never pruned, so errors are retried every sample. |
| Tree identity | Amends the initial-sample spec: a tree is (cluster fsid, filesystem name, `fs_path`, **`max_depth`**, **`max_dirs`**), the last two being the values the sample was run with (user's decision). A run with a different `--max-depth` or `--max-dirs` than any existing tree's belongs to a different tree: it takes a full initial sample and never sees the other tree's baseline. There is no depth guard and no way to "change" a tree's depth; deepening is a new tree. |
| Stale baseline | The baseline is read before the walk; no connection is held during it. At store time the tree row is locked and the store fails if the tree's latest sample is not the one the baseline came from. |
| Sample order | A sample whose `started` is not later than the tree's latest sample's `started` is refused, because the current-state view orders by `started`. |
| Baseline loading | The whole current state of the tree is read into memory (a dict) before the walk, through the view. No new index now; section 8 measures. |
| CLI | No new flag. `sample` takes an initial sample if the tree has none, an incremental one otherwise. The summary says which, and reports probed, changed, deleted and pruned counts. |
| `sample` row counts | `dirs` and `errors` keep meaning *probed* directories (they come from the header). Counts of stored rows are not kept on `sample` (dropped in version 3): count the sample's `dir_state` rows, tombstones apart. |

## 3. The algorithm

Terms: the **baseline** of a tree is its current state: per path, the latest
non-deleted `dir_state` row (the view). A **row** is a probe's values for the
`dir_state` columns after `path`, as `dir_state_row()` builds them.

### 3.1 Walk with pruning

`walker.walk` is unchanged. The walk's `should_list(probe)` (called with a
probe that has no children yet) becomes:

    (row(probe) != baseline.rows.get(probe.path)   # changed, or new
     or probe.path in baseline.retry)

`walk_within` applies the depth bound (`probe.depth < max_depth`) to this and
to every other predicate (section 6).

- A directory with no baseline row is new: its row differs from `None`, so it
  is listed.
- `baseline.retry` is the set of paths that have an error row in the baseline
  plus all of their ancestors (computed once when the baseline is loaded).
  Without it, a directory that failed once (e.g. `open EACCES` in a non-root
  run) would keep its error row forever: its unchanged parent would be pruned
  and the directory never probed again.
- For an initial sample the baseline is empty, so every directory is listed:
  one code path serves both kinds of sample.
- `should_list` only reads the baseline, so it is safe on the pool's threads.
- Replay uses the same function through `ReplayBackend`, so an incremental
  replay produces what a live run would have, given the same filesystem.

Pruning is sound because rctime, rbytes, rfiles and rsubdirs describe the
whole subtree: if a directory's row is unchanged, nothing below it changed (up
to the limits in section 9).

### 3.2 What the sample stores

After the walk, from the probes (all directories read) and the baseline:

- **Changed rows**: every probe whose row differs from its baseline row (new
  directories included), except probes that mean deletion (below). Includes
  probes with errors, because an error is a state change.
- **Tombstones**: a path is deleted if
  - it is in the baseline and absent from the `children` of its probed parent,
    where the parent was listed successfully (`children` is not None), or
  - its probe failed with `open ENOENT` and it has a baseline row.

  The tombstones also include every baseline descendant of a deleted path (no
  walking needed: they are in the baseline). An `open ENOENT` probe is not
  stored as an error row. `ENOENT` with no baseline row (created and removed
  within one walk) stores nothing.
- A listing is trusted as complete only if it really read the directory or
  `nlink == 2` and `rsubdirs == 1` agree that there are no subdirectories
  (section 6, `probe.py`); a failed rstats read confirms nothing, so that
  directory is read. A false "no subdirectories" would tombstone a whole subtree.
- Nothing is tombstoned below a directory that was not listed: pruned
  directories, directories at max depth, and directories that failed to open or
  to list. Their descendants keep their state.
- An unchanged, probed directory stores no row.

### 3.3 Guards (checked before the walk, rechecked at store)

Pre-check, in this order, each fatal with `ERROR:` and nothing stored: schema
current; the run's `started` is later than the tree's latest sample's (refuse
otherwise). A replay uses the capture's `started`; a live run uses the current
time, which is earlier than the real start, so it never refuses falsely but
catches a latest sample dated in the future.

Store (one transaction): lock the tree row; fail if the tree's latest sample
(by `started DESC, id DESC`) is not the baseline's sample, or if the new
`started` is not later than that sample's. The message says another sample
was stored during the walk (or that the sample is older than the tree's
latest) and that nothing was stored. For an initial sample the baseline's
sample is "none".

## 4. Schema version 2 (amended: version 3)

`SCHEMA_VERSION = 3`; `db init` creates version 3; any other version is
rejected (drop and re-init; no migration, as no real database exists yet).
Version 2 was the schema as first designed below. The review of the
incremental-sample commits amended it to version 3: `sample` loses `max_depth`,
`changed` and `deleted` (bullets below), and `dir_state.sample_id` no longer
cascades on delete.

Changes against the initial-sample spec §5, as first designed (the listing
below shows version 2's `sample` counters; version 3 has none):

```sql
-- tree: two key columns; the unique constraint covers all five
    max_depth    integer NOT NULL CHECK (max_depth >= 0),
    max_dirs     bigint  NOT NULL CHECK (max_dirs > 0),
    UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path, max_depth, max_dirs)

-- sample: two new counters, after errors
    changed          bigint      NOT NULL,  -- dir_state rows stored that are not tombstones
    deleted          bigint      NOT NULL,  -- tombstones stored

-- dir_state: new column, after rsubdirs... (any position)
    deleted     boolean NOT NULL DEFAULT false,
    CHECK (NOT deleted OR (ino IS NULL AND mode IS NULL AND uid IS NULL
        AND gid IS NULL AND nlink IS NULL AND rctime IS NULL AND rbytes IS NULL
        AND rfiles IS NULL AND rsubdirs IS NULL AND error_stage IS NULL))

CREATE VIEW trackdir.current_dir_state AS
SELECT * FROM (
    SELECT DISTINCT ON (s.tree_id, d.path) s.tree_id, d.*
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    ORDER BY s.tree_id, d.path, s.started DESC, s.id DESC
) latest
WHERE NOT deleted;
```

- `dir_state` rows now mean "the state of this path as of this sample, stored
  because it changed"; they are no longer one per probed directory. Docs and
  comments that say otherwise are updated.
- `sample.dirs` is the number of probed directories, `errors` the probed
  directories with an error. Version 2 also had `changed` (non-tombstone rows
  stored) and `deleted` (tombstones stored) on `sample`, and `max_depth`;
  version 3 drops all three because each is derivable (`max_depth` always
  equals its tree's; the counts are counts of the sample's `dir_state` rows)
  and a stored copy could silently disagree with the rows. `SampleResult`
  still reports `changed` and `deleted`, from the diff. They need not equal
  `dirs`: an `open ENOENT` probe counts in `dirs` and `errors` but stores no
  row, so for an initial sample the rows stored are at most `dirs`, with
  equality unless a directory vanished during the walk. `max_dirs` is stored on
  the tree only: the capture header has no such field and the format is not
  changed. A replay takes `max_dirs` from `--max-dirs` (default 1,000,000),
  like a live run.
- A sample's `dir_state` rows are changes, not a listing, so a sample cannot be
  deleted alone: deleting one would drop the only row of every directory
  unchanged since (it would vanish from the view; the next sample would store
  it as new) and could resurrect a path whose tombstone it held. Version 3's
  `dir_state.sample_id` foreign key therefore has no `ON DELETE CASCADE`, and
  deleting a sample that has rows fails. A tree is removed by deleting its
  `dir_state` rows, then its samples, then the tree row. There is no retention
  policy; expiring old samples would first need their rows merged forward.
- The `--max-depth` and `--max-dirs` options are capped at PostgreSQL's
  `integer` and `bigint` maxima (the columns' types), so an out-of-range value
  fails as a usage error rather than at the store, after the walk.
- The latest sample of a tree is defined everywhere as the one with the
  greatest `(started, id)`. Because of the order guard this is also the
  greatest `id`.

## 5. Commands

`trackdir sample` keeps its options (initial-sample spec §6.2); changes:

- `--max-depth` with ROOT stays required. With `--replay` it still defaults to
  the capture's. `--max-depth` and `--max-dirs` are part of the tree's
  identity: change either and the run is a different tree's initial sample.
  The summary and error messages name the tree with both values.
- `--help` no longer says it handles only a first sample. It says: the first
  sample of a tree stores every directory; later samples store only changed
  directories and deletions; a non-root run records unreadable directories as
  errors (retried by later samples) and is partial; the depth of a tree is
  part of its identity (a different one is a new tree).
- The `SamplesExist` fatal message (initial-sample plan: "incremental sampling
  is not implemented yet") is removed. Its replacement for the store-time
  race is the stale-baseline message of section 3.3.
- Live mode: the pre-check needs the tree key, which comes from opening ROOT,
  as now; it now also loads the baseline (one connection, closed before the
  walk).

Summary paragraph on success (stderr, as now), adding the kind and counts,
e.g. `Stored sample 7 (incremental): probed 1204 directories under ROOT to
depth 4, tree (...); 18 changed, 2 deleted, 31 unchanged subtrees skipped;
no errors. Walk 3.2 s, store 0.1 s.` "Skipped" counts probed directories
above max depth that were not listed: `children is None`, no error,
`depth < max_depth`. An initial sample says `(initial)` and skips nothing.

## 6. Code structure

- **New pure module `src/trackdir/baseline.py`** (no psycopg, no I/O):
  `Baseline` (rows by path, retry set, the baseline sample's id and
  `started`, or an empty baseline for a tree with no samples);
  `should_list_changed(baseline) -> ShouldList`; `diff(baseline, probes,
  max_depth) -> SampleDiff` giving changed rows, tombstone paths (section 3.2)
  and the number of skipped directories. The retry set is built by
  `Baseline.build`. The tombstone computation follows the baseline's `children`
  map.
- **`db.py`**: `load_baseline(conn, key) -> Baseline`; `store_sample(conn,
  header, key, baseline, diff, replayed_from) -> int` replaces
  `store_initial_sample` and `has_samples`; `SamplesExist` is replaced by an
  error for the stale baseline / order guard (one `DbError` subclass with the
  facts the message needs). DDL changes as in section 4; `DIR_STATE_COLUMNS`
  gains `deleted`.
- **`sample.py`**: `run_sample` loads the baseline in the pre-check, builds
  the pruned `should_list`, walks (live via `walk_live`, replay via
  `replay_walk`; both take a `should_list` now), runs `diff`, stores.
  `SampleResult` gains the kind and counts; `render_sample_summary` prints
  them.
- **`probe.py`** (builder's call, from the builder check): the `nlink == 2`
  shortcut (`children=()` without a readdir) is taken only if the probe's
  `rsubdirs` is 1 (the directory itself). If `rsubdirs` says there are
  subdirectories, or rstats failed and there is nothing to confirm `nlink`,
  the directory is read. (The first version also trusted `nlink` when rstats
  had failed; the review closed that gap.) Measured
  on the three committed fixtures: 34,933 directories with `nlink == 2`, all
  with `rsubdirs == 1`, and no listed directory with `len(children) !=
  nlink - 2`, so the guard costs no extra reads in practice. It matters now
  because a wrong empty listing would tombstone a subtree. Tests: a fake
  directory with `nlink == 2` and `rsubdirs == 3` is read, not shortcut.
- **`live.py`**: `walk_within` and `walk_live` take an optional `ShouldList`
  (None: list everything above max depth). `walk_within` itself combines it
  with `list_above(max_depth)`, so no walk lists a directory at max depth
  whatever the predicate says; `should_list_changed` does not repeat the
  bound. The walker and `model.py` are unchanged except what they export.
- Tombstone and changed-row construction are pure and tested without a
  database.

## 7. Testing

No test needs Ceph, root or a database unless it opts in (as before).

| Area | Tests |
|---|---|
| `baseline` (pure) | retry set = error paths plus ancestors; `should_list`: new dir listed, changed listed, unchanged pruned, at max depth never listed, error-retry listed; `diff`: unchanged stores nothing; changed and new stored; error rows stored; listed parent missing a baseline child tombstones it and all its baseline descendants; `open ENOENT` tombstones and stores no error row; ENOENT with no baseline stores nothing; nothing tombstoned below pruned, max-depth, unopenable or unlistable directories; recreated path after tombstone is stored |
| Incremental replay (pure, no DB) | replay a fixture, then a copy edited in memory (a changed rbytes/rctime chain, a removed subtree, a new directory): the walk reads only the changed chains and `diff` matches the edits; replaying an unmodified capture a second time reads only ROOT plus the retry chains of error directories |
| Schema v3 (opt-in DB) | `db init` creates v3 and is idempotent; another version is rejected; deleting a sample that has rows fails, and a tree is removed rows first; a tree's samples consume one tree id; the CHECK on tombstones; the view hides tombstones and returns the latest row per path |
| Incremental sample (opt-in DB) | trees differing only in `max_depth` or only in `max_dirs` are distinct (the second run is an initial sample and does not see the first's baseline); initial then unchanged: counts `changed 0`; edit then sample: only edited rows stored, view equals the edited capture's state; deletion: tombstones stored and view hides them; recreation shows the new row; error row retried and replaced by a good row once the directory is readable; stale baseline (a sample stored between pre-check and store) fails and stores nothing; older `started` refused; a failed store (e.g. COPY error) stores nothing |
| CLI | summary says initial vs incremental; `--help` text; messages for the guards (whitespace collapsed) |

The initial-sample test "second sample of the same tree fails before
walking" and "round trip of the walk" change to the incremental cases above;
the round trip becomes "baseline plus stored rows reproduces the walk".

**Manual acceptance** (dev container, replay only, as in the initial-sample
spec, so real names stay out of the dev database): `db init` (version 3),
`sample --replay tests/data/ceph1-sub-depth4.jsonl.gz`, then the same replay
again must fail on the order guard (same `started`); so for the acceptance a
second capture is needed, or a copy of the fixture with a later `started`
and a few edited directories, made by a small throwaway script (not
product code). Expect: only the edited directories and tombstones stored; the
view's row count equals the edited capture's directory count.

## 8. Measurements to take (not assumptions)

- Time and peak memory of `load_baseline` and of `diff` on the 72k-directory
  fixture after an initial sample and after several incremental ones, and
  extrapolated to 1,000,000 directories (`--max-dirs` default). If loading
  takes more than a few seconds per 100k directories, add `tree_id` to
  `dir_state` with an index on `(tree_id, path, sample_id DESC)` or a
  maintained current-state table, in a follow-up. Those remedies address load
  time only. If the baseline's memory exceeds about 0.5 GB per million
  directories, the follow-up is a leaner baseline (e.g. per-path row hashes
  instead of full row tuples).

### Results (2026-10-16)

Method: dev PostgreSQL 18 container on the same host, one tree from the
72,017-directory fixture `ceph1-sub-depth4` (41 error directories), `--replay`
only. An initial sample, then five incremental samples, each from the previous
capture with 20 random depth-3+ directories touched, one subtree removed and
one directory added (`tests/conftest.py: edited_capture`). Each stage was
measured in fresh Python processes: `load_baseline` against the database state
after that sample (time by `perf_counter`; RSS from `/proc/self/status` as growth
over the process after imports and capture loading; peak from `ru_maxrss`;
retained heap from `tracemalloc` in a second run), and `diff` of that stage's
capture against the state before it (best of 3). "Extrapolated" multiplies by
1,000,000 / 72,017 = 13.9; it assumes linear growth, which was not verified at
that size. Throwaway scripts, not in the repository.

| Measurement (72k directories) | Initial | Incremental 1-5 | Per 1M, extrapolated |
|---|---|---|---|
| `load_baseline` time | 0.19 s | 0.18-0.20 s | 2.6 s (0.26 s per 100k) |
| of which the view query alone | 0.13 s | 0.13-0.15 s | 1.9 s |
| baseline retained, tracemalloc | 34.9 MiB | 34.9 MiB | 485 MiB (0.497 KiB per directory) |
| baseline retained, RSS growth | 57.9 MiB | 57.9 MiB | 805 MiB (0.82 KiB per directory) |
| `load_baseline` peak RSS growth | 92 MiB | 119 MiB | 1.25-1.62 GiB |
| `diff`, pruned walk's probes | 0.037 s (72,017 probes, all changed) | 0.014-0.016 s (17,500-19,400 probes, 49-54 changed, 4-25 deleted) | not extrapolated: depends on the change, not the tree |
| `diff`, all 72k probes (worst case) | 0.038 s | 0.056-0.061 s | 0.8 s |
| `diff` peak extra heap, tracemalloc | 1.9 MiB | 2.4 MiB | 33 MiB |

The baseline's size and load time did not grow over the five incremental
samples: only changed rows were added, and the view returns one row per path.

Verdict:

- **Load time: no index needed.** 0.26 s per 100k is an order of magnitude under
  the "a few seconds per 100k" threshold. The view exposes `tree_id` (from
  `sample`), and `WHERE tree_id = X` is pushed into the view's `DISTINCT ON`
  subquery (`tree_id` is one of its `DISTINCT ON` columns). `EXPLAIN` on a
  database with three trees shows a filtered scan of `sample` followed by a
  nested loop into `dir_state_pkey` by `sample_id`, so only that tree's rows
  are read. (The first version of this note claimed that every `dir_state` row
  was scanned; the review corrected it.) The `sample` scan still grows with the
  number of samples. The timing measurement itself had one tree and six
  samples; revisit when the database holds many samples.
- **`diff`: no concern.** Under 0.1 s for 72k probes; about 15 ms for a realistic
  incremental walk.
- **Memory: the threshold is reached, and an index would not help.** The
  retained heap is 0.497 KiB per directory, 0.5 GB per million; the process
  grows by 0.8 GB per million, and the peak during loading is 1.25-1.62 GiB per
  million (cause not investigated).
  The remedies named above (a `tree_id` index, a current-state table) change
  how rows are found, not how many are held in the baseline dict, so they do not
  address this threshold. Nothing is added in this plan; if trees near
  `--max-dirs` matter, the follow-up is a smaller baseline (e.g. compact rows or
  keeping only what pruning compares), not an index.

## 9. Documentation

- README: status line; `sample` usage for incremental samples; the depth rule;
  non-root samples; limits below; `TRACKDIR_DB`/DSN notes unchanged.
- `docs/terms.md`: tree (now including `max_depth` and `max_dirs`), baseline, incremental sample, pruned (skipped) subtree,
  tombstone (deletion record), retry set; "sample" no longer says "later".
- `docs/conventions.md`: `dir_state` rows are stored changes, not a full
  listing; the latest sample is by `(started, id)`.
- Capture spec §14: amendment note (change rule, tombstones, error retry; the
  "future rctime" item dropped). Initial-sample spec: amendment note (tree identity
  extended, view revisited, `SamplesExist` replaced, schema version 2);
  `TreeKey`, `tree_key_of` and `describe_tree` gain the two fields (the key
  is built from the header plus `max_dirs`, which is not in the header).
- Review amendment (schema version 3): section 4.

## 10. Risks and open items

- **Lazy propagation.** CephFS propagates rstats up the tree with a delay. A
  change may be invisible at a parent for a while; the next sample sees it. A
  change is delayed, not lost.
- **Pinned rctime.** Under a pinned directory (future rctime) a change that
  keeps rbytes, rfiles and rsubdirs equal (a same-size rewrite) is invisible
  to pruning. Documented limit; an unpruned full sample would catch it.
- **Renames** are a deletion plus a creation; `ino` is recorded, not used.
- **Persistent errors** (e.g. a root-only directory seen by a non-root
  sampler) keep their ancestors in the retry set: each sample re-probes the
  path to them and lists each ancestor's children. Cost is proportional to
  the number of such directories times their depth, not to the tree.
- **Mixed users.** A non-root sample after a root one stores error rows for
  directories it cannot read; the next root sample restores them (the retry
  set reaches them). Anyone reading current state meanwhile sees the errors.
- **Memory.** The baseline is a dict of all current paths (about 0.5 KB per
  directory retained, up to about 1.6 GB peak per million while loading;
  measured, see section 8); it can exceed `--max-dirs` if directories
  were deleted and the tree has more history than live directories; tombstoned
  paths are not in the baseline.
- **Large deletions** write one tombstone per baseline descendant in one
  transaction.
- **A mistyped ROOT** that resolves to a CephFS directory with a new
  `fs_path` starts a new tree with a full initial sample; the summary says
  `(initial)`.
- **`max_dirs` in the identity is a trap.** (Resolved 2026-10-20: the key is
  now `max_depth` only, as recommended below; raising the default to
  10,000,000 would otherwise have split every existing tree.) It is a memory guard, not a
  property of the tracked space, and an incremental walk probes far fewer
  directories than the tree holds. If a sample ever exceeds the limit (a
  large reorganization) the user must raise `--max-dirs`, which starts a new
  tree with a full initial sample, exactly when that is most expensive, and
  the history is split. Recommendation recorded here, not applied: key on
  `max_depth` only and treat `max_dirs` as a per-run guard. Applied as the
  user decided, and confirmed after the review of these commits (2026-10-16):
  the key keeps `max_dirs`, and the limit error now says plainly that raising
  `--max-dirs` or changing `--max-depth` starts a new tree and leaves this
  tree's history behind. Revisit if it bites.
- **One ROOT, several trees.** Different `max_depth` store the
  same directories again under another tree; nothing links them. Readers of
  the database must pick one tree.
