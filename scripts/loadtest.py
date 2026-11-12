#!/usr/bin/env python3
"""Load a trackdir database with a synthetic tree, to see how samples scale.

Stores one tree of --dirs directories, then samples that change a few
directories each and, now and then, create a burst of directories that is
deleted a few samples later. Prints how long each sample took to load its
baseline and to store, next to the size of the database, so that a cost that
grows with stored history shows as a rising column.

Unlike scripts/postgres.py this imports trackdir, so run it with the project's
virtual environment. It creates the trackdir schema itself and refuses a
database that already has one: it never touches existing data. Nothing is
walked or replayed; the rows go straight to trackdir.db.store_sample, as
`trackdir sample` would store them.
"""

import argparse
import random
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from trackdir.baseline import Baseline, SampleDiff
from trackdir.capture import CaptureHeader
from trackdir.db import (
    DbError,
    SchemaMissing,
    check_schema,
    connect,
    init_schema,
    load_baseline,
    store_sample,
    tree_key_of,
)
from trackdir.model import ROOT, DirProbe

TREE = "loadtest"  # the name of the synthetic tree
FIRST_START = datetime(2030, 1, 1, tzinfo=UTC)  # samples are an hour apart
COLUMNS = (
    "sample",
    "live_dirs",
    "paths",
    "dir_state_rows",
    "baseline_s",
    "store_s",
    "bytes_on_disk",
)
_REPORT = """SELECT (SELECT count(*) FROM trackdir.current_dir_state),
       (SELECT count(*) FROM trackdir.path),
       (SELECT sum(new_dirs + modified_dirs + deleted_dirs) FROM trackdir.sample),
       (SELECT baseline_seconds FROM trackdir.sample WHERE id = %(sample)s),
       (SELECT store_seconds FROM trackdir.sample WHERE id = %(sample)s),
       (SELECT sum(pg_total_relation_size(oid))::bigint FROM pg_class
        WHERE relnamespace = 'trackdir'::regnamespace AND relkind = 'r')
"""


def fanout(dirs: int, depth: int) -> int:
    """Return the least fan-out that fits DIRS directories within DEPTH levels."""
    width = 1
    while sum(width**level for level in range(depth + 1)) < dirs:
        width += 1
    return width


def path_of(node: int, width: int) -> bytes:
    """Return the path of directory NODE.

    Directory 0 is ROOT; the parent of NODE is (NODE - 1) // WIDTH.
    """
    names = []
    while node:
        names.append(b"n%x" % node)
        node = (node - 1) // width
    return b"/".join(reversed(names)) or ROOT


def probe(path: bytes, sample: int, salt: int) -> DirProbe:
    """Return a readable directory at PATH whose rstats differ in every SAMPLE."""
    return DirProbe(
        path,
        ino=salt,
        mode=0o40755,
        uid=0,
        gid=0,
        nlink=2,
        rctime=sample,
        rbytes=sample * 1_000_000 + salt % 1_000_000,
        rfiles=1,
        rsubdirs=1,
    )


def burst_paths(sample: int, dirs: int) -> Iterator[bytes]:
    """Yield the directory of sample SAMPLE's burst, then its DIRS children."""
    parent = b"burst%d" % sample
    yield parent
    for child in range(dirs):
        yield parent + b"/%d" % child


def changes(sample: int, args: argparse.Namespace, rng: random.Random) -> SampleDiff:
    """Return what sample SAMPLE stores: the whole tree for sample 1.

    Later samples change args.changes random directories with their ancestors,
    create a burst every args.burst_every samples and delete the burst created
    args.burst_life samples earlier. The lists are not in tree order, and
    sample 1's is a generator, so that a large tree is never held in memory;
    store_sample only iterates them.
    """
    width = fanout(args.dirs, args.depth)
    if sample == 1:
        tree = (probe(path_of(n, width), sample, n) for n in range(args.dirs))
        return SampleDiff(changed=tree, deleted=[], skipped=0, skipped_pinned=0)  # type: ignore[arg-type]
    nodes = {0}
    for _ in range(args.changes):
        node = rng.randrange(args.dirs)
        while node not in nodes:
            nodes.add(node)
            node = (node - 1) // width
    changed = [probe(path_of(n, width), sample, n) for n in sorted(nodes)]
    deleted: list[bytes] = []
    if args.burst_every:
        if (sample - 1) % args.burst_every == 0:
            created = burst_paths(sample, args.burst_dirs)
            changed += [probe(path, sample, i) for i, path in enumerate(created)]
        born = sample - args.burst_life
        if born > 1 and (born - 1) % args.burst_every == 0:
            deleted = list(burst_paths(born, args.burst_dirs))
    return SampleDiff(changed=changed, deleted=deleted, skipped=0, skipped_pinned=0)


def header_of(sample: int, depth: int) -> CaptureHeader:
    """Return the header of sample SAMPLE of the synthetic tree."""
    started = (FIRST_START + timedelta(hours=sample)).isoformat()
    return CaptureHeader(
        root="/loadtest",
        max_depth=depth,
        cluster_fsid="loadtest",
        fs_name=None,
        fs_path="/loadtest",
        rstat_mode="dirstat",
        openat2=True,
        jobs=1,
        started=started,
        finished=started,
        euid=0,
        host=None,
        kernel="loadtest",
        trackdir_version="loadtest",
        dirs=0,
        errors=0,
        anonymized=False,
    )


def run(args: argparse.Namespace) -> None:
    """Store the samples and print the reports; raises DbError."""
    rng = random.Random(args.seed)
    with connect(args.db) as conn:
        try:
            check_schema(conn)
        except SchemaMissing:
            init_schema(conn)
        else:
            raise DbError(
                "the database already has a trackdir schema; the load test needs "
                "an empty database, so that it cannot touch existing data"
            )
        print(*COLUMNS)
        for sample in range(1, args.samples + 1):
            header = header_of(sample, args.depth)
            key = tree_key_of(header)
            clock = time.monotonic()
            baseline = (
                Baseline.empty() if sample == 1 else load_baseline(conn, TREE, key)
            )
            baseline_seconds = time.monotonic() - clock
            # Only the baseline's sample matters to the store; drop its rows
            # before building the sample's own.
            baseline = Baseline(
                baseline.sample_id, baseline.started, {}, frozenset(), {}
            )
            diff = changes(sample, args, rng)
            sample_id = store_sample(
                conn, TREE, header, key, baseline, diff, None, baseline_seconds
            )
            if sample in (1, args.samples) or sample % args.report_every == 0:
                live, paths, rows, loaded, stored, size = conn.execute(
                    _REPORT, {"sample": sample_id}
                ).fetchone()
                print(sample, live, paths, rows, f"{loaded:.2f}", f"{stored:.2f}", size)
                sys.stdout.flush()


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db", required=True, help="connection string of an empty database"
    )
    parser.add_argument(
        "--dirs", type=positive, required=True, help="directories of the tree"
    )
    parser.add_argument(
        "--samples",
        type=positive,
        required=True,
        help="samples to store, the first included",
    )
    parser.add_argument(
        "--changes",
        type=positive,
        default=1,
        help="directories changed per sample (default 1)",
    )
    parser.add_argument(
        "--burst-every",
        type=int,
        default=0,
        help="create a burst every N samples (default 0: never)",
    )
    parser.add_argument(
        "--burst-dirs",
        type=positive,
        default=1000,
        help="directories per burst (default 1000)",
    )
    parser.add_argument(
        "--burst-life",
        type=positive,
        default=1,
        help="samples until a burst is deleted (default 1)",
    )
    parser.add_argument(
        "--depth",
        type=positive,
        default=4,
        help="depth of the tree, at least 2 (default 4)",
    )
    parser.add_argument(
        "--report-every",
        type=positive,
        default=1,
        help="report every N samples (default 1)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="of the random choices (default 0)"
    )
    args = parser.parse_args(argv)
    if args.depth < 2 or args.burst_every < 0:
        parser.error("--depth must be at least 2 and --burst-every at least 0")
    try:
        run(args)
    except DbError as e:
        print(f"ERROR: {e}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
