"""trackdir daemon: sample the trees of a config file once per interval.

Logs are not expected to be watched, so the exit status is the alarm: any
error that is not routine (a FatalError, an overrun, a bug) is logged and
ends the daemon with status 1, leaving a failed unit for the service manager
to show. Routine errors are per-directory errors, which every sample stores
and counts. A stop by signal exits 0, so systemd counts it as clean.

Cycles are timed with the monotonic clock, start to start. The wall clock
only turns the trees' latest sample times from the database into the delay
before the first cycle, so a restart never causes an extra sample, and into
the times shown in the log.
"""

import logging
import signal
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, TextIO

from trackdir import __version__
from trackdir.config import DaemonConfig, Interval, TreeConfig, load
from trackdir.db import TreeKey, check_schema, latest_started
from trackdir.dbcli import DB_UNCHANGED, database, describe_tree
from trackdir.interrupts import Overrun, Terminated, deadline, terminate_on_signals
from trackdir.live import open_live_root
from trackdir.messages import FatalError
from trackdir.sample import SampleOptions, SampleResult, describe_sample, run_sample

LOGGER_NAME = "trackdir"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # with time.gmtime: UTC
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
# Shortest deadline armed for a cycle; setitimer(0) would mean "never".
_MIN_DEADLINE = 1e-3

type Sampler = Callable[[SampleOptions, Callable[[str], None]], SampleResult]
type Inspector = Callable[
    [DaemonConfig, Callable[[str], None]], tuple[datetime | None, ...]
]


class _StoppedWhileSampling(Terminated):
    """A stop signal arrived while ROOT was being sampled."""

    def __init__(self, signum: int, root: Path) -> None:
        super().__init__(signum)
        self.root = root


class OnceWarner:
    """A warn callback that logs each distinct warning once per daemon run.

    Degraded modes (no dirstat, no openat2) would otherwise repeat for every
    tree in every cycle.
    """

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._seen: set[str] = set()

    def __call__(self, message: str) -> None:
        if message not in self._seen:
            self._seen.add(message)
            self._log.warning("%s", message)


class _LogHandler(logging.StreamHandler):
    """The daemon's handler, told apart from handlers others installed."""


def setup_logging(stream: TextIO) -> logging.Logger:
    """Return the daemon's logger, writing one line per record to STREAM.

    The handler sits on the root logger at level INFO, so libraries' records
    (psycopg's warnings, say) share the stream and format instead of reaching
    stderr through logging.lastResort. Replaces the handler of an earlier
    call. Unencodable characters (path surrogates) are escaped rather than
    raising.
    """
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="backslashreplace")
    formatter = logging.Formatter(LOG_FORMAT, LOG_TIME_FORMAT)
    formatter.converter = time.gmtime
    handler = _LogHandler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for old in [h for h in root.handlers if isinstance(h, _LogHandler)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return logging.getLogger(LOGGER_NAME)


def first_start(
    latest: Sequence[datetime | None], interval: Interval, now: datetime
) -> float:
    """Return the seconds from NOW until the first cycle.

    LATEST holds each tree's latest sample start, None for a tree without
    samples. The first cycle is due one interval after the oldest of them,
    at once if a tree has no sample or that time is past.
    """
    if any(started is None for started in latest):
        return 0.0
    due = min(latest) + timedelta(seconds=interval.seconds)
    return max(0.0, (due - now).total_seconds())


def inspect_trees(
    config: DaemonConfig, warn: Callable[[str], None]
) -> tuple[datetime | None, ...]:
    """Check that every tree can be sampled; return their latest sample starts.

    Fails, with FatalError, on a missing or mismatched schema, a ROOT that
    cannot be opened or is not CephFS, and two entries naming the same tree:
    they would collide on every cycle.
    """
    with database(config.dsn, DB_UNCHANGED) as conn:
        check_schema(conn)
        keys: list[TreeKey] = []
        for tree in config.trees:
            with open_live_root(tree.root, warn) as live:
                keys.append(
                    TreeKey.of(
                        live.cluster_fsid, live.fs_name, live.fs_path, tree.max_depth
                    )
                )
        _check_distinct(config, keys)
        return tuple(latest_started(conn, key) for key in keys)


def run_cycles(
    config: DaemonConfig,
    first_delay: float,
    log: logging.Logger,
    warn: Callable[[str], None],
    *,
    sample: Sampler = run_sample,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> NoReturn:
    """Sample every tree once per interval, starting after FIRST_DELAY seconds.

    Returns only by exception: FatalError for a failed sample or an overrun
    (a cycle not finished one interval after its start), Terminated from a
    stop signal.
    """
    scheduled = clock() + first_delay
    while True:
        sleep(max(0.0, scheduled - clock()))
        start = max(scheduled, clock())
        scheduled = start + config.interval.seconds
        _cycle(config, log, warn, sample, max(scheduled - clock(), _MIN_DEADLINE))
        log.info(
            "cycle finished in %.1f s; next cycle at %s",
            clock() - start,
            _wall_time(scheduled - clock()),
        )


def run(
    config_path: Path,
    *,
    stream: TextIO | None = None,
    inspect: Inspector = inspect_trees,
    sample: Sampler = run_sample,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the daemon with the config file CONFIG_PATH; return the exit status.

    Logs to STREAM (default stdout). 0: stopped by SIGTERM, SIGHUP or SIGINT;
    1: an error. The other arguments replace the filesystem, database and
    clock in tests.
    """
    log = setup_logging(sys.stdout if stream is None else stream)
    try:
        with terminate_on_signals(STOP_SIGNALS):
            config = load(config_path)
            logging.getLogger().setLevel(config.log_level)
            warn = OnceWarner(log)
            latest = inspect(config, warn)
            trees = len(config.trees)
            log.info(
                "trackdir %s daemon started: config %s, %d %s, interval %s",
                __version__,
                config.path,
                trees,
                "tree" if trees == 1 else "trees",
                config.interval.text,
            )
            delay = first_start(latest, config.interval, datetime.now(UTC))
            log.info("next cycle at %s", _wall_time(delay))
            run_cycles(
                config, delay, log, warn, sample=sample, clock=clock, sleep=sleep
            )
    except FatalError as e:
        log.error("%s", e)
        return 1
    except _StoppedWhileSampling as e:
        log.info(
            "%s received while sampling %s; nothing was stored for it. Stopped.",
            signal.Signals(e.signum).name,
            e.root,
        )
        return 0
    except Terminated as e:
        log.info("%s received. Stopped.", signal.Signals(e.signum).name)
        return 0
    except Exception:
        log.exception("unexpected error")
        return 1


def _cycle(
    config: DaemonConfig,
    log: logging.Logger,
    warn: Callable[[str], None],
    sample: Sampler,
    seconds: float,
) -> None:
    """Sample every tree in config order; raise Overrun as FatalError after SECONDS."""
    current: TreeConfig | None = None  # the tree being sampled
    try:
        with deadline(seconds):
            for tree in config.trees:
                current = tree
                result = _sample_tree(config, tree, log, warn, sample)
                current = None
                log.info("%s", describe_sample(result))
    except Overrun:
        aborted = (
            ""
            if current is None
            else f"; sample of {current.root} aborted, nothing was stored for it"
        )
        raise FatalError(
            f"cycle did not finish within the interval {config.interval.text}"
            f"{aborted}. Raise interval or sample fewer or smaller trees."
        ) from None
    except Terminated as e:
        if current is None:
            raise
        raise _StoppedWhileSampling(e.signum, current.root) from None


def _sample_tree(
    config: DaemonConfig,
    tree: TreeConfig,
    log: logging.Logger,
    warn: Callable[[str], None],
    sample: Sampler,
) -> SampleResult:
    """Sample TREE; a FatalError gains the tree's ROOT."""
    opts = SampleOptions(
        dsn=config.dsn,
        root=tree.root,
        max_depth=tree.max_depth,
        max_dirs=tree.max_dirs,
        jobs=tree.jobs,
    )
    log.debug("sampling %s", tree.root)
    clock = time.monotonic()
    try:
        result = sample(opts, warn)
    except FatalError as e:
        raise FatalError(f"sample of {tree.root} failed: {e}") from e
    log.debug("sampled %s in %.1f s", tree.root, time.monotonic() - clock)
    return result


def _check_distinct(config: DaemonConfig, keys: Sequence[TreeKey]) -> None:
    """Raise FatalError if two trees of CONFIG have the same key in KEYS."""
    first: dict[TreeKey, int] = {}
    for j, key in enumerate(keys):
        i = first.setdefault(key, j)
        if i != j:
            raise FatalError(
                f"trees {i + 1} and {j + 1} in {config.path} "
                f"({config.trees[i].root}, {config.trees[j].root}) are the same "
                f"{describe_tree(key)}; remove one."
            )


def _wall_time(delay: float) -> str:
    """Return the UTC wall-clock time DELAY seconds from now, as logged."""
    moment = datetime.now(UTC) + timedelta(seconds=delay)
    return moment.strftime(LOG_TIME_FORMAT)
