# trackdir initial sample — design

Date: 2026-10-16. Status: approved by the user on 2026-10-16; next step is
the implementation plan.

Amended 2026-10-16 by the
[incremental-sample design](2026-10-16-trackdir-incremental-sample-design.md):
a tree's identity now also includes `max_depth` and `max_dirs` (another value
is another tree with its own initial sample); the current-state view is
revisited (it hides tombstoned paths, and "latest" means the greatest
`(started, id)`); `SamplesExist` and the refusal of a second sample are replaced
by incremental samples and by order and stale-baseline guards; the schema is
version 3 (`tree.max_depth`, `tree.max_dirs`, `dir_state.deleted`; `sample.max_depth`
is dropped, and `dir_state.sample_id` no longer cascades on delete: see the
incremental-sample spec's section 4), and `dir_state` rows are stored changes,
not one row per directory. `TreeKey`, `tree_key_of` and `describe_tree` carry
the two new fields.

## 1. Goal and scope

trackdir's sampler will record, at intervals, the directories whose state
changed since their last known state (capture spec §14). This spec covers
the first step: **the initial sample**. A tree has no samples yet, so there is
no baseline, nothing can be pruned, and the sample is every directory of the
tracked space (ROOT down to max depth). It is stored in PostgreSQL.

In scope:

- identifying which CephFS filesystem and which directory in it ROOT is
  (section 3), required by both `capture` and `sample`
- capture format version 2, which carries everything a sample needs; version
  1 is no longer read
- the database schema and `trackdir db init`
- `trackdir sample` for a tree with no samples, live or replayed from a capture
- the optional PostgreSQL dependency
- sharing the live-walk code between `capture` and `sample`

Out of scope (later specs): incremental samples and pruning, deletion
(ENOENT) handling, which attributes are monitored, how several sampler
instances share a database (overlapping ROOTs), indexes for current-state
queries, readers of the database.

The user asked for a depth-4 capture of a ceph1 subtree (called
`/mnt/ceph1/proj/sub` here) as the first test input. The v1 captures in `captures/` have since been deleted (the
user: they had little value), so the same ROOT, depth and key are captured
again as v2 (section 8).

## 2. Decisions

| Topic | Decision |
|---|---|
| Driver | psycopg 3 with the bundled libpq (`psycopg[binary]`, 3.3.6 has a cp314 wheel), as the optional extra `trackdir[postgres]`. `capture` keeps zero dependencies. Installing it into `venv/` was approved on 2026-10-16. |
| Path type | `bytea`: exact bytes, as paths are held internally. Postgres `text` cannot store the lone surrogates that non-UTF-8 names decode to. |
| Tree identity | (cluster fsid, CephFS filesystem name, ROOT's path inside that filesystem). The local mount path is not part of it, so a tree spans hosts and mount points. |
| Unknown filesystem name | NULL, meaning the cluster's default filesystem (old mount syntax without `fs=`/`mds_namespace=`, as on ceph1). NULL is a value in the tree key. |
| ROOT identity required | Both `capture` and `sample` fail if ROOT's fsid or `fs_path` cannot be determined (e.g. ceph-fuse, or no `ceph.cluster_fsid`). So every capture can be replayed into the database. |
| fsid cross-check | If the mount names an fsid, it must equal `ceph.cluster_fsid`; else fatal. Checked: equal on both dev mounts. |
| Capture format | Version 2 only: the header carries the tree key (`cluster_fsid`, `fs_name`, `fs_path`). v1 files are rejected like any unsupported version; the committed v1 fixtures are retaken. |
| Samples already exist for the tree | Fatal `ERROR:` saying incremental sampling is not implemented yet. |
| Live vs offline | `trackdir sample ROOT` walks CephFS; `trackdir sample --replay CAPTURE` walks a capture through `ReplayBackend`. Both go through `walker.walk()`. |
| Replay times | The capture's `started` and `finished`, not the time of storing. |
| Schema creation | Explicit `trackdir db init`; `sample` fails if the schema is missing or of another version. |
| Current state | A view: each path's row from its latest sample. |
| Path table | None: every row carries its path. A dir id table may come with incremental samples, if storage demands it. |
| Atomicity | One transaction per sample, rows loaded with COPY; a failure stores nothing. |
| DSN | `--db DSN`, else the environment variable `TRACKDIR_DB`. |
| DB tests | Opt in with `TRACKDIR_TEST_DSN`; skipped when it is unset or psycopg is missing. |

## 3. Which filesystem and directory ROOT is

`ceph.cluster_fsid` names the Ceph cluster, not the CephFS filesystem (a
cluster can hold several) nor the subdirectory that was mounted. Keyed by
fsid and local path alone, a mount of `mon:/projects` at `/mnt/x` and a mount
of `/` at `/mnt/x` would share a tree, and an incremental sample would prune
against the wrong baseline.

Measured on the two dev mounts (`/proc/self/mountinfo`):

```
309 41 0:58 / /mnt/ceph1 ro,relatime shared:265 - ceph 192.0.2.192,…,192.0.2.196:/ ro,name=admin,secret=<hidden>,fsid=11111111-…,dirstat,acl
464 41 0:62 / /mnt/ceph2 ro,relatime shared:275 - ceph admin@aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.default=/ ro,name=admin,…,dirstat,acl,mon_addr=…
```

**Finding ROOT's mount.** Read `mnt_id:` from `/proc/self/fdinfo/<ROOT fd>`
and take the mountinfo line with that mount id (field 1). This is exact even
with mounts stacked on one mount point and with bind mounts. Mountinfo fields
escape space, tab, newline and backslash as octal (`\040`); decode them.

**Parsing a mount of type `ceph`** (the field after ` - `):

- New syntax, source `[user@]FSID.FSNAME=SUBPATH`: fsid and fs name from the
  source.
- Old syntax, source `MONS:SUBPATH`: SUBPATH starts at the first `:/`
  (monitor lists contain no `/`; IPv6 monitors are bracketed). The fs name
  comes from super option `fs=` or `mds_namespace=`; without either it is
  None, i.e. the cluster's default filesystem. An `fsid=` super option, if
  present, gives the fsid.
- Any other filesystem type (e.g. `fuse.ceph-fuse`): not identifiable,
  `MountError` (fatal per the policy below).

**ROOT's path inside the filesystem** (`fs_path`): SUBPATH, then mountinfo
field 4 (the mount's root within the superblock, non-`/` for bind mounts),
then ROOT's resolved path relative to the mount point (field 5), joined and
normalized to an absolute path; the filesystem root is `/`. For
`/mnt/ceph1/proj/sub` this is `/proj/sub`.

If the mount names an fsid that differs from `ceph.cluster_fsid`: fatal for
both commands (the mount is not what it seems).

**Policy.** `capture` and live `sample` run the same ROOT inspection and
both need `cluster_fsid` and `fs_path`; either missing is fatal, before the
walk, with a message naming the cause (not the kernel client, fsid
unreadable). An unknown fs name is not an error. `rstats.cluster_fsid()`
returning None becomes this fatal error instead of a null header field.

**Code.** `src/trackdir/mount.py`: pure parsing of a mountinfo line and the
`fs_path` computation, plus a thin function that reads fdinfo and
mountinfo for an fd. The pure part is tested with synthetic lines: both
syntaxes, `fs=` and `mds_namespace=`, sub-mounts, bind mounts, octal
escapes, IPv6 monitors, non-ceph types, fsid mismatch.

Verified on the dev mounts: `ceph.cluster_fsid` equals the mountinfo fsid on
both (`fsid=` option on ceph1, device string on ceph2).

## 4. Capture format version 2

- `CaptureHeader` gains `fs_name: str | None` and `fs_path: str`, after
  `cluster_fsid`, which becomes `str` (no longer optional). `version` is 2.
  Everything else is unchanged.
- The loader reads only version 2 and validates every header field's type,
  `rstat_mode`, the times (ISO 8601 with a UTC offset) and that `fs_path` is
  absolute and normalized; any other version is rejected, as today.
- Anonymization: `fs_name` becomes a name token
  (`HMAC(key, b"name\0" + name)`, as path components); `fs_path` is
  tokenized per component as `root` is (`abs_path`), so `fs_path`'s tokens
  stay a suffix of `root`'s when the mount is `/`.
- The header now holds everything a `sample` row and its tree need, so a
  capture is a complete offline initial sample.
- README's format section and capture spec §9 are updated (the spec gets an
  amendment note).

## 5. Schema

All objects live in the PostgreSQL schema `trackdir` (builder's call:
qualifying names avoids `search_path` games, and tests reset everything with
`DROP SCHEMA trackdir CASCADE`). Target: PostgreSQL 18 (the dev container);
`NULLS NOT DISTINCT` needs 15 or newer.

```sql
CREATE SCHEMA trackdir;

CREATE TABLE trackdir.meta (
    schema_version integer NOT NULL
);  -- exactly one row

CREATE TABLE trackdir.tree (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_fsid text  NOT NULL,
    fs_name      text,            -- NULL: the cluster's default filesystem
    fs_path      bytea NOT NULL,  -- ROOT inside the filesystem; b'/' is its root
    UNIQUE NULLS NOT DISTINCT (cluster_fsid, fs_name, fs_path)
);

CREATE TABLE trackdir.sample (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tree_id          bigint      NOT NULL REFERENCES trackdir.tree,
    root             bytea       NOT NULL,  -- ROOT's resolved local path
    max_depth        integer     NOT NULL CHECK (max_depth >= 0),
    rstat_mode       text        NOT NULL CHECK (rstat_mode IN ('dirstat', 'getxattr')),
    openat2          boolean     NOT NULL,
    jobs             integer     NOT NULL,
    started          timestamptz NOT NULL,
    finished         timestamptz NOT NULL,
    euid             bigint      NOT NULL,
    host             text,
    kernel           text        NOT NULL,
    trackdir_version text        NOT NULL,  -- of the program that read the filesystem
    dirs             bigint      NOT NULL,
    errors           bigint      NOT NULL,  -- dirs with an error
    anonymized       boolean     NOT NULL,
    replayed_from    text,                  -- absolute capture path; NULL for live samples
    stored           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trackdir.dir_state (
    sample_id   bigint NOT NULL REFERENCES trackdir.sample ON DELETE CASCADE,
    path        bytea  NOT NULL,  -- relative to ROOT; '.' is ROOT
    ino         bigint,
    mode        integer,
    uid         bigint,
    gid         bigint,
    nlink       bigint,
    rctime      bigint,           -- ns since the epoch
    rbytes      bigint,
    rfiles      bigint,
    rsubdirs    bigint,
    error_stage text CHECK (error_stage IN ('open', 'stat', 'rstats', 'readdir')),
    error_code  text,
    PRIMARY KEY (sample_id, path),
    CHECK ((error_stage IS NULL) = (error_code IS NULL))
);

CREATE VIEW trackdir.current_dir_state AS
SELECT DISTINCT ON (s.tree_id, d.path) s.tree_id, d.*
FROM trackdir.dir_state d JOIN trackdir.sample s ON s.id = d.sample_id
ORDER BY s.tree_id, d.path, s.started DESC, s.id DESC;
```

- Columns mirror `DirProbe` and `CaptureHeader`; NULL means unknown, as an
  omitted field does in a capture. `children` is not stored: the rows below
  a listed dir are its children. Bytes columns hold `os.fsencode` of the
  header's strings.
- One `dir_state` row per directory of the sample, including dirs with
  errors (e.g. `open EACCES` with owner fields and no rstats).
- The view gives the right baseline only while samples are complete; the
  incremental phase revisits it (deletions, pruned subtrees, indexes).
- `bigint` limits: rctime fits until 2262 (pinned `4294967295 s` is
  4.3e18 ns); ino, rbytes and counts are far below 2^63 in practice. A value
  that does not fit makes the insert fail, and the sample is not stored.

`SCHEMA_VERSION = 1`, a named constant in the db module. No database exists
outside development yet, so there is no migration code.

## 6. Commands

### 6.1 `trackdir db init`

```
trackdir db init [--db DSN]
```

Creates the schema in one transaction. If the schema exists at the current
version: a note on stderr, exit 0. If it exists at another version, or
`trackdir` exists without a `meta` row: `ERROR:`, exit 1. `--help` states
that it changes the database.

### 6.2 `trackdir sample`

```
trackdir sample ROOT --max-depth N [--max-dirs 1000000] [--jobs 16] [--db DSN]
trackdir sample --replay CAPTURE [--max-depth N] [--max-dirs 1000000] [--db DSN]
```

- Exactly one of ROOT and `--replay`, else usage error (exit 2). `--jobs` is
  rejected with `--replay`. With `--replay`, `--max-depth` defaults to the
  capture's and may not exceed it (`ReplayBackend` already raises).
- No DSN from `--db` or `TRACKDIR_DB`: usage error. The README notes that a
  password belongs in `~/.pgpass` or `PGPASSWORD`, not in a DSN visible in
  `ps`.
- psycopg not installed: `ERROR:` telling the user to install
  `trackdir[postgres]`.
- Read-only on ROOT; writes only to the database. `--help` says that it
  writes to the database, that it currently handles only a tree's first
  sample, and that non-root samples are partial.

Steps:

1. **Pre-check** (own connection, short transaction, before any walk):
   check the schema version, look up the tree; if it has samples, fail.
   Tree lookups compare `fs_name` with `IS NOT DISTINCT FROM`, because NULL
   is the common case (ceph1) and `=` would never match it.
   Live mode first runs capture's ROOT checks (openat2 policy, CephFS, rstat
   mode) and section 3 to get the tree key. Replay takes the key from the
   capture header.
2. **Walk.** Live: the shared live walk (section 7). Replay: `load_capture`,
   then `walk(ReplayBackend(capture, max_depth), list-above-max-depth, ...)`
   with `jobs=1`.
3. **Store** (own connection, one transaction): `INSERT ... ON CONFLICT DO
   NOTHING` the tree, `SELECT ... FOR UPDATE` its row (again with
   `IS NOT DISTINCT FROM`), re-check that it has
   no samples (another sampler may have stored one during the walk), insert
   the `sample` row, COPY the `dir_state` rows, commit. The row lock
   serializes concurrent initial samples of one tree under READ COMMITTED.

No connection is held through the walk: a live walk can take hours.

Sample metadata comes from a `CaptureHeader`. Live: built as `capture` builds
it. Replay: the capture's header, with `max_depth`, `dirs` and `errors`
recomputed from the walk (they differ when replaying to a smaller depth).
All other fields, including `trackdir_version` and `anonymized`, describe the
run that read the filesystem and are kept.

Output follows `capture`: `WARNING:` lines as in capture, `ERROR:` on fatal
errors, and on success one stderr paragraph (sample id, tree, depth, dirs,
errors by stage and code, walk and store times). stdout: none. Exit codes:
0, 1 fatal, 2 usage, 130 interrupted (nothing stored), 128+N on SIGTERM and
SIGHUP, as in capture.

`run_sample(opts, warn) -> SampleResult` decides; `render_sample_summary()`
prints. Likewise `run_db_init()`.

## 7. Code structure

- **Shared live walk.** `run_capture` opens ROOT, applies the openat2
  policy, inspects ROOT, walks and builds the header inline. Move this into
  shared code used by both commands, in two steps: open-and-inspect ROOT
  (resolved path, fsid, mount identity, rstat mode, openat2 flag), then walk
  and build the header. Capture creates its writer, and sample runs its
  database pre-check, between the two. Capture's behavior changes only by
  the new header fields and the new fatal errors of section 3; its tests
  change only there.
- **mount module**: section 3.
- **db module** (`src/trackdir/db.py`): imports psycopg lazily, so
  `import trackdir.cli` works without it. Holds the DDL, `SCHEMA_VERSION`,
  `init_schema(conn)`, `check_schema(conn)`, the samples-exist lookup and
  `store_initial_sample(conn, header, probes, replayed_from) -> int`. Raises
  its own exception types (schema missing, version mismatch, samples exist). The CLI turns them into `FatalError` messages.
- **Rows** are built by a pure function from `DirProbe` to a tuple in
  `dir_state` column order, testable without a database.
- `cli.py` is 429 lines; with two more commands, the builder may split
  per-command code into modules. The parser's shared options (`--max-depth`,
  `--max-dirs`, `--jobs`) are defined once and reused.
- `pyproject.toml`: `[project.optional-dependencies] postgres =
  ["psycopg[binary]>=3.2"]`.

## 8. Testing

No test needs Ceph, root or a database unless it opts in.

| Area | Tests |
|---|---|
| Mount identity | section 3's synthetic mountinfo cases; fdinfo lookup on a temp dir (mount id found, type not ceph raises `MountError`) |
| Capture v2 | writes v2 with the new fields; v1 and null `cluster_fsid`/`fs_path` rejected; capture fails on a ROOT without identity; anonymization of `fs_name`/`fs_path` is stable and keeps `fs_path` a suffix of `root` for a `/` mount; round trip |
| Rows | `DirProbe` to row: all fields, missing fields as None, error split into stage and code, non-UTF-8 path as raw bytes |
| Replay pipeline | replaying a fixture at its own depth gives the capture's records and counts; at a smaller depth, depth-filtered records and recomputed `dirs`/`errors` |
| Shared live walk | existing capture tests pass, updated only for section 3 and 4 changes |
| CLI, no database | ROOT xor `--replay`; `--jobs` with `--replay`; missing DSN; psycopg missing; exit codes; messages with whitespace collapsed |
| Database (opt-in) | `db init` creates, is idempotent, rejects another version; `sample` without schema fails; initial sample of the v2 fixture, read back into `DirProbe`s, equals the walk's output (round trip); second sample of the same tree fails before walking; trees differing only in `fs_name` (incl. NULL vs a name) or `fs_path` are distinct, two NULL `fs_name`s are the same; non-UTF-8 path and pinned rctime round-trip; a failure during COPY leaves no `sample` or `tree` row |

DB tests connect to `TRACKDIR_TEST_DSN` (e.g. `scripts/postgres.sh url
--test`) and drop and recreate schema `trackdir` per test. Before dropping
anything, they refuse to run unless the DSN's database name ends in `_test`,
so a mistyped DSN cannot wipe development data. Reading a sample back into
`DirProbe`s is a test helper, not product code.

**Fixtures.** Capture v2 is implemented first, so fixtures are taken while
the dev mounts are still available. All are taken read-only, as uid 1000,
with the fixtures key, into `captures/` (capture never overwrites):

    K=.keys/fixtures.key
    trackdir capture /mnt/ceph1 --max-depth 3 --anonymize $K -o captures/ceph1-depth3.jsonl.gz
    trackdir capture /mnt/ceph2 --max-depth 3 --anonymize $K -o captures/ceph2-depth3.jsonl.gz
    trackdir capture /mnt/ceph1/proj/sub --max-depth 4 --jobs 32 --anonymize $K \
        -o captures/ceph1-sub-depth4.jsonl.gz

The first two repeat the v1 fixtures' ROOTs and depths (3,184 and 242
dirs). The third is the user's requested test input (about 72k dirs, about
20 s, about 2 MB; the deleted v1 capture's anonymized root identified its
ROOT under the fixtures key). Each new file must pass the
fixture invariants; then it is moved into `tests/data/`, replacing the v1
file of the same name. The v1 capture of the subtree
had 41 errors, all `open EACCES` at depths 3 and 4, no readdir errors and no
non-UTF-8 names (those stay covered by synthetic tests), and every value fit
in `bigint`.

Manual acceptance against the dev container: `scripts/postgres.sh start
--ephemeral`, `db init`, `sample --replay
tests/data/ceph1-sub-depth4.jsonl.gz`. Expect `dir_state` rows and errors
equal to the header's `dirs` and `errors`, the capture's times, tree key
(ceph1 fsid token, NULL, `/<proj>/<sub>` tokens); a second run fails;
`current_dir_state` returns the same row count.

Mount identity on real mounts is checked through captures, not live samples,
so real names stay out of the dev database (README: any local user can
connect to it): a raw `--max-depth 0` capture of `/mnt/ceph2` into
`captures/` must give `fs_name` `default` and `fs_path` `/`; the subtree
fixture must give `fs_name` null and `fs_path` equal to the last two tokens
of its `root`. The raw capture is deleted afterwards.

## 9. Documentation

- README: `sample` and `db init` usage, the `trackdir[postgres]` extra,
  `TRACKDIR_DB`, `TRACKDIR_TEST_DSN`, the password note, capture header v2
  fields; status line updated.
- `docs/terms.md`: tree, filesystem path (`fs_path`), sample (no longer
  planned), initial sample, dir state, current state.
- `docs/conventions.md`: paths are `bytea` in the database; DB tests opt in
  with `TRACKDIR_TEST_DSN`.
- Capture spec: amendment note for format v2 and the ROOT identity
  requirement.

## 10. Risks and open items

- The dev mounts are temporary. If they disappear before capture v2 can
  retake the fixtures, the test suite has no real data; hence capture v2
  comes first in the plan.
- An unknown fs name means "the cluster's default filesystem". If an admin
  changes the default (`ceph fs set-default`), the tree silently refers to
  another filesystem. Mounting one filesystem by name and once implicitly
  yields two trees (the safe direction).
- Mountinfo semantics for sub-mounts and bind mounts of CephFS are taken
  from the kernel's documented format; the dev mounts are both `/`, so only
  synthetic tests cover them.
- A sample's rows are held in memory until stored (~0.6 KB per dir, as in
  capture); `--max-dirs` bounds it.
- The current-state view scans all rows; acceptable until incremental
  samples, which add indexes.
- Replay samples share the database with live ones; `replayed_from` tells
  them apart. Anonymized replays get trees of their own, because fsid, fs
  name and path are anonymized too.
