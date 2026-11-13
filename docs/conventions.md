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
- `trackdir daemon` writes no messages: it has no results and logs to
  stdout instead, one line per event (`YYYY-MM-DDTHH:MM:SSZ LEVEL message`,
  UTC) through a handler on the root logger, so library records share the
  stream; no progress line. Message text is shared with the other commands:
  a one-line `describe_*` function gives the text, and a manual command wraps
  it with `paragraph`.
- Signals become exceptions derived from BaseException (`interrupts.py`), so
  a run unwinds and cleans up: `walker.walk` cancels pending probes, a
  transaction rolls back. Code that catches them re-raises them or decides
  the exit status (`cli.main`, `daemon.run`). The daemon's overrun timer is
  not one of them: `interrupts.call_after` calls a function from a timer
  thread when the time passes and interrupts nothing, so an overrun is
  logged and the cycle finishes. No code uses SIGALRM: a handler would run in
  the middle of a log write, and a ctypes call such as openat2 would fail
  with EINTR.
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
  individually (`dir_state.sample_id` has no `ON DELETE CASCADE`).
- The work of a sample depends on the tree's live directories and on what
  changed, never on how much history is stored. (One accepted exception: the
  store of a sample with very many rows reads all of `path`; see "Measured
  on version 5" in the schema-v5 spec.) Reads that would sort or scan
  a tree's history are avoided by storing what they would compute:
  `current_dir_state`, `dir_state`'s `prev_*` and `created`, and `sample`'s
  counts of new, modified and deleted rows. Such copies
  are allowed only where they remove a read of history, and only
  `db.store_sample` writes them, in the transaction that stores the rows;
  a test rebuilds each from `dir_state` (`conftest.assert_invariants`).
- Queries that join `path` or `current_dir_state` by tree, or `dir_state`
  by sample, depend on the planner knowing how many rows a tree or a sample
  has. `db.store_sample` runs `ANALYZE` after it stores `db.ANALYZE_AFTER`
  rows or more, and the DDL makes autovacuum do so after as many changed
  rows. Test a new such query on a database that holds another tree and has
  been analyzed before the rows under test existed: without statistics, or
  with one tree, a bad plan does not show.
- A tree is told from another in one place, `db._resolve_tree`, which checks
  a tree name against a mount identity (`db.TreeKey`). No other code compares
  names in place of mount identities or looks a tree up by one of them alone
  in order to store into it.
- A tree's latest sample is the one with the greatest `(started, id)`, in the
  baseline, the current state, the order guard, the stale-baseline check and
  the summary alike.
- Schema version 5 is the first production version. Its DDL is frozen when
  the first production database is created with `db init`; until then it
  may still change, and development databases are created again. After
  that, a change to the DDL raises `SCHEMA_VERSION` and adds an upgrade step
  from the previous version, run by `trackdir db upgrade` in one transaction. trackdir refuses
  a database at an older version and names that command; it refuses a newer
  one and asks for a newer trackdir. `db upgrade` is built with the first
  version-6 change; versions before 5 cannot be upgraded.
- `docs/schema.md` changes with the DDL; `test_schema_doc_covers_the_ddl`
  checks that it names every table, column and the schema version.
