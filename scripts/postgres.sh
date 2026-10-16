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
    [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && (($1 <= 65535)) ||
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
    local deadline=$((SECONDS + 10#$READY_TIMEOUT)) # 10#: "08" is not octal
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
        "${storage[@]}" "$IMAGE" >/dev/null || {
        # A failed run (e.g. the port cannot be bound) leaves a Created container
        # that keeps the bad port and would trap the next start; remove it.
        if [[ $(container_label "$LABEL_MODE" 2>/dev/null) == "$mode" ]]; then
            podman rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
        fi
        die "podman could not create the container; see its message above."
    }
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
    [[ $READY_TIMEOUT =~ ^[0-9]{1,6}$ ]] ||
        die "TRACKDIR_PG_READY_TIMEOUT must be a number of seconds (up to 6 digits), got '$READY_TIMEOUT'."

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
            # An earlier start may have timed out and left it running.
            note "$CONTAINER is already running ($mode mode, port $port)."
        else
            require_port_free "$port"
            podman start "$CONTAINER" >/dev/null
        fi
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
    local state mode port
    state=$(container_state)
    if [[ -z $state ]]; then
        echo state=absent
        return 1
    fi
    mode=$(own_mode)
    port=$(container_label "$LABEL_PORT")
    printf 'state=%s\nmode=%s\nport=%s\n' "$state" "$mode" "$port"
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
    # No -f: it would also remove any other container that mounts the volume.
    if podman volume exists "$VOLUME"; then
        podman volume rm "$VOLUME" >/dev/null ||
            die "could not remove volume $VOLUME; another container may be using it (see podman's message above)."
    fi
    note "Removed container $CONTAINER and volume $VOLUME."
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

Environment:
  TRACKDIR_PG_PORT           Port on 127.0.0.1 (default 5432); used when the
                             container is created.
  TRACKDIR_PG_IMAGE          PostgreSQL 18+ image (default $DEFAULT_IMAGE);
                             used when the container is created.
  TRACKDIR_PG_READY_TIMEOUT  Seconds start waits for readiness (default 30).

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
