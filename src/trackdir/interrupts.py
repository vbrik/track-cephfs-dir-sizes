"""Signals turned into exceptions, so a run unwinds and cleans up.

Both exceptions derive from BaseException, so no `except Exception` on the
way up swallows them. Unwinding is what removes capture's hidden temp file,
cancels a walk's pending probes and rolls back a database transaction.
Python runs signal handlers in the main thread only, so these work only
there.
"""

import contextlib
import signal
from collections.abc import Iterable, Iterator


class Terminated(BaseException):
    """A termination signal arrived."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class Overrun(BaseException):
    """A deadline set by `deadline` passed."""


@contextlib.contextmanager
def terminate_on_signals(
    signums: Iterable[int] = (signal.SIGTERM, signal.SIGHUP),
) -> Iterator[None]:
    """Turn each of SIGNUMS into Terminated while the body runs.

    The default action of SIGTERM and SIGHUP kills the process without
    unwinding (timeout, systemd, a dropped ssh session).
    """

    def handler(signum: int, frame: object) -> None:
        raise Terminated(signum)

    previous = {signum: signal.signal(signum, handler) for signum in signums}
    try:
        yield
    finally:
        for signum, action in previous.items():
            signal.signal(signum, action)


@contextlib.contextmanager
def deadline(seconds: float) -> Iterator[None]:
    """Raise Overrun in the body if it runs longer than SECONDS (> 0).

    Uses SIGALRM and ITIMER_REAL, so one deadline at a time per process.
    """
    if seconds <= 0:  # setitimer with 0 would disarm, not fire at once
        raise ValueError(f"deadline must be in the future: {seconds} s")

    def handler(signum: int, frame: object) -> None:
        raise Overrun

    previous = signal.signal(signal.SIGALRM, handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
