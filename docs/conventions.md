# Conventions

- Paths are `bytes` internally, relative to ROOT and joined with `/`; ROOT is
  `b"."`. They become `str` only in JSON, through `os.fsdecode`
  (surrogateescape), so names that are not UTF-8 round-trip exactly.
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
- Tests need neither Ceph nor root. Real CephFS data enters tests only as
  anonymized captures in `tests/data/`.
