# Agent Lessons Learned
- When two kernel interfaces share one file descriptor (e.g. `read()` of CephFS dirstat and readdir on a dir fd), probe them together on the real system before designing around either. Prefer calls that don't move the offset (`pread`) when an fd is shared.
- ruff 0.16+ formats Python code blocks inside Markdown files; scope ruff to Python files (e.g. `extend-exclude = ["*.md"]` in `pyproject.toml`) so docs are not rewritten as a side effect.
- Add round-trip tests (write → load → compare) whenever one component produces data another consumes: they catch ordering and encoding assumptions that unit tests on each side miss.
- Before recommending a selection or ranking rule over recursive statistics
  (rbytes, rfiles), run it by hand on a tiny example tree; parents dominate
  children, so plausible-sounding rules can collapse to a trivial answer.
- Text meant for a terminal must not carry `os.fsdecode` surrogates: a strict
  UTF-8 stdout raises `UnicodeEncodeError` on them. Use `dbcli.display_path`
  (backslashreplace) for human-readable output, fsdecode only for JSON with
  `ensure_ascii`. Test with a non-UTF-8 name such as `b"caf\xe9"`; anonymized
  fixtures never contain one.
