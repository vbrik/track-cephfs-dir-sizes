# Agent Lessons Learned
- When two kernel interfaces share one file descriptor (e.g. `read()` of CephFS dirstat and readdir on a dir fd), probe them together on the real system before designing around either. Prefer calls that don't move the offset (`pread`) when an fd is shared.
