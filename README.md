# trackdir — track CephFS directory sizes over time

trackdir records how the sizes of directories on a CephFS filesystem change
over time, in PostgreSQL. It reads CephFS recursive statistics
(`ceph.dir.rbytes`, `rfiles`, `rsubdirs`, `rctime`), so the size of a whole
subtree costs one metadata request instead of a full scan, and a repeated
sample reads only the subtrees that changed.

- `trackdir sample` stores a sample of a directory tree in the database.
- `trackdir db summary` reports what the database holds, including the
  directories that grew the most.
- `trackdir capture` records a tree into a file, for offline debugging and
  tests.

All commands are read-only on ROOT. See `trackdir COMMAND --help` for options.

## Requirements

- Linux with the CephFS kernel client (not ceph-fuse). Running as root needs
  openat2 (Linux 5.6+).
- Python 3.12+; `sample` and `db` also need psycopg and a PostgreSQL server.
- Recommended: the `dirstat` mount option, which cuts the metadata requests
  per directory from four to one.

## Install

    git clone <repository-url> && cd track-cephfs-dir-sizes
    python3 -m venv venv && . venv/bin/activate
    pip install -e '.[postgres]'    # or `pip install -e .` for capture only

To update, `git pull`; no reinstall is needed.

## Sample into PostgreSQL

    export TRACKDIR_DB=postgresql://trackdir@db.example.org/trackdir
    trackdir db init                                     # once per database
    trackdir sample /mnt/cephfs/projects --max-depth 3   # run it again and again

Put the database password in `~/.pgpass` or `PGPASSWORD`, never in `--db`:
`ps` shows command lines to every local user.

A tree is identified by cluster fsid, filesystem name, path inside the
filesystem and `--max-depth`, whichever host or mount point samples it. A run
with another depth is a different tree and starts a new history.

A tree's first sample stores every directory. Later samples list a directory's
children only if the directory changed since the last sample, and store only
changed, new and deleted directories; see [docs/schema.md](docs/schema.md) for
reading them.


### Limits of incremental samples

- CephFS propagates rstats up the tree with a delay, so a change may show up
  only in a later sample.
- Under a directory with a pinned rctime (often `4294967295`), a rewrite that
  keeps size, file count and directory count unchanged is not seen. The
  sample's message counts the skipped subtrees with a pinned rctime.
- A rename is recorded as a deletion plus a creation.
- The previous state is held in memory: about 0.5 GB, and up to 1.6 GB while
  loading, per million directories.

## Summarize the database

    trackdir db summary                  # every tree, its 10 newest samples
    trackdir db summary --tree 3 --top 10
    trackdir db summary --json | jq '.trees[] | {id, current_dirs}'

Example (placeholders):

    Tree 1 (cluster 1111…, default filesystem, path /proj/sub, depth 4)
      samples: 2, 2026-10-16T20:48:11Z to 2026-10-16T20:48:30Z
      current: 72017 directories; 41 had errors: open EACCES 41
      ROOT:    1.2 PiB, 412345678 files, 5123456 directories, last changed in sample 2
      change:  +20.5 GiB, +1234 files, -3 directories since sample 1
      sample  kind         started (UTC)           took  probed    new  modified  deleted  errors  euid  source
           2  incremental  2026-10-16T20:48:30Z   0.1 s     962      0        17        0      41  1000  live
           1  initial      2026-10-16T20:48:11Z  13.2 s   72017  72017         0        0      41  1000  live
      grew most since sample 1, by own growth:
               own  share       total  directory
         +12.0 GiB    58%   +12.0 GiB  /proj/sub/proj-a/run/2026/raw (subtree)
          +8.1 GiB    39%    +8.5 GiB  /proj/sub/proj-b
        +409.6 MiB     1%  +409.6 MiB  /proj/sub/proj-b/2025/q4/raw (subtree)

A directory's own growth is that of the files directly in it, not of its
subdirectories, so the list points at where the growth is, at any depth.
A directory at max depth, or above a part of the tree that could not be
read, owns the growth below it and is marked (subtree). Growth spread
thinly over many directories does not stand out.

## Capture directory state

    trackdir capture /mnt/cephfs/projects --max-depth 3 -o projects.jsonl.gz
    trackdir sample --replay projects.jsonl.gz           # store a capture

A capture is a gzipped JSON Lines file: a header line identifying the tree,
then one line per directory (inspect with `zcat FILE | jq`):

    {"path":"proj/2019","ino":1099511628279,"mode":16877,"uid":0,"gid":0,"nlink":5,"rctime":1792088576117716441,"rbytes":172057275971107,"rfiles":1234,"rsubdirs":4}
    {"path":"secret","ino":1099511627777,"mode":16872,"uid":0,"gid":0,"nlink":6,"error":{"stage":"open","code":"EACCES"}}

To share a capture, anonymize it with `--anonymize KEYFILE` and keep the key
out of the repository.

## Development

    python3 -m venv venv && venv/bin/pip install pytest 'psycopg[binary]>=3.2'
    venv/bin/python -m pytest
    scripts/postgres.py start            # disposable PostgreSQL in podman
    export TRACKDIR_DB=$(scripts/postgres.py url)
    venv/bin/trackdir db init
    TRACKDIR_TEST_DSN=$(scripts/postgres.py url --test) venv/bin/python -m pytest
    ruff format . && ruff check .

Tests need neither Ceph nor root. Database tests run only when
`TRACKDIR_TEST_DSN` is set; they wipe schema `trackdir`, so they refuse a
database whose name does not end in `_test`.

`scripts/postgres.py --help` lists the dev server's commands (`reset` and
`destroy` delete its data). Several servers can run side by side, e.g.
`scripts/postgres.py --name pg-other --port 5433 start`; `list` shows them.
Any local user can connect to them, so keep real data out of them. A server
on an address other than loopback needs `--password` (or `TRACKDIR_PG_PASSWORD`).

## Documentation

- [docs/terms.md](docs/terms.md): terms used in code and docs.
- [docs/conventions.md](docs/conventions.md): project conventions.
- [docs/schema.md](docs/schema.md): the database schema, for tools that read it.
- [Design of capture](docs/superpowers/specs/2026-10-15-trackdir-capture-design.md).
- [Design of the initial sample](docs/superpowers/specs/2026-10-16-trackdir-initial-sample-design.md).
- [Design of incremental samples](docs/superpowers/specs/2026-10-16-trackdir-incremental-sample-design.md).
