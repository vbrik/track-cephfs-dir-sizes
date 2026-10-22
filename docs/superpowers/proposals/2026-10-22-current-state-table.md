# Proposal: a maintained current-state table

Status: proposal, not approved. Written 2026-10-22 at the end of a session that
reworked `trackdir db summary` (commits 66254fe, b934745). This is a handoff
for a new session: it explains the problem, the proposed fix, and the
decisions that only the user can make. PostgreSQL stays the database.

## Read this first

- `AGENTS.md`, and `docs/conventions.md`, `docs/schema.md`, `docs/terms.md`.
- Ask clarifying questions before designing. The user prefers questions in
  one numbered list with your default in brackets, and answers by number.
- **Measure before building anything** (see "Step 0"). The problem below is
  a cost seen on a small test database; it is not yet confirmed on the
  user's real one.
- Tests: `venv/bin/python -m pytest`. DB tests need
  `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)`; start the
  dev server with `python3 scripts/postgres.py start --ephemeral`.
- `scripts/` and `tests/test_postgres_script.py` are being changed by
  another session. Don't edit or commit them; their test failures are not
  yours.
- The repo is public: no real cluster fsids, IPs, host names or real
  directory paths in files or commit messages; use placeholders.
- Don't commit unless the user says "commit"; never push.

## How data is stored today

A **sample** stores only what changed since the tree's previous sample: one
`dir_state` row per new or changed directory, plus a tombstone row
(`deleted = true`) per deleted one. A directory with no row in a sample is
unchanged. See `docs/schema.md`.

So "the current state of a tree" is not stored anywhere. It is rebuilt on
every read by the view `trackdir.current_dir_state` (DDL in
`src/trackdir/db.py`):

```sql
SELECT * FROM (
    SELECT DISTINCT ON (s.tree_id, d.path) s.tree_id, d.*
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    ORDER BY s.tree_id, d.path, s.started DESC, s.id DESC
) latest
WHERE NOT deleted;
```

## The problem

Every read of current state sorts the full history of the tree and keeps
each path's latest row. Readers:

| Reader | Where | When |
|---|---|---|
| `load_baseline` | `db.py` | every `trackdir sample` run, before the walk |
| `_CURRENT_ERRORS` | `db.py`, `summarize` | every `db summary` |
| `_CURRENT_ENDS` (growth) | `db.py`, `_growth` | every `db summary`, once per tree |

Costs observed or inferred:

1. **Each read sorts the tree's whole history.** The cost tracks the number
   of `dir_state` rows of the tree, not the number of current directories.
   History grows slowly when trees change little (about 50 rows per
   incremental sample on the test tree), but faster on trees with a lot of
   churn, and the initial sample's rows are sorted every time regardless.
2. **A query for one tree reads all trees' rows.** The tree filter applies
   to `sample` only. `EXPLAIN` showed a sequential scan of the whole
   `dir_state` table hash-joined to `sample`. With T trees, a summary scans
   `dir_state` about T + 2 times.
3. **Planner misestimates.** `sample` is small, so autovacuum may never
   analyze it. The planner then guesses a few rows where there are hundreds
   of thousands. Combined with CTEs, this produced plans that ran for
   minutes, which is why growth's tree logic moved to Python in b934745.
4. **Separate but similar:** `_SAMPLES` in `summarize` derives each sample's
   new/modified/deleted counts with a window function (`lag`) over every
   `dir_state` row of the reported trees. That is also a full-history sort
   on every summary.

Measured on the dev database (one tree, 227k directories, 12 samples, 228k
`dir_state` rows): `db summary` takes 1.5 s, and 0.45 s with `--top 0` (no
growth). The user's database has 4 trees, 784 samples, 3.3M `dir_state`
rows and 977 MiB on disk; its timings are **unknown**.

## Step 0: measure on the user's database

Ask the user to run these. They are read-only, and the numbers decide
whether this proposal is worth doing:

```sh
time trackdir db summary > /dev/null
time trackdir db summary --top 0 > /dev/null
time trackdir sample ...           # the total, including the baseline load
```

and in `psql`:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM trackdir.current_dir_state WHERE tree_id = 1;
SELECT relname, n_live_tup, last_analyze, last_autoanalyze
FROM pg_stat_user_tables WHERE schemaname = 'trackdir';
```

If the summary and the baseline load take a few seconds, stop here and tell
the user the proposal isn't needed yet.

## Proposed fix

### 1. A table that holds each tree's current state

Replace the view with a table `trackdir.current_dir_state` that has the same
columns (`tree_id`, then those of `dir_state`, `deleted` included or
dropped). Primary key `(tree_id, path)`. Keeping the name means
`docs/schema.md`'s rule "read current state from `current_dir_state`" and
external queries keep working.

`store_sample` maintains it in the same transaction that inserts the
`dir_state` rows. That transaction already locks the tree row, so samples of
one tree are serialized:

- For changed rows, upsert: `INSERT ... ON CONFLICT (tree_id, path) DO
  UPDATE`, or `MERGE`.
- For tombstones, delete the path's row.
- Efficient bulk form: COPY the sample's rows into a temporary table (or
  reuse the `COPY` into `dir_state` and select back by `sample_id`), then
  apply one `INSERT ... SELECT ... ON CONFLICT` and one `DELETE ... USING`.

A tree's initial sample writes every directory twice: once in `dir_state`
and once in the current table. Incremental samples write only their few
changed rows twice.

### 2. Readers use the table

- `load_baseline`, `_CURRENT_ERRORS` and `_CURRENT_ENDS` read the table with
  `WHERE tree_id = %s`. That is an index range scan on the primary key, with
  no sort and no scan of other trees.
- The growth "start state" query (`_START_ENDS`) is unchanged. It reads only
  the earlier rows of paths changed since the window's start.

### 3. Statistics

Run `ANALYZE trackdir.sample` (or all `trackdir` tables) after storing a
sample, or document that the operator runs it. That stops the planner from
taking `sample` for a near-empty table.

### 4. Tests

- **Invariant test:** after a sequence of samples (new, changed, deleted,
  deleted then re-created, error rows, a whole tree deleted then reborn),
  the table equals the old view's `DISTINCT ON` rebuild over `dir_state`.
  Keep that rebuild query in the tests as the reference.
- Existing sample, baseline and summary tests must pass unchanged.
- A concurrent sample of one tree is still refused at store, leaving the
  table untouched. `test_concurrent_sample_is_refused_at_store` in
  `tests/test_incremental.py` covers the refusal; extend it to check the
  table.

## Decisions for the user

1. **Schema version 5 and existing data.** The project's rule is "no
   migrations; a version mismatch is fatal", and before now a version bump
   meant dropping and re-initializing the database. Here that would lose
   the user's 784 samples. Options:
   - a one-off upgrade command that creates the table and fills it from the
     old view;
   - a documented SQL script that does the same;
   - re-initializing (history is lost).

   [Recommend a one-off `trackdir db upgrade` for v4 → v5 only, or a
   documented SQL script.]
2. **Keep the name `current_dir_state` for the table?** The alternative is
   a new table name with a compatibility view. [Keep the name.]
3. **Keep tombstones in the current table?** Without them it holds only
   present paths, as the view did. [Drop them, as the view did.]
4. **The per-sample counts (`_SAMPLES`, cost 4).** Options:
   - leave them as they are;
   - store new/modified/deleted on the `sample` row at store time;
   - compute them only for the samples shown.

   Storing them contradicts an earlier, recorded decision: "What a sample
   stored is counted from its dir_state rows, not kept redundantly" (comment
   above `DDL` in `db.py`). Flag that to the user. [Ask; measure its cost
   first.]
5. **`ANALYZE` after each sample, or leave it to operators.** [After each
   sample: cheap, and the sample command already owns the transaction.]

## Out of scope

- Changing the database engine. PostgreSQL stays: several hosts may sample
  into one server.
- Snapshot or checkpoint tables for fast "state at sample N" reads. Growth
  needs the state at a window's start, but `_START_ENDS` already avoids a
  full rebuild by reading only paths changed since then.
- The growth algorithm itself (`db.own_growth`), finished in b934745.
