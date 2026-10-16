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
| tree | A directory of a CephFS filesystem, identified by (cluster fsid, fs name, fs path); the same on every host and mount point. Samples belong to a tree. |
| filesystem path (`fs_path`) | ROOT's absolute path inside its CephFS filesystem; `/` is the filesystem's root. Unlike ROOT, it does not depend on where the filesystem is mounted. |
| fs name (`fs_name`) | The CephFS filesystem's name, as the mount names it; NULL (None) means the cluster's default filesystem. |
| sample | The state of a tree's directories at one time, stored in PostgreSQL. Later samples will record only the directories that changed since their last known state. |
| initial sample | A tree's first sample: every directory of the tracked space. The only kind `trackdir sample` stores so far. |
| dir state | One directory's row in a sample (`trackdir.dir_state`): owner, mode, nlink, rstats or its error. |
| current state | Each directory's latest dir state across a tree's samples (view `trackdir.current_dir_state`). |
