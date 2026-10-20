"""Concurrent directory traversal over any Backend.

Probes run on a thread pool: they spend their time in syscalls (MDS round
trips) that release the GIL. Only a few probes per thread are submitted at a
time; the rest of the frontier waits as bare paths, because a pending Future
costs about 2 KB. Completed probes reach the main thread through a queue, which
costs O(1) per directory; concurrent.futures.wait() would rescan all pending
futures each time. Waiting on the queue times out every REPORT_INTERVAL, so
progress is reported even while every probe in flight is stuck on the MDS.
"""

import queue
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor

from trackdir.model import ROOT, Backend, DirProbe, ShouldList, join, tree_key
from trackdir.progress import NO_PROGRESS, Progress

# Probes submitted per worker thread: enough to keep every worker busy.
IN_FLIGHT_PER_JOB = 4
# Seconds between progress reports while no probe completes.
REPORT_INTERVAL = 1.0


class DirLimitExceeded(Exception):
    """The walk would probe more directories than allowed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit} directories")
        self.limit = limit


def walk(
    backend: Backend,
    should_list: ShouldList,
    *,
    max_dirs: int,
    jobs: int,
    progress: Progress = NO_PROGRESS,
) -> list[DirProbe]:
    """Probe ROOT and, recursively, the subdirectories of every listed directory.

    Returns the probes in tree order, the same for any JOBS. Reports to
    PROGRESS at the start, after every probe, and every REPORT_INTERVAL
    while none completes. Raises DirLimitExceeded as soon as more than
    MAX_DIRS directories (ROOT included) are found; exceptions from
    backend.probe propagate. On any exception, including KeyboardInterrupt,
    pending probes are cancelled and running ones are awaited.
    """
    done: queue.SimpleQueue[Future[DirProbe]] = queue.SimpleQueue()
    frontier: deque[bytes] = deque([ROOT])
    found = 1  # directories queued so far, ROOT included
    probes: list[DirProbe] = []
    errors = 0  # probes with an error
    in_flight = 0
    max_in_flight = jobs * IN_FLIGHT_PER_JOB

    def report() -> None:
        progress.walk(probed=len(probes), queued=found - len(probes), errors=errors)

    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="probe") as pool:
        try:
            report()
            while frontier or in_flight:
                while frontier and in_flight < max_in_flight:
                    future = pool.submit(backend.probe, frontier.popleft(), should_list)
                    future.add_done_callback(done.put)
                    in_flight += 1
                try:
                    future = done.get(timeout=REPORT_INTERVAL)
                except queue.Empty:
                    report()
                    continue
                probe = future.result()
                in_flight -= 1
                probes.append(probe)
                errors += probe.error is not None
                for name in probe.children or ():
                    found += 1
                    if found > max_dirs:
                        raise DirLimitExceeded(max_dirs)
                    frontier.append(join(probe.path, name))
                report()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    probes.sort(key=lambda probe: tree_key(probe.path))
    return probes
