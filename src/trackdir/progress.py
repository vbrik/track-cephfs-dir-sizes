"""Progress of a long run, so it shows it is working.

A manual command draws a StatusLine on stderr. Only a terminal gets it: logs
and pipes stay free of carriage returns. The line is redrawn in place and
erased before any other message, so it never mixes with them, and it is cut to
the terminal's width, since a wrapped line cannot be redrawn.

The daemon logs a walk's counts with LogProgress instead, once per interval.
"""

import logging
import os
import time
from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self, TextIO

ERASE_TO_END = "\x1b[K"
DEFAULT_COLUMNS = 80  # when the terminal does not report its width


class Progress(Protocol):
    """Where a run reports its steps and its walk's counts."""

    def phase(self, text: str) -> None:
        """Report the step TEXT; the next walk call starts a new walk."""

    def walk(self, *, probed: int, queued: int, errors: int) -> None:
        """Report a walk's counts; the first call after a phase starts its clock."""


class StatusLine:
    """The status line of one run; a context manager that erases it on exit.

    Does nothing unless STREAM is a terminal. CLOCK returns seconds; the walk
    line is redrawn at most once per INTERVAL seconds.
    """

    def __init__(
        self,
        stream: TextIO | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = 1.0,
    ) -> None:
        self._stream = stream if stream is not None and stream.isatty() else None
        self._clock = clock
        self._interval = interval
        self._drawn = False  # the line holds text
        self._walk_started: float | None = None
        self._last_draw = 0.0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.clear()

    def phase(self, text: str) -> None:
        """Show TEXT now; the next walk call starts a new walk."""
        self._walk_started = None
        self._draw(text)

    def walk(self, *, probed: int, queued: int, errors: int) -> None:
        """Show a walk's counts; the first call after a phase starts its clock."""
        if self._stream is None:
            return
        now = self._clock()
        if self._walk_started is None:
            self._walk_started = now
        elif now - self._last_draw < self._interval:
            return
        self._draw(walk_text(probed, queued, errors, now - self._walk_started))

    def clear(self) -> None:
        """Erase the line, if it holds text, so another message can follow."""
        if self._stream is not None and self._drawn:
            self._stream.write("\r" + ERASE_TO_END)
            self._stream.flush()
            self._drawn = False

    def _draw(self, text: str) -> None:
        if self._stream is None:
            return
        self._stream.write(f"\r{text[: self._columns() - 1]}{ERASE_TO_END}")
        self._stream.flush()
        self._drawn = True
        self._last_draw = self._clock()

    def _columns(self) -> int:
        try:
            columns = os.get_terminal_size(self._stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return DEFAULT_COLUMNS
        return columns or DEFAULT_COLUMNS  # a pty nobody sized reports 0


class LogProgress:
    """Logs a walk's counts at DEBUG to LOG, once per INTERVAL seconds of it.

    Each line starts with "progress of LABEL". Nothing is logged in a walk's
    first INTERVAL seconds, nor for steps. CLOCK returns seconds.
    """

    def __init__(
        self,
        log: logging.Logger,
        label: object,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float,
    ) -> None:
        self._log = log
        self._label = label
        self._clock = clock
        self._interval = interval
        self._walk_started: float | None = None
        self._last_log = 0.0

    def phase(self, text: str) -> None:
        """Start a new walk at the next walk call; TEXT is not logged."""
        self._walk_started = None

    def walk(self, *, probed: int, queued: int, errors: int) -> None:
        """Log the counts if INTERVAL seconds passed since the walk's last line."""
        if not self._log.isEnabledFor(logging.DEBUG):
            return
        now = self._clock()
        if self._walk_started is None:
            self._walk_started = self._last_log = now
            return
        if now - self._last_log < self._interval:
            return
        self._last_log = now
        self._log.debug(
            "progress of %s: %s",
            self._label,
            walk_text(probed, queued, errors, now - self._walk_started),
        )


# Shown by nothing; the default where a caller passes no Progress.
NO_PROGRESS = StatusLine(None)


def walk_text(probed: int, queued: int, errors: int, elapsed: float) -> str:
    """Return a walk's counts, rate and ELAPSED seconds as one line."""
    rate = probed / elapsed if elapsed > 0 else 0
    return (
        f"probed {probed:,} dirs, {queued:,} queued, {errors:,} errors, "
        f"{rate:,.0f} dirs/s, {_duration(elapsed)}"
    )


def _duration(seconds: float) -> str:
    """Return SECONDS as 3m12s, or 1h03m12s from an hour up."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"
