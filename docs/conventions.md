# Conventions

- Paths are `bytes` internally, relative to ROOT and joined with `/`; ROOT is
  `b"."`. They become `str` only in JSON, through `os.fsdecode`
  (surrogateescape), so names that are not UTF-8 round-trip exactly. In the
  database they are `bytea`; header strings become bytes through
  `os.fsencode`.
- Times are integer nanoseconds since the epoch, never floats. Capture
  headers use ISO 8601 UTC strings.
- Error codes are errno names (`EACCES`; `errno<N>` if unnamed, `EUNKNOWN`
  without an errno), or `DIRSTAT_STALE` and `PARSE`.
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
- The one exception is the progress status line (`progress.Progress`): stderr
  only, only on a terminal, redrawn in place. It is erased before any message:
  on exit from its `with` block, and by the warn callback (`cli._warner`).
  Anything else that writes to stderr during a run calls `Progress.clear()`
  first.
- `walk_within` alone enforces the depth limit: no directory at max depth is
  listed whatever a `should_list` says, so predicates need not repeat it.
- Database errors are `db.DbError` subclasses (psycopg errors are wrapped);
  `dbcli.py` turns them into `FatalError` messages, in one place.
- Tests need neither Ceph nor root. Real CephFS data enters tests only as
  anonymized captures in `tests/data/`.
- The development database is the podman container `trackdir-postgres`,
  managed by `scripts/postgres.py` (stdlib only, so it runs without the
  venv). Database tests opt in through
  `TRACKDIR_TEST_DSN` (the `db` fixture) and are skipped without it; they
  refuse a database whose name does not end in `_test`.
  `tests/test_postgres_script.py` tests the script against a stub `podman`.
- Code reads the database by the [reading rules](schema.md#reading-rules) of
  `docs/schema.md`: `dir_state` rows are stored changes, so a tree's state comes
  from `current_dir_state`. For the same reason samples are not deleted
  individually (`dir_state.sample_id` has no `ON DELETE CASCADE`), and counts of
  what a sample stored are counted from its `dir_state` rows, not stored on
  `sample`.
- A tree's latest sample is the one with the greatest `(started, id)`, in the
  baseline, the view, the order guard, the stale-baseline check and the summary
  alike.
- `docs/schema.md` changes with the DDL; `test_schema_doc_covers_the_ddl`
  checks that it names every table, column and the schema version.
