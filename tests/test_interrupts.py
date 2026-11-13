import ctypes
import os
import signal
import threading
import time

import pytest

from trackdir.interrupts import Terminated, call_after, terminate_on_signals


def timer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "trackdir-call-after"]


def test_call_after_calls_once_and_lets_the_body_finish():
    calls = []
    finished = False
    with call_after(0.05, lambda: calls.append(1)):
        for _ in range(30):
            time.sleep(0.01)
        finished = True
    assert calls == [1]
    assert finished
    assert timer_threads() == []


def test_call_after_does_not_interrupt_a_blocking_call_in_the_main_thread():
    """A signal would interrupt it, and a call through ctypes (openat2) does
    not retry on EINTR: the call must come from elsewhere."""
    libc = ctypes.CDLL(None, use_errno=True)
    calls = []
    with call_after(0.05, lambda: calls.append(1)):
        result = libc.usleep(300_000)
        errno = ctypes.get_errno()
    assert (result, errno) == (0, 0)
    assert calls == [1]


def test_call_after_does_not_call_a_body_that_finished_in_time():
    before = signal.getsignal(signal.SIGALRM)
    calls = []
    with call_after(0.05, lambda: calls.append(1)):
        pass
    time.sleep(0.1)
    assert calls == []
    assert timer_threads() == []
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_call_after_waits_for_a_call_in_progress_before_returning():
    started = threading.Event()
    done = []

    def slow():
        started.set()
        time.sleep(0.2)
        done.append(1)

    with call_after(0.01, slow):
        started.wait(1)
    assert done == [1]
    assert timer_threads() == []


@pytest.mark.parametrize("seconds", [0, -1])
def test_call_after_rejects_non_positive(seconds):
    with pytest.raises(ValueError), call_after(seconds, lambda: None):
        pass


def test_terminate_on_signals_includes_sigint():
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(Terminated) as raised, terminate_on_signals((signal.SIGINT,)):
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)  # the handler raises long before this returns
    assert raised.value.signum == signal.SIGINT
    assert signal.getsignal(signal.SIGINT) is before


def test_terminate_on_signals_defaults_to_sigterm_and_sighup():
    with terminate_on_signals():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with pytest.raises(Terminated):
                os.kill(os.getpid(), signum)
                time.sleep(5)
