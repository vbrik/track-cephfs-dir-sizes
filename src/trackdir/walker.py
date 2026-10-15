"""Concurrent directory traversal over any Backend.

Probes run on a thread pool: they spend their time in syscalls (MDS round
trips) that release the GIL. Only a few probes per thread are submitted at a
time; the rest of the frontier waits as bare paths, because a pending Future
costs about 2 KB. Completed probes reach the main thread through a queue, which
costs O(1) per directory; concurrent.futures.wait() would rescan all pending
futures each time.
"""

import queue
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor

from trackdir.model import ROOT, Backend, DirProbe, ShouldList, join, tree_key

# Probes submitted per worker thread: enough to keep every worker busy.
IN_FLIGHT_PER_JOB = 4


class DirLimitExceeded(Exception):
    """The walk would probe more directories than allowed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit} directories")
        self.limit = limit


def walk(
    backend: Backend, should_list: ShouldList, *, max_dirs: int, jobs: int
) -> list[DirProbe]:
    """Probe ROOT and, recursively, the subdirectories of every listed directory.

    Returns the probes in tree order, the same for any JOBS. Raises
    DirLimitExceeded as soon as more than MAX_DIRS directories (ROOT included)
    are found; exceptions from backend.probe propagate. On any exception,
    including KeyboardInterrupt, pending probes are cancelled and running ones
    are awaited.
    """
    done: queue.SimpleQueue[Future[DirProbe]] = queue.SimpleQueue()
    frontier: deque[bytes] = deque([ROOT])
    found = 1  # directories queued so far, ROOT included
    probes: list[DirProbe] = []
    in_flight = 0
    max_in_flight = jobs * IN_FLIGHT_PER_JOB
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="probe") as pool:
        try:
            while frontier or in_flight:
                while frontier and in_flight < max_in_flight:
                    future = pool.submit(backend.probe, frontier.popleft(), should_list)
                    future.add_done_callback(done.put)
                    in_flight += 1
                probe = done.get().result()
                in_flight -= 1
                probes.append(probe)
                for name in probe.children or ():
                    found += 1
                    if found > max_dirs:
                        raise DirLimitExceeded(max_dirs)
                    frontier.append(join(probe.path, name))
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    probes.sort(key=lambda probe: tree_key(probe.path))
    return probes
