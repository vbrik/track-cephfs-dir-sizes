import io
import os

from conftest import Terminal
from conftest import status_lines as lines

from trackdir.progress import Progress

ERASE = "\x1b[K"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_nothing_is_written_unless_the_stream_is_a_terminal():
    stream = io.StringIO()
    with Progress(stream) as progress:
        progress.phase("loading the baseline")
        progress.walk(probed=1, queued=2, errors=0)
    assert stream.getvalue() == ""


def test_a_phase_is_drawn_at_once_in_place():
    stream = Terminal()
    progress = Progress(stream)
    progress.phase("loading the baseline")
    progress.phase("storing 3 directories")
    assert stream.getvalue() == (
        f"\rloading the baseline{ERASE}\rstoring 3 directories{ERASE}"
    )


def test_walk_line_shows_counts_rate_and_elapsed_time():
    stream, clock = Terminal(), Clock()
    progress = Progress(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 192
    progress.walk(probed=412_331, queued=1_204, errors=3)
    assert lines(stream.getvalue())[-1] == (
        "probed 412,331 dirs, 1,204 queued, 3 errors, 2,148 dirs/s, 3m12s"
    )


def test_walk_elapsed_time_counts_hours():
    stream, clock = Terminal(), Clock()
    progress = Progress(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 3 * 3600 + 5 * 60 + 7
    progress.walk(probed=0, queued=1, errors=0)
    assert lines(stream.getvalue())[-1].endswith(", 3h05m07s")


def test_walk_redraws_at_most_once_per_interval():
    stream, clock = Terminal(), Clock()
    progress = Progress(stream, clock=clock, interval=1.0)
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
    progress = Progress(stream, clock=clock)
    progress.walk(probed=0, queued=1, errors=0)
    clock.now += 60
    progress.phase("loading the baseline")
    progress.walk(probed=0, queued=1, errors=0)
    assert lines(stream.getvalue())[-1].endswith(", 0m00s")


def test_line_is_cut_to_fit_the_terminal():
    stream = Terminal()
    Progress(stream).phase("x" * 200)
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
        Progress(stream).phase("x" * 200)
        assert lines(stream.getvalue()) == ["x" * 79]
    finally:
        os.close(leader)
        os.close(follower)


def test_exit_erases_the_line():
    stream = Terminal()
    with Progress(stream) as progress:
        progress.phase("loading the baseline")
    assert lines(stream.getvalue())[-1] == ""


def test_clear_writes_nothing_when_nothing_is_drawn():
    stream = Terminal()
    progress = Progress(stream)
    progress.clear()
    progress.phase("a")
    progress.clear()
    progress.clear()
    assert lines(stream.getvalue()) == ["a", ""]
