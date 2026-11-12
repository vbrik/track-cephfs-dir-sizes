# Schema version 5 and long-run robustness — design

Date: 2026-11-11. Status: draft; the decisions in section 2 were agreed in
discussion on 2026-11-11, the document itself awaits approval.

## 1. Goal and scope

trackdir is meant to record 10 years of hourly samples of about 10 million
long-lived directories, plus seasonal bursts of short-lived ones, and to stay
usable throughout. PostgreSQL stays (section 3). Schema version 4 does not
meet the goal: the work of every sample and every summary grows with the
tree's whole history, and the daemon stops when a cycle overruns.

The design rule: **the work of a sample depends on the tree's live
directories and on what changed, never on how much history is stored.**

The data has two uses, and the schema serves both without a second
representation:

- **Reconstruction**: a batch script replays all changes from the first
  sample, to feed a display tool not chosen yet. Hours are acceptable.
- **Recent activity**: "what happened in the last day, week or month", from
  an interactive CLI. Not built here; the schema must make it fast.

In scope:

- schema version 5: a path dictionary, a stored current state, previous
  values and counts kept redundantly, per-phase times
- `trackdir sample` and `trackdir db summary` on the new schema, with the
  same output apart from tree names
- the daemon's overrun rule: an overrun no longer stops the daemon
- tree names: a tree is named by the user, and the name is checked against
  the mount on every sample
- a migration rule for later schema versions
- documentation: `docs/schema.md`, terms, conventions, README, `--help`

Out of scope:

- the recent-activity command, anomaly detection ("anything unusual"), and
  any rollup table for them: "unusual" has no definition yet, and a rollup
  can be derived from the change log later
- the reconstruction script
- keeping baselines in the daemon's memory between cycles: decided "not yet,
  measure first" (section 10)
- thinning old samples, table partitioning, de-duplicating overlapping trees
- letting a tree change depth: another `max_depth` needs another tree

The four parts (schema, overrun rule, tree names, migration rule) are
related only by the goal. They are one spec so the reasoning is in one
place, but each gets its own implementation plan (section 9).

## 2. Decisions

| Topic | Decision |
|---|---|
| Database | PostgreSQL stays. |
| Existing data | The current database is test data: version 5 is created with `db init` on an empty database, with no migration from version 4. |
| Paths | Stored once, in a dictionary table; every other table refers to a path by integer id. A path deleted and created again keeps its id, so its history continues. Renames are not tracked. |
| Path form | Relative to the tree's ROOT, as today. A path means nothing without its tree: trees may overlap, and the same relative path can exist in several. |
| Current state | A table maintained by the transaction that stores a sample, holding live directories only. It replaces the view. |
| Redundancy | Allowed where it removes a read of history: each change row carries the previous values of the three additive rstats, and each sample row carries its counts of new, modified and deleted directories. This reverses the earlier rule "counted from its rows, not kept redundantly". |
| Overrun | A cycle that runs past the next one's start is logged as an error and finishes; nothing is aborted and the daemon keeps running. It stops only on real errors, however many cycles overrun. |
| Tree names | The user names each tree. A new name creates a tree and records what the mount says it is; a known name is sampled only if the mount still says the same. So a changed mount, a moved daemon or a wrong ROOT cannot split or corrupt a history silently. |
| Changed mount | Accepting a new mount identity for an existing name is a documented one-row SQL update; there is no command for it. |
| Table names | `dir_state` and `current_dir_state` are kept, so the terms in `docs/terms.md` stay valid. |
| Overlapping trees | Stored, sampled and reported per tree; nothing is de-duplicated. |
| Migrations | Version 5 is the first production schema. Every later version ships an upgrade step from the version before it, and trackdir refuses to run until it is applied. |
| Baseline in memory | Not now. Measure the baseline load on the new schema first. |

## 3. Measurements behind the design

From a test database of 4 trees and 3.3 million live directories, sampled for
21 days, the last 5 hourly (2026-11-11, PostgreSQL 18):

| Measure | Value |
|---|---|
| Stored rows | 3.66 million; 3.24 million of them from the four initial samples |
| Size | 1.14 GB: 752 MB heap, 387 MB primary-key index; about 311 bytes per row |
| Rows per hourly sample | 41 to 271 per tree, about 500 together; one sample stored 7,516 |
| Rows per path | median 1 in every tree; 99th percentile 1 to 5 |
| Rows from the busiest 1% of paths | 8% to 50% per tree: ancestors change in every sample |
| Paths ever deleted | about 8,800; about 2,000 of them created again, up to 7 times |
| Current state of the largest tree through the view | 2.0 s: sorts 2.1 million rows, spills to disk |
| History of one path, 3,065 rows | 28 ms: one index probe per sample of the tree |
| Walk of one tree, hourly | 75 s on average, 1,881 s at worst |

Projected to 10 years at 10 million long-lived paths (3 times the measured
rate), with about 3 rows per short-lived directory:

| Short-lived directories per year | Rows | Size at 311 bytes per row |
|---|---|---|
| almost none, as measured | about 140 million | about 45 GB |
| 10 million | about 440 million | about 140 GB |
| 100 million | about 3.1 billion | about 1 TB |

The churn figures are assumptions: the test data has almost none. These
volumes are ordinary for PostgreSQL. The alternatives were rejected: a
time-series database treats each path as a series, and millions of mostly
short-lived series are what those handle worst (most also require UTF-8
labels); a columnar store compresses better but adds operations work for a
volume that does not need it; SQLite could hold the data but gives up network
access, read-only roles and readers concurrent with the sampler.

### Measured on version 5

`scripts/loadtest.py`, 2026-11-11, PostgreSQL 18 on local NVMe storage (faster
than the network storage planned for production, so read the trend, not the
absolute times): one tree of 1 million directories, 200 samples that change
about 3,700 rows each, and every 20 samples a burst of 200,000 directories
that is deleted 10 samples later.

| Measure | Start of the run | End of the run |
|---|---|---|
| `dir_state` rows | 1.0 million | 5.3 million |
| Paths ever seen | 1.0 million | 2.8 million |
| Baseline load, 1.0 million live directories | 1.8 s | 1.8 s |
| Store of an ordinary sample | 0.25 s | 0.20 s |
| Store of a burst's creation, 200,000 new directories | 6.4 s | 9.5 s |
| Store of a burst's deletion, 200,000 tombstones | 3.8 s | 5.5 s |

The first sample, 1 million directories, took 29 s to store. A change row
costs about 160 bytes with its index entry (version 4: 311). With the short
paths of this test (16 bytes on average) a path costs about 140 bytes in
`path` with its two indexes; real paths are several times longer.

- **Ordinary samples do not depend on history**, as designed.
- **A large sample does, within a bound.** For a sample of hundreds of
  thousands of rows the planner joins the staging table to `path` by hashing,
  which reads every path the tree ever held, in three statements (four with
  deletions). That explains part of the rise; the rest was not attributed and
  is probably index maintenance on the growing tables. Once `path` is large
  enough, index lookups are cheaper than the scan, so the cost levels off at
  about the sample's rows times an index lookup.
- Foreign-key checks cost 0.5 to 0.7 s per constraint per 200,000 rows,
  whatever the history: about half of a burst's store time.

Decided 2026-11-12: the burst cost is accepted as it is. A burst of 200,000
directories stores in seconds against an hourly interval, and the rise is
bounded. Resolving path ids once into the staging table (one join instead of
three or four) would reduce it and is the first thing to try if large samples
ever become slow. The foreign keys stay, including those of
`current_dir_state`. A run with 10 million directories was not made, so the
figures for that size remain the projections above.

## 4. Schema version 5

`meta` is unchanged; `tree` gains a `name` (section 7). `dir_state` keeps its
name and its meaning (a row is stored only when a directory changed), and
`current_dir_state` keeps its name but becomes a table.

```sql
CREATE TABLE trackdir.path (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id bigint NOT NULL REFERENCES trackdir.tree,
    path    bytea  NOT NULL,  -- relative to ROOT; '.' is ROOT
    UNIQUE (tree_id, path)
);

CREATE TABLE trackdir.dir_state (
    path_id       bigint  NOT NULL REFERENCES trackdir.path,
    sample_id     bigint  NOT NULL REFERENCES trackdir.sample,
    ino bigint, mode integer, uid bigint, gid bigint, nlink bigint,
    rctime bigint, rbytes bigint, rfiles bigint, rsubdirs bigint,
    error_stage   text,
    error_code    text,
    created       boolean NOT NULL,  -- the path had no current row before
    deleted       boolean NOT NULL,  -- a tombstone
    prev_rbytes   bigint,
    prev_rfiles   bigint,
    prev_rsubdirs bigint,
    PRIMARY KEY (path_id, sample_id)
    -- plus version 4's CHECKs, and CHECK (NOT (created AND deleted))
);
CREATE INDEX ON trackdir.dir_state USING brin (sample_id);

CREATE TABLE trackdir.current_dir_state (
    tree_id   bigint NOT NULL REFERENCES trackdir.tree,
    path_id   bigint NOT NULL REFERENCES trackdir.path,
    path      bytea  NOT NULL,                             -- copy of path.path
    sample_id bigint NOT NULL REFERENCES trackdir.sample,  -- stored this row
    ino bigint, mode integer, uid bigint, gid bigint, nlink bigint,
    rctime bigint, rbytes bigint, rfiles bigint, rsubdirs bigint,
    error_stage text,
    error_code  text,
    PRIMARY KEY (tree_id, path_id)
);
CREATE INDEX ON trackdir.current_dir_state (tree_id)
    WHERE error_stage IS NOT NULL;
```

`sample` gains five columns:

| Column | Type | Meaning |
|---|---|---|
| `new_dirs`, `modified_dirs`, `deleted_dirs` | bigint | Rows the sample stored with `created`, with neither flag, and with `deleted`. |
| `baseline_seconds` | double precision | Time to load the baseline before the walk. |
| `store_seconds` | double precision | Time from the start of the store transaction to just before its commit. |

Why each piece:

- **`path`** is append-only and holds every path ever seen, once. A change
  row shrinks to fixed-width integers (an estimate from column widths: about
  half of today's bytes per row), and the index that serves a directory's
  history has a 16-byte key instead of the path.
- **Primary key `(path_id, sample_id)`** reads one directory's history as one
  index range, however many samples the tree has.
- **BRIN on `sample_id`**: the table is only appended to, so rows lie in
  sample order and a block-range index finds a window of samples at a
  fraction of a B-tree's size. A window's rows are one contiguous read.
- **`prev_*`** hold the path's `rbytes`, `rfiles` and `rsubdirs` from its
  current row before the sample: NULL if it had none (`created`) or that row
  had no rstats. A tombstone has NULL values and the last known `prev_*`, so
  the size of what was deleted is in the row. Growth over a window is the
  window's last row minus its first row's `prev_*`: no read outside the
  window. `created` tells "grew from nothing" from "unknown before".
- **`current_dir_state`** holds live directories only: a tombstone deletes
  the row. It carries the path bytes so that loading a baseline never touches
  `path`, which grows with every short-lived directory. It is the only table
  updated in place; it is small and autovacuum keeps it so.
- **The partial index** finds current errors without scanning a tree.
- **Counts and times on `sample`** make each sample's cost readable without
  touching `dir_state`, for the summary and for watching trends.

Invariants, each with a test (section 8):

1. `current_dir_state` equals each path's latest non-tombstone row of
   `dir_state`, rebuilt as version 4's view did.
2. A row's `prev_*` equal the values of the path's previous row; `created` is
   true exactly when there is none or it is a tombstone.
3. `sample`'s three counts equal the counts of its rows.

No views. `docs/schema.md` gives example queries instead: a directory's
history with times, a tree as of a sample, a window's changes, and a path's
absolute path inside the filesystem (`tree.fs_path` joined with `path.path`).

## 5. Storing and loading

**Baseline.** `load_baseline` reads `current_dir_state WHERE tree_id = …`:
live directories only, no sort. The `Baseline` class and the walk are
unchanged.

**Store.** One transaction, under the tree-row lock as today:

1. Insert the `sample` row.
2. `COPY` the changed probes and the deleted paths, with path bytes, into a
   temporary staging table.
3. Insert the staged paths that `path` lacks, so a path seen before keeps
   its id. The tree lock makes a plain anti-join safe, and unlike
   `ON CONFLICT DO NOTHING` it uses no id for a path that exists.
4. Insert into `dir_state` from staging joined to `path` and left-joined to
   `current_dir_state`, which supplies `prev_*` and `created`.
5. Upsert the non-deleted rows into `current_dir_state`; delete the
   tombstoned paths from it.
6. Set the sample's counts and `store_seconds`.

The database, not the client, resolves path ids and previous values, under
the lock that serializes a tree's samples. So they cannot disagree with what
is stored, and the client sends the same data as today. Every step costs in
proportion to the sample's rows.

**Removing a tree** deletes its `dir_state` rows (through its samples), its
`current_dir_state` rows, its samples, its paths, then the tree row.
`docs/schema.md` gives the statements.

**`db summary`** prints what it prints today, plus each tree's name. Counts come from `sample`;
ROOT's history from the primary key; current errors from the partial index;
growth reads `current_dir_state` and the earlier rows of changed paths as
today. The plan of 2026-11-05 (restricted own growth) was written against
version 4 and must be revised: with `prev_*`, a window's start states are in
the window's rows.

## 6. The daemon's overrun rule

Today an overrun aborts the sample in progress and exits with status 1. On
the test database one tree's walk took 1,881 s against an average of 75 s,
with the database idle: the filesystem alone can cause an overrun. A sample
stores changes since the baseline, however old, so a late or missed cycle
loses resolution and nothing else; a stopped daemon loses everything until
someone restarts it.

The new rule:

- No deadline is armed. A cycle always finishes; no sample is aborted.
- When a cycle is still running one interval after it started, the daemon
  logs an ERROR at that moment, naming the tree being sampled.
- The loop's existing rule then applies: the next cycle is due at the last
  start plus one interval, and starts at once if that is past. Missed cycles
  are not queued. Under lasting overload the cycles run back to back, so the
  effective interval is the cycle's duration.
- The log line of a finished cycle says by how much it overran.

Status 1 remains for a `FatalError` and a bug. A probe stuck on an
unresponsive MDS blocks the daemon as it does today.

Accepted risk: the daemon spec made the exit status the alarm because logs
are not watched. Overruns now show only in the log and as gaps between
`sample.started` values; no number of overruns in a row stops the daemon.

## 7. Tree names and migration rule

### Tree names

Today a tree is whatever the mount says: cluster fsid, fs name, fs path and
`max_depth`, and the first sample of an unknown combination creates a tree. A
mount that names the filesystem differently from an earlier one (see "fs
name" in `docs/terms.md`) therefore starts a new, empty history without a
word, for example after the daemon moves to another host. The config file
cannot prevent it: it holds a local ROOT, not what is mounted there.

From version 5 the user names each tree, and the database records what the
name stands for:

```sql
CREATE TABLE trackdir.tree (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text  NOT NULL UNIQUE,
    cluster_fsid text  NOT NULL,
    fs_name      text,
    fs_path      bytea NOT NULL,
    max_depth    integer NOT NULL CHECK (max_depth >= 0),
    UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path, max_depth)
);
```

The four columns after `name` are the tree's **mount identity**, read as
today: the fsid from ROOT's `ceph.cluster_fsid`, which the client takes from
the cluster it is connected to (an all-zero fsid in the mount entry only
warns), the rest from the mount entry. A name is 1 to 64 characters of
`A-Z a-z 0-9 . _ -`, starting with a letter or digit.

Rules, applied before the walk and again in the store transaction:

| The name is | The mount identity is | Result |
|---|---|---|
| unknown | unknown | The tree is created with this name and identity. |
| unknown | recorded under another name | Refused, naming that tree: one directory and depth is one tree. |
| known | the one recorded for it | The sample goes on. |
| known | different | Refused. The message shows the recorded identity and the mount's, field by field. |

Choosing a new name is the explicit request for a new tree, so there is no
separate flag. A wrong ROOT, another filesystem mounted at the same place and
a changed mount option all end in the last row instead of in a new or
damaged history.

Interfaces:

- Config file: each `[[tree]]` needs `name`; two entries with one name are
  rejected when the file is loaded. The daemon checks every tree against the
  rules at startup, so a mismatch stops it before the first cycle.
- `trackdir sample ROOT --tree NAME` and `trackdir sample --replay FILE
  --tree NAME`: `--tree` is required. A replay's mount identity is the
  capture header's.
- `trackdir db summary` shows each tree's name, and its `--tree` takes a
  name instead of an id.

Changing a tree's `root` in the config, or its host, needs nothing as long
as the mount identity is the same. When it is not, and the operator knows it
is the same directory (typically only `fs_name` differs after a move),
`docs/schema.md` gives the one-row update that makes the name stand for the
new identity, so the history continues:

```sql
UPDATE trackdir.tree SET fs_name = 'cephfs' WHERE name = 'projects';
```

`max_depth` is part of what is checked: another depth needs another name.

### Migration rule

Recorded in `docs/conventions.md`:

- Version 5 is the first production schema version.
- A change to the DDL raises the version and adds an upgrade step from the
  previous version, run by `trackdir db upgrade` in one transaction.
- trackdir refuses a database at an older version and names that command; it
  refuses a newer one and asks for a newer trackdir, as today.
- `db upgrade` is built with the first version-6 change, not before.

## 8. Tests

None need Ceph or root; database tests need `TRACKDIR_TEST_DSN`.

- The three invariants of section 4, after a sequence of samples that covers:
  new, modified, deleted, deleted then created again (same `path_id`), error
  rows, a tree emptied and refilled, and two trees sharing relative paths.
- Round trip: store, load the baseline, compare with the stored probes.
- A concurrent or out-of-order sample is refused and leaves every table
  untouched.
- Existing sample, baseline and summary tests pass; their output
  changes only by the tree names.
- `test_schema_doc_covers_the_ddl` covers the new tables and columns.
- Tree names: the four rows of section 7's table, for `sample`, a replay and
  daemon startup; a mismatch message that names the differing field; a
  duplicate or malformed name in the config; a sample that goes on after the
  documented update.
- Overrun: with a fake clock, a cycle longer than the interval logs one
  ERROR, finishes, and the next starts at once; status stays 0 on SIGTERM.
- A synthetic load test, run by hand and not part of the suite
  (`scripts/loadtest.py`): one tree and simulated churn bursts, recording
  baseline load, store time and size. Section 3 has the results of a run with
  1 million directories; none was made with 10 million.

## 9. Implementation order

Three plans, each leaving the tests green:

1. Schema version 5 with `sample`, `db summary` and the documentation.
2. Tree names.
3. The overrun rule.

The migration rule is a paragraph in `docs/conventions.md`, written with
plan 1.

## 10. Alternatives considered

- **Keep the view, add indexes.** An index led by tree and path would avoid
  the sort, but the read would still pass every row of history, and every
  deleted path, to find the live ones.
- **Current state as columns of `path`.** One table less, but deleted paths
  stay in it for ever, so a baseline load would again grow with churn.
- **Absolute paths in `path`.** An absolute path is still ambiguous without
  the cluster and filesystem, which the tree row holds, so it adds a prefix
  to every row and removes no join.
- **Path ids and previous values computed by the client.** The client has
  the baseline, but would need path ids in it and could disagree with the
  database; the join in the store transaction costs the same.
- **One history per filesystem instead of per tree**, with trees reduced to
  sampling scopes. It would de-duplicate overlapping trees and survive depth
  changes, but a directory leaving a scope would have to be told apart from
  a deleted one. Rejected with the decision to keep depth in the identity.
- **Baselines kept in the daemon's memory.** Removes the per-cycle load, at
  about 0.55 GB per million directories for all trees at once. Deferred
  until the load on the new schema is measured.
- **Skipping a tree whose cycle overran**, or aborting its sample as today.
  A tree that is always slow would then never be stored.
- **A flag or config key to allow a new tree** (`--new-tree`,
  `new_tree = true`), keeping the mount as the only identity. A key left in
  the config would disable the guard exactly when a move makes it matter,
  and a history split by a mount change could still not be rejoined.
- **The mount identity written in the config file** (fs name, fs path,
  fsid) and checked against the mount. The same check as a name gives, but
  the operator types what trackdir can read for itself.
- **The name as the only identity, unchecked.** A typo or a wrong ROOT would
  sample another directory into an existing history: a tombstone for every
  old path and a row for every new one, with no error.
