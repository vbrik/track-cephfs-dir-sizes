# trackdir — track CephFS directory sizes over time

trackdir records how the sizes of directories on a CephFS filesystem change
over time. It reads CephFS recursive statistics (`ceph.dir.rbytes`,
`rfiles`, `rsubdirs`, `rctime`), so the size of a whole subtree costs one
metadata request instead of a full scan.

Status: `trackdir capture` works, and `trackdir sample` stores a tree's first
sample in PostgreSQL. Later samples, which record only what changed, are
planned.

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
    trackdir sample /mnt/cephfs/projects --max-depth 3
    trackdir sample --replay projects.jsonl.gz           # from a capture

`db init` creates the tables in schema `trackdir`; repeating it changes
nothing. `sample` stores every directory under ROOT down to `--max-depth`,
read as `capture` reads it, as the first sample of ROOT's tree. A tree is a
directory of a CephFS filesystem: cluster fsid, filesystem name and path inside
the filesystem, the same whichever host or mount point samples it. A tree that
already has a sample is refused before walking; later samples are not
implemented yet. `sample` is read-only on ROOT, holds no database connection
during the walk, and stores the sample in one transaction, so a failed or
interrupted run stores nothing. Run it as root for a complete sample.

`--replay CAPTURE` stores a capture instead of reading CephFS, keeping the
capture's times and provenance; `--max-depth` then defaults to the capture's
and may be smaller.

The database comes from `--db DSN` or `TRACKDIR_DB`, as a libpq connection
string or URI. Put the password in `~/.pgpass` or `PGPASSWORD`, never in
`--db`: `ps` shows command lines to every local user.

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
