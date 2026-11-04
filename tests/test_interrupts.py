import os
import signal
import time

import pytest

from trackdir.interrupts import Overrun, Terminated, deadline, terminate_on_signals


def test_deadline_raises_overrun():
    before = signal.getsignal(signal.SIGALRM)
    with pytest.raises(Overrun), deadline(0.05):
        time.sleep(2)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


def test_deadline_disarms_on_normal_exit():
    before = signal.getsignal(signal.SIGALRM)
    with deadline(5):
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


@pytest.mark.parametrize("seconds", [0, -1])
def test_deadline_rejects_non_positive(seconds):
    with pytest.raises(ValueError), deadline(seconds):
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
