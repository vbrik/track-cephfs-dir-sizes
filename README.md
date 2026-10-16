# trackdir — track CephFS directory sizes over time

trackdir records how the sizes of directories on a CephFS filesystem change
over time. It reads CephFS recursive statistics (`ceph.dir.rbytes`,
`rfiles`, `rsubdirs`, `rctime`), so the size of a whole subtree costs one
metadata request instead of a full scan.

Status: `trackdir capture` works, and `trackdir sample` stores samples of a
tree in PostgreSQL: the first one holds every directory, later ones only what
changed. Readers of the database (reports, queries by size growth) are not
written yet.

## Requirements

- Linux with the CephFS kernel client; trackdir reads the mount table to
  identify ROOT's filesystem and fails on other clients (e.g. ceph-fuse).
- openat2 (Linux 5.6+); required when running as root.
- Python 3.12 or newer. `capture` has no other dependencies; `sample` and
  `db init` need psycopg and a PostgreSQL server.
- Recommended: the `dirstat` mount option. It cuts the metadata requests
  per directory from four to one; trackdir warns when it is missing.

## Install

    pip install .                # capture only
    pip install '.[postgres]'    # also sample and db init (adds psycopg)

Or run from a checkout without installing: `PYTHONPATH=src python3 -m trackdir`.

## Capture directory state

    trackdir capture /mnt/cephfs/projects --max-depth 3 -o projects.jsonl.gz

`capture` records every directory under ROOT down to `--max-depth` into one
file, for offline debugging, development and tests. It is read-only on ROOT
and writes only the output file, which appears only if the capture succeeds
and is never overwritten. Run it as root for a complete capture: as another
user, unreadable directories are recorded as EACCES errors.

`--max-dirs` (default 1,000,000) stops the run before memory runs out;
`--jobs` (default 16) sets how many directories are probed concurrently.

To share a capture, anonymize it with a secret key kept out of the
repository. Captures taken with the same key line up. trackdir refuses a key
that group or others can read:

    (umask 077; head -c 32 /dev/urandom > trackdir.key)
    trackdir capture /mnt/cephfs/projects --max-depth 3 \
        --anonymize trackdir.key -o projects-anon.jsonl.gz

### Capture file format

A gzipped JSON Lines file. Line 1 is a header (`format`, `version` 2, `root`,
`max_depth`, `cluster_fsid`, `fs_name`, `fs_path`, `rstat_mode`, timestamps,
counts, `anonymized`, …); the fsid, filesystem name and path in the filesystem
identify the tree. Every other line is one directory, in tree order:

    {"path":"proj/2019","ino":1099511628279,"mode":16877,"uid":0,"gid":0,"nlink":5,"rctime":1792088576117716441,"rbytes":172057275971107,"rfiles":1234,"rsubdirs":4}
    {"path":"secret","ino":1099511627777,"mode":16872,"uid":0,"gid":0,"nlink":6,"error":{"stage":"open","code":"EACCES"}}

`path` is relative to ROOT (`.` is ROOT itself); `rctime` is in
nanoseconds; fields that could not be read are omitted. Inspect a capture
with `zcat FILE | jq`.

## Sample into PostgreSQL

    export TRACKDIR_DB=postgresql://trackdir@db.example.org/trackdir
    trackdir db init                                     # once per database
    trackdir sample /mnt/cephfs/projects --max-depth 3   # run it again and again
    trackdir sample --replay projects.jsonl.gz           # from a capture

`db init` creates the tables in schema `trackdir` (schema version 2); repeating
it changes nothing. A version-1 database is refused: drop schema `trackdir` and
run `db init` again (there is no migration).

`sample` reads ROOT's directories down to `--max-depth`, as `capture` does, and
stores them as a sample of ROOT's tree. A tree is a directory of a CephFS
filesystem: cluster fsid, filesystem name, path inside the filesystem, and the
`--max-depth` and `--max-dirs` the sample ran with. It is the same whichever
host or mount point samples it.

- **Initial sample.** A tree with no samples gets one that stores every
  directory.
- **Incremental sample.** Otherwise `sample` loads the tree's current state (its
  baseline) from the database, and lists a directory's children only if the
  directory differs from the baseline in some stored column (ino, mode, uid,
  gid, nlink, rctime, rbytes, rfiles, rsubdirs or error), or if it is an error
  directory or an ancestor of one (the retry set: errors are retried by every
  sample). CephFS rstats cover a whole subtree, so otherwise an unchanged
  directory means an unchanged subtree, which is skipped without being read.
  The sample stores only the directories that changed (new ones included) and
  one deletion record (tombstone) for every directory that disappeared, with its
  former descendants.
  Unchanged directories store nothing, so `trackdir.dir_state` holds changes,
  not a listing; `trackdir.current_dir_state` (a view) gives each directory's
  latest state, without deleted ones.

The summary on stderr says which kind it was, e.g. (placeholders):

    Stored sample 7 (incremental): probed 1204 directories under ROOT to depth
    4, tree (...); 18 changed, 2 deleted, 31 unchanged subtrees skipped; 41 had
    errors: open EACCES 41. Walk 3.2 s, store 0.1 s.

"Probed" counts the directories read, including every one on the way to a
change. "N had errors" counts probed directories with an error, not new ones:
the unreadable directories of a non-root run (below) are probed again by every
sample, so they are in that number every time. "Unchanged subtrees skipped" are
directories above max depth that were not listed.

`--max-depth` and `--max-dirs` are part of the tree's identity. A run with
another value of either is a different tree: it takes a full initial sample and
never sees the other tree's baseline. There is no way to deepen a tree, and
one ROOT sampled at two depths is stored twice, with nothing linking the
copies; readers of the database must pick a tree. `--max-dirs` is a memory
guard for a run, but it counts too, so if a large reorganization pushes a run
over it, raising it starts a new tree.

Run `sample` as root for a complete sample. As another user it is partial:
directories you cannot read are stored as errors (EACCES) without rstats. Their
ancestors are never pruned, so every later sample reads those directories again,
and a later root sample replaces the errors with real rows. Until then,
anyone reading the current state sees the errors.

`sample` is read-only on ROOT, holds no database connection during the walk,
and stores in one transaction, so a failed or interrupted run stores nothing.
It refuses a sample if another one was stored for the tree during the walk, and
a sample that did not start after the tree's latest sample (the view orders by
`started`); a replayed capture must therefore be newer than the tree's last
sample.

`--replay CAPTURE` stores a capture instead of reading CephFS, keeping the
capture's times and provenance; it prunes with the baseline as a live run would.
`--max-depth` then defaults to the capture's and may be smaller.

The database comes from `--db DSN` or `TRACKDIR_DB`, as a libpq connection
string or URI. Put the password in `~/.pgpass` or `PGPASSWORD`, never in
`--db`: `ps` shows command lines to every local user.

### Limits of incremental samples

- CephFS propagates rstats up the tree with a delay, so a change can be
  invisible at a parent for a while. The next sample sees it: a change is
  delayed, not lost.
- A pinned rctime (in the future, often `4294967295`) never changes again, so
  a same-size rewrite under such a directory keeps all four rstats equal and is
  not seen. A change in size, file count or directory count is.
- A rename is a deletion plus a creation; the inode number is stored but not
  used.
- The baseline is held in memory, for all current directories of the tree:
  about 0.5 GB retained, and up to about 1.6 GB at peak while loading, per
  million current directories. The default `--max-dirs` is 1,000,000, so large
  trees need RAM accordingly.

## Development

    python3 -m venv venv && venv/bin/pip install pytest 'psycopg[binary]>=3.2'
    venv/bin/python -m pytest
    TRACKDIR_TEST_DSN=$(scripts/postgres.sh url --test) venv/bin/python -m pytest
    ruff format . && ruff check .

Tests need neither Ceph nor root; real data comes from anonymized captures
in `tests/data/`. The database tests run only when `TRACKDIR_TEST_DSN` is
set; they drop and recreate schema `trackdir`, so they refuse a database
whose name does not end in `_test`.

### Development database

`scripts/postgres.sh` runs a disposable PostgreSQL 18 server in a rootless
podman container, for developing and testing trackdir (needs podman; the first
start pulls the image):

    scripts/postgres.sh start              # data kept in volume trackdir-pg-data
    scripts/postgres.sh start --ephemeral  # data in memory, gone after stop
    scripts/postgres.sh status             # state=, mode=, port= lines; exit 0 if running
    scripts/postgres.sh url                # postgresql://trackdir:trackdir@127.0.0.1:5432/trackdir
    scripts/postgres.sh psql               # psql inside the container
    scripts/postgres.sh reset              # recreate databases trackdir and trackdir_test
    scripts/postgres.sh stop
    scripts/postgres.sh destroy            # remove the container and the volume

Start creates two databases: `trackdir` for development and `trackdir_test`,
which tests may wipe (`url --test` prints its connection string). The server
listens on 127.0.0.1 only, with fixed credentials (user, password and database
are all `trackdir`); any local user on the machine can connect, so keep real
data out of it. `TRACKDIR_PG_PORT` (default 5432) and `TRACKDIR_PG_IMAGE` (a
PostgreSQL 18 or newer image) apply when the container is created; run
`destroy` first to change them. `TRACKDIR_PG_READY_TIMEOUT` (seconds, default
30) is how long each `start` waits for the server.
The script touches only the container `trackdir-postgres` and the volume
`trackdir-pg-data`.

## Documentation

- [docs/terms.md](docs/terms.md): terms used in code and docs.
- [docs/conventions.md](docs/conventions.md): project conventions.
- [Design of capture](docs/superpowers/specs/2026-10-15-trackdir-capture-design.md).
- [Design of the initial sample](docs/superpowers/specs/2026-10-16-trackdir-initial-sample-design.md).
- [Design of incremental samples](docs/superpowers/specs/2026-10-16-trackdir-incremental-sample-design.md).
