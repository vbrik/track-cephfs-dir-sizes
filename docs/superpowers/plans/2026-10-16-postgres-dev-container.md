# Local PostgreSQL Dev Container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One script, `scripts/postgres.sh`, that runs a disposable PostgreSQL
server in a rootless podman container, with tests that need no real podman.

**Architecture:** A single bash script with one `cmd_<name>` function per
subcommand and small shared helpers. Container state is read back from podman
(`container inspect`) and from two container labels (`trackdir.mode`,
`trackdir.port`), so the script keeps no state files. Tests run the script with
`PATH` limited to a temp directory that holds a stub `podman` (plus symlinks to
`cat`, `fold`, `rm`, `sleep`), so the real podman can never be reached.

**Tech Stack:** bash 5, podman (rootless), `postgres:18` image, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-16-postgres-dev-container-design.md`

## Global Constraints

- Script path: `scripts/postgres.sh`; bash with `set -euo pipefail`.
- Container `trackdir-postgres`, volume `trackdir-pg-data`; touch nothing else.
- Default image `docker.io/library/postgres:18` (fully qualified), override
  with `TRACKDIR_PG_IMAGE`; it must be an 18+ tag (data directory layout).
- Port published on `127.0.0.1` only; default `5432`, override with
  `TRACKDIR_PG_PORT`.
- User, password and database are all `trackdir`; second database
  `trackdir_test`.
- Persistent mode: volume mounted at `/var/lib/postgresql`. Ephemeral mode:
  `--rm` and `--tmpfs /var/lib/postgresql`.
- Mode is stored in label `trackdir.mode`; `start` never switches mode
  silently.
- Readiness is checked over TCP (`pg_isready -h 127.0.0.1`), up to 30 s.
- stdout carries results only (`url`, `status`, `--help`); notes go to stderr as
  one wrapped paragraph each, prefixed `ERROR:` or `WARNING:`.
- Tests need neither podman, network nor root, and must not touch the host's
  podman state.
- Project rules (`AGENTS.md`): do not create git commits and do not push; do
  not modify the host (no installs, no `sudo`); `ruff format` and `ruff check`
  must pass on Python files. `AGENTS.md.d/AGENTS-v5.md` already has uncommitted
  changes of the user's: leave them alone.

## Clarifications to the spec

These fill gaps the spec leaves open; none changes an approved behavior.

1. The port in use is stored in label `trackdir.port` at creation. `url`,
   `status` and restarts read it, so they report the real port even when
   `TRACKDIR_PG_PORT` later differs. `start` on an existing container warns
   when `TRACKDIR_PG_PORT` is set and differs.
2. `TRACKDIR_PG_READY_TIMEOUT` (seconds, default 30) overrides the readiness
   wait, so the timeout path is testable without a 30 s sleep.
3. A container named `trackdir-postgres` without label `trackdir.mode` was not
   made by this script; `start`, `stop`, `reset` and `destroy` refuse to touch
   it.
4. `podman run` prints the container ID on stdout; the script discards it.
5. `url` needs no running container: without one it prints the DSN for the
   configured port.

## Review Focus

Most likely first; each line is pinned by a test in the named task.

1. A foreign container called `trackdir-postgres` must not be stopped, removed
   or reused (Tasks 2, 3).
2. The container crashes during startup (e.g. a pre-18 image against the new
   mount): fail at once, not after the timeout (Task 2).
3. `TRACKDIR_PG_PORT` differs from the port of the existing container: `url`
   must print the real port, `start` must warn (Tasks 2, 3).
4. `podman run`'s container ID must not leak to stdout, which is for results
   (Task 2).
5. `psql` fed from a pipe must keep stdin attached (`-i`) and must not demand
   a tty (Task 3).

---

### Task 1: Test harness, script skeleton, help and dispatch

**Files:**
- Create: `scripts/postgres.sh`
- Create: `tests/test_postgres_script.py`

**Interfaces:**
- Produces (script): constants `PROG CONTAINER VOLUME DB_USER DB_PASSWORD
  DB_NAME TEST_DB_NAME DEFAULT_IMAGE DATA_MOUNT LABEL_MODE LABEL_PORT`;
  variables `IMAGE CONFIGURED_PORT READY_TIMEOUT`; functions `note`, `warn`,
  `die` (all take one message string; `die` exits 1), `usage`, `main`.
  `main` dispatches `start|stop|status|psql|url|reset|destroy` to
  `cmd_<name> "$@"` (later tasks define them).
- Produces (tests): fixture `pg` returning `FakePodman` with `.port` (free
  port as int), `.state` (Path), `.seed(status, mode="persistent", port=None)`,
  `.remove_stub()`, `.calls` (list of logged podman command lines),
  `.called(verb)` (calls whose first word is `verb`),
  `.run(*args, env=None, stdin=None)` returning `CompletedProcess[str]`
  (`env` values of `None` unset a variable); helper `collapse(text)`.

- [ ] **Step 1: Write the harness and the failing tests**

Create `tests/test_postgres_script.py`:

```python
"""Tests for scripts/postgres.sh, run against a stub podman (no real podman)."""

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "postgres.sh"
BASH = shutil.which("bash") or "/usr/bin/bash"
# The script runs with PATH limited to a directory holding the stub and links
# to these tools, so the host's real podman can never be reached.
HOST_TOOLS = ("cat", "fold", "rm", "sleep")

STUB = r"""#!@BASH@
# Fake podman: log every call; keep container state in files in $STUB_STATE.
# status: running|exited (absent file = no such container); mode, port: labels.
# Test switches: not_ready (pg_isready fails), crash (container exits instead),
# test_db (contains 1 when database trackdir_test exists).
set -u
state=$STUB_STATE
echo "$*" >> "$state/log"
read_file() { cat "$state/$1" 2>/dev/null || echo; }
case $1 in
    container)  # container inspect --format FORMAT NAME
        [[ -f $state/status ]] || { echo "Error: no such container" >&2; exit 125; }
        case $4 in
            *State.Status*) cat "$state/status" ;;
            *trackdir.mode*) read_file mode ;;
            *trackdir.port*) read_file port ;;
        esac ;;
    run)
        for arg in "$@"; do
            case $arg in
                trackdir.mode=*) echo "${arg#*=}" > "$state/mode" ;;
                trackdir.port=*) echo "${arg#*=}" > "$state/port" ;;
            esac
        done
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
    volume) ;;
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
            [BASH, str(SCRIPT), *args],
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
    assert "Usage: postgres.sh COMMAND" in result.stdout
    assert "start [--ephemeral]" in result.stdout
    assert result.stderr == ""


def test_no_command_prints_usage_to_stderr(pg):
    result = pg.run()
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr


def test_unknown_command_is_named(pg):
    result = pg.run("frobnicate")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "ERROR: unknown command 'frobnicate'" in collapse(result.stderr)


def test_missing_podman_is_reported(pg):
    pg.remove_stub()
    result = pg.run("status")
    assert result.returncode == 1
    assert "ERROR: podman is not installed" in collapse(result.stderr)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_postgres_script.py -v`
Expected: 4 FAILED (the script does not exist; bash exits 127).

- [ ] **Step 3: Write the skeleton**

Create `scripts/postgres.sh` (then `chmod +x scripts/postgres.sh`):

```bash
#!/usr/bin/env bash
# Manage a disposable PostgreSQL container for developing and testing trackdir.
# Run with --help for usage.
set -euo pipefail

readonly PROG=${0##*/}
readonly CONTAINER=trackdir-postgres
readonly VOLUME=trackdir-pg-data
readonly DB_USER=trackdir
readonly DB_PASSWORD=trackdir
readonly DB_NAME=trackdir
readonly TEST_DB_NAME=trackdir_test
readonly DEFAULT_IMAGE=docker.io/library/postgres:18
# PostgreSQL 18+ images keep data in /var/lib/postgresql/<major>/docker.
readonly DATA_MOUNT=/var/lib/postgresql
# Labels record how the container was made. They let the script refuse a
# container it did not create and never switch storage mode silently.
readonly LABEL_MODE=trackdir.mode
readonly LABEL_PORT=trackdir.port

IMAGE=${TRACKDIR_PG_IMAGE:-$DEFAULT_IMAGE}
# No colon: only an unset variable gets the default, so an empty value fails
# validation instead of silently turning into the default.
CONFIGURED_PORT=${TRACKDIR_PG_PORT-5432}
READY_TIMEOUT=${TRACKDIR_PG_READY_TIMEOUT-30}

# Messages go to stderr, one wrapped paragraph each.
note() { printf '%s\n' "$*" | fold -s -w 78 >&2; }
warn() { note "WARNING: $*"; }
die() {
    note "ERROR: $*"
    exit 1
}

usage() {
    cat <<EOF
Usage: $PROG COMMAND [ARGS]

Run a disposable PostgreSQL server in a rootless podman container, for
developing and testing trackdir. Only the container $CONTAINER and the
volume $VOLUME are touched.

Commands:
  start [--ephemeral]  Create or start the server and wait until it is ready.
                       Data is kept in volume $VOLUME; with --ephemeral
                       it lives in memory and vanishes on stop.
  stop                 Stop the server (ephemeral data is discarded).
  status               Print state=, mode= and port= lines to stdout; exit 0
                       only if the server is running.
  url [--test]         Print the connection string to stdout (--test: the
                       $TEST_DB_NAME database that tests may wipe).
  psql [ARGS...]       Run psql inside the container; ARGS go to psql.
  reset                Drop and recreate databases $DB_NAME and $TEST_DB_NAME.
  destroy              Remove the container and the volume (deletes all data).

Environment (applied when the container is created):
  TRACKDIR_PG_PORT           Port on 127.0.0.1 (default 5432).
  TRACKDIR_PG_IMAGE          PostgreSQL 18+ image (default $DEFAULT_IMAGE).
  TRACKDIR_PG_READY_TIMEOUT  Seconds to wait for readiness (default 30).

User, password and database are all "$DB_USER". The server listens on
127.0.0.1 only, but any local user can connect. Needs podman; the first
start pulls the image. Notes and errors go to stderr.
EOF
}

main() {
    local cmd=${1:-}
    case $cmd in
        -h | --help | help)
            usage
            return 0
            ;;
        '')
            usage >&2
            exit 2
            ;;
        start | stop | status | psql | url | reset | destroy) ;;
        *) die "unknown command '$cmd'. Commands: start, stop, status, psql, url, reset, destroy. Try: $PROG --help" ;;
    esac
    shift
    command -v podman >/dev/null || die "podman is not installed or not in PATH."
    "cmd_$cmd" "$@"
}

main "$@"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `bash -n scripts/postgres.sh && pytest tests/test_postgres_script.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint**

Run: `ruff format tests/test_postgres_script.py && ruff check tests/test_postgres_script.py`
Expected: no remaining findings. Do not commit (project rule).

---

### Task 2: `start` and `stop`

**Files:**
- Modify: `scripts/postgres.sh` (insert the new functions above `usage()`)
- Modify: `tests/test_postgres_script.py` (append the tests)

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces (script): `reject_extra_args CMD [ARG...]` (dies on any ARG),
  `container_state` (prints podman status or nothing), `container_label NAME`,
  `own_mode` (prints the container's mode; dies if it is foreign),
  `check_port VALUE`, `port_in_use PORT` (status 0 if something listens),
  `require_port_free PORT`, `admin_psql ARGS...` (psql as superuser in the
  `postgres` database, no stdin), `wait_ready`, `ensure_test_db`,
  `create_container MODE PORT`, `cmd_start`, `cmd_stop`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_postgres_script.py`:

```python
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


def test_start_on_running_container_is_a_no_op(pg):
    pg.seed("running")
    result = pg.run("start")
    assert result.returncode == 0
    assert "already running" in collapse(result.stderr)
    # Only read-only inspect calls: nothing is created, started or probed.
    assert {c.split()[0] for c in pg.calls} == {"container"}


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


@pytest.mark.parametrize("value", ["abc", "0", "70000", "5432x", ""])
def test_start_rejects_bad_port(pg, value):
    result = pg.run("start", env={"TRACKDIR_PG_PORT": value})
    assert result.returncode == 1
    assert f"got '{value}'" in collapse(result.stderr)
    assert pg.called("run") == []


def test_start_rejects_bad_timeout(pg):
    result = pg.run("start", env={"TRACKDIR_PG_READY_TIMEOUT": "soon"})
    assert result.returncode == 1
    assert "TRACKDIR_PG_READY_TIMEOUT" in collapse(result.stderr)
    assert pg.called("run") == []


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


def test_start_rejects_unknown_option(pg):
    result = pg.run("start", "--fast")
    assert result.returncode == 1
    assert "unknown option '--fast'" in collapse(result.stderr)
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
    assert result.returncode == 1
    assert "unexpected argument 'now'" in collapse(result.stderr)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_postgres_script.py -v`
Expected: the new tests FAIL (`cmd_start: command not found`, exit 127); the 4
Task 1 tests still pass.

- [ ] **Step 3: Implement the helpers, `cmd_start` and `cmd_stop`**

Insert above `usage()` in `scripts/postgres.sh`:

```bash
# Dies naming the first unexpected argument of command $1.
reject_extra_args() {
    local cmd=$1
    shift
    (($# == 0)) || die "unexpected argument '$1' for '$cmd'. Try: $PROG --help"
}

# Prints podman's status of the container (e.g. running, exited), or nothing
# when there is no such container.
container_state() {
    podman container inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true
}

# Prints the value of label $1 of the existing container (empty if unset).
container_label() {
    podman container inspect --format "{{index .Config.Labels \"$1\"}}" "$CONTAINER"
}

# Prints the storage mode of the existing container; dies if this script did
# not create it, so that we never stop or delete someone else's container.
own_mode() {
    local mode
    mode=$(container_label "$LABEL_MODE")
    [[ -n $mode ]] ||
        die "container $CONTAINER exists but was not created by this script (it has no $LABEL_MODE label). Remove or rename it yourself."
    printf '%s\n' "$mode"
}

check_port() {
    [[ $1 =~ ^[0-9]+$ ]] && (($1 >= 1 && $1 <= 65535)) ||
        die "TRACKDIR_PG_PORT must be a port number from 1 to 65535, got '$1'."
}

# Succeeds if something accepts connections on 127.0.0.1:$1.
port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

require_port_free() {
    ! port_in_use "$1" ||
        die "port $1 is already in use. Stop what listens there, or choose another port with TRACKDIR_PG_PORT=<port>."
}

# Runs psql as the superuser in the maintenance database; needed for DROP/CREATE
# DATABASE, which cannot target the database psql is connected to. No stdin.
admin_psql() {
    podman exec "$CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

# Waits until the server accepts TCP connections. A TCP check (not the unix
# socket) is needed: during first initialization the image runs a temporary
# server that listens on the socket only.
wait_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    until podman exec "$CONTAINER" pg_isready -q -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME"; do
        [[ $(container_state) == running ]] ||
            die "the container stopped while starting. Look at: podman logs $CONTAINER (ephemeral containers are removed when they exit)."
        ((SECONDS < deadline)) ||
            die "PostgreSQL did not become ready within $READY_TIMEOUT s. Look at: podman logs $CONTAINER"
        sleep 1
    done
}

# The image creates $DB_NAME itself; $TEST_DB_NAME we create (idempotently).
ensure_test_db() {
    local exists
    exists=$(admin_psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$TEST_DB_NAME'")
    [[ $exists == 1 ]] || admin_psql -c "CREATE DATABASE $TEST_DB_NAME" >/dev/null
}

# create_container MODE PORT: MODE is persistent or ephemeral.
create_container() {
    local mode=$1 port=$2
    local -a storage
    if [[ $mode == ephemeral ]]; then
        storage=(--rm --tmpfs "$DATA_MOUNT")
    else
        storage=(-v "$VOLUME:$DATA_MOUNT")
    fi
    note "Creating $CONTAINER from $IMAGE in $mode mode on 127.0.0.1:$port (the first run pulls the image)."
    podman run -d --name "$CONTAINER" \
        --label "$LABEL_MODE=$mode" --label "$LABEL_PORT=$port" \
        -p "127.0.0.1:$port:5432" \
        -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$DB_PASSWORD" \
        -e POSTGRES_DB="$DB_NAME" \
        "${storage[@]}" "$IMAGE" >/dev/null ||
        die "podman could not create the container; see its message above."
}

cmd_start() {
    local mode=persistent
    while (($#)); do
        case $1 in
            --ephemeral) mode=ephemeral ;;
            *) die "unknown option '$1' for 'start'. Usage: $PROG start [--ephemeral]" ;;
        esac
        shift
    done
    [[ $READY_TIMEOUT =~ ^[0-9]+$ ]] ||
        die "TRACKDIR_PG_READY_TIMEOUT must be a number of seconds, got '$READY_TIMEOUT'."

    local state port have_mode
    state=$(container_state)
    if [[ -n $state ]]; then
        have_mode=$(own_mode)
        [[ $have_mode == "$mode" ]] ||
            die "container $CONTAINER exists in $have_mode mode. Run '$PROG destroy' (this deletes its data) before starting it in $mode mode."
        port=$(container_label "$LABEL_PORT")
        if [[ -n ${TRACKDIR_PG_PORT:-} && $TRACKDIR_PG_PORT != "$port" ]]; then
            warn "container $CONTAINER already uses port $port; ignoring TRACKDIR_PG_PORT=$TRACKDIR_PG_PORT. Run '$PROG destroy' (this deletes its data) to change the port."
        fi
        if [[ $state == running ]]; then
            note "$CONTAINER is already running ($mode mode, port $port)."
            return 0
        fi
        require_port_free "$port"
        podman start "$CONTAINER" >/dev/null
    else
        port=$CONFIGURED_PORT
        check_port "$port"
        require_port_free "$port"
        create_container "$mode" "$port"
    fi
    wait_ready
    ensure_test_db
    note "PostgreSQL is ready on 127.0.0.1:$port ($mode mode). Connection string: $PROG url"
}

cmd_stop() {
    reject_extra_args stop "$@"
    local mode
    if [[ -z $(container_state) ]]; then
        note "$CONTAINER does not exist; nothing to stop."
        return 0
    fi
    mode=$(own_mode)
    podman stop "$CONTAINER" >/dev/null
    if [[ $mode == ephemeral ]]; then
        note "Stopped $CONTAINER; its ephemeral data is gone."
    else
        note "Stopped $CONTAINER; data is kept in volume $VOLUME."
    fi
}
```

Note for the implementer: in `cmd_start` the `-n ${TRACKDIR_PG_PORT:-}` test is
deliberately `:-`: an empty variable there means "not asking for a port". (The
empty-value case of `test_start_rejects_bad_port` is rejected by `check_port`
because the skeleton's defaults use `-`, not `:-`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `bash -n scripts/postgres.sh && pytest tests/test_postgres_script.py -v`
Expected: all pass. If `test_start_on_running_container_is_a_no_op` fails on
the exact call list, fix the script (it must read state, mode and port only),
not the expectation, unless the spec's "no-op" is clearly met by an extra
read-only `inspect`.

- [ ] **Step 5: Lint**

Run: `ruff format tests/test_postgres_script.py && ruff check tests/test_postgres_script.py`
Expected: clean. Do not commit.

---

### Task 3: `status`, `url`, `psql`, `reset`, `destroy`

**Files:**
- Modify: `scripts/postgres.sh` (insert the new functions above `usage()`)
- Modify: `tests/test_postgres_script.py` (append the tests)

**Interfaces:**
- Consumes: Task 2's `reject_extra_args`, `container_state`, `container_label`,
  `own_mode`, `check_port`, `admin_psql`.
- Produces (script): `dsn DATABASE PORT` (prints the DSN), `require_running`,
  `cmd_status`, `cmd_url`, `cmd_psql`, `cmd_reset`, `cmd_destroy`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_postgres_script.py`:

```python
def test_status_running(pg):
    pg.seed("running", mode="ephemeral")
    result = pg.run("status")
    assert result.returncode == 0
    assert result.stdout == f"state=running\nmode=ephemeral\nport={pg.port}\n"
    assert result.stderr == ""


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
    assert result.returncode == 1
    assert "unknown option '--prod'" in collapse(result.stderr)


def test_psql_passes_arguments_and_keeps_stdin(pg):
    pg.seed("running")
    result = pg.run("psql", "-c", "select 1", stdin="select 2;\n")
    assert result.returncode == 0, result.stderr
    # No tty here, so -i without -t; the stub's exec call is logged verbatim.
    assert pg.called("exec") == [
        "exec -i trackdir-postgres psql -U trackdir -d trackdir -c select 1"
    ]


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
    assert pg.called("volume") == ["volume rm -f trackdir-pg-data"]


def test_destroy_without_container_still_removes_volume(pg):
    result = pg.run("destroy")
    assert result.returncode == 0
    assert pg.called("rm") == []
    assert pg.called("volume") == ["volume rm -f trackdir-pg-data"]


def test_destroy_refuses_foreign_container(pg):
    pg.seed("running", mode=None)
    result = pg.run("destroy")
    assert result.returncode == 1
    assert pg.called("rm") == []
    assert pg.called("volume") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_postgres_script.py -v`
Expected: the new tests FAIL (`cmd_status: command not found` etc.); earlier
tests still pass.

- [ ] **Step 3: Implement the five commands**

Insert above `usage()` in `scripts/postgres.sh`:

```bash
# dsn DATABASE PORT: prints the connection string.
dsn() {
    printf 'postgresql://%s:%s@127.0.0.1:%s/%s\n' "$DB_USER" "$DB_PASSWORD" "$2" "$1"
}

require_running() {
    [[ $(container_state) == running ]] ||
        die "$CONTAINER is not running. Start it with: $PROG start"
    own_mode >/dev/null
}

cmd_status() {
    reject_extra_args status "$@"
    local state
    state=$(container_state)
    if [[ -z $state ]]; then
        echo state=absent
        return 1
    fi
    printf 'state=%s\nmode=%s\nport=%s\n' \
        "$state" "$(own_mode)" "$(container_label "$LABEL_PORT")"
    [[ $state == running ]]
}

cmd_url() {
    local db=$DB_NAME port
    while (($#)); do
        case $1 in
            --test) db=$TEST_DB_NAME ;;
            *) die "unknown option '$1' for 'url'. Usage: $PROG url [--test]" ;;
        esac
        shift
    done
    if [[ -n $(container_state) ]]; then
        own_mode >/dev/null
        port=$(container_label "$LABEL_PORT")
    else
        port=$CONFIGURED_PORT
        check_port "$port"
    fi
    dsn "$db" "$port"
}

cmd_psql() {
    require_running
    local -a flags=(-i)
    if [[ -t 0 && -t 1 ]]; then
        flags=(-it)
    fi
    exec podman exec "${flags[@]}" "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"
}

cmd_reset() {
    reject_extra_args reset "$@"
    require_running
    # Separate -c options run as separate commands; DROP/CREATE DATABASE cannot
    # run inside one transaction block.
    admin_psql \
        -c "DROP DATABASE IF EXISTS $DB_NAME WITH (FORCE)" \
        -c "CREATE DATABASE $DB_NAME" \
        -c "DROP DATABASE IF EXISTS $TEST_DB_NAME WITH (FORCE)" \
        -c "CREATE DATABASE $TEST_DB_NAME" >/dev/null
    note "Recreated databases $DB_NAME and $TEST_DB_NAME."
}

cmd_destroy() {
    reject_extra_args destroy "$@"
    if [[ -n $(container_state) ]]; then
        own_mode >/dev/null
        podman rm -f -v "$CONTAINER" >/dev/null
    fi
    podman volume rm -f "$VOLUME" >/dev/null
    note "Removed container $CONTAINER and volume $VOLUME."
}
```

Notes for the implementer:
- `test_psql_passes_arguments_and_keeps_stdin` expects the exact log line; the
  stub joins arguments with spaces, so `"select 1"` appears as `select 1`.
- The stub's `exec` with no matching pattern returns 0 and prints nothing,
  which is what `reset` needs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `bash -n scripts/postgres.sh && pytest tests/test_postgres_script.py -v`
Expected: all pass.

- [ ] **Step 5: Lint**

Run: `ruff format tests/test_postgres_script.py && ruff check tests/test_postgres_script.py`
Expected: clean. Do not commit.

---

### Task 4: Documentation, smoke test against real podman, final checks

**Files:**
- Modify: `README.md` (new subsection at the end of "## Development")
- Modify: `docs/conventions.md` (append one bullet)

- [ ] **Step 1: Document**

In `README.md`, after the paragraph "Tests need neither Ceph nor root; real
data comes from anonymized captures in `tests/data/`." add:

````markdown
### Development database

`scripts/postgres.sh` runs a disposable PostgreSQL 18 server in a rootless
podman container, for developing and testing trackdir (needs podman; the first
start pulls the image):

    scripts/postgres.sh start              # data kept in volume trackdir-pg-data
    scripts/postgres.sh start --ephemeral  # data in memory, gone after stop
    scripts/postgres.sh url                # postgresql://trackdir:trackdir@127.0.0.1:5432/trackdir
    scripts/postgres.sh psql               # psql inside the container
    scripts/postgres.sh reset              # recreate databases trackdir and trackdir_test
    scripts/postgres.sh stop
    scripts/postgres.sh destroy            # remove the container and the volume

Start creates two databases: `trackdir` for development and `trackdir_test`,
which tests may wipe (`url --test` prints its connection string). The server
listens on 127.0.0.1 only, with fixed credentials (user, password and database
are all `trackdir`); any local user on the machine can connect, so keep real
data out of it. `TRACKDIR_PG_PORT` (default 5432), `TRACKDIR_PG_IMAGE` (a
PostgreSQL 18 or newer image) and `TRACKDIR_PG_READY_TIMEOUT` (seconds, default
30) apply when the container is created; run `destroy` first to change them.
The script touches only the container `trackdir-postgres` and the volume
`trackdir-pg-data`.
````

Append to `docs/conventions.md`:

```markdown
- The development database is the podman container `trackdir-postgres`,
  managed by `scripts/postgres.sh`; the existing tests do not need it.
  `tests/test_postgres_script.py` tests the script against a stub `podman`.
```

- [ ] **Step 2: Run the whole suite and linters**

Run: `pytest -q && ruff format --check . && ruff check . && bash -n scripts/postgres.sh`
Expected: 185 earlier tests plus the new ones pass; ruff clean (it ignores
`*.md`).

- [ ] **Step 3: Smoke test against real podman**

This pulls `postgres:18` (about 150 MB) and creates only `trackdir-*` objects.
First record the baseline: `podman ps -a --format '{{.Names}}' | sort > "$SCRATCH/ps.before"`
and the same for `podman volume ls -q` (`$SCRATCH` = the session scratchpad).
Then run, checking each expectation:

    scripts/postgres.sh start                       # ready message; stdout empty
    scripts/postgres.sh status                      # state=running mode=persistent port=5432
    scripts/postgres.sh url; scripts/postgres.sh url --test
    scripts/postgres.sh psql -c 'select version()'  # PostgreSQL 18.x
    echo 'select 1' | scripts/postgres.sh psql      # piped stdin works
    scripts/postgres.sh psql -c 'create table keep(i int)'
    scripts/postgres.sh psql -d trackdir_test -c 'create table wipe(i int)'
    scripts/postgres.sh start --ephemeral           # ERROR: exists in persistent mode
    scripts/postgres.sh stop
    scripts/postgres.sh start                       # restart; table "keep" survives
    scripts/postgres.sh psql -c '\dt'
    scripts/postgres.sh reset                       # both tables gone
    scripts/postgres.sh psql -c '\dt'
    scripts/postgres.sh destroy
    podman volume ls -q | grep trackdir || echo "volume gone"
    scripts/postgres.sh start --ephemeral
    scripts/postgres.sh psql -c 'create table gone(i int)'
    scripts/postgres.sh stop                        # says ephemeral data is gone
    podman ps -a --format '{{.Names}}' | grep trackdir || echo "container gone"
    scripts/postgres.sh start --ephemeral           # fresh server: table "gone" absent
    scripts/postgres.sh psql -c '\dt'
    scripts/postgres.sh destroy
    TRACKDIR_PG_PORT=15432 scripts/postgres.sh start && scripts/postgres.sh url
    scripts/postgres.sh destroy

Finally compare `podman ps -a` and `podman volume ls -q` with the baseline: they
must be identical. If anything fails, fix the script and the matching test, not
just the script.

Known risk (spec section 6): if `--ephemeral` fails because tmpfs ownership is
wrong for the postgres user under rootless podman, change `storage=(--rm --tmpfs
"$DATA_MOUNT")` in `create_container` to `storage=(--rm)` (the image's
anonymous volume is removed together with the `--rm` container), update
`test_start_ephemeral_uses_tmpfs_and_rm` and the spec (sections 3 and 6), and
tell the user.

- [ ] **Step 4: Report**

Do not commit. Tell the user which files changed, the smoke-test results (or
what failed), and offer this commit message for them to use:

```text
Add scripts/postgres.sh: disposable PostgreSQL container for dev and tests

Intent: give the planned `trackdir sample` and its tests a real PostgreSQL
without installing anything on the host; there is no production database yet,
so the image tracks the current major (18).

Design choices:
- One script with subcommands (start/stop/status/url/psql/reset/destroy)
  instead of several scripts, so names, ports and credentials live in one
  place.
- Persistent (named volume) and ephemeral (--rm + tmpfs) modes, recorded in a
  container label; start refuses to switch modes silently because that would
  hide or destroy data.
- The port is a label too, so url/status report what the container really
  uses even if TRACKDIR_PG_PORT changed later.
- Readiness uses pg_isready over TCP: the image's init phase runs a temporary
  socket-only server, so a socket check reports ready too early.
- A container named trackdir-postgres without our label is never touched.
- Tests run the script against a stub podman on a PATH limited to a temp
  directory, so they need no podman and cannot reach the host's podman state.
- TRACKDIR_PG_READY_TIMEOUT exists so the timeout path is testable in
  milliseconds.
```
