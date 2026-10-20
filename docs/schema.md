# Database schema

The PostgreSQL schema `trackdir`, for tools that read the database. The DDL in
`src/trackdir/db.py` (`DDL`) is authoritative; this file explains it.

## Versioning

Table `meta` has one row, `schema_version` (currently 4). Check it before
reading: there are no migrations, and any version may rename, drop or change
columns. trackdir itself refuses a database at another version.

## Reading rules

- **A sample stores changes, not a listing.** `dir_state` gets a row for a
  directory only if it is new or differs from its previous row, plus a
  tombstone (`deleted`) for each directory that disappeared. A directory with
  no row in a sample is unchanged, not missing.
- **Read current state from `current_dir_state`**, never from one sample's
  `dir_state` rows.
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
host or mount point samples it. The four identity columns are unique together.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | Primary key. |
| `cluster_fsid` | text | Ceph cluster fsid. |
| `fs_name` | text | CephFS filesystem name; NULL means the cluster's default filesystem. |
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

### `dir_state`

One directory's state as one sample stored it. Primary key `(sample_id, path)`.

| Column | Type | Meaning |
|---|---|---|
| `sample_id` | bigint | The sample (`sample.id`). |
| `path` | bytea | Relative to ROOT, components joined by `/`; `.` is ROOT. Depth is the number of components. |
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
| `deleted` | boolean | A tombstone: the directory is gone as of this sample. Every other column but the key is NULL. |

A value that could not be read is NULL, and the error columns say why: `open`
and `stat` errors usually leave everything NULL (an `open` EACCES keeps the
`ino` to `nlink` values), and `rstats` errors leave the four rstats NULL. A
`readdir` error means the directory's subdirectories were not read; it takes
the place of an `rstats` error, so its rstats can be NULL too.

rstats cover the directory's whole subtree, including levels below
`max_depth`. CephFS propagates them upward with a delay, so a parent can lag
behind its children within one sample.

## View `current_dir_state`

Each path's latest `dir_state` row in its tree, by the sample's
`(started, id)`, without tombstoned paths: the tree as of its latest sample.
Columns are `tree_id` followed by those of `dir_state`.

## Example queries

A tree's current size:

```sql
SELECT rbytes, rfiles, rsubdirs FROM trackdir.current_dir_state
WHERE tree_id = 1 AND path = convert_to('.', 'UTF8');
```

A directory's history. A row appears only in samples where the directory
changed, so the value holds until the next row:

```sql
SELECT s.started, d.rbytes, d.deleted
FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
WHERE s.tree_id = 1 AND d.path = convert_to('proj/2019', 'UTF8')
ORDER BY s.started, s.id;
```

A tree as of sample 7, built as the view is but ignoring later samples:

```sql
SELECT * FROM (
    SELECT DISTINCT ON (d.path) d.*
    FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
    WHERE s.tree_id = 1
      AND (s.started, s.id) <= (SELECT started, id FROM trackdir.sample WHERE id = 7)
    ORDER BY d.path, s.started DESC, s.id DESC
) asof
WHERE NOT deleted;
```
