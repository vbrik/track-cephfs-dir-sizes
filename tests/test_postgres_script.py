"""Tests for scripts/postgres.py, run against a stub podman (no real podman)."""

import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "postgres.py"
BASH = shutil.which("bash") or "/usr/bin/bash"
# The script runs with PATH limited to a directory holding the stub and links
# to the tools the stub uses, so the host's real podman can never be reached.
HOST_TOOLS = ("cat", "mkdir", "rm")
DEFAULT = "trackdir-postgres"

STUB = r"""#!@BASH@
# Fake podman: log every call; keep the state of each container NAME in files
# in $STUB_STATE/containers/NAME: status (running|exited|created; absent
# directory = no such container), the labels mode, address, port and volume,
# and password (from POSTGRES_PASSWORD).
# Test switches: not_ready (pg_isready fails), crash (container exits instead),
# test_db (contains 1 when database trackdir_test exists), no_volume (the volume
# does not exist), volume_in_use (volume rm fails, as with another container),
# run_fails (run leaves a Created container behind and exits 125).
# Whatever exec -i reads from stdin is appended to $STUB_STATE/stdin.
set -u
state=$STUB_STATE
line=$*
echo "${line//$'\n'/\\n}" >> "$state/log"  # one line per call
read_file() { cat "$1" 2>/dev/null || echo; }
case $1 in
    container)  # container inspect --format FORMAT NAME
        dir=$state/containers/$5
        [[ -f $dir/status ]] || { echo "Error: no such container" >&2; exit 125; }
        [[ $4 == '{{.State.Status}}'* ]] || { echo "bad template" >&2; exit 99; }
        for field in status mode address port volume; do read_file "$dir/$field"; done
        echo "PATH=/usr/bin"
        if [[ -f $dir/password ]]; then
            echo "POSTGRES_PASSWORD=$(cat "$dir/password")"
        fi
        ;;
    run)
        prev=
        labels=()
        for arg in "$@"; do
            case $prev in
                --name) name=$arg ;;
                --label) labels+=("$arg") ;;
            esac
            prev=$arg
        done
        dir=$state/containers/$name
        mkdir -p "$dir"
        for label in "${labels[@]}"; do
            key=${label%%=*}
            echo "${label#*=}" > "$dir/${key#trackdir.}"
        done
        if [[ -n ${POSTGRES_PASSWORD+set} ]]; then
            echo "$POSTGRES_PASSWORD" > "$dir/password"
        fi
        if [[ -f $state/run_fails ]]; then  # like a port that cannot be bound:
            echo created > "$dir/status"    # podman leaves the container behind
            echo "Error: cannot listen" >&2
            exit 125
        fi
        echo running > "$dir/status"
        echo 0123456789abcdef ;;
    start) echo running > "$state/containers/$2/status" ;;
    stop)
        dir=$state/containers/$2
        if [[ $(read_file "$dir/mode") == ephemeral ]]; then  # --rm removes it
            rm -rf "$dir"
        else
            echo exited > "$dir/status"
        fi ;;
    rm) rm -rf "$state/containers/$4" ;;  # rm -f -v NAME
    ps)  # ps -a --filter FILTER --format {{.Names}}
        for dir in "$state"/containers/*/; do
            [[ -d $dir ]] || continue
            name=${dir%/}
            name=${name##*/}
            case $4 in
                label=trackdir.mode) if [[ -f $dir/mode ]]; then echo "$name"; fi ;;
                volume=*)
                    if [[ $(read_file "$dir/volume") == "${4#volume=}" ]]; then
                        echo "$name"
                    fi ;;
                *) echo "stub podman: unexpected filter: $4" >&2; exit 99 ;;
            esac
        done ;;
    volume)  # volume exists NAME | volume rm NAME
        case $2 in
            exists) [[ ! -f $state/no_volume ]] ;;
            rm)
                [[ ! -f $state/volume_in_use ]] || { echo "in use" >&2; exit 2; } ;;
        esac ;;
    exec)  # exec [-i] NAME COMMAND...
        name=$2
        if [[ $2 == -i ]]; then
            name=$3
            cat >> "$state/stdin"
        fi
        case "$*" in
            *pg_isready*)
                if [[ -f $state/crash ]]; then
                    echo exited > "$state/containers/$name/status"
                fi
                [[ ! -f $state/not_ready && ! -f $state/crash ]] ;;
            *pg_database*) cat "$state/test_db" 2>/dev/null || true ;;
        esac ;;
    *) echo "stub podman: unexpected command: $*" >&2; exit 99 ;;
esac
"""


def collapse(text: str) -> str:
    return " ".join(text.split())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakePodman:
    """A temp directory with a stub podman, and a way to run the script on it."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.bin.mkdir()
        (self.state / "containers").mkdir(parents=True)
        for tool in HOST_TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool) or pytest.fail(tool))
        stub = self.bin / "podman"
        stub.write_text(STUB.replace("@BASH@", BASH))
        stub.chmod(0o755)
        self.port = free_port()

    def remove_stub(self) -> None:
        (self.bin / "podman").unlink()

    def container(self, name: str = DEFAULT) -> Path:
        return self.state / "containers" / name

    def seed(
        self,
        status: str,
        mode: str | None = "persistent",
        port=None,
        *,
        name: str = DEFAULT,
        address: str | None = "127.0.0.1",
        volume: str | None = None,
        password: str = "trackdir",
    ) -> None:
        """Pre-create a container; mode=None makes it look foreign (no labels),
        address=None like one made before the address label existed."""
        files = {"status": status, "password": password}
        if mode is not None:
            files |= {"mode": mode, "port": f"{port or self.port}"}
            if address is not None:
                files["address"] = address
            if mode == "persistent":
                files["volume"] = volume or f"{name}-data"
        directory = self.container(name)
        directory.mkdir()
        for file, value in files.items():
            (directory / file).write_text(value + "\n")

    def read(self, field: str, name: str = DEFAULT) -> str:
        return (self.container(name) / field).read_text().strip()

    @property
    def calls(self) -> list[str]:
        log = self.state / "log"
        return log.read_text().splitlines() if log.exists() else []

    def called(self, verb: str) -> list[str]:
        return [c for c in self.calls if c.split()[0] == verb]

    @property
    def stdin(self) -> str:
        path = self.state / "stdin"
        return path.read_text() if path.exists() else ""

    def run(self, *args: str, env=None, stdin=None) -> subprocess.CompletedProcess:
        environ = {
            "PATH": str(self.bin),
            "STUB_STATE": str(self.state),
            "TRACKDIR_PG_PORT": str(self.port),
        }
        for name, value in (env or {}).items():
            if value is None:
                environ.pop(name, None)
            else:
                environ[name] = value
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=environ,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


@pytest.fixture
def pg(tmp_path):
    return FakePodman(tmp_path)


def test_help_goes_to_stdout_and_needs_no_podman(pg):
    pg.remove_stub()
    result = pg.run("--help")
    assert result.returncode == 0
    out = collapse(result.stdout)
    assert (
        "usage: postgres.py [-h] [--name NAME] [--address ADDR] [--port PORT] "
        "[--volume VOLUME] [--password PASSWORD] COMMAND" in out
    )
    for command in ("start", "stop", "status", "list", "url", "psql", "reset"):
        assert f"\n    {command} " in result.stdout
    assert "\n    destroy " in result.stdout
    for variable in (
        "TRACKDIR_PG_CONTAINER",
        "TRACKDIR_PG_ADDRESS",
        "TRACKDIR_PG_PORT",
        "TRACKDIR_PG_VOLUME",
        "TRACKDIR_PG_PASSWORD",
        "TRACKDIR_PG_READY_TIMEOUT",
    ):
        assert variable in result.stdout
    assert result.stderr == ""


def test_no_command_prints_usage_to_stderr(pg):
    result = pg.run()
    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage:" in result.stderr


def test_unknown_command_is_named(pg):
    result = pg.run("frobnicate")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid choice: 'frobnicate'" in collapse(result.stderr)
    assert pg.calls == []


def test_missing_podman_is_reported(pg):
    pg.remove_stub()
    result = pg.run("status")
    assert result.returncode == 1
    assert "ERROR: podman is not installed" in collapse(result.stderr)


def test_start_persistent_creates_container(pg):
    result = pg.run("start")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # the container ID printed by podman is discarded
    (run,) = pg.called("run")
    assert "--name trackdir-postgres " in run
    assert f"-p 127.0.0.1:{pg.port}:5432" in run
    assert "--label trackdir.mode=persistent" in run
    assert "--label trackdir.address=127.0.0.1" in run
    assert f"--label trackdir.port={pg.port}" in run
    assert "--label trackdir.volume=trackdir-postgres-data" in run
    assert "-v trackdir-postgres-data:/var/lib/postgresql" in run
    assert "--rm" not in run
    assert "--tmpfs" not in run
    assert run.endswith(" docker.io/library/postgres:18")
    execs = pg.called("exec")
    assert any("pg_isready -q -h 127.0.0.1" in c for c in execs)
    assert any("CREATE DATABASE trackdir_test" in c for c in execs)
    assert pg.called("ps") == [
        "ps -a --filter volume=trackdir-postgres-data --format {{.Names}}"
    ]


def test_start_ephemeral_uses_tmpfs_and_rm(pg):
    result = pg.run("start", "--ephemeral")
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert "--label trackdir.mode=ephemeral" in run
    assert "--rm" in run.split()
    assert "--tmpfs /var/lib/postgresql" in run
    assert " -v " not in run
    assert "trackdir.volume" not in run


def test_start_ephemeral_rejects_a_volume(pg):
    result = pg.run("--volume", "v", "start", "--ephemeral")
    assert result.returncode == 1
    assert "ephemeral container has no volume; drop --volume" in collapse(result.stderr)
    assert pg.called("run") == []


def test_start_honors_image_override(pg):
    result = pg.run("start", env={"TRACKDIR_PG_IMAGE": "example.test/pg:19"})
    assert result.returncode == 0, result.stderr
    assert pg.called("run")[0].endswith(" example.test/pg:19")


def test_start_skips_test_db_that_exists(pg):
    (pg.state / "test_db").write_text("1\n")
    assert pg.run("start").returncode == 0
    assert not any("CREATE DATABASE" in c for c in pg.called("exec"))


def test_start_on_running_container_creates_nothing_but_still_converges(pg):
    pg.seed("running")
    result = pg.run("start")
    assert result.returncode == 0
    assert "already running" in collapse(result.stderr)
    # No create/start/stop, but readiness, the password and the test database
    # are still checked: an earlier start may have timed out and left it running.
    assert {c.split()[0] for c in pg.calls} == {"container", "exec"}
    execs = pg.called("exec")
    assert any("pg_isready" in c for c in execs)
    assert any("CREATE DATABASE trackdir_test" in c for c in execs)
    assert "ALTER ROLE trackdir PASSWORD 'trackdir';" in pg.stdin


def test_start_on_running_container_that_is_not_ready_fails(pg):
    pg.seed("running")
    (pg.state / "not_ready").touch()
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": "0"})
    assert result.returncode == 1
    assert "did not become ready" in collapse(result.stderr)


def test_start_restarts_stopped_container(pg):
    pg.seed("exited")
    result = pg.run("start")
    assert result.returncode == 0, result.stderr
    assert pg.called("start") == ["start trackdir-postgres"]
    assert pg.called("run") == []


def test_start_refuses_to_switch_mode(pg):
    pg.seed("running", mode="persistent")
    result = pg.run("start", "--ephemeral")
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert "ERROR:" in err
    assert "persistent mode" in err
    assert "destroy" in err
    assert pg.called("run") == []
    assert pg.called("start") == []


def test_start_warns_when_port_differs_from_container(pg):
    pg.seed("exited", port=free_port())
    result = pg.run("start")
    assert result.returncode == 0, result.stderr
    err = collapse(result.stderr)
    assert "WARNING:" in err
    assert f"ignoring TRACKDIR_PG_PORT={pg.port}" in err
    assert "destroy --keep-volume" in err


def test_start_warns_about_each_ignored_setting(pg):
    pg.seed("running", password="old")
    result = pg.run(
        *("--address", "127.0.0.2", "--volume", "other", "--password", "N3wPw"),
        "start",
    )
    assert result.returncode == 0, result.stderr
    err = collapse(result.stderr)
    assert "uses address 127.0.0.1; ignoring --address=127.0.0.2" in err
    assert "uses volume trackdir-postgres-data; ignoring --volume=other" in err
    assert "has another password; ignoring --password." in err
    assert "N3wPw" not in err  # the password is secret
    assert "PORT" not in err  # the port matches
    # The container's own password is kept in sync, not the ignored one.
    assert "PASSWORD 'old';" in pg.stdin


@pytest.mark.parametrize("command", ["start", "stop"])
def test_foreign_container_is_not_touched(pg, command):
    pg.seed("running", mode=None)
    result = pg.run(command)
    assert result.returncode == 1
    assert "not created by this script" in collapse(result.stderr)
    assert pg.called("run") == []
    assert pg.called("start") == []
    assert pg.called("stop") == []


@pytest.mark.parametrize("existing", [False, True], ids=["new", "stopped"])
def test_start_refuses_port_in_use(pg, existing):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        if existing:
            pg.seed("exited", port=port)
        result = pg.run("start", env={"TRACKDIR_PG_PORT": str(port)})
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert f"127.0.0.1:{port} is already in use" in err
    assert "TRACKDIR_PG_PORT" in err
    assert pg.called("run") == []
    assert pg.called("start") == []


BAD_PORTS = ["abc", "0", "70000", "5432x", "", "08080", "18446744073709551617"]
# int() accepts these: a trailing newline, a space, non-ASCII digits.
INT_LOOKALIKES = ["5432\n", " 5432", "٥٤٣٢"]


@pytest.mark.parametrize("value", [*BAD_PORTS, *INT_LOOKALIKES])
def test_start_rejects_bad_port(pg, value):
    result = pg.run("start", env={"TRACKDIR_PG_PORT": value})
    assert result.returncode == 1
    assert f"got {value!r}" in collapse(result.stderr)
    assert pg.called("run") == []


@pytest.mark.parametrize(
    ("option", "variable", "value"),
    [
        ("--name", "TRACKDIR_PG_CONTAINER", "-x"),
        ("--name", "TRACKDIR_PG_CONTAINER", "a/b"),
        ("--name", "TRACKDIR_PG_CONTAINER", ""),
        ("--volume", "TRACKDIR_PG_VOLUME", "a b"),
        ("--address", "TRACKDIR_PG_ADDRESS", "localhost"),
        ("--address", "TRACKDIR_PG_ADDRESS", "::1"),
        ("--address", "TRACKDIR_PG_ADDRESS", "127.000.0.1"),
        ("--address", "TRACKDIR_PG_ADDRESS", "127.0.0.1\n"),
        ("--port", "TRACKDIR_PG_PORT", "0"),
    ],
)
def test_bad_settings_are_rejected_naming_their_source(pg, option, variable, value):
    for args, env, source in (
        ((f"{option}={value}",), {}, option),
        ((), {variable: value}, variable),
    ):
        result = pg.run(*args, "status", env=env)
        assert result.returncode == 1
        assert f"{source} must be" in collapse(result.stderr)
        assert f"got {value!r}" in collapse(result.stderr)
    assert pg.calls == []


@pytest.mark.parametrize("value", ["", "a\nsecret"])
def test_bad_password_is_rejected_without_echoing_it(pg, value):
    for args, env, source in (
        ((f"--password={value}",), {}, "--password"),
        ((), {"TRACKDIR_PG_PASSWORD": value}, "TRACKDIR_PG_PASSWORD"),
    ):
        result = pg.run(*args, "start", env=env)
        assert result.returncode == 1
        err = collapse(result.stderr)
        assert f"{source} must be one non-empty line." in err
        assert "secret" not in err
    assert pg.calls == []


def test_options_beat_environment(pg):
    port = free_port()
    result = pg.run(
        *("--name", "dev2", "--port", str(port), "--volume", "vol2"),
        *("--address", "127.0.0.2", "--password", "opt", "start"),
        env={
            "TRACKDIR_PG_PASSWORD": "env",
            "TRACKDIR_PG_CONTAINER": "envname",
            "TRACKDIR_PG_VOLUME": "envvol",
            "TRACKDIR_PG_ADDRESS": "127.0.0.3",
        },  # and TRACKDIR_PG_PORT=pg.port
    )
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert "--name dev2 " in run
    assert f"-p 127.0.0.2:{port}:5432" in run
    assert "-v vol2:/var/lib/postgresql" in run
    assert "envname" not in run and "envvol" not in run
    assert pg.read("password", "dev2") == "opt"


def test_environment_selects_container_and_its_default_volume(pg):
    result = pg.run("start", env={"TRACKDIR_PG_CONTAINER": "dev2"})
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert "--name dev2 " in run
    assert "-v dev2-data:/var/lib/postgresql" in run
    assert "--name dev2 url" in collapse(result.stderr)  # the hint names it


def test_second_instance_runs_beside_the_first(pg):
    pg.seed("running")
    port = free_port()
    result = pg.run("--name", "dev2", "--port", str(port), "start")
    assert result.returncode == 0, result.stderr
    assert pg.read("status") == "running"  # the first one is untouched
    assert pg.read("volume", "dev2") == "dev2-data"
    assert not any(DEFAULT in c for c in pg.called("run") + pg.called("exec"))
    result = pg.run("list")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"name=dev2 state=running mode=persistent address=127.0.0.1 "
        f"port={port} volume=dev2-data\n"
        f"name={DEFAULT} state=running mode=persistent address=127.0.0.1 "
        f"port={pg.port} volume={DEFAULT}-data\n"
    )
    stop = pg.run("--name", "dev2", "stop")
    assert stop.returncode == 0, stop.stderr
    assert pg.called("stop") == ["stop dev2"]


def test_list_shows_only_our_containers(pg):
    assert pg.run("list").stdout == ""
    pg.seed("running", mode=None, name="foreign")
    pg.seed("exited", mode="ephemeral", name="eph")
    result = pg.run("list")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"name=eph state=exited mode=ephemeral address=127.0.0.1 "
        f"port={pg.port} volume=\n"
    )


def test_start_refuses_a_volume_in_use(pg):
    pg.seed("exited")
    result = pg.run("--name", "dev2", "--volume", f"{DEFAULT}-data", "start")
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert f"volume {DEFAULT}-data is used by container {DEFAULT}" in err
    assert pg.called("run") == []


def test_start_on_a_specific_address(pg):
    result = pg.run("--address", "127.0.0.2", "start")
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert f"-p 127.0.0.2:{pg.port}:5432" in run
    assert "--label trackdir.address=127.0.0.2" in run
    url = pg.run("url")  # no --address: the container's own one counts
    assert (
        url.stdout == f"postgresql://trackdir:trackdir@127.0.0.2:{pg.port}/trackdir\n"
    )


@pytest.mark.parametrize("address", ["0.0.0.0", "192.0.2.10"])
def test_non_loopback_address_needs_a_password(pg, address):
    result = pg.run("--address", address, "start")
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert f"other hosts can reach {address}" in err
    assert "--password or TRACKDIR_PG_PASSWORD" in err
    assert pg.called("run") == []


def test_all_addresses_with_a_password(pg):
    password = "it's a s3cret/@:"
    env = {"TRACKDIR_PG_PASSWORD": password}
    result = pg.run("--address", "0.0.0.0", "start", env=env)
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert f"-p 0.0.0.0:{pg.port}:5432" in run
    assert "-e POSTGRES_PASSWORD " in run  # copied from the environment,
    assert password not in "\n".join(pg.calls)  # so never in an argv
    assert pg.read("password") == password
    assert "ALTER ROLE trackdir PASSWORD 'it''s a s3cret/@:';" in pg.stdin
    # url reads the password from the container and reaches it on loopback.
    url = pg.run("url", env={"TRACKDIR_PG_PASSWORD": None})
    secret = "it%27s%20a%20s3cret%2F%40%3A"
    assert (
        url.stdout == f"postgresql://trackdir:{secret}@127.0.0.1:{pg.port}/trackdir\n"
    )


@pytest.mark.parametrize("value", ["soon", "", "-1", "1234567", "+8", " 8", "8_0", "٨"])
def test_start_rejects_bad_timeout(pg, value):
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": value})
    assert result.returncode == 1
    assert "TRACKDIR_PG_READY_TIMEOUT" in collapse(result.stderr)
    assert pg.called("run") == []


def test_start_reads_timeout_with_leading_zero_as_decimal(pg):
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": "08"})
    assert result.returncode == 0, result.stderr


def test_start_fails_when_server_never_gets_ready(pg):
    (pg.state / "not_ready").touch()
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": "0"})
    assert result.returncode == 1
    assert "did not become ready" in collapse(result.stderr)


def test_start_fails_at_once_when_container_exits(pg):
    (pg.state / "crash").touch()
    # A long timeout proves the failure does not wait for it.
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": "600"})
    assert result.returncode == 1
    assert "stopped while starting" in collapse(result.stderr)


def test_start_removes_the_leftover_of_a_failed_run(pg):
    (pg.state / "run_fails").touch()
    result = pg.run("start")
    assert result.returncode == 1
    assert "could not create the container" in collapse(result.stderr)
    # Otherwise the Created container keeps its port and traps the next start.
    assert pg.called("rm") == ["rm -f -v trackdir-postgres"]
    assert not pg.container().exists()


def test_start_rejects_unknown_option(pg):
    result = pg.run("start", "--fast")
    assert result.returncode == 2
    assert "unrecognized arguments: --fast" in collapse(result.stderr)
    assert pg.calls == []


def test_global_options_go_before_the_command(pg):
    result = pg.run("start", "--port", "5433")
    assert result.returncode == 2
    assert "unrecognized arguments: --port 5433" in collapse(result.stderr)


def test_options_are_not_abbreviated(pg):
    result = pg.run("--nam", "x", "status")
    assert result.returncode == 2


def test_stop_persistent_keeps_data(pg):
    pg.seed("running", mode="persistent", volume="mine")
    result = pg.run("stop")
    assert result.returncode == 0
    assert pg.called("stop") == ["stop trackdir-postgres"]
    assert "kept in volume mine" in collapse(result.stderr)


def test_stop_ephemeral_says_data_is_gone(pg):
    pg.seed("running", mode="ephemeral")
    result = pg.run("stop")
    assert result.returncode == 0
    assert "ephemeral data is gone" in collapse(result.stderr)


def test_stop_without_container_is_fine(pg):
    result = pg.run("stop")
    assert result.returncode == 0
    assert "nothing to stop" in collapse(result.stderr)
    assert pg.called("stop") == []


def test_stop_rejects_arguments(pg):
    result = pg.run("stop", "now")
    assert result.returncode == 2
    assert "unrecognized arguments: now" in collapse(result.stderr)
    assert pg.calls == []


def test_status_running(pg):
    pg.seed("running", mode="ephemeral")
    result = pg.run("status")
    assert result.returncode == 0
    assert result.stdout == (
        f"state=running\nmode=ephemeral\naddress=127.0.0.1\nport={pg.port}\nvolume=\n"
    )
    assert result.stderr == ""


def test_status_of_a_container_without_address_label(pg):
    pg.seed("running", address=None, name="old")
    result = pg.run("--name", "old", "status")
    assert result.returncode == 0
    assert "\naddress=127.0.0.1\n" in result.stdout


def test_status_refuses_foreign_container(pg):
    pg.seed("running", mode=None)
    result = pg.run("status")
    assert result.returncode == 1
    assert result.stdout == ""  # no plausible-looking state=running
    assert "not created by this script" in collapse(result.stderr)


def test_url_refuses_foreign_container(pg):
    pg.seed("running", mode=None)
    result = pg.run("url")
    assert result.returncode == 1
    assert result.stdout == ""


def test_status_stopped_and_absent_exit_nonzero(pg):
    assert pg.run("status").stdout == "state=absent\n"
    assert pg.run("status").returncode == 1
    pg.seed("exited")
    result = pg.run("status")
    assert result.returncode == 1
    assert result.stdout.startswith("state=exited\n")


def test_url_without_container_uses_configured_settings(pg):
    result = pg.run("url")
    assert result.returncode == 0
    dsn = f"postgresql://trackdir:trackdir@127.0.0.1:{pg.port}/trackdir"
    assert result.stdout == dsn + "\n"
    assert result.stderr == ""
    assert pg.called("run") == []
    result = pg.run("--address", "127.0.0.2", "--port", "6000", "url")
    assert result.stdout == "postgresql://trackdir:trackdir@127.0.0.2:6000/trackdir\n"


def test_url_test_database(pg):
    result = pg.run("url", "--test")
    assert result.stdout.endswith("/trackdir_test\n")


def test_url_reports_the_real_port_of_the_container(pg):
    real = free_port()
    pg.seed("running", port=real)
    result = pg.run("url")  # TRACKDIR_PG_PORT says something else
    assert result.stdout.endswith(f"@127.0.0.1:{real}/trackdir\n")


def test_url_rejects_bad_port_and_option(pg):
    assert pg.run("url", env={"TRACKDIR_PG_PORT": "x"}).returncode == 1
    result = pg.run("url", "--prod")
    assert result.returncode == 2
    assert "unrecognized arguments: --prod" in collapse(result.stderr)


def test_psql_passes_arguments_and_keeps_stdin(pg):
    pg.seed("running")
    result = pg.run("psql", "-c", "select 1", stdin="select 2;\n")
    assert result.returncode == 0, result.stderr
    # No tty here, so -i without -t; the stub's exec call is logged verbatim.
    assert pg.called("exec") == [
        "exec -i trackdir-postgres psql -U trackdir -d trackdir -c select 1"
    ]
    assert pg.stdin == "select 2;\n"


@pytest.mark.parametrize(
    "args",
    [("--help",), ("-h", "db.example"), ("-d", "trackdir_test", "-c", "x", "-X")],
)
def test_psql_passes_its_options_through_in_order(pg, args):
    pg.seed("running")
    result = pg.run("psql", *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # not our --help
    (call,) = pg.called("exec")
    assert call.endswith(" -d trackdir " + " ".join(args))


@pytest.mark.parametrize(
    ("ours", "name", "theirs"),
    [
        (("--name", "dev2"), "dev2", ("-c", "x", "--help")),
        (("--name=dev2",), "dev2", ()),
        (("--name", "psql"), "psql", ("-X",)),
        ((), DEFAULT, ("--name", "dev2")),  # after psql: psql's
    ],
)
def test_psql_after_options_of_ours(pg, ours, name, theirs):
    pg.seed("running", name=name)
    result = pg.run(*ours, "psql", *theirs)
    assert result.returncode == 0, result.stderr
    (call,) = pg.called("exec")
    expected = f"exec -i {name} psql -U trackdir -d trackdir"
    assert call == " ".join([expected, *theirs])


def test_psql_after_an_unknown_option_is_a_usage_error(pg):
    pg.seed("running")
    result = pg.run("--bogus", "psql", "-c", "x")
    assert result.returncode == 2
    assert "unrecognized arguments" in collapse(result.stderr)
    assert pg.called("exec") == []


def test_psql_needs_a_running_server(pg):
    result = pg.run("--name", "dev2", "psql")
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert "dev2 is not running" in err
    assert "postgres.py --name dev2 start" in err


def test_reset_recreates_both_databases(pg):
    pg.seed("running")
    result = pg.run("reset")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    (call,) = pg.called("exec")
    for statement in (
        "DROP DATABASE IF EXISTS trackdir WITH (FORCE)",
        "CREATE DATABASE trackdir",
        "DROP DATABASE IF EXISTS trackdir_test WITH (FORCE)",
        "CREATE DATABASE trackdir_test",
    ):
        assert f"-c {statement} " in call + " "
    assert call.count(" -c ") == 4  # one per statement, no extras
    assert "-d postgres" in call  # cannot drop the database we are connected to


def test_reset_needs_a_running_server_and_refuses_foreign(pg):
    assert pg.run("reset").returncode == 1
    pg.seed("running", mode=None)
    result = pg.run("reset")
    assert result.returncode == 1
    assert "not created by this script" in collapse(result.stderr)
    assert pg.called("exec") == []


def test_destroy_removes_container_and_its_volume(pg):
    pg.seed("running", volume="mine")
    result = pg.run("destroy")
    assert result.returncode == 0, result.stderr
    assert pg.called("rm") == ["rm -f -v trackdir-postgres"]
    assert pg.called("volume") == [
        "volume exists mine",
        "volume rm mine",  # no -f: -f would remove other users of it
    ]
    assert "Removed container trackdir-postgres and volume mine." in collapse(
        result.stderr
    )


def test_destroy_without_container_removes_configured_volume(pg):
    result = pg.run("destroy")
    assert result.returncode == 0
    assert pg.called("rm") == []
    assert pg.called("volume") == [
        "volume exists trackdir-postgres-data",
        "volume rm trackdir-postgres-data",
    ]
    assert "Removed volume trackdir-postgres-data." in collapse(result.stderr)
    pg.run("--volume", "v2", "destroy")
    assert pg.called("volume")[-1] == "volume rm v2"


def test_destroy_keep_volume_removes_only_the_container(pg):
    pg.seed("exited", name="dev2")
    result = pg.run("--name", "dev2", "destroy", "--keep-volume")
    assert result.returncode == 0, result.stderr
    assert pg.called("rm") == ["rm -f -v dev2"]  # -v: anonymous volumes only
    assert pg.called("volume") == []
    assert "kept volume dev2-data" in collapse(result.stderr)


def test_destroy_without_volume_removes_nothing_more(pg):
    (pg.state / "no_volume").touch()
    result = pg.run("destroy")
    assert result.returncode == 0, result.stderr
    assert pg.called("volume") == ["volume exists trackdir-postgres-data"]
    err = collapse(result.stderr)
    assert "Neither container trackdir-postgres nor volume" in err
    assert "nothing to remove" in err


def test_destroy_ephemeral_without_volume_names_only_the_container(pg):
    pg.seed("running", mode="ephemeral")
    (pg.state / "no_volume").touch()
    result = pg.run("destroy")
    assert result.returncode == 0, result.stderr
    assert "Removed container trackdir-postgres." in collapse(result.stderr)


def test_destroy_keep_volume_without_container(pg):
    result = pg.run("destroy", "--keep-volume")
    assert result.returncode == 0, result.stderr
    assert pg.called("rm") == [] and pg.called("volume") == []
    assert "does not exist; nothing to remove" in collapse(result.stderr)


def test_destroy_reports_volume_in_use_by_another_container(pg):
    (pg.state / "volume_in_use").touch()
    result = pg.run("destroy")
    assert result.returncode == 1
    err = collapse(result.stderr)
    assert "ERROR:" in err
    assert "another container" in err


def test_destroy_refuses_foreign_container(pg):
    pg.seed("running", mode=None)
    result = pg.run("destroy")
    assert result.returncode == 1
    assert pg.called("rm") == []
    assert pg.called("volume") == []
