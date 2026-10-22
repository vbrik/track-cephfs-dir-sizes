"""Command-line interface: trackdir capture, sample, db init and db summary."""

import argparse
import contextlib
import os
import signal
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from trackdir import __version__, dbcli, sample, summary
from trackdir.anonymize import AnonymizationError, anonymize, key_recipe, load_key
from trackdir.capture import CaptureHeader, CaptureWriter
from trackdir.live import (
    DEFAULT_JOBS,
    DEFAULT_MAX_DIRS,
    open_live_root,
    walk_live,
)
from trackdir.messages import FatalError, describe_errors, paragraph
from trackdir.model import MAX_DEPTH_LIMIT, MAX_DIRS_LIMIT, DirProbe, error_counts
from trackdir.progress import NO_PROGRESS, Progress
from trackdir.rstats import RStatMode

NO_CAPTURE = "no capture was written"  # what a failed capture leaves


def _exit_status(unchanged: str, success: str = "") -> str:
    """Return the epilog paragraph on exit statuses, shared by all commands.

    UNCHANGED is what every status but 0 leaves; SUCCESS, if any, qualifies 0.
    """
    qualifier = f" ({success})" if success else ""
    return paragraph(
        f"Exit status: 0 success{qualifier}, 1 error, 2 usage error, "
        "130 interrupted, 128+N on signal N (SIGTERM or SIGHUP). On a non-zero "
        f"status, {unchanged}."
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

{_exit_status(NO_CAPTURE, "per-directory errors included")}"""

SAMPLE_DESCRIPTION = f"""\
Store a sample of a tree in PostgreSQL: the directories under ROOT, down to
--max-depth, with owner, mode, nlink, CephFS rstats and per-directory errors.
A sample with nothing to compare against (a tree's first) stores every
directory. Later samples skip subtrees whose directory did not change and
store only changed directories and deletions. With --replay, read the
directories from a capture instead of CephFS.

Read-only on ROOT; writes to the database given by --db or {dbcli.DSN_ENV}.
The database schema must exist: run `trackdir db init` once.
"""

SAMPLE_EPILOG = f"""\
Run as root for a complete sample. As another user the sample is partial:
directories you cannot read are stored as EACCES errors, without rstats, and
later samples retry them.

A tree is a directory of a CephFS filesystem, identified by the cluster fsid,
the filesystem and the directory's path inside it, so it is the same tree
whichever host or mount point samples it. --max-depth is part of a tree's
identity too: a run with another depth is a different tree, and its first
sample stores every directory again. --max-dirs is not: a run over it stores
nothing, and a higher limit continues the same tree.

DSN is a libpq connection string or URI, e.g.
postgresql://trackdir@db.example.org/trackdir. Put the password in ~/.pgpass
or PGPASSWORD, never in --db: ps shows command lines to every local user.

{_exit_status(dbcli.NOTHING_STORED, "per-directory errors included")}"""

DB_INIT_DESCRIPTION = f"""\
Create trackdir's tables in the database given by --db or {dbcli.DSN_ENV}.
Changes the database; safe to repeat.
"""

DB_INIT_EPILOG = _exit_status(dbcli.DB_UNCHANGED, "also when the schema already exists")

DB_SUMMARY_DESCRIPTION = f"""\
Report what the database given by --db or {dbcli.DSN_ENV} holds: totals, then
for each tree its samples, its current directories and errors, ROOT's size and
its change since the first sample, the newest samples with what each stored,
and the directories, at any depth, with the most own growth since the first
sample and since the second-to-last. Read-only; the report goes to stdout.
"""

DB_SUMMARY_EPILOG = f"""\
A sample is initial if its tree had no current directories before it, else
incremental. A directory a sample stored is new if it had no current row
before the sample, else modified; deleted counts its tombstones. Telling them
apart reads every stored row of the trees reported.

Growth since the first sample needs 2 samples, since the second-to-last 3;
both windows end at the latest sample. A directory's growth is that of its
rbytes; its own growth is that minus the growth of its subdirectories: the
growth of the files directly in it. Own growths add up to ROOT's growth, so
none is counted twice; "of ROOT" is that share. A directory below max depth
is not stored, so its growth shows in its ancestor at max depth, whose own
growth is then its subtree's; so does that of a subdirectory of an unlisted
directory (an error other than rstats) or of one without rstats at either
end. Such a directory is marked (subtree): its growth is somewhere below it.
Growth spread over many subdirectories, each growing a little, makes none
of them rank, and does not rank their parent either.

A directory new in a window grows from 0 and is marked (new), unless its
nearest ancestor at the start was not listed, so it may have existed unseen;
then its growth is unknown. A directory with an unknown growth, or gone by
the end, is left out. --top 0 skips growth.

{_exit_status(dbcli.DB_UNCHANGED)}"""


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


@dataclass(frozen=True, slots=True)
class _Command:
    """A parsed command line, ready to run."""

    run: Callable[[], str]  # does the work; returns the message for stderr, or ""
    unchanged: str  # what an interrupted run leaves, e.g. "nothing was stored"


class _Terminated(BaseException):
    """A termination signal arrived; unwinding cleans up (temp file, transaction)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def run_capture(
    opts: CaptureOptions,
    warn: Callable[[str], None],
    *,
    progress: Progress = NO_PROGRESS,
) -> CaptureResult:
    """Capture opts.root into opts.output and return what happened.

    Calls WARN once per degraded mode, before walking. Shows the walk's counts,
    then the write, on PROGRESS. Raises FatalError; KeyboardInterrupt
    propagates. The output file exists only if this returns.
    """
    _check_output(opts.output, opts.root)
    key = _load_key(opts.key_file) if opts.key_file is not None else None
    with open_live_root(opts.root, warn) as live, _create_writer(opts.output) as writer:
        result = walk_live(
            live,
            max_depth=opts.max_depth,
            max_dirs=opts.max_dirs,
            jobs=opts.jobs,
            progress=progress,
        )
        header, probes = result.header, result.probes
        progress.phase(f"writing {opts.output}")
        if key is not None:
            header, probes = _anonymize(header, probes, key)
        _commit(writer, header, probes, opts.output)
    return CaptureResult(
        root=live.path,
        output=opts.output,
        max_depth=opts.max_depth,
        dirs=len(probes),
        errors=error_counts(probes),
        elapsed=result.elapsed,
        rstat_mode=live.rstat_mode,
        openat2=live.openat2,
        anonymized=key is not None,
    )


def render_summary(result: CaptureResult) -> str:
    """Return the success message for RESULT as one wrapped paragraph."""
    return paragraph(
        f"Captured {result.dirs} directories under {result.root} to depth "
        f"{result.max_depth} into {result.output} in {result.elapsed:.1f} s; "
        f"{describe_errors(result.errors)}."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the trackdir command line and return the exit status."""
    args = _parser().parse_args(argv)
    command = args.prepare(args)
    try:
        with _terminate_on_signals():
            message = command.run()
    except FatalError as e:
        sys.stderr.write(paragraph(f"ERROR: {e}"))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write(paragraph(f"ERROR: interrupted; {command.unchanged}."))
        return 130
    except _Terminated as e:
        name = signal.Signals(e.signum).name
        sys.stderr.write(paragraph(f"ERROR: {name} received; {command.unchanged}."))
        return 128 + e.signum
    sys.stderr.write(message)
    return 0


def _prepare_capture(args: argparse.Namespace) -> _Command:
    opts = CaptureOptions(
        root=args.root,
        max_depth=args.max_depth,
        output=args.output,
        max_dirs=args.max_dirs,
        jobs=_jobs(args),
        key_file=args.anonymize,
    )

    def run() -> str:
        with Progress(sys.stderr) as progress:
            result = run_capture(opts, _warner(progress), progress=progress)
        return render_summary(result)

    return _Command(run, NO_CAPTURE)


def _prepare_sample(args: argparse.Namespace) -> _Command:
    """Check the combination of options; exits with a usage error if it is wrong."""
    error = args.parser.error
    if args.root is not None and args.replay is not None:
        error("give ROOT or --replay CAPTURE, not both")
    if args.root is None and args.replay is None:
        error("give ROOT or --replay CAPTURE")
    if args.replay is not None and args.jobs is not None:
        error("--jobs applies to ROOT only; a replay uses one job")
    if args.root is not None and args.max_depth is None:
        error("--max-depth is required with ROOT")
    opts = sample.SampleOptions(
        dsn=_dsn(args),
        root=args.root,
        replay=args.replay,
        max_depth=args.max_depth,
        max_dirs=args.max_dirs,
        jobs=_jobs(args),
    )

    def run() -> str:
        with Progress(sys.stderr) as progress:
            result = sample.run_sample(opts, _warner(progress), progress=progress)
        return sample.render_sample_summary(result)

    return _Command(run, dbcli.NOTHING_STORED)


def _prepare_db_init(args: argparse.Namespace) -> _Command:
    dsn = _dsn(args)
    return _Command(
        lambda: sample.render_db_init(sample.run_db_init(dsn)),
        dbcli.DB_UNCHANGED,
    )


def _prepare_db_summary(args: argparse.Namespace) -> _Command:
    dsn = _dsn(args)
    render = summary.render_json if args.json else summary.render_text

    def run() -> str:
        stats = summary.run_summary(dsn, args.tree, args.samples, args.top)
        sys.stdout.write(render(stats))
        return ""

    return _Command(run, dbcli.DB_UNCHANGED)


def _jobs(args: argparse.Namespace) -> int:
    return DEFAULT_JOBS if args.jobs is None else args.jobs


def _dsn(args: argparse.Namespace) -> str:
    """Return --db, else $TRACKDIR_DB; exits with a usage error if neither is set."""
    dsn = args.db or os.environ.get(dbcli.DSN_ENV)
    if not dsn:
        args.parser.error(f"no database given: pass --db DSN or set {dbcli.DSN_ENV}")
    return dsn


@contextlib.contextmanager
def _terminate_on_signals() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into _Terminated while the body runs.

    Their default action kills the process without unwinding, which would
    leave capture's hidden temp file behind (timeout, systemd, a dropped ssh
    session).
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


def _create_writer(output: Path) -> CaptureWriter:
    try:
        return CaptureWriter(output)
    except OSError as e:
        raise FatalError(f"cannot create {output}: {e.strerror}.") from e


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


def _warner(progress: Progress) -> Callable[[str], None]:
    """Return a warn callback that erases PROGRESS's line before each warning."""

    def warn(message: str) -> None:
        progress.clear()
        sys.stderr.write(paragraph(f"WARNING: {message}"))

    return warn


def _int_between(minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    """Return an argparse type for integers >= MINIMUM and, if given, <= MAXIMUM."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}: {value}")
        if maximum is not None and value > maximum:
            raise argparse.ArgumentTypeError(f"must be at most {maximum}: {value}")
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

    capture = _add_command(
        commands,
        "capture",
        _prepare_capture,
        help="record directory state under ROOT for offline use",
        description=CAPTURE_DESCRIPTION,
        epilog=CAPTURE_EPILOG,
    )
    capture.add_argument("root", metavar="ROOT", type=Path, help="CephFS directory")
    _add_max_depth(capture, required=True)
    capture.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="FILE",
        help="capture file to create (.jsonl.gz)",
    )
    _add_max_dirs(capture)
    _add_jobs(capture)
    capture.add_argument(
        "--anonymize",
        type=Path,
        metavar="KEYFILE",
        help="replace names and ids with HMAC tokens keyed by KEYFILE",
    )

    sample_parser = _add_command(
        commands,
        "sample",
        _prepare_sample,
        help="store a sample of the tree at ROOT in PostgreSQL",
        description=SAMPLE_DESCRIPTION,
        epilog=SAMPLE_EPILOG,
    )
    sample_parser.add_argument(
        "root", metavar="ROOT", type=Path, nargs="?", help="CephFS directory"
    )
    sample_parser.add_argument(
        "--replay",
        type=Path,
        metavar="CAPTURE",
        help="read the directories from the capture file CAPTURE instead of ROOT",
    )
    _add_max_depth(
        sample_parser, required=False, note="; default with --replay: the capture's"
    )
    _add_max_dirs(sample_parser)
    _add_jobs(sample_parser)
    _add_db(sample_parser)

    db = commands.add_parser(
        "db",
        help="create or summarize trackdir's database",
        description="Create or summarize trackdir's database.",
    )
    db_commands = db.add_subparsers(dest="db_command", required=True, metavar="COMMAND")
    init = _add_command(
        db_commands,
        "init",
        _prepare_db_init,
        help="create trackdir's tables (changes the database)",
        description=DB_INIT_DESCRIPTION,
        epilog=DB_INIT_EPILOG,
    )
    _add_db(init)
    summarize = _add_command(
        db_commands,
        "summary",
        _prepare_db_summary,
        help="report trees, samples and sizes stored in the database",
        description=DB_SUMMARY_DESCRIPTION,
        epilog=DB_SUMMARY_EPILOG,
    )
    summarize.add_argument(
        "--tree", type=_int_between(1), metavar="ID", help="report tree ID only"
    )
    summarize.add_argument(
        "--samples",
        type=_int_between(0),
        default=10,
        metavar="N",
        help="list the N newest samples of each tree (default: %(default)s)",
    )
    summarize.add_argument(
        "--top",
        type=_int_between(0, MAX_DIRS_LIMIT),
        default=3,
        metavar="N",
        help="list the N directories with the most own growth (default: %(default)s)",
    )
    summarize.add_argument(
        "--json", action="store_true", help="print JSON instead of text"
    )
    _add_db(summarize)
    return parser


def _add_command(
    commands: argparse._SubParsersAction,
    name: str,
    prepare: Callable[[argparse.Namespace], _Command],
    **kwargs: str,
) -> argparse.ArgumentParser:
    """Add subcommand NAME; main() runs what PREPARE returns for its arguments."""
    command = commands.add_parser(
        name, formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs
    )
    command.set_defaults(prepare=prepare, parser=command)
    return command


def _add_max_depth(
    parser: argparse.ArgumentParser, *, required: bool, note: str = ""
) -> None:
    parser.add_argument(
        "--max-depth",
        type=_int_between(0, MAX_DEPTH_LIMIT),
        required=required,
        metavar="N",
        help=f"include directories at most N levels below ROOT (0: ROOT only){note}",
    )


def _add_max_dirs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-dirs",
        type=_int_between(1, MAX_DIRS_LIMIT),
        default=DEFAULT_MAX_DIRS,
        metavar="N",
        help="fail if there are more than N directories (default: %(default)s)",
    )


def _add_jobs(parser: argparse.ArgumentParser) -> None:
    # No argparse default, so a command can tell whether --jobs was given.
    parser.add_argument(
        "--jobs",
        type=_int_between(1),
        metavar="N",
        help=f"directories probed concurrently (default: {DEFAULT_JOBS})",
    )


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        metavar="DSN",
        help=f"PostgreSQL connection string (default: ${dbcli.DSN_ENV})",
    )
