import contextlib
import io
import logging
import os
import re
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import sample_result

from trackdir import __version__, daemon, live
from trackdir.config import DaemonConfig, Interval, TreeConfig
from trackdir.daemon import (
    OnceWarner,
    first_start,
    inspect_trees,
    run,
    run_cycles,
    setup_logging,
)
from trackdir.interrupts import Terminated
from trackdir.live import MSG_NO_DIRSTAT
from trackdir.messages import FatalError
from trackdir.mount import CephMount

HOUR = 3600.0
LINE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ (DEBUG|INFO|WARNING|ERROR) (.*)")
ROOTS = ("/mnt/cephfs/a", "/mnt/cephfs/b")


class FakeTime:
    """A monotonic clock and a sleep that advances it.

    The sleep call after STOP_AFTER sleeps raises Terminated(SIGTERM), which
    ends a daemon run as a stop would. Every requested sleep is recorded.
    """

    def __init__(self, stop_after: int) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.stop_after = stop_after

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.stop_after:
            raise Terminated(signal.SIGTERM)
        self.now += seconds


class FakeSampler:
    """Records (root, start time) per call; each sample takes DURATION seconds.

    ACTION, if given, runs first with the options and the warn callback.
    """

    def __init__(self, fake_time: FakeTime, duration: float = 10.0, action=None):
        self.time = fake_time
        self.duration = duration
        self.action = action
        self.calls: list[tuple[str, float]] = []

    def __call__(self, opts, warn):
        self.calls.append((str(opts.root), self.time.now))
        if self.action is not None:
            self.action(opts, warn)
        self.time.now += self.duration
        return sample_result(root=str(opts.root))


def write_config(tmp_path: Path, roots=ROOTS, extra: str = "") -> Path:
    trees = "".join(f'[[tree]]\nroot = "{root}"\nmax_depth = 1\n\n' for root in roots)
    path = tmp_path / "trackdir.toml"
    path.write_text(f'interval = "1h"\ndb = "postgresql://x/y_test"\n{extra}\n{trees}')
    return path


def lines(stream: io.StringIO) -> list[tuple[str, str]]:
    """Return (level, message) per log line; fails on a line of another form."""
    parsed = []
    for line in stream.getvalue().splitlines():
        match = LINE.fullmatch(line)
        assert match, f"not a log line: {line!r}"
        parsed.append(match.groups())
    return parsed


def never_inspect(config, warn):
    pytest.fail("inspected the trees although loading the config should have failed")


def config_of(interval: Interval, roots=ROOTS) -> DaemonConfig:
    return DaemonConfig(
        path=Path("trackdir.toml"),
        interval=interval,
        dsn="postgresql://x/y_test",
        log_level=logging.INFO,
        trees=tuple(TreeConfig(Path(root), 1) for root in roots),
    )


def test_first_start():
    t0 = datetime(2026, 11, 4, 12, tzinfo=UTC)
    six = Interval(6 * HOUR, "6h")
    assert first_start([None, t0], six, t0) == 0
    assert first_start([t0 - timedelta(hours=7), t0 - timedelta(hours=1)], six, t0) == 0
    assert (
        first_start([t0 - timedelta(hours=5), t0 - timedelta(hours=1)], six, t0) == HOUR
    )
    assert first_start([t0 + timedelta(hours=1)], six, t0) == 7 * HOUR


def test_log_line_format():
    out = io.StringIO()
    setup_logging(out)
    log = setup_logging(out)  # a second call must not add a second handler
    log.info("a b")
    text = out.getvalue()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO a b\n", text)
    stamp = datetime.strptime(text[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 5


def test_log_survives_surrogates():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    setup_logging(stream).info("caf\udce9")
    assert b"caf\\udce9" in stream.buffer.getvalue()


def test_library_logs_go_to_the_stream(capsys):
    out = io.StringIO()
    setup_logging(out)
    logging.getLogger("psycopg").warning("error ignored in rollback")
    assert lines(out) == [("WARNING", "error ignored in rollback")]
    assert capsys.readouterr().err == ""


def test_once_warner():
    out = io.StringIO()
    warn = OnceWarner(setup_logging(out))
    warn("x")
    warn("x")
    warn("y")
    assert lines(out) == [("WARNING", "x"), ("WARNING", "y")]


def test_cycles_sample_trees_in_order_start_to_start(tmp_path):
    path = write_config(tmp_path)
    fake = FakeTime(stop_after=3)
    sampler = FakeSampler(fake)
    out = io.StringIO()
    status = run(
        path,
        stream=out,
        inspect=lambda config, warn: (None, None),
        sample=sampler,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 0
    assert sampler.calls == [
        (ROOTS[0], 0),
        (ROOTS[1], 10),
        (ROOTS[0], HOUR),
        (ROOTS[1], HOUR + 10),
        (ROOTS[0], 2 * HOUR),
        (ROOTS[1], 2 * HOUR + 10),
    ]
    assert fake.sleeps == [0, HOUR - 20, HOUR - 20, HOUR - 20]
    logged = lines(out)
    assert logged[0] == (
        "INFO",
        f"trackdir {__version__} daemon started: config {path}, 2 trees, interval 1h",
    )
    assert re.fullmatch(r"next cycle at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", logged[1][1])
    messages = [message for _, message in logged]
    finished = [m for m in messages if m.startswith("cycle finished in 20.0 s; next")]
    assert len(finished) == 3
    assert sum(m.startswith("Stored sample 7 (initial)") for m in messages) == 6
    assert logged[-1] == ("INFO", "SIGTERM received. Stopped.")


def test_first_cycle_waits_for_the_database(tmp_path):
    now = datetime.now(UTC)
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace('"1h"', '"6h"'))
    fake = FakeTime(stop_after=0)
    status = run(
        path,
        stream=io.StringIO(),
        inspect=lambda config, warn: (
            now - timedelta(hours=1),
            now - timedelta(hours=2),
        ),
        sample=FakeSampler(fake),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 0
    assert fake.sleeps[0] == pytest.approx(4 * HOUR, abs=1)


def test_fatal_sample_error_exits_1(tmp_path):
    def fail_on_b(opts, warn):
        if str(opts.root) == ROOTS[1]:
            raise FatalError("database error: x.")

    fake = FakeTime(stop_after=5)
    out = io.StringIO()
    status = run(
        write_config(tmp_path),
        stream=out,
        inspect=lambda config, warn: (None, None),
        sample=FakeSampler(fake, action=fail_on_b),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 1
    assert lines(out)[-1] == (
        "ERROR",
        f"sample of {ROOTS[1]} failed: database error: x.",
    )


def test_bug_is_logged_with_traceback(tmp_path, capsys):
    def boom(opts, warn):
        raise RuntimeError("boom")

    fake = FakeTime(stop_after=5)
    out = io.StringIO()
    status = run(
        write_config(tmp_path),
        stream=out,
        inspect=lambda config, warn: (None, None),
        sample=FakeSampler(fake, action=boom),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 1
    assert re.search(r"Z ERROR unexpected error\n", out.getvalue())
    assert "RuntimeError: boom" in out.getvalue()
    assert capsys.readouterr().err == ""


def test_warnings_logged_once(tmp_path):
    fake = FakeTime(stop_after=2)
    sampler = FakeSampler(fake, action=lambda opts, warn: warn(MSG_NO_DIRSTAT))
    out = io.StringIO()
    run(
        write_config(tmp_path),
        stream=out,
        inspect=lambda config, warn: (None, None),
        sample=sampler,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert len(sampler.calls) == 4
    assert [level for level, _ in lines(out)].count("WARNING") == 1


def test_overrun_exits_1():
    def slow(opts, warn):
        time.sleep(2)
        pytest.fail("the deadline did not interrupt the sample")

    log = setup_logging(io.StringIO())
    with pytest.raises(FatalError) as raised:
        run_cycles(
            config_of(Interval(0.2, "0.2s")), 0, log, OnceWarner(log), sample=slow
        )
    assert str(raised.value).startswith(
        f"cycle did not finish within the interval 0.2s; sample of {ROOTS[0]} "
        "aborted, nothing was stored for it."
    )
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


class UnhandledSignal(BaseException):
    """Raised by the test's guard handler if the daemon did not handle the signal."""


def test_signal_during_sample_stops_cleanly(tmp_path):
    def guard(signum, frame):
        raise UnhandledSignal(signum)

    def terminate(opts, warn):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # the handler raises long before this returns
        pytest.fail("the signal did not interrupt the sample")

    fake = FakeTime(stop_after=5)
    out = io.StringIO()
    previous = signal.signal(signal.SIGTERM, guard)  # keeps pytest alive on failure
    try:
        status = run(
            write_config(tmp_path),
            stream=out,
            inspect=lambda config, warn: (None, None),
            sample=FakeSampler(fake, action=terminate),
            clock=fake.clock,
            sleep=fake.sleep,
        )
        assert signal.getsignal(signal.SIGTERM) is guard
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert status == 0
    assert lines(out)[-1] == (
        "INFO",
        f"SIGTERM received while sampling {ROOTS[0]}; nothing was stored for it. "
        "Stopped.",
    )


def test_signal_while_idle_stops_cleanly(tmp_path):
    recent = datetime.now(UTC) - timedelta(minutes=1)
    fake = FakeTime(stop_after=0)
    sampler = FakeSampler(fake)
    out = io.StringIO()
    status = run(
        write_config(tmp_path),
        stream=out,
        inspect=lambda config, warn: (recent, recent),
        sample=sampler,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 0
    assert sampler.calls == []
    assert lines(out)[-1] == ("INFO", "SIGTERM received. Stopped.")


def test_bad_config_exits_1(tmp_path):
    out = io.StringIO()
    path = write_config(tmp_path, extra='intervall = "1h"')
    status = run(path, stream=out, inspect=never_inspect)
    assert status == 1
    [(level, message)] = lines(out)
    assert level == "ERROR"
    assert "intervall" in message


def test_log_level_applies_after_loading(tmp_path):
    def fail(opts, warn):
        raise FatalError("database error: x.")

    fake = FakeTime(stop_after=5)
    out = io.StringIO()
    status = run(
        write_config(tmp_path, extra='log_level = "ERROR"'),
        stream=out,
        inspect=lambda config, warn: (None, None),
        sample=FakeSampler(fake, action=fail),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 1
    assert [level for level, _ in lines(out)] == ["ERROR"]


@pytest.fixture
def no_database(monkeypatch):
    """Let inspect_trees run without PostgreSQL: no schema, no samples."""
    monkeypatch.setattr(
        daemon, "database", lambda dsn, unchanged: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(daemon, "check_schema", lambda conn: None)
    monkeypatch.setattr(daemon, "latest_started", lambda conn, key: None)


def test_startup_rejects_duplicate_trees(tmp_path, fake_ceph, no_database, monkeypatch):
    # fake_ceph alone puts every root at /x; follow the resolved root instead.
    monkeypatch.setattr(live, "identify", lambda fd, root: CephMount(None, None, root))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    log = setup_logging(io.StringIO())

    def inspect(*trees: TreeConfig):
        config = DaemonConfig(Path("t.toml"), Interval(HOUR, "1h"), "x", 0, trees)
        return inspect_trees(config, OnceWarner(log))

    assert inspect(TreeConfig(a, 1), TreeConfig(b, 1)) == (None, None)
    assert inspect(TreeConfig(a, 1), TreeConfig(a, 2)) == (None, None)
    with pytest.raises(FatalError) as raised:
        inspect(TreeConfig(a, 1), TreeConfig(a / ".", 1))
    message = str(raised.value)
    assert "trees 1 and 2 in t.toml" in message
    assert "remove one" in message


def test_startup_rejects_non_cephfs_root(tmp_path, no_database):
    fake = FakeTime(stop_after=5)
    sampler = FakeSampler(fake)
    out = io.StringIO()
    root = tmp_path / "plain"
    root.mkdir()
    status = run(
        write_config(tmp_path, roots=(str(root),)),
        stream=out,
        sample=sampler,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert status == 1
    [(level, message)] = [line for line in lines(out) if line[0] != "WARNING"]
    assert level == "ERROR"
    assert str(root) in message
    assert sampler.calls == []
