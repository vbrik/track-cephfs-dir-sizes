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

from trackdir import __version__, daemon, dbcli, live
from trackdir.cli import main
from trackdir.config import DaemonConfig, Interval, TreeConfig
from trackdir.daemon import (
    OnceWarner,
    first_start,
    inspect_trees,
    run,
    run_cycles,
    setup_logging,
)
from trackdir.db import TreeKey, TreeMismatch
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

    ACTION, if given, runs first with the options, the warn callback and the
    progress.
    """

    def __init__(self, fake_time: FakeTime, duration: float = 10.0, action=None):
        self.time = fake_time
        self.duration = duration
        self.action = action
        self.calls: list[tuple[str, float]] = []

    def __call__(self, opts, warn, *, progress):
        self.calls.append((str(opts.root), self.time.now))
        if self.action is not None:
            self.action(opts, warn, progress)
        self.time.now += self.duration
        return sample_result(root=str(opts.root))


def write_config(tmp_path: Path, roots=ROOTS, extra: str = "") -> Path:
    trees = "".join(
        f'[[tree]]\nname = "t{n}"\nroot = "{root}"\nmax_depth = 1\n\n'
        for n, root in enumerate(roots, 1)
    )
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
        trees=tuple(
            TreeConfig(f"t{n}", Path(root), 1) for n, root in enumerate(roots, 1)
        ),
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
    assert (
        sum(m.startswith("Stored sample 7 (initial) of tree x:") for m in messages) == 6
    )
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
    def fail_on_b(opts, warn, progress):
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
    def boom(opts, warn, progress):
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
    sampler = FakeSampler(
        fake, action=lambda opts, warn, progress: warn(MSG_NO_DIRSTAT)
    )
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


OVERRUN_ADVICE = (
    "it will finish, and the next cycle starts when it does. Raise interval or "
    "sample fewer or smaller trees if this repeats."
)
SHORT = Interval(0.2, "0.2s")  # for tests of the real timer


def stop_before_cycle(number: int):
    """Return a sleep for run_cycles that stops it before cycle NUMBER, and
    the list of the sleeps it was asked for."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == number:
            raise Terminated(signal.SIGTERM)

    return sleep, sleeps


def slow_sampler(seconds: float, before_return=lambda: None):
    def sample(opts, warn, *, progress):
        time.sleep(seconds)
        before_return()
        return sample_result(root=str(opts.root), name=opts.tree)

    return sample


def test_overrun_is_logged_when_the_interval_passes_and_the_sample_finishes():
    before = signal.getsignal(signal.SIGALRM)
    out = io.StringIO()
    log = setup_logging(out)
    logged_during_the_sample = []
    sample = slow_sampler(0.5, lambda: logged_during_the_sample.extend(lines(out)))
    sleep, sleeps = stop_before_cycle(2)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(SHORT, ROOTS[:1]),
            0,
            log,
            OnceWarner(log),
            sample=sample,
            sleep=sleep,
        )
    overrun = (
        "ERROR",
        f"cycle still running after the interval 0.2s, sampling {ROOTS[0]} "
        f"(tree t1); {OVERRUN_ADVICE}",
    )
    assert logged_during_the_sample == [overrun]
    logged = lines(out)
    assert logged[0] == overrun
    assert [level for level, _ in logged] == ["ERROR", "INFO", "INFO"]
    assert logged[1][1].startswith("Stored sample")
    assert re.fullmatch(
        r"cycle finished in 0\.[5-9] s, 0\.[3-7] s past the interval; "
        r"next cycle starts now",
        logged[2][1],
    )
    assert sleeps == [0, 0]  # the next cycle was due at once
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


def test_a_cycle_of_several_intervals_logs_one_error():
    out = io.StringIO()
    log = setup_logging(out)
    sleep, _ = stop_before_cycle(2)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(SHORT, ROOTS[:1]),
            0,
            log,
            OnceWarner(log),
            sample=slow_sampler(0.9),
            sleep=sleep,
        )
    assert [level for level, _ in lines(out)].count("ERROR") == 1


def test_every_overrunning_cycle_logs_its_error_and_the_daemon_carries_on():
    out = io.StringIO()
    log = setup_logging(out)
    sleep, sleeps = stop_before_cycle(4)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(SHORT, ROOTS[:1]),
            0,
            log,
            OnceWarner(log),
            sample=slow_sampler(0.3),
            sleep=sleep,
        )
    logged = lines(out)
    assert [level for level, _ in logged] == ["ERROR", "INFO", "INFO"] * 3
    assert sleeps == [0, 0, 0, 0]


def test_overrun_between_trees_names_no_tree(monkeypatch):
    out = io.StringIO()
    log = setup_logging(out)
    real = daemon.describe_sample
    described = []

    def slow_describe(result):
        if not described:
            time.sleep(0.5)  # the interval passes while no tree is sampled
        described.append(result)
        return real(result)

    monkeypatch.setattr(daemon, "describe_sample", slow_describe)
    sleep, _ = stop_before_cycle(2)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(SHORT),
            0,
            log,
            OnceWarner(log),
            sample=slow_sampler(0),
            sleep=sleep,
        )
    errors = [message for level, message in lines(out) if level == "ERROR"]
    assert errors == [f"cycle still running after the interval 0.2s; {OVERRUN_ADVICE}"]
    assert len(described) == len(ROOTS)  # the cycle went on to the other tree


def test_next_cycle_starts_at_once_after_an_overrun_and_missed_cycles_are_skipped():
    fake = FakeTime(stop_after=3)
    out = io.StringIO()
    log = setup_logging(out)

    def first_takes_long(opts, warn, progress):
        if not sampler.calls[1:]:
            fake.now += 2.5 * HOUR - sampler.duration

    sampler = FakeSampler(fake, action=first_takes_long)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(Interval(HOUR, "1h"), ROOTS[:1]),
            0,
            log,
            OnceWarner(log),
            sample=sampler,
            clock=fake.clock,
            sleep=fake.sleep,
        )
    assert [start for _, start in sampler.calls] == [0, 2.5 * HOUR, 3.5 * HOUR]
    finished = [m for _, m in lines(out) if m.startswith("cycle finished")]
    assert finished[0] == (
        "cycle finished in 9000.0 s, 5400.0 s past the interval; next cycle starts now"
    )
    assert finished[1].startswith("cycle finished in 10.0 s; next cycle at ")


def test_cycles_run_back_to_back_under_lasting_overload():
    fake = FakeTime(stop_after=3)
    log = setup_logging(io.StringIO())
    sampler = FakeSampler(fake, duration=1.5 * HOUR)
    with pytest.raises(Terminated):
        run_cycles(
            config_of(Interval(HOUR, "1h"), ROOTS[:1]),
            0,
            log,
            OnceWarner(log),
            sample=sampler,
            clock=fake.clock,
            sleep=fake.sleep,
        )
    assert [start for _, start in sampler.calls] == [0, 1.5 * HOUR, 3 * HOUR]
    assert fake.sleeps[:3] == [0, 0, 0]


def test_sigterm_during_an_overrunning_sample_stops_with_status_0(tmp_path):
    before = signal.getsignal(signal.SIGALRM)

    def stopped_late(opts, warn, *, progress):
        time.sleep(1.2)  # past the interval of 1 s
        raise Terminated(signal.SIGTERM)

    config = tmp_path / "trackdir.toml"
    config.write_text(
        'interval = "1s"\ndb = "postgresql://x/y_test"\n'
        f'[[tree]]\nname = "t1"\nroot = "{ROOTS[0]}"\nmax_depth = 1\n'
    )
    out = io.StringIO()
    status = run(config, stream=out, inspect=lambda c, w: (None,), sample=stopped_late)
    assert status == 0
    logged = lines(out)
    assert [message for level, message in logged if level == "ERROR"] == [
        f"cycle still running after the interval 1s, sampling {ROOTS[0]} (tree t1); "
        f"{OVERRUN_ADVICE}"
    ]
    assert logged[-1] == (
        "INFO",
        f"SIGTERM received while sampling {ROOTS[0]}; nothing was stored for it. "
        "Stopped.",
    )
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is before


def test_walk_progress_is_logged_at_debug_once_per_interval(tmp_path):
    def walk(opts, warn, progress):
        progress.phase("loading the baseline")
        progress.walk(probed=0, queued=1, errors=0)  # starts the walk's clock
        fake.now += daemon.PROGRESS_INTERVAL - 1
        progress.walk(probed=10, queued=5, errors=0)  # too early
        fake.now += 1
        progress.walk(probed=600, queued=40, errors=2)
        fake.now += daemon.PROGRESS_INTERVAL / 2
        progress.walk(probed=900, queued=9, errors=2)  # too early again

    fake = FakeTime(stop_after=1)
    out = io.StringIO()
    run(
        write_config(tmp_path, roots=ROOTS[:1], extra='log_level = "DEBUG"'),
        stream=out,
        inspect=lambda config, warn: (None,),
        sample=FakeSampler(fake, action=walk),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    progress = [m for level, m in lines(out) if m.startswith("progress of ")]
    assert progress == [
        f"progress of {ROOTS[0]}: probed 600 dirs, 40 queued, 2 errors, 2 dirs/s, 5m00s"
    ]
    assert ("DEBUG", progress[0]) in lines(out)


def test_walk_progress_is_not_logged_at_info(tmp_path):
    def walk(opts, warn, progress):
        progress.walk(probed=0, queued=1, errors=0)
        fake.now += 10 * daemon.PROGRESS_INTERVAL
        progress.walk(probed=1, queued=0, errors=0)

    fake = FakeTime(stop_after=1)
    out = io.StringIO()
    run(
        write_config(tmp_path, roots=ROOTS[:1]),
        stream=out,
        inspect=lambda config, warn: (None,),
        sample=FakeSampler(fake, action=walk),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert not any(m.startswith("progress of ") for _, m in lines(out))


class UnhandledSignal(BaseException):
    """Raised by the test's guard handler if the daemon did not handle the signal."""


def test_signal_during_sample_stops_cleanly(tmp_path):
    def guard(signum, frame):
        raise UnhandledSignal(signum)

    def terminate(opts, warn, progress):
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
    def fail(opts, warn, progress):
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
    monkeypatch.setattr(daemon, "latest_started", lambda conn, name, key: None)


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

    assert inspect(TreeConfig("a", a, 1), TreeConfig("b", b, 1)) == (None, None)
    assert inspect(TreeConfig("a", a, 1), TreeConfig("a2", a, 2)) == (None, None)
    with pytest.raises(FatalError) as raised:
        inspect(TreeConfig("a", a, 1), TreeConfig("b", a / ".", 1))
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


def test_startup_fails_when_a_name_stands_for_another_mount(
    tmp_path, fake_ceph, monkeypatch
):
    recorded = TreeKey("fsid", "cephfs", b"/x", 1)

    def mismatch(conn, name, key):
        raise TreeMismatch(name, recorded, key)

    # The real database() turns the DbError into the message; only the
    # connection and the queries are replaced.
    monkeypatch.setattr(dbcli, "connect", lambda dsn: contextlib.nullcontext(object()))
    monkeypatch.setattr(daemon, "check_schema", lambda conn: None)
    monkeypatch.setattr(daemon, "latest_started", mismatch)
    root = tmp_path / "a"
    root.mkdir()
    config = DaemonConfig(
        Path("t.toml"), Interval(HOUR, "1h"), "x", 0, (TreeConfig("projects", root, 1),)
    )
    with pytest.raises(FatalError) as raised:
        inspect_trees(config, OnceWarner(setup_logging(io.StringIO())))
    message = str(raised.value)
    assert message.startswith(
        f"tree 1 in t.toml ({root}): tree projects is recorded in the database as"
    )
    assert "fs name differs (recorded cephfs, found the default filesystem)" in message
    assert "the database was not changed" in message


def test_samples_are_taken_under_the_configured_names(tmp_path):
    fake = FakeTime(stop_after=1)
    names = []
    sampler = FakeSampler(
        fake, action=lambda opts, warn, progress: names.append(opts.tree)
    )
    run(
        write_config(tmp_path),
        stream=io.StringIO(),
        inspect=lambda config, warn: (None,) * len(ROOTS),
        sample=sampler,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert names[: len(ROOTS)] == [f"t{n}" for n in range(1, len(ROOTS) + 1)]


def test_help_lists_the_tree_name_key(capsys):
    with pytest.raises(SystemExit):
        main(["daemon", "--help"])
    text = capsys.readouterr().out
    assert 'name = "projects"' in text
    assert "checked against" in text


def test_daemon_help_describes_an_overrun_as_logged_not_fatal(capsys):
    with pytest.raises(SystemExit):
        main(["daemon", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "overrun" in text
    assert "the cycle finishes" in text
    assert "keeps running" in text
    for old in ("aborted", "exits once", "(including an overrun)"):
        assert old not in text
    assert "1 error, 2 usage error" in text
