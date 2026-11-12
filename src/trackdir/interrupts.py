"""Signals turned into exceptions, so a run unwinds and cleans up.

Both exceptions derive from BaseException, so no `except Exception` on the
way up swallows them. Unwinding is what removes capture's hidden temp file,
cancels a walk's pending probes and rolls back a database transaction.
Python runs signal handlers in the main thread only, so these work only
there.
"""

import contextlib
import signal
from collections.abc import Callable, Iterable, Iterator


class Terminated(BaseException):
    """A termination signal arrived."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


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
def call_after(seconds: float, on_expire: Callable[[], None]) -> Iterator[None]:
    """Call ON_EXPIRE once if the body still runs after SECONDS (> 0).

    The body is not interrupted: ON_EXPIRE runs in the main thread, between
    two of its bytecodes or during a blocking call that then resumes, so it
    must be short and must not raise. Uses SIGALRM and ITIMER_REAL, so one
    timer at a time per process; both are restored on exit.
    """
    if seconds <= 0:  # setitimer with 0 would disarm, not fire at once
        raise ValueError(f"the time must be in the future: {seconds} s")

    def handler(signum: int, frame: object) -> None:
        on_expire()

    previous = signal.signal(signal.SIGALRM, handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
