# Local PostgreSQL container for development and tests: design

Date: 2026-10-16. Status: implemented. Amended after the final review: `start` on a running
container still checks readiness; `psql` needs both stdin and stdout to be
ttys; `destroy` never force-removes other containers; a failed `podman run`
leaves no container behind; port and timeout are validated strictly.

## 1. Purpose

Give developers a disposable PostgreSQL server, run by rootless podman, for
developing the planned `trackdir sample` and for tests that need a real
database. There is no production database yet, so nothing here must match a
deployed version.

In scope: one script, `scripts/postgres.sh`, its tests and its docs.

Out of scope: the sampler's schema, installing `psycopg` into the virtual
environment (needs the user's approval when the sampler starts), any change
to the `trackdir` package, and any use of the container by the existing
tests (they stay free of podman, Ceph and root).

## 2. Interface

`scripts/postgres.sh COMMAND [ARGS]`, bash with `set -euo pipefail`.

| Command | Behavior |
|---|---|
| `start [--ephemeral]` | Create or start the container, wait until it accepts TCP connections, ensure databases `trackdir` and `trackdir_test` exist. |
| `stop` | Stop the container. In ephemeral mode, say that the data is gone. |
| `status` | Print state, mode and port. Exit 0 if running, 1 if not. |
| `psql [ARGS...]` | Run `psql` inside the container as user `trackdir` on database `trackdir`; pass ARGS through. Allocate a tty only if both stdin and stdout are ttys. |
| `url [--test]` | Print the DSN. `--test` selects `trackdir_test`. |
| `reset` | Drop and recreate both databases. Container and volume stay. |
| `destroy` | Remove the container and the volume. |

DSN: `postgresql://trackdir:trackdir@127.0.0.1:PORT/DATABASE`.

Configuration through the environment:

- `TRACKDIR_PG_PORT`: host port, default `5432`.
- `TRACKDIR_PG_IMAGE`: default `docker.io/library/postgres:18`. Must be an
  18+ tag (see section 3). The name is fully qualified so podman never
  prompts for a registry.

## 3. Behavior

- **Names.** Container `trackdir-postgres`, volume `trackdir-pg-data`. The
  script touches nothing else, so unrelated containers and volumes on the
  host are safe.
- **Modes.** Persistent (default) mounts the named volume at
  `/var/lib/postgresql`. PostgreSQL 18 images keep data in
  `/var/lib/postgresql/18/docker`, hence the mount point. Ephemeral
  (`--ephemeral`) uses `--rm` and a tmpfs data directory, so nothing is
  left behind after `stop`.
- **Mode label.** The container carries label `trackdir.mode`. `start` on a
  running container creates and restarts nothing (note on stderr), but still
  waits for readiness and ensures `trackdir_test`, so a start that timed out
  earlier can be repeated safely. `start` in a mode
  different from the existing container's fails with `ERROR:` and tells the
  user to run `destroy` first; the mode never changes silently.
- **Binding.** The port is published on `127.0.0.1` only. Credentials are
  fixed development values (user, password and database all `trackdir`). On
  a shared host any local user can reach the loopback port; the README says
  so.
- **Readiness.** Poll `pg_isready -h 127.0.0.1` inside the container for up
  to 30 s, then fail with `ERROR:`. The TCP check matters: during first
  initialization the image runs a temporary server that listens only on the
  unix socket, so a socket check can report ready too early.
- **Databases.** After readiness `start` runs an idempotent
  `CREATE DATABASE trackdir_test` if it is missing; `reset` reuses the same
  helper. `trackdir_test` lets tests wipe data without touching development
  data.
- **Port clash.** If the port is taken, `start` fails early with `ERROR:`
  naming `TRACKDIR_PG_PORT`.
- **Output.** Only `url` and `status` write to stdout. Notes, warnings and
  errors go to stderr as one wrapped paragraph each, prefixed `ERROR:` or
  `WARNING:` (see `docs/conventions.md`). Unknown commands and options are
  named in the error.

## 4. Testing

- `tests/test_postgres_script.py` puts a stub `podman` first on `PATH`. The
  stub records its arguments and answers from canned state, so the tests
  need no podman and no network. They check: the commands built for each
  mode, the mode-mismatch error, the running-container `start`, the `url` output, the
  stdout/stderr split, the port override, and unknown-command errors.
  Assertions collapse whitespace so wrapping does not matter.
- One manual smoke run against real podman before declaring the work done
  (pulls `postgres:18`, uses only `trackdir-*` names): start in each mode,
  connect, `reset`, `stop`, `destroy`.
- The tests must not touch the host's real podman state.

## 5. Documentation

- README.md: a "Development database" section (commands, environment
  variables, the loopback note), under Development.
- `docs/conventions.md`: one line saying the dev database lives in
  container `trackdir-postgres` and that the existing tests do not need it.
- `docs/terms.md`: no change.

## 6. Risks and open items

- Rootless podman on the target host may lack tmpfs ownership support for
  the postgres uid; the smoke run verifies ephemeral mode. If it fails, fall
  back to `--rm` with an anonymous volume.
- Overriding `TRACKDIR_PG_IMAGE` with a pre-18 tag breaks persistence
  (different data directory). The README states the 18+ requirement; the
  script does not check it.
- How tests will opt in (`TRACKDIR_TEST_DSN`, skip when unset) is decided
  with the sampler spec, not here.
