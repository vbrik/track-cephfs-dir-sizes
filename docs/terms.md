# Terms

| Term | Meaning |
|---|---|
| ROOT | The directory whose subtree trackdir captures. Paths in captures are relative to it; ROOT itself is `.`. |
| depth | Number of path components below ROOT; ROOT is depth 0. |
| max depth | Deepest level recorded. Directories at max depth are probed but not listed. |
| tracked space | All directories from ROOT down to max depth. |
| probe | Reading one directory: open, fstat, rstats and, if it is listed, its subdirectory names. |
| listed | A directory whose subdirectory names were read. |
| rstats | CephFS recursive statistics, covering a directory's whole subtree: `rbytes`, `rfiles`, `rsubdirs`, `rctime`. |
| rbytes | Total size of the files in the subtree, in bytes. |
| rfiles, rsubdirs | Number of files and directories in the subtree; `rsubdirs` counts the directory itself. |
| rctime | Latest change time (ctime) in the subtree, as integer nanoseconds since the epoch. |
| pinned rctime | An rctime in the future, often `4294967295.000000000`. Because rctime is a maximum, it no longer changes when the subtree does. |
| MDS round trip | One request to the Ceph metadata server. Every `ceph.dir.*` getxattr costs one. |
| dirstat mode | Reading all rstats with one getxattr plus `pread()` on the directory; needs the `dirstat` mount option. |
| getxattr mode | Reading each rstat with its own getxattr; used when the mount lacks `dirstat`. |
| capture | A file with one probe result per directory of the tracked space, for offline use. |
| replay | Serving a capture to the walker instead of a live filesystem. |
| anonymized capture | A capture whose names, uids, gids, cluster fsid, fs name and fs path are replaced by HMAC tokens. |
| tree | A directory of a CephFS filesystem sampled to a given depth, identified by (cluster fsid, fs name, fs path, `max_depth`, `max_dirs`); the same on every host and mount point. Samples belong to a tree. The same ROOT with another `max_depth` or `max_dirs` is another tree. |
| filesystem path (`fs_path`) | ROOT's absolute path inside its CephFS filesystem; `/` is the filesystem's root. Unlike ROOT, it does not depend on where the filesystem is mounted. |
| fs name (`fs_name`) | The CephFS filesystem's name, as the mount names it; NULL (None) means the cluster's default filesystem. |
| sample | One run of `trackdir sample`, stored in PostgreSQL: the directories of a tree whose state changed since the baseline (the first sample of a tree: all of them), and its deletions. |
| initial sample | A sample against an empty baseline (the tree's first, or the first after every directory was deleted): every directory of the tracked space; nothing is pruned. |
| incremental sample | Any sample against a non-empty baseline: it prunes the walk with the baseline and stores only changed directories and tombstones. |
| baseline | A tree's current state read from the database before a sample's walk: each directory's last known row, possibly many samples old, plus the retry set. |
| dir state | One directory's row in a sample (`trackdir.dir_state`): owner, mode, nlink, rstats or its error. It is stored only if the directory changed (or is a tombstone), so a sample holds a subset of the tree. |
| current state | Each directory's latest dir state across a tree's samples, by `(started, id)` of the sample, without tombstoned paths (view `trackdir.current_dir_state`). |
| unchanged | A probed directory whose row equals its baseline row in every dir state column. Unless it is in the retry set, its children are not listed. |
| skipped (pruned) subtree | An unchanged directory above max depth and outside the retry set, whose children the walk did not read. |
| new, modified | A row a sample stored is new if its path had no current row before the sample (never seen, or tombstoned), else modified. `db summary` counts both per sample. |
| tombstone | A dir state row with `deleted` true and no other values: the path was deleted. Every deleted directory gets one, including each of its baseline descendants. A path created again later gets an ordinary row. |
| retry set | The paths with an error row in the baseline plus all their ancestors; they are listed even if unchanged, so errors are retried by every sample. |
