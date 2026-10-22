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
HOST_TOOLS = ("cat", "rm")

STUB = r"""#!@BASH@
# Fake podman: log every call; keep container state in files in $STUB_STATE.
# status: running|exited (absent file = no such container); mode, port: labels.
# Test switches: not_ready (pg_isready fails), crash (container exits instead),
# test_db (contains 1 when database trackdir_test exists), no_volume (the volume
# does not exist), volume_in_use (volume rm fails, as with another container),
# run_fails (run leaves a Created container behind and exits 125).
set -u
state=$STUB_STATE
echo "$*" >> "$state/log"
read_file() { cat "$state/$1" 2>/dev/null || echo; }
case $1 in
    container)  # container inspect --format FORMAT NAME
        [[ -f $state/status ]] || { echo "Error: no such container" >&2; exit 125; }
        case $4 in  # exact templates, so that a malformed one fails the tests
            '{{.State.Status}}') cat "$state/status" ;;
            '{{index .Config.Labels "trackdir.mode"}}') read_file mode ;;
            '{{index .Config.Labels "trackdir.port"}}') read_file port ;;
            *) echo "stub podman: unexpected template: $4" >&2; exit 99 ;;
        esac ;;
    run)
        for arg in "$@"; do
            case $arg in
                trackdir.mode=*) echo "${arg#*=}" > "$state/mode" ;;
                trackdir.port=*) echo "${arg#*=}" > "$state/port" ;;
            esac
        done
        if [[ -f $state/run_fails ]]; then  # like a port that cannot be bound:
            echo created > "$state/status"  # podman leaves the container behind
            echo "Error: cannot listen" >&2
            exit 125
        fi
        echo running > "$state/status"
        echo 0123456789abcdef ;;
    start) echo running > "$state/status" ;;
    stop)
        if [[ $(read_file mode) == ephemeral ]]; then  # --rm removes it
            rm -f "$state/status" "$state/mode" "$state/port"
        else
            echo exited > "$state/status"
        fi ;;
    rm) rm -f "$state/status" "$state/mode" "$state/port" ;;
    volume)  # volume exists NAME | volume rm NAME
        case $2 in
            exists) [[ ! -f $state/no_volume ]] ;;
            rm)
                [[ ! -f $state/volume_in_use ]] || { echo "in use" >&2; exit 2; } ;;
        esac ;;
    exec)
        case "$*" in
            *pg_isready*)
                if [[ -f $state/crash ]]; then echo exited > "$state/status"; fi
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
        self.state.mkdir()
        for tool in HOST_TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool) or pytest.fail(tool))
        stub = self.bin / "podman"
        stub.write_text(STUB.replace("@BASH@", BASH))
        stub.chmod(0o755)
        self.port = free_port()

    def remove_stub(self) -> None:
        (self.bin / "podman").unlink()

    def seed(self, status: str, mode: str | None = "persistent", port=None) -> None:
        """Pre-create a container; mode=None makes it look foreign (no labels)."""
        (self.state / "status").write_text(status + "\n")
        if mode is not None:
            (self.state / "mode").write_text(mode + "\n")
            (self.state / "port").write_text(f"{port or self.port}\n")

    @property
    def calls(self) -> list[str]:
        log = self.state / "log"
        return log.read_text().splitlines() if log.exists() else []

    def called(self, verb: str) -> list[str]:
        return [c for c in self.calls if c.split()[0] == verb]

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
    assert "usage: postgres.py [-h] COMMAND" in result.stdout
    for command in ("start", "stop", "status", "url", "psql", "reset", "destroy"):
        assert f"\n    {command} " in result.stdout
    assert "TRACKDIR_PG_READY_TIMEOUT" in result.stdout
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
    assert f"-p 127.0.0.1:{pg.port}:5432" in run
    assert "--label trackdir.mode=persistent" in run
    assert f"--label trackdir.port={pg.port}" in run
    assert "-v trackdir-pg-data:/var/lib/postgresql" in run
    assert "--rm" not in run
    assert "--tmpfs" not in run
    assert run.endswith(" docker.io/library/postgres:18")
    execs = pg.called("exec")
    assert any("pg_isready -q -h 127.0.0.1" in c for c in execs)
    assert any("CREATE DATABASE trackdir_test" in c for c in execs)


def test_start_ephemeral_uses_tmpfs_and_rm(pg):
    result = pg.run("start", "--ephemeral")
    assert result.returncode == 0, result.stderr
    (run,) = pg.called("run")
    assert "--label trackdir.mode=ephemeral" in run
    assert "--rm" in run.split()
    assert "--tmpfs /var/lib/postgresql" in run
    assert " -v " not in run


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
    # No create/start/stop, but readiness and the test database are still checked:
    # an earlier start may have timed out and left the container running.
    assert {c.split()[0] for c in pg.calls} == {"container", "exec"}
    execs = pg.called("exec")
    assert any("pg_isready" in c for c in execs)
    assert any("CREATE DATABASE trackdir_test" in c for c in execs)


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
    assert f"port {port} is already in use" in err
    assert "TRACKDIR_PG_PORT" in err
    assert pg.called("run") == []
    assert pg.called("start") == []


BAD_PORTS = ["abc", "0", "70000", "5432x", "", "08080", "18446744073709551617"]
# int() accepts these: a trailing newline, a space, non-ASCII digits.
INT_LOOKALIKES = ["5432\n", " 5432", "\u0665\u0664\u0663\u0662"]


@pytest.mark.parametrize("value", [*BAD_PORTS, *INT_LOOKALIKES])
def test_start_rejects_bad_port(pg, value):
    result = pg.run("start", env={"TRACKDIR_PG_PORT": value})
    assert result.returncode == 1
    assert f"got {value!r}" in collapse(result.stderr)
    assert pg.called("run") == []


@pytest.mark.parametrize(
    "value", ["soon", "", "-1", "1234567", "+8", " 8", "8_0", "\u0668"]
)
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
    assert not (pg.state / "status").exists()


def test_start_rejects_unknown_option(pg):
    result = pg.run("start", "--fast")
    assert result.returncode == 2
    assert "unrecognized arguments: --fast" in collapse(result.stderr)
    assert pg.calls == []


def test_stop_persistent_keeps_data(pg):
    pg.seed("running", mode="persistent")
    result = pg.run("stop")
    assert result.returncode == 0
    assert pg.called("stop") == ["stop trackdir-postgres"]
    assert "kept in volume trackdir-pg-data" in collapse(result.stderr)


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
    assert result.stdout == f"state=running\nmode=ephemeral\nport={pg.port}\n"
    assert result.stderr == ""


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


def test_url_without_container_uses_configured_port(pg):
    result = pg.run("url")
    assert result.returncode == 0
    dsn = f"postgresql://trackdir:trackdir@127.0.0.1:{pg.port}/trackdir"
    assert result.stdout == dsn + "\n"
    assert result.stderr == ""
    assert pg.called("run") == []


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


def test_psql_after_an_option_of_ours_is_a_usage_error(pg):
    pg.seed("running")
    result = pg.run("--bogus", "psql", "-c", "x")
    assert result.returncode == 2
    assert "unrecognized arguments" in collapse(result.stderr)
    assert pg.called("exec") == []


def test_psql_needs_a_running_server(pg):
    result = pg.run("psql")
    assert result.returncode == 1
    assert "is not running" in collapse(result.stderr)
    assert "start" in collapse(result.stderr)


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


def test_destroy_removes_container_and_volume(pg):
    pg.seed("running")
    result = pg.run("destroy")
    assert result.returncode == 0, result.stderr
    assert pg.called("rm") == ["rm -f -v trackdir-postgres"]
    assert pg.called("volume") == [
        "volume exists trackdir-pg-data",
        "volume rm trackdir-pg-data",  # no -f: -f would remove other users of it
    ]


def test_destroy_without_container_still_removes_volume(pg):
    result = pg.run("destroy")
    assert result.returncode == 0
    assert pg.called("rm") == []
    assert pg.called("volume") == [
        "volume exists trackdir-pg-data",
        "volume rm trackdir-pg-data",
    ]


def test_destroy_without_volume_removes_nothing_more(pg):
    (pg.state / "no_volume").touch()
    result = pg.run("destroy")
    assert result.returncode == 0, result.stderr
    assert pg.called("volume") == ["volume exists trackdir-pg-data"]


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
