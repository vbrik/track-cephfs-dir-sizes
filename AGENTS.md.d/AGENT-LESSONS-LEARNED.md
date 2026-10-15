# Agent Lessons Learned
- When two kernel interfaces share one file descriptor (e.g. `read()` of CephFS dirstat and readdir on a dir fd), probe them together on the real system before designing around either. Prefer calls that don't move the offset (`pread`) when an fd is shared.
- ruff 0.16+ formats Python code blocks inside Markdown files; scope ruff to Python files (e.g. `extend-exclude = ["*.md"]` in `pyproject.toml`) so docs are not rewritten as a side effect.
- Add round-trip tests (write → load → compare) whenever one component produces data another consumes: they catch ordering and encoding assumptions that unit tests on each side miss.
