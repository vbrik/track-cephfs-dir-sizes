# trackdir — track CephFS directory sizes over time

trackdir records how the sizes of directories on a CephFS filesystem change
over time. It reads CephFS recursive statistics (`ceph.dir.rbytes`,
`rfiles`, `rsubdirs`, `rctime`), so the size of a whole subtree costs one
metadata request instead of a full scan.

Status: `trackdir capture` works. Periodic sampling into PostgreSQL
(`trackdir sample`) is planned.

## Requirements

- Linux with the CephFS kernel client.
- openat2 (Linux 5.6+); required when running as root.
- Python 3.12 or newer; no other dependencies.
- Recommended: the `dirstat` mount option. It cuts the metadata requests
  per directory from four to one; trackdir warns when it is missing.

## Install

    pip install .

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

A gzipped JSON Lines file. Line 1 is a header (`format`, `version`, `root`,
`max_depth`, `rstat_mode`, timestamps, counts, `anonymized`, …). Every other
line is one directory, in tree order:

    {"path":"proj/2019","ino":1099511628279,"mode":16877,"uid":0,"gid":0,"nlink":5,"rctime":1792088576117716441,"rbytes":172057275971107,"rfiles":1234,"rsubdirs":4}
    {"path":"secret","ino":1099511627777,"mode":16872,"uid":0,"gid":0,"nlink":6,"error":{"stage":"open","code":"EACCES"}}

`path` is relative to ROOT (`.` is ROOT itself); `rctime` is in
nanoseconds; fields that could not be read are omitted. Inspect a capture
with `zcat FILE | jq`.

## Development

    pytest
    ruff format . && ruff check .

Tests need neither Ceph nor root; real data comes from anonymized captures
in `tests/data/`.

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
