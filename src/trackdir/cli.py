"""Command-line interface: trackdir capture."""

import argparse
import contextlib
import functools
import os
import signal
import socket
import stat
import sys
import textwrap
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trackdir import __version__
from trackdir.anonymize import AnonymizationError, anonymize, key_recipe, load_key
from trackdir.capture import CaptureHeader, CaptureWriter
from trackdir.model import ROOT, DirProbe, ShouldList
from trackdir.probe import LiveBackend
from trackdir.rstats import (
    NotCephFSError,
    RStatMode,
    cluster_fsid,
    detect_mode,
    read_rstats,
)
from trackdir.safeopen import SafeRoot, openat2_supported
from trackdir.walker import DirLimitExceeded, walk

# Probes stay in memory until the end: ~0.6 KB each (measured), so ~0.6 GB.
DEFAULT_MAX_DIRS = 1_000_000
DEFAULT_JOBS = 16  # concurrent probes; each mostly waits on MDS round trips
WRAP_WIDTH = 79

MSG_NO_OPENAT2_AS_ROOT = (
    "this kernel does not provide openat2 (Linux 5.6+), which trackdir needs "
    "to walk user-writable trees safely as root. Run it on a newer kernel."
)
MSG_NO_OPENAT2 = (
    "this kernel does not provide openat2 (Linux 5.6+), so directories are "
    "opened one path component at a time. Symlinks are still never followed, "
    "but mount points are detected only after they are opened."
)
MSG_NO_DIRSTAT = (
    "ROOT's mount lacks the dirstat option, so each directory costs four MDS "
    "round trips instead of one. Mount with -o dirstat (a separate mount for "
    "trackdir is enough) to cut the MDS load."
)

CAPTURE_DESCRIPTION = """\
Record every directory under ROOT, down to --max-depth, into one gzipped JSON
Lines file: owner, mode, nlink, CephFS rstats (rbytes, rfiles, rsubdirs,
rctime) and per-directory errors. Use captures to develop, debug and test
trackdir offline with data from a real filesystem.

Read-only on ROOT. FILE appears only if the capture succeeds and is never
overwritten.
"""

CAPTURE_EPILOG = f"""\
Run as root for a complete capture. As another user, directories you cannot
read are recorded as EACCES errors, without rstats.

--anonymize makes a capture safe to publish. Create the key once and keep it
out of the repository; captures taken with the same key line up:
  {key_recipe("trackdir.key")}

Exit status: 0 success (per-directory errors included), 1 error, 2 usage
error, 130 interrupted.
"""


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    """What to capture and where; see `trackdir capture --help`."""

    root: Path
    max_depth: int
    output: Path
    max_dirs: int = DEFAULT_MAX_DIRS
    jobs: int = DEFAULT_JOBS
    key_file: Path | None = None


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Outcome of a successful capture."""

    root: str
    output: Path
    max_depth: int
    dirs: int
    errors: Counter[tuple[str, str]]  # (stage, code) -> number of directories
    elapsed: float  # seconds spent walking
    rstat_mode: RStatMode
    openat2: bool
    anonymized: bool


class FatalError(Exception):
    """Stops the run; the message is printed as one ERROR: paragraph."""


class _Terminated(BaseException):
    """A termination signal arrived; unwinding removes the temp file."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def run_capture(opts: CaptureOptions, warn: Callable[[str], None]) -> CaptureResult:
    """Capture opts.root into opts.output and return what happened.

    Calls WARN once per degraded mode, before walking. Raises FatalError;
    KeyboardInterrupt propagates. The output file exists only if this returns.
    """
    _check_output(opts.output, opts.root)
    key = _load_key(opts.key_file) if opts.key_file is not None else None
    use_openat2 = openat2_supported()
    if not use_openat2:
        if os.geteuid() == 0:
            raise FatalError(MSG_NO_OPENAT2_AS_ROOT)
        warn(MSG_NO_OPENAT2)
    with _open_root(opts.root, use_openat2) as root:
        root_path = root.resolved_path()
        mode, fsid = _inspect_root(root, opts.root)
        if mode is RStatMode.GETXATTR:
            warn(MSG_NO_DIRSTAT)
        backend = LiveBackend(root, functools.partial(read_rstats, mode=mode))
        with _create_writer(opts.output) as writer:
            started = datetime.now(UTC)
            clock = time.monotonic()
            probes = _walk(backend, opts)
            elapsed = time.monotonic() - clock
            header = CaptureHeader(
                root=root_path,
                max_depth=opts.max_depth,
                cluster_fsid=fsid,
                rstat_mode=mode.value,
                openat2=use_openat2,
                jobs=opts.jobs,
                started=started.isoformat(),
                finished=datetime.now(UTC).isoformat(),
                euid=os.geteuid(),
                host=socket.gethostname(),
                kernel=os.uname().release,
                trackdir_version=__version__,
                dirs=len(probes),
                errors=sum(p.error is not None for p in probes),
                anonymized=False,
            )
            if key is not None:
                header, probes = _anonymize(header, probes, key)
            _commit(writer, header, probes, opts.output)
    return CaptureResult(
        root=root_path,
        output=opts.output,
        max_depth=opts.max_depth,
        dirs=len(probes),
        errors=Counter((str(p.error.stage), p.error.code) for p in probes if p.error),
        elapsed=elapsed,
        rstat_mode=mode,
        openat2=use_openat2,
        anonymized=key is not None,
    )


def render_summary(result: CaptureResult) -> str:
    """Return the success message for RESULT as one wrapped paragraph."""
    if result.errors:
        counts = ", ".join(
            f"{stage} {code} {n}" for (stage, code), n in sorted(result.errors.items())
        )
        outcome = f"{result.errors.total()} had errors: {counts}"
    else:
        outcome = "none had errors"
    return _paragraph(
        f"Captured {result.dirs} directories under {result.root} to depth "
        f"{result.max_depth} into {result.output} in {result.elapsed:.1f} s; "
        f"{outcome}."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the trackdir command line and return the exit status."""
    args = _parser().parse_args(argv)
    opts = CaptureOptions(
        root=args.root,
        max_depth=args.max_depth,
        output=args.output,
        max_dirs=args.max_dirs,
        jobs=args.jobs,
        key_file=args.anonymize,
    )
    try:
        with _terminate_on_signals():
            result = run_capture(opts, _warn)
    except FatalError as e:
        sys.stderr.write(_paragraph(f"ERROR: {e}"))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write(_paragraph("ERROR: interrupted; no capture was written."))
        return 130
    except _Terminated as e:
        name = signal.Signals(e.signum).name
        sys.stderr.write(_paragraph(f"ERROR: {name} received; no capture was written."))
        return 128 + e.signum
    sys.stderr.write(render_summary(result))
    return 0


@contextlib.contextmanager
def _terminate_on_signals() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into _Terminated while the body runs.

    Their default action kills the process without unwinding, which would
    leave the hidden temp file behind (timeout, systemd, a dropped ssh session).
    """

    def handler(signum: int, frame: object) -> None:
        raise _Terminated(signum)

    signums = (signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.signal(signum, handler) for signum in signums}
    try:
        yield
    finally:
        for signum, action in previous.items():
            signal.signal(signum, action)


def _list_above(max_depth: int) -> ShouldList:
    """List directories above MAX_DEPTH, so the walk stops there."""
    return lambda probe: probe.depth < max_depth


def _check_output(output: Path, root: Path) -> None:
    """Raise FatalError unless OUTPUT is new, its directory exists, and it is
    outside ROOT. Uses explicit stat calls, so permission problems are reported
    as such rather than as a missing file."""
    try:
        os.lstat(output)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as e:
        raise FatalError(f"cannot use the output {output}: {e.strerror}.") from e
    else:
        raise FatalError(
            f"{output} already exists; trackdir never overwrites a capture. "
            "Choose another name."
        )
    try:
        parent = os.stat(output.parent)
    except FileNotFoundError:
        raise FatalError(
            f"the output directory {output.parent} does not exist."
        ) from None
    except OSError as e:
        raise FatalError(
            f"cannot use the output directory {output.parent}: {e.strerror}."
        ) from e
    if not stat.S_ISDIR(parent.st_mode):
        raise FatalError(f"the output directory {output.parent} is not a directory.")
    if output.parent.resolve().is_relative_to(root.resolve()):
        raise FatalError(
            f"the output {output} is inside ROOT {root}; writing there would "
            "change the tree being captured. Write it elsewhere."
        )


def _load_key(path: Path) -> bytes:
    try:
        return load_key(path)
    except OSError as e:
        raise FatalError(f"cannot read the key file {path}: {e.strerror}.") from e
    except ValueError as e:
        raise FatalError(f"{e}.") from e


def _open_root(path: Path, use_openat2: bool) -> SafeRoot:
    try:
        return SafeRoot(path, use_openat2=use_openat2)
    except OSError as e:
        raise FatalError(
            f"cannot open ROOT {path}: {e.strerror}. ROOT must be a directory, "
            "not a symlink."
        ) from e


def _inspect_root(root: SafeRoot, path: Path) -> tuple[RStatMode, str | None]:
    """Return the rstat mode and cluster fsid of ROOT's filesystem."""
    try:
        fd = root.open(ROOT, os.O_RDONLY)
        try:
            return detect_mode(fd), cluster_fsid(fd)
        finally:
            os.close(fd)
    except NotCephFSError:
        raise FatalError(
            f"ROOT {path} is not on CephFS (it has no ceph.dir.rctime)."
        ) from None
    except OSError as e:
        raise FatalError(f"cannot read ROOT {path}: {e.strerror}.") from e


def _create_writer(output: Path) -> CaptureWriter:
    try:
        return CaptureWriter(output)
    except OSError as e:
        raise FatalError(f"cannot create {output}: {e.strerror}.") from e


def _walk(backend: LiveBackend, opts: CaptureOptions) -> list[DirProbe]:
    try:
        return walk(
            backend,
            _list_above(opts.max_depth),
            max_dirs=opts.max_dirs,
            jobs=opts.jobs,
        )
    except DirLimitExceeded as e:
        raise FatalError(
            f"ROOT has more than {e.limit} directories within depth "
            f"{opts.max_depth}. Raise --max-dirs if memory allows, or lower "
            "--max-depth."
        ) from e


def _anonymize(
    header: CaptureHeader, probes: list[DirProbe], key: bytes
) -> tuple[CaptureHeader, list[DirProbe]]:
    try:
        return anonymize(header, probes, key)
    except AnonymizationError as e:
        raise FatalError(f"{e}.") from e


def _commit(
    writer: CaptureWriter, header: CaptureHeader, probes: list[DirProbe], output: Path
) -> None:
    try:
        writer.commit(header, probes)
    except OSError as e:
        raise FatalError(f"cannot write {output}: {e.strerror}.") from e


def _paragraph(text: str) -> str:
    wrapped = textwrap.fill(
        text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    return wrapped + "\n"


def _warn(message: str) -> None:
    sys.stderr.write(_paragraph(f"WARNING: {message}"))


def _int_at_least(minimum: int) -> Callable[[str], int]:
    """Return an argparse type for integers >= MINIMUM."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}: {value}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trackdir", description="Track CephFS directory sizes over time."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    capture = commands.add_parser(
        "capture",
        help="record directory state under ROOT for offline use",
        description=CAPTURE_DESCRIPTION,
        epilog=CAPTURE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    capture.add_argument("root", metavar="ROOT", type=Path, help="CephFS directory")
    capture.add_argument(
        "--max-depth",
        type=_int_at_least(0),
        required=True,
        metavar="N",
        help="record directories at most N levels below ROOT (0: ROOT only)",
    )
    capture.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="FILE",
        help="capture file to create (.jsonl.gz)",
    )
    capture.add_argument(
        "--max-dirs",
        type=_int_at_least(1),
        default=DEFAULT_MAX_DIRS,
        metavar="N",
        help="fail if there are more than N directories (default: %(default)s)",
    )
    capture.add_argument(
        "--jobs",
        type=_int_at_least(1),
        default=DEFAULT_JOBS,
        metavar="N",
        help="directories probed concurrently (default: %(default)s)",
    )
    capture.add_argument(
        "--anonymize",
        type=Path,
        metavar="KEYFILE",
        help="replace names and ids with HMAC tokens keyed by KEYFILE",
    )
    return parser
