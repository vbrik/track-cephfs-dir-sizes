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
| pinned rctime | An rctime in the future, often `4294967295.000000000`, from a future ctime somewhere in the subtree. Because rctime is a maximum, it no longer changes when the subtree does. A directory that changes during a walk also shows an rctime later than the walk's start without being pinned, so a sample counts only skipped directories, whose rows an earlier sample read. |
| MDS round trip | One request to the Ceph metadata server. Every `ceph.dir.*` getxattr costs one. |
| dirstat mode | Reading all rstats with one getxattr plus `pread()` on the directory; needs the `dirstat` mount option. |
| getxattr mode | Reading each rstat with its own getxattr; used when the mount lacks `dirstat`. |
| capture | A file with one probe result per directory of the tracked space, for offline use. |
| replay | Serving a capture to the walker instead of a live filesystem. |
| anonymized capture | A capture whose names, uids, gids, cluster fsid, fs name and fs path are replaced by HMAC tokens. |
| tree | A directory of a CephFS filesystem sampled to a given depth, under a name its user chose. Samples belong to a tree. The name stands for the tree's mount identity, and a sample is stored only if what is sampled still has it. The same ROOT with another `max_depth` is another tree and needs another name; another `max_dirs` is the same tree. |
| tree name | What the user calls a tree (`trackdir sample --tree`, `name` in the daemon's config): 1 to 64 characters of `A-Z a-z 0-9 . _ -`, the first a letter or digit; case matters. A new name starts a tree. |
| mount identity | What the mount says a tree is: cluster fsid, fs name, fs path and `max_depth` (`db.TreeKey`). The same on every host and mount point that names the filesystem the same way (see fs name). The database records it when a tree is created; a name used with another mount identity, or a mount identity used with another name, is refused. |
| filesystem path (`fs_path`) | ROOT's absolute path inside its CephFS filesystem; `/` is the filesystem's root. Unlike ROOT, it does not depend on where the filesystem is mounted. |
| fs name (`fs_name`) | The CephFS filesystem's name, as the mount names it; NULL (None) means the mount names none, so it is the cluster's default filesystem. A mount that names the default filesystem gives another fs name, hence another mount identity: a tree sampled through it is refused until its recorded identity is changed (see "Changed mount" in [schema](schema.md)). |
| sample | One run of `trackdir sample`, stored in PostgreSQL: the directories of a tree whose state changed since the baseline (the first sample of a tree: all of them), and its deletions. |
| initial sample | A sample against an empty baseline (the tree's first, or the first after every directory was deleted): every directory of the tracked space; nothing is pruned. |
| incremental sample | Any sample against a non-empty baseline: it prunes the walk with the baseline and stores only changed directories and tombstones. |
| baseline | A tree's current state read from the database before a sample's walk: each directory's last known row, possibly many samples old, plus the retry set. |
| dir state | One directory's row in a sample (`trackdir.dir_state`): owner, mode, nlink, rstats or its error, with the rstats it replaced. It is stored only if the directory changed (or is a tombstone), so a sample holds a subset of the tree. |
| path id | The number that names a path of a tree in the database (`trackdir.path`). A path is stored once; one deleted and created again keeps its id, so its history continues. The same relative path in another tree has another id. |
| current state | Each directory's latest dir state across a tree's samples, by `(started, id)` of the sample, without tombstoned paths: the directories that exist now. Stored in table `trackdir.current_dir_state`, which the transaction that stores a sample keeps up to date. |
| unchanged | A probed directory whose row equals its baseline row in every dir state column. Unless it is in the retry set, its children are not listed. |
| skipped (pruned) subtree | An unchanged directory above max depth and outside the retry set, whose children the walk did not read. |
| new, modified | A row a sample stored is new if its path had no current row before the sample (never seen, or tombstoned), else modified. The row's `created` column says which. `db summary` counts both per sample. |
| growth | How much a directory's rbytes grew between two samples: its state as of the later one minus its state as of the earlier one (each path's latest row up to that sample); a directory absent at the start grows from 0. Growth is unknown without rstats at either end, and below a directory unlisted at either end: a row there may predate that end, or a new directory may have existed unseen. |
| own growth | A directory's growth minus that of the directories below it that have a known growth, each counted at its nearest such ancestor: the growth of its files. Own growths add up to ROOT's growth. A directory at max depth, or unlisted at the end, owns its subtree's growth, as does the nearest ancestor of a directory with an unknown growth; `db summary` marks such a directory (subtree). `db summary` ranks directories at every depth by it, with each one's share of all positive own growth. CephFS propagates rstats upward with a delay, so an own growth can be off by its children's latest changes. |
| interval | The time between the starts of two cycles of `trackdir daemon`, from its config file. |
| cycle | One round of `trackdir daemon`: a sample of every tree in its config file, in order. |
| overrun | A cycle not finished one interval after it started. The sample in progress is aborted and stores nothing, and the daemon exits with status 1. |
| tombstone | A dir state row with `deleted` true and no values but the rstats it replaced: the path was deleted. Every deleted directory gets one, including each of its baseline descendants. A path created again later gets an ordinary row. |
| retry set | The paths with an error row in the baseline plus all their ancestors; they are listed even if unchanged, so errors are retried by every sample. |
