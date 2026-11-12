# Database schema

The PostgreSQL schema `trackdir`, for tools that read the database. The DDL in
`src/trackdir/db.py` (`DDL`) is authoritative; this file explains it.

## Versioning

Table `meta` has one row, `schema_version` (currently 5). Check it before
reading: trackdir refuses a database at another version. Version 5 is the
first production version; version 4 and earlier cannot be migrated. From
version 5 on, every change ships an upgrade step (see
[conventions](conventions.md)).

## Reading rules

- **A sample stores changes, not a listing.** `dir_state` gets a row for a
  directory only if it is new or differs from its previous row, plus a
  tombstone (`deleted`) for each directory that disappeared. A directory with
  no row in a sample is unchanged, not missing.
- **Read current state from `current_dir_state`**, a table of the directories
  that exist now, never from one sample's `dir_state` rows.
- **A path needs its tree.** `dir_state` names a directory by `path_id`; table
  `path` gives its bytes and its tree. Trees may overlap, and the same
  relative path can exist in several, each with its own id and history. A
  path deleted and created again keeps its id.
- **Find a directory by its path through `path`**, whose `(tree_id, path)`
  index gives its id; `dir_state` and `current_dir_state` are indexed by that
  id. A condition on `current_dir_state.path` alone reads the tree's whole
  current state.
- **Each row says what it replaced.** `prev_rbytes`, `prev_rfiles` and
  `prev_rsubdirs` are the directory's rstats before the sample, so the change
  over a window of samples is its last row's value minus its first row's
  `prev_` value: no row outside the window is needed. `created` tells a
  directory that did not exist before from one whose earlier rstats are
  unknown; both have NULL `prev_` values.
- **A tree's samples are ordered by `(started, id)`**; the latest has the
  greatest. trackdir stores a sample only if it started after the tree's
  latest, so `id` order agrees.
- **Pick a tree.** The same directory sampled with another `max_depth` is
  another tree, with its own samples; nothing links the two. So is a replayed
  anonymized capture of it, whose fsid and paths are tokens.
- **Paths are `bytea`**: the filesystem's bytes, which need not be valid
  UTF-8. Decode them as your platform decodes file names (Python:
  `os.fsdecode`). In SQL, write a path as `convert_to('proj/2019', 'UTF8')`.

## Tables

### `tree`

A directory of a CephFS filesystem, sampled to one depth; the same tree whatever
host or mount point samples it, as long as every mount names the filesystem
the same way (see `fs_name`). The four identity columns are unique together.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | Primary key. |
| `cluster_fsid` | text | Ceph cluster fsid. |
| `fs_name` | text | CephFS filesystem name as the mount names it; NULL: the mount did not name one, so it is the cluster's default. A mount that names the default filesystem gives another tree. |
| `fs_path` | bytea | The directory's absolute path inside the filesystem; `/` is its root. |
| `max_depth` | integer | Deepest level sampled; ROOT is depth 0. |

### `sample`

One run of `trackdir sample` over a tree.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | Primary key. |
| `tree_id` | bigint | The tree (`tree.id`). |
| `root` | bytea | ROOT's local path on the host that read it. |
| `rstat_mode` | text | `dirstat` or `getxattr`: how rstats were read (see [terms](terms.md)). |
| `openat2` | boolean | Whether directories were opened with openat2. |
| `jobs` | integer | Directories probed concurrently. |
| `started`, `finished` | timestamptz | When the filesystem walk ran. For a replay, the capture's times. |
| `euid` | bigint | Effective uid of the run. Not root means a partial sample: unreadable directories are errors. |
| `host` | text | Host name; NULL for an anonymized capture. |
| `kernel` | text | Kernel release (`uname -r`). |
| `trackdir_version` | text | Version of the trackdir that read the filesystem. |
| `dirs` | bigint | Directories probed. An incremental sample probes only part of the tree. |
| `errors` | bigint | Probed directories with an error. |
| `anonymized` | boolean | From an anonymized capture: path components, uids, gids (except 0), `euid`, the fsid and `fs_name` are keyed tokens, and `host` is NULL. |
| `replayed_from` | text | Absolute path of the replayed capture; NULL for a live sample. |
| `stored` | timestamptz | When the sample was stored. |
| `new_dirs`, `modified_dirs`, `deleted_dirs` | bigint | The sample's `dir_state` rows: those with `created`, those with neither `created` nor `deleted`, and tombstones. They always equal a count of its rows, and spare readers that count. |
| `baseline_seconds` | double precision | Time the run took to load the tree's current state before the walk; connecting and the checks made before the walk are not counted. |
| `store_seconds` | double precision | Time the store took, from the start of its transaction to just before the commit. |

### `path`

Every directory a tree has ever held, once. Rows are only added. `tree_id`
and `path` are unique together.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | Primary key: the path id. |
| `tree_id` | bigint | The tree (`tree.id`). |
| `path` | bytea | Relative to ROOT, components joined by `/`; `.` is ROOT. Depth is the number of components. |

### `dir_state`

One directory's state as one sample stored it. Primary key
`(path_id, sample_id)`, so a directory's history is one index range. Rows are
only added, in sample order; a block-range index on `sample_id` finds the
rows of a window of samples.

| Column | Type | Meaning |
|---|---|---|
| `path_id` | bigint | The directory (`path.id`). |
| `sample_id` | bigint | The sample (`sample.id`). |
| `ino` | bigint | Inode number. |
| `mode` | integer | `st_mode`, including the file type bits (e.g. 16877 is `drwxr-xr-x`). |
| `uid`, `gid` | bigint | Owner and group. |
| `nlink` | bigint | Link count. |
| `rctime` | bigint | Latest ctime in the subtree, nanoseconds since the epoch. Can be pinned in the future (see [terms](terms.md)). |
| `rbytes` | bigint | Total size of the files in the subtree, in bytes. |
| `rfiles` | bigint | Files in the subtree. |
| `rsubdirs` | bigint | Directories in the subtree, counting this one. |
| `error_stage` | text | Step that failed: `open`, `stat`, `rstats` or `readdir`; NULL without an error. |
| `error_code` | text | errno name (e.g. `EACCES`; `errno<N>` for an unnamed one, `EUNKNOWN` without one), `DIRSTAT_STALE` or `PARSE`; set exactly when `error_stage` is. |
| `created` | boolean | The directory had no current row before the sample: it is new, or was deleted earlier. Never true for a tombstone. |
| `deleted` | boolean | A tombstone: the directory is gone as of this sample. Every column from `ino` to `error_code` is NULL. |
| `prev_rbytes`, `prev_rfiles`, `prev_rsubdirs` | bigint | The directory's `rbytes`, `rfiles` and `rsubdirs` in its current row before the sample; NULL if it had none (`created`) or that row had no rstats. A tombstone carries the last known values, so it says how much was deleted. |

A value that could not be read is NULL, and the error columns say why: `open`
and `stat` errors usually leave everything NULL (an `open` EACCES keeps the
`ino` to `nlink` values), and `rstats` errors leave the four rstats NULL. A
`readdir` error means the directory's subdirectories were not read; it takes
the place of an `rstats` error, so its rstats can be NULL too.

rstats cover the directory's whole subtree, including levels below
`max_depth`. CephFS propagates them upward with a delay, so a parent can lag
behind its children within one sample.

### `current_dir_state`

The directories that exist as of each tree's latest sample: every path's
latest `dir_state` row, unless that is a tombstone. Primary key
`(tree_id, path_id)`. trackdir keeps it up to date in the transaction that
stores a sample; it holds nothing that `dir_state` does not.

| Column | Type | Meaning |
|---|---|---|
| `tree_id` | bigint | The tree (`tree.id`). |
| `path_id` | bigint | The directory (`path.id`). |
| `path` | bytea | The directory's path, as in `path`. |
| `sample_id` | bigint | The sample that stored this state: the latest in which the directory changed. |
| `ino` to `error_code` | | As in `dir_state`. |

An index on `tree_id` restricted to rows with an error finds a tree's
current errors without reading the tree.

## Example queries

A tree's current size:

```sql
SELECT c.rbytes, c.rfiles, c.rsubdirs
FROM trackdir.path p
JOIN trackdir.current_dir_state c ON c.tree_id = p.tree_id AND c.path_id = p.id
WHERE p.tree_id = 1 AND p.path = convert_to('.', 'UTF8');
```

A directory's history. A row appears only in samples where the directory
changed, so the value holds until the next row:

```sql
SELECT s.started, d.rbytes, d.deleted
FROM trackdir.path p
JOIN trackdir.dir_state d ON d.path_id = p.id
JOIN trackdir.sample s ON s.id = d.sample_id
WHERE p.tree_id = 1 AND p.path = convert_to('proj/2019', 'UTF8')
ORDER BY s.started, s.id;
```

What changed in tree 1 since a moment, with each directory's growth. The
first row of a directory in the window holds its size before the window, the
last its size now. A directory created in the window grew from 0; one whose
size before the window was not read has an unknown growth, not its whole
size:

```sql
SELECT DISTINCT p.path,
       last_value(d.rbytes) OVER w
       - CASE WHEN first_value(d.created) OVER w THEN 0
              ELSE first_value(d.prev_rbytes) OVER w END AS grew,
       first_value(d.created) OVER w AS created,
       last_value(d.deleted) OVER w AS deleted
FROM trackdir.sample s
JOIN trackdir.dir_state d ON d.sample_id = s.id
JOIN trackdir.path p ON p.id = d.path_id
WHERE s.tree_id = 1 AND s.started >= now() - interval '7 days'
WINDOW w AS (PARTITION BY d.path_id ORDER BY s.started, s.id
             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING);
```

`grew` is NULL for a deleted directory (`prev_rbytes` of its tombstone is
what was deleted) and wherever rstats were not read.

A tree as of sample 7, ignoring later samples:

```sql
SELECT p.path, asof.* FROM (
    SELECT DISTINCT ON (d.path_id) d.*
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    WHERE s.tree_id = 1
      AND (s.started, s.id) <= (SELECT started, id FROM trackdir.sample WHERE id = 7)
    ORDER BY d.path_id, s.started DESC, s.id DESC
) asof JOIN trackdir.path p ON p.id = asof.path_id
WHERE NOT asof.deleted;
```

Each directory's absolute path inside its filesystem, to compare trees that
overlap:

```sql
SELECT t.id AS tree_id, p.id AS path_id,
       CASE WHEN p.path = convert_to('.', 'UTF8') THEN t.fs_path
            WHEN t.fs_path = convert_to('/', 'UTF8') THEN '\x2f'::bytea || p.path
            ELSE t.fs_path || '\x2f'::bytea || p.path END AS fs_dir
FROM trackdir.path p JOIN trackdir.tree t ON t.id = p.tree_id;
```

## Access

The schema needs PostgreSQL 15 or newer. `trackdir db init` creates it, so
its role needs `CREATE` on the database, and schema `trackdir` must not exist
yet: an existing one, even empty, is refused as incomplete. That role owns
every table; run `trackdir sample` and `trackdir daemon` as it. Storing a
sample creates a temporary table, so the role also needs `TEMPORARY` on the
database, which every role has unless it was revoked.

A read-only role can run `trackdir db summary` and the example queries above;
sampling as it fails with "permission denied" and stores nothing:

```sql
GRANT USAGE ON SCHEMA trackdir TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA trackdir TO reader;
```

### Removing a tree

A sample's rows are changes, not a listing, so samples cannot be deleted one
by one (deleting one that has rows fails). Remove a whole tree, here tree 3,
rows first, while nothing samples it:

```sql
BEGIN;
CREATE INDEX removal_sample ON trackdir.current_dir_state (sample_id);
CREATE INDEX removal_path ON trackdir.current_dir_state (path_id);
DELETE FROM trackdir.dir_state
WHERE sample_id IN (SELECT id FROM trackdir.sample WHERE tree_id = 3);
DELETE FROM trackdir.current_dir_state WHERE tree_id = 3;
DELETE FROM trackdir.sample WHERE tree_id = 3;
DELETE FROM trackdir.path WHERE tree_id = 3;
DELETE FROM trackdir.tree WHERE id = 3;
DROP INDEX trackdir.removal_sample, trackdir.removal_path;
COMMIT;
```

The two indexes exist only for the removal. Deleting a sample or a path makes
PostgreSQL check that no row of `current_dir_state` refers to it, among the
other trees' rows too, and without an index each check reads that whole
table: once per sample and once per path. They are not kept, because an
index on `sample_id` would slow every sample down. Creating them blocks
samples of every tree until the `COMMIT`.
