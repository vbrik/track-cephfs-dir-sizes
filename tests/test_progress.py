import io
import logging
import os
from pathlib import Path

from conftest import Terminal
from conftest import status_lines as lines

from trackdir.progress import LogProgress, StatusLine

ERASE = "\x1b[K"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_nothing_is_written_unless_the_stream_is_a_terminal():
    stream = io.StringIO()
    with StatusLine(stream) as progress:
        progress.phase("loading the baseline")
        progress.walk(probed=1, queued=2, errors=0)
    assert stream.getvalue() == ""


def test_a_phase_is_drawn_at_once_in_place():
    stream = Terminal()
    progress = StatusLine(stream)
    progress.phase("loading the baseline")
    progress.phase("storing 3 directories")
    assert stream.getvalue() == (
        f"\rloading the baseline{ERASE}\rstoring 3 directories{ERASE}"
    )


def test_walk_line_shows_counts_rate_and_elapsed_time():
    stream, clock = Terminal(), Clock()
    progress = StatusLine(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 192
    progress.walk(probed=412_331, queued=1_204, errors=3)
    assert lines(stream.getvalue())[-1] == (
        "probed 412,331 dirs, 1,204 queued, 3 errors, 2,148 dirs/s, 3m12s"
    )


def test_walk_elapsed_time_counts_hours():
    stream, clock = Terminal(), Clock()
    progress = StatusLine(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 3 * 3600 + 5 * 60 + 7
    progress.walk(probed=0, queued=1, errors=0)
    assert lines(stream.getvalue())[-1].endswith(", 3h05m07s")


def test_walk_redraws_at_most_once_per_interval():
    stream, clock = Terminal(), Clock()
    progress = StatusLine(stream, clock=clock, interval=1.0)
    progress.walk(probed=0, queued=1, errors=0)  # the first is drawn at once
    clock.now += 0.5
    progress.walk(probed=1, queued=0, errors=0)
    clock.now += 0.5
    progress.walk(probed=2, queued=0, errors=0)
    assert [line.split(",")[0] for line in lines(stream.getvalue())] == [
        "probed 0 dirs",
        "probed 2 dirs",
    ]


def test_walk_after_a_phase_restarts_the_clock():
    stream, clock = Terminal(), Clock()
    progress = StatusLine(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 60
    progress.phase("loading the baseline")
    progress.walk(probed=0, queued=1, errors=0)
    assert lines(stream.getvalue())[-1].endswith(", 0m00s")


def test_line_is_cut_to_fit_the_terminal():
    stream = Terminal()
    StatusLine(stream).phase("x" * 200)
    assert lines(stream.getvalue()) == [
        "x" * 79
    ]  # 80 columns assumed, last one left free


def test_a_terminal_without_a_size_counts_as_80_columns():
    leader, follower = os.openpty()  # a new pty reports 0 columns

    class Pty(Terminal):
        def fileno(self):
            return follower

    try:
        stream = Pty()
        StatusLine(stream).phase("x" * 200)
        assert lines(stream.getvalue()) == ["x" * 79]
    finally:
        os.close(leader)
        os.close(follower)


def test_exit_erases_the_line():
    stream = Terminal()
    with StatusLine(stream) as progress:
        progress.phase("loading the baseline")
    assert lines(stream.getvalue())[-1] == ""


def test_clear_writes_nothing_when_nothing_is_drawn():
    stream = Terminal()
    progress = StatusLine(stream)
    progress.clear()
    progress.phase("a")
    progress.clear()
    progress.clear()
    assert lines(stream.getvalue()) == ["a", ""]


def log_progress(caplog, clock, level=logging.DEBUG, interval=300.0):
    log = logging.getLogger("trackdir.test_progress")
    caplog.set_level(level, logger=log.name)
    return LogProgress(log, Path("/mnt/cephfs/a"), clock=clock, interval=interval)


def logged(caplog) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records]


def test_log_progress_waits_one_interval_after_the_walk_starts(caplog):
    clock = Clock()
    progress = log_progress(caplog, clock)
    progress.phase("loading the baseline")
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 299
    progress.walk(probed=10, queued=1, errors=0)
    assert logged(caplog) == []  # a walk shorter than the interval logs nothing
    clock.now += 1
    progress.walk(probed=600, queued=1_204, errors=3)
    assert logged(caplog) == [
        (
            logging.DEBUG,
            "progress of /mnt/cephfs/a: probed 600 dirs, 1,204 queued, 3 errors, "
            "2 dirs/s, 5m00s",
        )
    ]


def test_log_progress_logs_at_most_once_per_interval(caplog):
    clock = Clock()
    progress = log_progress(caplog, clock, interval=10)
    progress.walk(probed=0, queued=1, errors=0)
    for probed in range(1, 31):
        clock.now += 1
        progress.walk(probed=probed, queued=1, errors=0)
    assert [m.split(": ")[1].split(",")[0] for _, m in logged(caplog)] == [
        "probed 10 dirs",
        "probed 20 dirs",
        "probed 30 dirs",
    ]


def test_log_progress_phase_restarts_the_walk(caplog):
    clock = Clock()
    progress = log_progress(caplog, clock, interval=10)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 9
    progress.phase("storing 3 directories")
    progress.walk(probed=0, queued=1, errors=0)  # starts a new walk
    clock.now += 9
    progress.walk(probed=1, queued=0, errors=0)
    assert logged(caplog) == []
    clock.now += 1
    progress.walk(probed=1, queued=0, errors=0)
    [(_, message)] = logged(caplog)
    assert message.endswith(", 0m10s")


def test_log_progress_reads_no_clock_above_debug(caplog):
    def clock():
        raise AssertionError("the clock was read")

    progress = log_progress(caplog, clock, level=logging.INFO)
    progress.walk(probed=0, queued=1, errors=0)
    progress.walk(probed=1, queued=0, errors=0)
    assert logged(caplog) == []
