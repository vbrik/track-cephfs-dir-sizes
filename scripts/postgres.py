#!/usr/bin/env python3
"""Manage disposable PostgreSQL containers for developing and testing trackdir.

Run with --help for usage. Standard library only, so that it runs without the
project's virtual environment.
"""

import argparse
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

PROG = Path(sys.argv[0]).name
DEFAULT_CONTAINER = "trackdir-postgres"
VOLUME_SUFFIX = "-data"  # container NAME keeps its data in volume NAME-data
DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_PORT = "5432"
DB_USER = "trackdir"
DEFAULT_PASSWORD = "trackdir"
DB_NAME = "trackdir"
TEST_DB_NAME = "trackdir_test"
DEFAULT_IMAGE = "docker.io/library/postgres:18"
DEFAULT_READY_TIMEOUT = "30"
# PostgreSQL 18+ images keep data in /var/lib/postgresql/<major>/docker.
DATA_MOUNT = "/var/lib/postgresql"
# Labels record how the container was made. They let the script refuse a
# container it did not create, never switch storage mode silently, and act on
# an existing container with the settings it was created with.
LABEL_MODE = "trackdir.mode"
LABEL_ADDRESS = "trackdir.address"
LABEL_PORT = "trackdir.port"
LABEL_VOLUME = "trackdir.volume"  # persistent mode only
# One inspect call reads all we need: state, labels, then the environment
# (which holds the password), one item per line.
INSPECT_FORMAT = "\n".join(
    [
        "{{.State.Status}}",
        *(
            f'{{{{index .Config.Labels "{label}"}}}}'
            for label in (LABEL_MODE, LABEL_ADDRESS, LABEL_PORT, LABEL_VOLUME)
        ),
        "{{range .Config.Env}}{{println .}}{{end}}",
    ]
)
NAME_PATTERN = r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"  # podman's rule for names
READY_POLL_SECONDS = 1
PORT_PROBE_SECONDS = 1  # loopback answers at once; this only bounds a hang
WRAP_WIDTH = 79  # as in trackdir.messages


class Mode(StrEnum):
    """Where the container keeps its data; stored in label LABEL_MODE."""

    PERSISTENT = "persistent"  # in a volume, named in label LABEL_VOLUME
    EPHEMERAL = "ephemeral"  # in a tmpfs; podman removes the container on stop


class FatalError(Exception):
    """Stops the run; the message is printed as one ERROR: paragraph."""


def is_name(text: str) -> bool:
    return re.fullmatch(NAME_PATTERN, text) is not None


def is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def is_password(text: str) -> bool:
    # A newline would also break reading the password back (INSPECT_FORMAT).
    return bool(text) and "\n" not in text


def is_port(text: str) -> bool:
    # Plain decimal only: int() alone would also accept spaces, signs and
    # non-ASCII digits.
    return bool(re.fullmatch(r"[1-9][0-9]{0,4}", text)) and int(text) <= 65535


@dataclass(frozen=True)
class Option:
    """A setting given as option --FLAG before COMMAND, else as variable ENV.

    FLAG is also the name of the Instance and Container field that holds it.
    """

    flag: str
    env: str
    metavar: str
    valid: Callable[[str], bool]
    must: str  # what a valid value is, for the error message
    help: str


OPTIONS = (
    Option(
        "name",
        "TRACKDIR_PG_CONTAINER",
        "NAME",
        is_name,
        "a container name ([a-zA-Z0-9][a-zA-Z0-9_.-]*)",
        f"container to act on (default {DEFAULT_CONTAINER})",
    ),
    Option(
        "address",
        "TRACKDIR_PG_ADDRESS",
        "ADDR",
        is_ipv4,
        "an IPv4 address",
        "host address to publish the port on: 0.0.0.0 for all, or one of the "
        f"host's (default {DEFAULT_ADDRESS})",
    ),
    Option(
        "port",
        "TRACKDIR_PG_PORT",
        "PORT",
        is_port,
        "a port number from 1 to 65535",
        f"host port (default {DEFAULT_PORT})",
    ),
    Option(
        "volume",
        "TRACKDIR_PG_VOLUME",
        "VOLUME",
        is_name,
        "a volume name ([a-zA-Z0-9][a-zA-Z0-9_.-]*)",
        f"volume for persistent data (default NAME{VOLUME_SUFFIX})",
    ),
    Option(
        "password",
        "TRACKDIR_PG_PASSWORD",
        "PASSWORD",
        is_password,
        "one non-empty line",
        f"password of user {DB_USER} (default {DEFAULT_PASSWORD}); required for "
        "an address other than loopback. Other local users can read the option, "
        "but not the variable, in the process list",
    ),
)


@dataclass(frozen=True)
class Instance:
    """The container to act on and the settings to create it with."""

    name: str
    address: str
    port: str
    volume: str
    password: str
    # Where each explicitly given setting came from (an option or a variable),
    # by field name; settings that were not given have their default.
    sources: Mapping[str, str]


@dataclass(frozen=True)
class Container:
    """An existing container, as podman reports it."""

    state: str  # e.g. running, exited
    mode: str  # "" if this script did not create it
    address: str
    port: str
    volume: str  # "" in ephemeral mode
    password: str

    def fields(self) -> list[str]:
        """Return the KEY=VALUE items that status and list print."""
        return [
            f"state={self.state}",
            f"mode={self.mode}",
            f"address={self.address}",
            f"port={self.port}",
            f"volume={self.volume}",
        ]


def note(text: str) -> None:
    """Write TEXT to stderr as one wrapped paragraph."""
    wrapped = textwrap.fill(
        text, WRAP_WIDTH, break_long_words=False, break_on_hyphens=False
    )
    sys.stderr.write(wrapped + "\n")


def warn(text: str) -> None:
    note(f"WARNING: {text}")


def podman(
    *args: str,
    quiet: bool = False,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run podman with ARGS; return the result with stdout captured.

    podman reads STDIN (default: nothing) and gets ENV on top of our
    environment. Its stderr goes to ours unless QUIET. Never raises on a
    non-zero exit; pass the result to check() for that.
    """
    return subprocess.run(
        ["podman", *args],
        **({"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if quiet else None,
        env=None if env is None else {**os.environ, **env},
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


def configured(args: argparse.Namespace) -> Instance:
    """Return the instance that the options and the environment select.

    An option beats its variable; only an unset variable gets the default, so
    an empty value fails validation instead of silently turning into the
    default. Raises FatalError for an invalid value.
    """
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for option in OPTIONS:
        value, source = getattr(args, option.flag, None), f"--{option.flag}"
        if value is None:
            value, source = os.environ.get(option.env), option.env
        if value is None:
            continue
        if not option.valid(value):
            got = "" if option.flag == "password" else f", got {value!r}"
            raise FatalError(f"{source} must be {option.must}{got}.")
        values[option.flag] = value
        sources[option.flag] = source
    name = values.get("name", DEFAULT_CONTAINER)
    return Instance(
        name=name,
        address=values.get("address", DEFAULT_ADDRESS),
        port=values.get("port", DEFAULT_PORT),
        volume=values.get("volume", name + VOLUME_SUFFIX),
        password=values.get("password", DEFAULT_PASSWORD),
        sources=sources,
    )


def inspect(name: str) -> Container | None:
    """Return container NAME, or None if there is no such container."""
    result = podman(
        "container", "inspect", "--format", INSPECT_FORMAT, name, quiet=True
    )
    if result.returncode:
        return None
    state, mode, address, port, volume, *env = result.stdout.split("\n")
    password = next(
        (
            v[len("POSTGRES_PASSWORD=") :]
            for v in env
            if v.startswith("POSTGRES_PASSWORD=")
        ),
        DEFAULT_PASSWORD,
    )
    # Containers made before the address label existed listen on loopback.
    return Container(state, mode, address or DEFAULT_ADDRESS, port, volume, password)


def ours(name: str) -> Container | None:
    """Return container NAME, or None if there is no such container.

    Raises FatalError if this script did not create it, so that we never stop or
    delete someone else's container.
    """
    have = inspect(name)
    if have is not None and not have.mode:
        raise FatalError(
            f"container {name} exists but was not created by this script (it "
            f"has no {LABEL_MODE} label). Remove or rename it yourself."
        )
    return have


def hint(inst: Instance, command: str) -> str:
    """Return the command line that runs COMMAND on INST, for messages."""
    if inst.name == DEFAULT_CONTAINER:
        return f"{PROG} {command}"
    return f"{PROG} --name {inst.name} {command}"


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


def client_host(address: str) -> str:
    """Return the host a local client reaches a server published on ADDRESS at."""
    return DEFAULT_ADDRESS if address == "0.0.0.0" else address


def port_in_use(address: str, port: str) -> bool:
    """Return whether something accepts connections on ADDRESS:PORT."""
    host = client_host(address)
    try:
        with socket.create_connection((host, int(port)), PORT_PROBE_SECONDS):
            return True
    except OSError:
        return False


def require_port_free(address: str, port: str) -> None:
    if port_in_use(address, port):
        raise FatalError(
            f"{address}:{port} is already in use. Stop what listens there, or "
            "choose another port with --port or TRACKDIR_PG_PORT."
        )


def admin_psql(name: str, *args: str, stdin: str | None = None) -> str:
    """Run psql with ARGS (and STDIN, if given) in container NAME as the
    superuser in the maintenance database; return its stripped stdout.

    The maintenance database is needed for DROP/CREATE DATABASE, which cannot
    target the database psql is connected to. Raises FatalError if psql fails.
    """
    return check(
        podman(
            *("exec", *(["-i"] if stdin is not None else []), name),
            *("psql", "-U", DB_USER, "-d", "postgres"),
            *("-v", "ON_ERROR_STOP=1", *args),
            stdin=stdin,
        )
    )


def wait_ready(name: str, timeout: int) -> None:
    """Return once the server in container NAME accepts TCP connections; raise
    FatalError if the container stops or TIMEOUT seconds pass first.

    A TCP check (not the unix socket) is needed: during first initialization
    the image runs a temporary server that listens on the socket only.
    """
    is_ready = ("pg_isready", "-q", "-h", "127.0.0.1", "-U", DB_USER, "-d", DB_NAME)
    deadline = time.monotonic() + timeout
    while podman("exec", name, *is_ready).returncode:
        have = inspect(name)
        if have is None or have.state != "running":
            raise FatalError(
                f"the container stopped while starting. Look at: podman logs "
                f"{name} (ephemeral containers are removed when they exit)."
            )
        if time.monotonic() >= deadline:
            raise FatalError(
                f"PostgreSQL did not become ready within {timeout} s. Look at: "
                f"podman logs {name}"
            )
        time.sleep(READY_POLL_SECONDS)


def ensure_password(name: str, password: str) -> None:
    """Give user DB_USER PASSWORD.

    The image sets the password only when it initializes an empty volume, so a
    container created on a kept volume would otherwise have the old one. The
    statement goes through stdin to keep the password out of the process list.
    """
    literal = "'" + password.replace("'", "''") + "'"
    admin_psql(name, stdin=f"ALTER ROLE {DB_USER} PASSWORD {literal};\n")


def ensure_test_db(name: str) -> None:
    """Create TEST_DB_NAME unless it exists; the image creates DB_NAME itself."""
    exists = admin_psql(
        name, "-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{TEST_DB_NAME}'"
    )
    if exists != "1":
        admin_psql(name, "-c", f"CREATE DATABASE {TEST_DB_NAME}")


def create_container(inst: Instance, mode: Mode) -> None:
    """Create and start container INST in MODE.

    Raises FatalError, leaving no container behind, if podman cannot or if the
    settings are unsafe.
    """
    if not ipaddress.IPv4Address(inst.address).is_loopback and (
        inst.password == DEFAULT_PASSWORD
    ):
        raise FatalError(
            f"other hosts can reach {inst.address}, and the default password is "
            "public. Set another one with --password or TRACKDIR_PG_PASSWORD."
        )
    require_port_free(inst.address, inst.port)
    labels = [f"{LABEL_MODE}={mode}", f"{LABEL_ADDRESS}={inst.address}"]
    labels.append(f"{LABEL_PORT}={inst.port}")
    if mode is Mode.EPHEMERAL:
        storage = ["--rm", "--tmpfs", DATA_MOUNT]
    else:
        users = check(
            podman(
                "ps",
                "-a",
                "--filter",
                f"volume={inst.volume}",
                "--format",
                "{{.Names}}",
            )
        ).split()
        if users:
            raise FatalError(
                f"volume {inst.volume} is used by container {', '.join(users)}; "
                "two servers on one data directory corrupt it. Choose another "
                "volume with --volume or TRACKDIR_PG_VOLUME."
            )
        storage = ["-v", f"{inst.volume}:{DATA_MOUNT}"]
        labels.append(f"{LABEL_VOLUME}={inst.volume}")
    # Only an unset or empty variable gets the default.
    image = os.environ.get("TRACKDIR_PG_IMAGE") or DEFAULT_IMAGE
    where = "" if mode is Mode.EPHEMERAL else f", volume {inst.volume}"
    note(
        f"Creating {inst.name} from {image} in {mode} mode on "
        f"{inst.address}:{inst.port}{where} (the first run pulls the image)."
    )
    result = podman(
        *("run", "-d", "--name", inst.name),
        *(arg for label in labels for arg in ("--label", label)),
        *("-p", f"{inst.address}:{inst.port}:5432"),
        *("-e", f"POSTGRES_USER={DB_USER}"),
        # Without a value podman copies the variable from its environment, so
        # the password stays out of the process list.
        *("-e", "POSTGRES_PASSWORD"),
        *("-e", f"POSTGRES_DB={DB_NAME}"),
        *storage,
        image,
        env={"POSTGRES_PASSWORD": inst.password},
    )
    if result.returncode:
        # A failed run (e.g. the port cannot be bound) leaves a Created container
        # that keeps the bad port and would trap the next start; remove it.
        have = inspect(inst.name)
        if have is not None and have.mode == mode:
            podman("rm", "-f", "-v", inst.name, quiet=True)
        raise FatalError(
            "podman could not create the container; see its message above."
        )


def dsn(database: str, address: str, port: str, password: str) -> str:
    secret = urllib.parse.quote(password, safe="")
    host = client_host(address)
    return f"postgresql://{DB_USER}:{secret}@{host}:{port}/{database}"


def require_running(inst: Instance) -> None:
    """Raise FatalError unless our container INST is running."""
    have = ours(inst.name)
    if have is None or have.state != "running":
        raise FatalError(
            f"{inst.name} is not running. Start it with: {hint(inst, 'start')}"
        )


def warn_ignored(inst: Instance, have: Container) -> None:
    """Warn about each explicitly given setting that existing container HAVE
    does not use; only creating a container applies settings."""
    for field in ("address", "port", "volume", "password"):
        source = inst.sources.get(field)
        wanted = getattr(inst, field)
        if source is None or wanted == getattr(have, field):
            continue
        if field == "password":
            what = f"container {inst.name} has another password; ignoring {source}."
        else:
            what = (
                f"container {inst.name} uses {field} {getattr(have, field)}; "
                f"ignoring {source}={wanted}."
            )
        warn(
            f"{what} Settings apply only when the container is created: "
            f"'{hint(inst, 'destroy --keep-volume')}' removes it but keeps its "
            "data."
        )


def start(inst: Instance, args: argparse.Namespace) -> int:
    mode = Mode.EPHEMERAL if args.ephemeral else Mode.PERSISTENT
    timeout = ready_timeout()
    if mode is Mode.EPHEMERAL and "volume" in inst.sources:
        raise FatalError(
            f"an ephemeral container has no volume; drop {inst.sources['volume']} "
            "or --ephemeral."
        )
    if have := ours(inst.name):
        if have.mode != mode:
            raise FatalError(
                f"container {inst.name} exists in {have.mode} mode. Run "
                f"'{hint(inst, 'destroy')}' (this deletes its data) before "
                f"starting it in {mode} mode."
            )
        warn_ignored(inst, have)
        if have.state == "running":
            # An earlier start may have timed out and left it running.
            note(
                f"{inst.name} is already running ({mode} mode, "
                f"{have.address}:{have.port})."
            )
        else:
            require_port_free(have.address, have.port)
            check(podman("start", inst.name))
        address, port, password = have.address, have.port, have.password
    else:
        create_container(inst, mode)
        address, port, password = inst.address, inst.port, inst.password
    wait_ready(inst.name, timeout)
    ensure_password(inst.name, password)
    ensure_test_db(inst.name)
    note(
        f"PostgreSQL is ready on {address}:{port} ({mode} mode). Connection "
        f"string: {hint(inst, 'url')}"
    )
    return 0


def stop(inst: Instance, args: argparse.Namespace) -> int:
    have = ours(inst.name)
    if have is None:
        note(f"{inst.name} does not exist; nothing to stop.")
        return 0
    check(podman("stop", inst.name))
    if have.mode == Mode.EPHEMERAL:
        note(f"Stopped {inst.name}; its ephemeral data is gone.")
    else:
        note(f"Stopped {inst.name}; data is kept in volume {have.volume}.")
    return 0


def status(inst: Instance, args: argparse.Namespace) -> int:
    have = ours(inst.name)
    if have is None:
        print("state=absent")
        return 1
    print("\n".join(have.fields()))
    return 0 if have.state == "running" else 1


def list_(inst: Instance, args: argparse.Namespace) -> int:
    names = check(
        podman("ps", "-a", "--filter", f"label={LABEL_MODE}", "--format", "{{.Names}}")
    ).split()
    for name in sorted(names):
        # None if it was removed meanwhile, e.g. an ephemeral one that stopped.
        if have := inspect(name):
            print(" ".join([f"name={name}", *have.fields()]))
    return 0


def url(inst: Instance, args: argparse.Namespace) -> int:
    database = TEST_DB_NAME if args.test else DB_NAME
    have = ours(inst.name)
    source = inst if have is None else have
    print(dsn(database, source.address, source.port, source.password))
    return 0


def psql(inst: Instance, args: argparse.Namespace) -> NoReturn:
    """Replace this process with psql in the container, so that stdin, signals
    and the exit status pass straight through."""
    require_running(inst)
    # A tty only if both ends are one; otherwise it would mangle piped data.
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    command = [
        *("podman", "exec", "-it" if tty else "-i", inst.name),
        *("psql", "-U", DB_USER, "-d", DB_NAME, *args.psql_args),
    ]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(command[0], command)


def reset(inst: Instance, args: argparse.Namespace) -> int:
    require_running(inst)
    # Separate -c options run as separate commands; DROP/CREATE DATABASE cannot
    # run inside one transaction block.
    admin_psql(
        inst.name,
        *("-c", f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)"),
        *("-c", f"CREATE DATABASE {DB_NAME}"),
        *("-c", f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"),
        *("-c", f"CREATE DATABASE {TEST_DB_NAME}"),
    )
    note(f"Recreated databases {DB_NAME} and {TEST_DB_NAME} in {inst.name}.")
    return 0


def destroy(inst: Instance, args: argparse.Namespace) -> int:
    have = ours(inst.name)
    removed = []
    if have is not None:
        check(podman("rm", "-f", "-v", inst.name))
        removed.append(f"container {inst.name}")
    if args.keep_volume:
        if have is None:
            note(f"{inst.name} does not exist; nothing to remove.")
        elif have.volume:
            note(f"Removed container {inst.name}; kept volume {have.volume}.")
        else:
            note(f"Removed container {inst.name}.")
        return 0
    # An ephemeral container has no volume; then remove the one a persistent
    # container of this name would use, as an earlier one may have left it.
    volume = have.volume if have and have.volume else inst.volume
    if podman("volume", "exists", volume).returncode == 0:
        # No -f: it would also remove any other container that mounts the volume.
        if podman("volume", "rm", volume).returncode:
            raise FatalError(
                f"could not remove volume {volume}; another container may be "
                "using it (see podman's message above)."
            )
        removed.append(f"volume {volume}")
    if removed:
        note(f"Removed {' and '.join(removed)}.")
    else:
        note(
            f"Neither container {inst.name} nor volume {volume} exists; nothing "
            "to remove."
        )
    return 0


DESCRIPTION = """\
Run disposable PostgreSQL servers in rootless podman containers, for
developing and testing trackdir. Each server is one container, chosen with
--name; several can run side by side on different ports. Data is kept in a
volume, or in memory with start --ephemeral. Nothing but the chosen container
and its volume is touched."""

EPILOG = f"""\
Each option above may instead be set with its environment variable; the
option wins. --address, --port, --volume and --password take effect when
start creates the container; afterwards commands use the container's own
settings.

environment:
  TRACKDIR_PG_CONTAINER      --name
  TRACKDIR_PG_ADDRESS        --address
  TRACKDIR_PG_PORT           --port
  TRACKDIR_PG_VOLUME         --volume
  TRACKDIR_PG_PASSWORD       --password
  TRACKDIR_PG_IMAGE          PostgreSQL 18+ image, used when the container is
                             created (default {DEFAULT_IMAGE})
  TRACKDIR_PG_READY_TIMEOUT  seconds start waits for readiness
                             (default {DEFAULT_READY_TIMEOUT})

User and database are "{DB_USER}". Any local user can connect to the
server. Needs podman; the first start pulls the image. Only status, list and
url write to stdout; notes and errors go to stderr. Exit status: 0 success,
1 error (for status: not running), 2 usage error. Run '%(prog)s COMMAND
--help' for a command's options."""


def build_parser() -> argparse.ArgumentParser:
    # No abbreviations: command_index() must recognize our options exactly.
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    for option in OPTIONS:
        parser.add_argument(
            f"--{option.flag}", metavar=option.metavar, help=option.help
        )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str,
        run: Callable[[Instance, argparse.Namespace], int],
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
    summary = (
        "print state=, mode=, address=, port= and volume= lines; exit 0 if running"
    )
    add("status", status, summary)
    add("list", list_, "print one line like status for each container of this script")
    command = add("url", url, "print the connection string")
    command.add_argument(
        "--test",
        action="store_true",
        help=f"of database {TEST_DB_NAME}, which tests may wipe",
    )
    # main() routes "psql ..." past argparse; this entry only lists it in --help.
    summary = "run psql in the container with all arguments, even --help"
    add("psql", psql, summary, add_help=False)
    add("reset", reset, f"drop and recreate databases {DB_NAME} and {TEST_DB_NAME}")
    command = add(
        "destroy", destroy, "remove the container and its volume (deletes all data)"
    )
    command.add_argument(
        "--keep-volume",
        action="store_true",
        help="remove only the container, e.g. to recreate it with other settings",
    )
    return parser


def command_index(argv: Sequence[str]) -> int | None:
    """Return the index of the command word in ARGV, or None if there is none.

    Skips our options and their values the way argparse reads them.
    """
    with_value = {f"--{option.flag}" for option in OPTIONS}
    i = 0
    while i < len(argv):
        if argv[i] in with_value:
            i += 2
        elif argv[i].startswith("-"):  # --flag=value, --help, or an error
            i += 1
        else:
            return i
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit status."""
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    i = command_index(argv)
    if i is not None and argv[i] == "psql":
        # Every argument after psql belongs to psql, options included, so
        # argparse must not see them: parse_known_args would also hand psql
        # unknown options of ours.
        args = parser.parse_args(argv[: i + 1])
        args.psql_args = argv[i + 1 :]
    else:
        args = parser.parse_args(argv)
    try:
        inst = configured(args)
        if shutil.which("podman") is None:
            raise FatalError("podman is not installed or not in PATH.")
        return args.run(inst, args)
    except FatalError as e:
        note(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        note("ERROR: interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
