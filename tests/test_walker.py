import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from conftest import Terminal, status_lines

from trackdir import walker as walker_module
from trackdir.model import ROOT, DirProbe, ProbeError, Stage, join
from trackdir.progress import Progress
from trackdir.walker import DirLimitExceeded, walk


class TreeBackend:
    """Backend over {path: [child names]}; JITTER shuffles completion order."""

    def __init__(self, tree, jitter=False):
        self.tree = tree
        self.jitter = jitter

    def probe(self, path, should_list):
        if self.jitter:
            time.sleep(random.random() / 1000)
        probe = DirProbe(path, nlink=2 + len(self.tree[path]))
        if not should_list(probe):
            return probe
        return replace(probe, children=tuple(self.tree[path]))


TREE = {
    ROOT: [b"b", b"a"],
    b"a": [b"y", b"x"],
    b"a/x": [b"deep"],
    b"a/x/deep": [],
    b"a/y": [],
    b"b": [],
}


def above(max_depth):
    return lambda probe: probe.depth < max_depth


def paths(probes):
    return [probe.path for probe in probes]


def wide_tree(fanout, depth):
    tree = {}

    def add(path, level):
        names = [b"%d" % i for i in range(fanout)] if level < depth else []
        tree[path] = names
        for name in names:
            add(join(path, name), level + 1)

    add(ROOT, 0)
    return tree


def test_returns_every_dir_in_tree_order():
    probes = walk(TreeBackend(TREE), above(10), max_dirs=100, jobs=4)
    assert paths(probes) == [ROOT, b"a", b"a/x", b"a/x/deep", b"a/y", b"b"]


def test_dirs_at_max_depth_are_probed_but_not_listed():
    probes = walk(TreeBackend(TREE), above(1), max_dirs=100, jobs=4)
    assert paths(probes) == [ROOT, b"a", b"b"]
    assert [probe.children is None for probe in probes] == [False, True, True]


def test_max_depth_zero_probes_root_only():
    assert paths(walk(TreeBackend(TREE), above(0), max_dirs=100, jobs=4)) == [ROOT]


def test_dir_limit_counts_root_and_allows_exactly_max_dirs():
    assert len(walk(TreeBackend(TREE), above(10), max_dirs=6, jobs=4)) == 6
    with pytest.raises(DirLimitExceeded) as exc:
        walk(TreeBackend(TREE), above(10), max_dirs=5, jobs=4)
    assert exc.value.limit == 5


def test_output_does_not_depend_on_jobs():
    tree = wide_tree(fanout=5, depth=3)  # 1 + 5 + 25 + 125 directories
    one = walk(TreeBackend(tree, jitter=True), above(10), max_dirs=1000, jobs=1)
    many = walk(TreeBackend(tree, jitter=True), above(10), max_dirs=1000, jobs=16)
    assert len(one) == 156
    assert one == many


def test_probe_exception_aborts_the_walk():
    class Broken(TreeBackend):
        def probe(self, path, should_list):
            if path == b"a/x":
                raise RuntimeError("bug")
            return super().probe(path, should_list)

    with pytest.raises(RuntimeError, match="bug"):
        walk(Broken(TREE), above(10), max_dirs=100, jobs=4)


def test_probes_in_flight_are_bounded(monkeypatch):
    # One Future per queued directory would cost ~2 KB each across a wide level.
    lock = threading.Lock()
    outstanding = most = 0

    class CountingPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            nonlocal outstanding, most
            with lock:
                outstanding += 1
                most = max(most, outstanding)
            future = super().submit(*args, **kwargs)
            future.add_done_callback(lambda _: release())
            return future

    def release():
        nonlocal outstanding
        with lock:
            outstanding -= 1

    monkeypatch.setattr(walker_module, "ThreadPoolExecutor", CountingPool)
    tree = wide_tree(fanout=200, depth=1)
    probes = walk(TreeBackend(tree), above(10), max_dirs=1000, jobs=2)
    assert len(probes) == 201
    assert most <= 2 * walker_module.IN_FLIGHT_PER_JOB


def test_progress_reports_the_start_and_every_probe():
    class Failing(TreeBackend):
        def probe(self, path, should_list):
            probe = super().probe(path, should_list)
            if path == b"b":
                return replace(probe, error=ProbeError(Stage.OPEN, "EACCES"))
            return probe

    stream = Terminal()
    walk(
        Failing(TREE),
        above(10),
        max_dirs=100,
        jobs=1,
        progress=Progress(stream, interval=0),
    )
    counts = [line.split(", ")[:3] for line in status_lines(stream.getvalue())]
    assert counts[0] == ["probed 0 dirs", "1 queued", "0 errors"]
    assert counts[-1] == ["probed 6 dirs", "0 queued", "1 errors"]
    assert len(counts) == 7


def test_progress_keeps_reporting_while_a_probe_stalls(monkeypatch):
    class Stalling(TreeBackend):
        def probe(self, path, should_list):
            time.sleep(0.3)
            return super().probe(path, should_list)

    monkeypatch.setattr(walker_module, "REPORT_INTERVAL", 0.01)
    stream = Terminal()
    walk(
        Stalling({ROOT: []}),
        above(10),
        max_dirs=100,
        jobs=1,
        progress=Progress(stream, interval=0),
    )
    assert (
        len(status_lines(stream.getvalue())) >= 5
    )  # 2 without reports during the stall
