import os
import signal
import time

import pytest

from trackdir.interrupts import Terminated, call_after, terminate_on_signals


def test_call_after_calls_once_and_lets_the_body_finish():
    before = signal.getsignal(signal.SIGALRM)
    calls = []
    finished = False
    with call_after(0.05, lambda: calls.append(1)):
        for _ in range(30):
            time.sleep(0.01)
        finished = True
    assert calls == [1]
    assert finished
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


def test_call_after_does_not_interrupt_a_sleep():
    calls = []
    clock = time.monotonic()
    with call_after(0.05, lambda: calls.append(1)):
        time.sleep(0.3)
    assert calls == [1]
    assert time.monotonic() - clock >= 0.3


def test_call_after_does_not_call_a_body_that_finished_in_time():
    before = signal.getsignal(signal.SIGALRM)
    calls = []
    with call_after(0.05, lambda: calls.append(1)):
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    time.sleep(0.1)
    assert calls == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


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
