# Conventions

- Paths are `bytes` internally, relative to ROOT and joined with `/`; ROOT is
  `b"."`. They become `str` only in JSON, through `os.fsdecode`
  (surrogateescape), so names that are not UTF-8 round-trip exactly. In the
  database they are `bytea`; header strings become bytes through
  `os.fsencode`.
- Times are integer nanoseconds since the epoch, never floats. Capture
  headers use ISO 8601 UTC strings.
- Error codes are errno names (`EACCES`), or `DIRSTAT_STALE` and `PARSE`.
- Output is in tree order (`model.tree_key`): ROOT first, each directory
  right before its subtree.
- Directories are opened only through `SafeRoot.open`, never by absolute
  path.
- Every `ceph.dir.*` getxattr is an MDS round trip; count them when
  changing how directories are probed.
- Code that reads rstats never moves a directory fd's offset: `pread`, not
  `read`, because readdir on the same fd may follow.
- Messages go to stderr as one wrapped paragraph each, prefixed `ERROR:` or
  `WARNING:`; stdout carries results only.
- `walk_within` alone enforces the depth limit: no directory at max depth is
  listed whatever a `should_list` says, so predicates need not repeat it.
- Database errors are `db.DbError` subclasses (psycopg errors are wrapped);
  `sample.py` turns them into `FatalError` messages, in one place.
- Tests need neither Ceph nor root. Real CephFS data enters tests only as
  anonymized captures in `tests/data/`.
- The development database is the podman container `trackdir-postgres`,
  managed by `scripts/postgres.sh`. Database tests opt in through
  `TRACKDIR_TEST_DSN` (the `db` fixture) and are skipped without it; they
  refuse a database whose name does not end in `_test`.
  `tests/test_postgres_script.py` tests the script against a stub `podman`.
- `trackdir.dir_state` rows are stored changes, not a listing: a sample stores a
  directory only if its row differs from the baseline, plus tombstones
  (`deleted` rows with every value NULL) for deleted paths. Read a tree's state
  through `trackdir.current_dir_state`, never by taking the rows of one sample.
  For the same reason samples are not deleted individually: `dir_state.sample_id`
  has no `ON DELETE CASCADE`. Counts of what a sample stored are counted from
  its `dir_state` rows, not stored on `sample`.
- A tree's latest sample is the one with the greatest `(started, id)`, in the
  baseline, the view, the order guard and the stale-baseline check alike. A sample
  must start later than the latest one, so this is also the greatest `id`.
- A path that is not in the view is deleted or never stored; unchanged
  directories carry no row in newer samples, so absence from a sample means
  nothing.
