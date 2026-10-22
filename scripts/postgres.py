#!/usr/bin/env python3
"""Manage a disposable PostgreSQL container for developing and testing trackdir.

Run with --help for usage. Standard library only, so that it runs without the
project's virtual environment.
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

PROG = Path(sys.argv[0]).name
CONTAINER = "trackdir-postgres"
VOLUME = "trackdir-pg-data"
DB_USER = "trackdir"
DB_PASSWORD = "trackdir"
DB_NAME = "trackdir"
TEST_DB_NAME = "trackdir_test"
DEFAULT_IMAGE = "docker.io/library/postgres:18"
DEFAULT_PORT = "5432"
DEFAULT_READY_TIMEOUT = "30"
# PostgreSQL 18+ images keep data in /var/lib/postgresql/<major>/docker.
DATA_MOUNT = "/var/lib/postgresql"
# Labels record how the container was made. They let the script refuse a
# container it did not create and never switch storage mode silently.
LABEL_MODE = "trackdir.mode"
LABEL_PORT = "trackdir.port"
READY_POLL_SECONDS = 1
PORT_PROBE_SECONDS = 1  # loopback answers at once; this only bounds a hang
WRAP_WIDTH = 79  # as in trackdir.messages


class Mode(StrEnum):
    """Where the container keeps its data; stored in label LABEL_MODE."""

    PERSISTENT = "persistent"  # in volume VOLUME
    EPHEMERAL = "ephemeral"  # in a tmpfs; podman removes the container on stop


class FatalError(Exception):
    """Stops the run; the message is printed as one ERROR: paragraph."""


def note(text: str) -> None:
    """Write TEXT to stderr as one wrapped paragraph."""
    wrapped = textwrap.fill(
        text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    sys.stderr.write(wrapped + "\n")


def warn(text: str) -> None:
    note(f"WARNING: {text}")


def podman(*args: str, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    """Run podman with ARGS and no stdin; return the result with stdout captured.

    podman's stderr goes to ours unless QUIET. Never raises on a non-zero exit;
    pass the result to check() for that.
    """
    return subprocess.run(
        ["podman", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if quiet else None,
        text=True,
        check=False,
    )


def check(result: subprocess.CompletedProcess[str]) -> str:
    """Return the stripped stdout of podman RESULT, or raise FatalError if it failed.

    podman has already said why on stderr; the error names its subcommand.
    """
    if result.returncode:
        raise FatalError(
            f"'podman {result.args[1]}' failed with exit status "
            f"{result.returncode}; see podman's message above."
        )
    return result.stdout.strip()


def container_state() -> str:
    """Return podman's status of the container (e.g. running, exited), or "" if
    there is no such container."""
    result = podman(
        "container", "inspect", "--format", "{{.State.Status}}", CONTAINER, quiet=True
    )
    return "" if result.returncode else result.stdout.strip()


def container_label(name: str, *, missing_ok: bool = False) -> str:
    """Return the value of label NAME of the container ("" if unset).

    Raises FatalError if podman fails, unless MISSING_OK: then returns "" quietly.
    """
    template = f'{{{{index .Config.Labels "{name}"}}}}'
    result = podman(
        "container", "inspect", "--format", template, CONTAINER, quiet=missing_ok
    )
    return "" if missing_ok and result.returncode else check(result)


def own_mode() -> str:
    """Return the storage mode of the existing container.

    Raises FatalError if this script did not create it, so that we never stop or
    delete someone else's container.
    """
    mode = container_label(LABEL_MODE)
    if not mode:
        raise FatalError(
            f"container {CONTAINER} exists but was not created by this script (it "
            f"has no {LABEL_MODE} label). Remove or rename it yourself."
        )
    return mode


def configured_port() -> str:
    """Return TRACKDIR_PG_PORT, or the default if it is unset.

    Only an unset variable gets the default, so an empty value fails validation
    instead of silently turning into the default. Raises FatalError unless the
    value is a port number in plain decimal.
    """
    port = os.environ.get("TRACKDIR_PG_PORT", DEFAULT_PORT)
    if not (re.fullmatch(r"[1-9][0-9]{0,4}", port) and int(port) <= 65535):
        raise FatalError(
            f"TRACKDIR_PG_PORT must be a port number from 1 to 65535, got {port!r}."
        )
    return port


def ready_timeout() -> int:
    """Return TRACKDIR_PG_READY_TIMEOUT (default 30) in seconds.

    Raises FatalError unless it is plain decimal digits; int() alone would also
    accept signs, spaces, underscores and non-ASCII digits.
    """
    text = os.environ.get("TRACKDIR_PG_READY_TIMEOUT", DEFAULT_READY_TIMEOUT)
    if not re.fullmatch(r"[0-9]{1,6}", text):
        raise FatalError(
            "TRACKDIR_PG_READY_TIMEOUT must be a number of seconds (up to 6 "
            f"digits), got {text!r}."
        )
    return int(text)


def port_in_use(port: str) -> bool:
    """Return whether something accepts connections on 127.0.0.1:PORT."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), PORT_PROBE_SECONDS):
            return True
    except OSError:
        return False


def require_port_free(port: str) -> None:
    if port_in_use(port):
        raise FatalError(
            f"port {port} is already in use. Stop what listens there, or choose "
            "another port with TRACKDIR_PG_PORT=<port>."
        )


def admin_psql(*args: str) -> str:
    """Run psql with ARGS as the superuser in the maintenance database; return
    its stripped stdout.

    The maintenance database is needed for DROP/CREATE DATABASE, which cannot
    target the database psql is connected to. Raises FatalError if psql fails.
    """
    return check(
        podman(
            *("exec", CONTAINER, "psql", "-U", DB_USER, "-d", "postgres"),
            *("-v", "ON_ERROR_STOP=1", *args),
        )
    )


def wait_ready(timeout: int) -> None:
    """Return once the server accepts TCP connections; raise FatalError if the
    container stops or TIMEOUT seconds pass first.

    A TCP check (not the unix socket) is needed: during first initialization
    the image runs a temporary server that listens on the socket only.
    """
    is_ready = ("pg_isready", "-q", "-h", "127.0.0.1", "-U", DB_USER, "-d", DB_NAME)
    deadline = time.monotonic() + timeout
    while podman("exec", CONTAINER, *is_ready).returncode:
        if container_state() != "running":
            raise FatalError(
                f"the container stopped while starting. Look at: podman logs "
                f"{CONTAINER} (ephemeral containers are removed when they exit)."
            )
        if time.monotonic() >= deadline:
            raise FatalError(
                f"PostgreSQL did not become ready within {timeout} s. Look at: "
                f"podman logs {CONTAINER}"
            )
        time.sleep(READY_POLL_SECONDS)


def ensure_test_db() -> None:
    """Create TEST_DB_NAME unless it exists; the image creates DB_NAME itself."""
    exists = admin_psql(
        "-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{TEST_DB_NAME}'"
    )
    if exists != "1":
        admin_psql("-c", f"CREATE DATABASE {TEST_DB_NAME}")


def create_container(mode: Mode, port: str) -> None:
    """Create and start the container in MODE, publishing PORT on 127.0.0.1.

    Raises FatalError, leaving no container behind, if podman cannot.
    """
    if mode is Mode.EPHEMERAL:
        storage = ["--rm", "--tmpfs", DATA_MOUNT]
    else:
        storage = ["-v", f"{VOLUME}:{DATA_MOUNT}"]
    # Only an unset or empty variable gets the default.
    image = os.environ.get("TRACKDIR_PG_IMAGE") or DEFAULT_IMAGE
    note(
        f"Creating {CONTAINER} from {image} in {mode} mode on 127.0.0.1:{port} "
        "(the first run pulls the image)."
    )
    result = podman(
        *("run", "-d", "--name", CONTAINER),
        *("--label", f"{LABEL_MODE}={mode}", "--label", f"{LABEL_PORT}={port}"),
        *("-p", f"127.0.0.1:{port}:5432"),
        *("-e", f"POSTGRES_USER={DB_USER}"),
        *("-e", f"POSTGRES_PASSWORD={DB_PASSWORD}"),
        *("-e", f"POSTGRES_DB={DB_NAME}"),
        *storage,
        image,
    )
    if result.returncode:
        # A failed run (e.g. the port cannot be bound) leaves a Created container
        # that keeps the bad port and would trap the next start; remove it.
        if container_label(LABEL_MODE, missing_ok=True) == mode:
            podman("rm", "-f", "-v", CONTAINER, quiet=True)
        raise FatalError(
            "podman could not create the container; see its message above."
        )


def dsn(database: str, port: str) -> str:
    return f"postgresql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{port}/{database}"


def require_running() -> None:
    """Raise FatalError unless our container is running."""
    if container_state() != "running":
        raise FatalError(f"{CONTAINER} is not running. Start it with: {PROG} start")
    own_mode()


def start(args: argparse.Namespace) -> int:
    mode = Mode.EPHEMERAL if args.ephemeral else Mode.PERSISTENT
    timeout = ready_timeout()
    if state := container_state():
        have_mode = own_mode()
        if have_mode != mode:
            raise FatalError(
                f"container {CONTAINER} exists in {have_mode} mode. Run '{PROG} "
                f"destroy' (this deletes its data) before starting it in {mode} mode."
            )
        port = container_label(LABEL_PORT)
        wanted = os.environ.get("TRACKDIR_PG_PORT", "")
        if wanted and wanted != port:
            warn(
                f"container {CONTAINER} already uses port {port}; ignoring "
                f"TRACKDIR_PG_PORT={wanted}. Run '{PROG} destroy' (this deletes "
                "its data) to change the port."
            )
        if state == "running":
            # An earlier start may have timed out and left it running.
            note(f"{CONTAINER} is already running ({mode} mode, port {port}).")
        else:
            require_port_free(port)
            check(podman("start", CONTAINER))
    else:
        port = configured_port()
        require_port_free(port)
        create_container(mode, port)
    wait_ready(timeout)
    ensure_test_db()
    note(
        f"PostgreSQL is ready on 127.0.0.1:{port} ({mode} mode). Connection "
        f"string: {PROG} url"
    )
    return 0


def stop(args: argparse.Namespace) -> int:
    if not container_state():
        note(f"{CONTAINER} does not exist; nothing to stop.")
        return 0
    mode = own_mode()
    check(podman("stop", CONTAINER))
    if mode == Mode.EPHEMERAL:
        note(f"Stopped {CONTAINER}; its ephemeral data is gone.")
    else:
        note(f"Stopped {CONTAINER}; data is kept in volume {VOLUME}.")
    return 0


def status(args: argparse.Namespace) -> int:
    state = container_state()
    if not state:
        print("state=absent")
        return 1
    mode = own_mode()
    port = container_label(LABEL_PORT)
    print(f"state={state}\nmode={mode}\nport={port}")
    return 0 if state == "running" else 1


def url(args: argparse.Namespace) -> int:
    if container_state():
        own_mode()
        port = container_label(LABEL_PORT)
    else:
        port = configured_port()
    print(dsn(TEST_DB_NAME if args.test else DB_NAME, port))
    return 0


def psql(args: argparse.Namespace) -> NoReturn:
    """Replace this process with psql in the container, so that stdin, signals
    and the exit status pass straight through."""
    require_running()
    # A tty only if both ends are one; otherwise it would mangle piped data.
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    command = [
        *("podman", "exec", "-it" if tty else "-i", CONTAINER),
        *("psql", "-U", DB_USER, "-d", DB_NAME, *args.psql_args),
    ]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(command[0], command)


def reset(args: argparse.Namespace) -> int:
    require_running()
    # Separate -c options run as separate commands; DROP/CREATE DATABASE cannot
    # run inside one transaction block.
    admin_psql(
        *("-c", f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"),
        *("-c", f"CREATE DATABASE {DB_NAME}"),
        *("-c", f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"),
        *("-c", f"CREATE DATABASE {TEST_DB_NAME}"),
    )
    note(f"Recreated databases {DB_NAME} and {TEST_DB_NAME}.")
    return 0


def destroy(args: argparse.Namespace) -> int:
    if container_state():
        own_mode()
        check(podman("rm", "-f", "-v", CONTAINER))
    # No -f: it would also remove any other container that mounts the volume.
    if (
        podman("volume", "exists", VOLUME).returncode == 0
        and podman("volume", "rm", VOLUME).returncode
    ):
        raise FatalError(
            f"could not remove volume {VOLUME}; another container may be using it "
            "(see podman's message above)."
        )
    note(f"Removed container {CONTAINER} and volume {VOLUME}.")
    return 0


DESCRIPTION = f"""\
Run a disposable PostgreSQL server in a rootless podman container, for
developing and testing trackdir. Data is kept in volume {VOLUME}, or in
memory with start --ephemeral. Nothing but that volume and the container
{CONTAINER} is touched."""

EPILOG = f"""\
environment:
  TRACKDIR_PG_PORT           port on 127.0.0.1, used when the container is
                             created (default {DEFAULT_PORT})
  TRACKDIR_PG_IMAGE          PostgreSQL 18+ image, used when the container is
                             created (default {DEFAULT_IMAGE})
  TRACKDIR_PG_READY_TIMEOUT  seconds start waits for readiness
                             (default {DEFAULT_READY_TIMEOUT})

User, password and database are all "{DB_USER}". The server listens on
127.0.0.1 only, but any local user can connect. Needs podman; the first
start pulls the image. Only status and url write to stdout; notes and errors
go to stderr. Exit status: 0 success, 1 error (for status: not running),
2 usage error. Run '%(prog)s COMMAND --help' for a command's options."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str,
        run: Callable[[argparse.Namespace], int],
        summary: str,
        **kwargs: bool,
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=summary, description=summary, **kwargs)
        command.set_defaults(run=run)
        return command

    command = add("start", start, "create or start the server and wait until ready")
    command.add_argument(
        "--ephemeral",
        action="store_true",
        help="keep data in memory only, so that stop discards it",
    )
    add("stop", stop, "stop the server (ephemeral data is discarded)")
    add("status", status, "print state=, mode= and port= lines; exit 0 if running")
    command = add("url", url, "print the connection string")
    command.add_argument(
        "--test",
        action="store_true",
        help=f"of database {TEST_DB_NAME}, which tests may wipe",
    )
    # main() routes "psql ..." past argparse; this entry only lists it in --help.
    summary = "run psql in the container with all arguments, even --help"
    command = add("psql", psql, summary, add_help=False)
    command.set_defaults(psql_args=[])
    add("reset", reset, f"drop and recreate databases {DB_NAME} and {TEST_DB_NAME}")
    add("destroy", destroy, "remove the container and the volume (deletes all data)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit status."""
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    if argv[:1] == ["psql"]:
        # Every argument belongs to psql, options included, so argparse must not
        # see them: parse_known_args would also hand psql unknown options of ours.
        args = argparse.Namespace(run=psql, psql_args=argv[1:])
    else:
        args = parser.parse_args(argv)
    try:
        if shutil.which("podman") is None:
            raise FatalError("podman is not installed or not in PATH.")
        return args.run(args)
    except FatalError as e:
        note(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        note("ERROR: interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
