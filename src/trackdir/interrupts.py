"""Signals turned into exceptions, so a run unwinds and cleans up; and a
timer that interrupts nothing (call_after).

Both exceptions derive from BaseException, so no `except Exception` on the
way up swallows them. Unwinding is what removes capture's hidden temp file,
cancels a walk's pending probes and rolls back a database transaction.
Python runs signal handlers in the main thread only, so these work only
there.
"""

import contextlib
import signal
import threading
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
    """Call ON_EXPIRE once, from a timer thread, if the body still runs after
    SECONDS (> 0); wait for that call to finish before returning.

    Nothing is interrupted, and no signal is used. A signal would run its
    handler in the main thread, in the middle of whatever it is doing: inside
    a log write (which then fails or interleaves), or not until a call stuck
    in the kernel returns; and a call made through ctypes, such as openat2,
    would fail with EINTR instead of being retried.
    """
    if seconds <= 0:
        raise ValueError(f"the time must be in the future: {seconds} s")
    timer = threading.Timer(seconds, on_expire)
    timer.name = "trackdir-call-after"
    timer.daemon = True  # a hung ON_EXPIRE must not keep the process alive
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()
