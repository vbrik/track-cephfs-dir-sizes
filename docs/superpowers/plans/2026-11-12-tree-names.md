# Tree names — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every tree has a name chosen by the user, and a sample is stored only if the mount still says what the database recorded for that name, so a changed mount, a moved daemon or a wrong ROOT can neither split nor corrupt a history silently.

**Architecture:** `tree` gains a unique `name`; its four other columns stay as the recorded mount identity (`TreeKey`, unchanged). The database layer resolves a tree from a name and a mount identity by the four rules of the spec, before the walk (`load_baseline`, `latest_started`) and again under the tree lock (`store_sample`). `trackdir sample` takes `--tree NAME`, the daemon's config takes `name` per tree, and `db summary` shows and filters by name.

**Tech Stack:** Python ≥ 3.12 stdlib; psycopg ≥ 3.2 (optional extra); PostgreSQL ≥ 15; pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-schema-v5-design.md`, section 7 ("Tree names") and the "Tree names" and "Changed mount" rows of section 2. This is plan 2 of 3; the overrun rule (section 6) is plan 3.

## Global Constraints

- Read `AGENTS.md`, `AGENTS.md.d/`, `docs/conventions.md` and `docs/terms.md` first.
- Tests: `venv/bin/python -m pytest`. Database tests need `python3 scripts/postgres.py start --ephemeral` and `TRACKDIR_TEST_DSN=$(python3 scripts/postgres.py url --test)`; run every task's tests with it set, or they are skipped.
- `ruff format . && ruff check .` before proposing a commit.
- No branches, never push. A "Propose the commit" step shows the message and waits for the user's go-ahead. Commit bodies state the intention and the design choices (`AGENTS.md.d/GIT-v1.md`).
- The repository is public: no real cluster fsids, host names, IPs or real directory paths anywhere.
- Never write to `/mnt/ceph*`.
- `SCHEMA_VERSION` stays 5: version 5 is not in production until this plan lands, and there is no migration before production. Re-create any development database after Task 1 (`scripts/postgres.py reset`, then `trackdir db init`).
- A name is 1 to 64 characters of `A-Z a-z 0-9 . _ -`, starting with a letter or digit: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. Names are case-sensitive.
- `TreeKey` keeps its four fields and its meaning: the mount identity. A name never replaces it in a comparison.
- The four rules, verbatim from the spec:

  | The name is | The mount identity is | Result |
  |---|---|---|
  | unknown | unknown | The tree is created with this name and identity. |
  | unknown | recorded under another name | Refused, naming that tree. |
  | known | the one recorded for it | The sample goes on. |
  | known | different | Refused. The message shows the recorded identity and the mount's, field by field. |

- There is no flag or config key that permits a new tree, and no command that changes a recorded identity: `docs/schema.md` gives a one-row `UPDATE`.

## Review Focus

1. The name is known and only `fs_name` differs (NULL recorded, a name mounted, or the reverse): refused before the walk with a message that names `fs name` as the differing field; after the documented `UPDATE` the next sample is incremental and the tree id is unchanged (Task 1).
2. Two first samples race, with one name and two identities, or one identity and two names: exactly one tree row results and the loser gets the refusal of the rules, never an unhandled unique violation (Task 1).
3. Bad names: empty, 65 characters, a leading `-` or `.`, a space, a slash, non-ASCII; and `Proj` beside `proj`, which are two names. Rejected by `--tree` as a usage error (status 2) and by the config with the tree's index; the database's own `CHECK` rejects them too (Tasks 1 and 2).
4. A replayed capture (for example an anonymized one, whose identity is tokens) under the name of a live tree is refused by rule 4, and stores no tombstones (Task 1).
5. `db summary --tree 3` treats `3` as a name; an unknown name reports no tree and exits as an unknown id did before (Task 3). A mount that changes while the daemon runs stops it with status 1 at the next cycle, by the same refusal (Task 2).

---

### Task 1: Names in the schema, the database layer and `trackdir sample`

**Files:**
- Modify: `src/trackdir/db.py`, `src/trackdir/dbcli.py`, `src/trackdir/sample.py`, `src/trackdir/cli.py` (the `sample` subcommand only), `src/trackdir/daemon.py` and `src/trackdir/config.py` (only what keeps them working: see Step 6), `scripts/loadtest.py`
- Modify: `docs/schema.md`
- Test: `tests/conftest.py`, `tests/test_db.py`, `tests/test_sample.py`, `tests/test_incremental.py`, `tests/test_summary.py`, `tests/test_cli.py`, `tests/test_loadtest_script.py`

**Interfaces:**
- Produces:
  - `db.TREE_NAME: re.Pattern[str]` (the pattern above) and `db.valid_tree_name(name: str) -> bool`.
  - DDL: `tree.name text NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')`, placed after `id`; and `CREATE INDEX sample_tree_started ON trackdir.sample (tree_id, started, id)`, which serves `_LATEST_SAMPLE`.
  - `class TreeMismatch(DbError)` with `name: str`, `recorded: TreeKey`, `found: TreeKey`.
  - `class TreeNamedOtherwise(DbError)` with `name: str` (asked for), `other: str` (recorded), `key: TreeKey`.
  - `load_baseline(conn, name: str, key: TreeKey, *, max_dirs: int | None = None) -> Baseline`
  - `latest_started(conn, name: str, key: TreeKey) -> datetime | None`
  - `store_sample(conn, name: str, header, key, baseline, diff, replayed_from, baseline_seconds) -> int`
  - All three raise `TreeMismatch` or `TreeNamedOtherwise` by the rules; only `store_sample` creates a tree.
  - `SampleOptions.tree: str` (required, after `dsn`); `SampleResult.name: str`.
  - `conftest.name_of(header_or_key) -> str`: a valid name that is the same for equal mount identities and different otherwise, for tests that do not care about names.

- [ ] **Step 1: Give every existing test a name**

`tests/test_db.py`'s `store()` gets `name: str | None = None` and defaults to `name_of(header)`; `conftest.replay()` gets `tree: str | None = None` and defaults to `name_of` of the capture's header at the replayed depth. Calls of `load_baseline`, `latest_started` and `main(["sample", ...])` pass a name the same way. `test_depth_starts_a_new_tree` and `test_trees_differ_by_depth_fs_name_and_fs_path` keep passing through `name_of`; do not weaken them.

- [ ] **Step 2: Write the failing tests for the rules**

In `tests/test_db.py`, with `HEADER` and small hand-made probes:

```python
def test_new_name_and_new_identity_create_a_tree(db):
    # store(db, HEADER, name="projects"): tree row has name "projects" and HEADER's identity

def test_known_name_with_its_identity_continues_the_tree(db):
    # two stores under "projects": one tree row, the second sample is incremental

@pytest.mark.parametrize("change", [{"fs_name": "cephfs"}, {"fs_path": "/y"},
                                    {"max_depth": 2}, {"cluster_fsid": "other"}])
def test_known_name_with_another_identity_is_refused(db, change):
    # load_baseline, latest_started and store_sample each raise TreeMismatch with
    # .name == "projects", .recorded == tree_key_of(HEADER), .found == the changed key;
    # counts(db) unchanged

def test_new_name_for_a_recorded_identity_is_refused(db):
    # after store under "projects": the same HEADER under "other" raises
    # TreeNamedOtherwise(name="other", other="projects") from all three; counts unchanged

def test_unknown_tree_has_an_empty_baseline_and_no_latest_sample(db):
    # load_baseline == Baseline.empty(), latest_started is None, no tree row created

def test_documented_update_makes_a_name_stand_for_a_new_identity(db):
    # store under "projects" with fs_name None; run doc_sql("UPDATE trackdir.tree")
    # (it sets fs_name = 'cephfs' for 'projects'); a store with fs_name "cephfs"
    # is then incremental under the same tree id, and the old identity is refused

def test_racing_first_samples_make_one_tree(db):
    # as test_stale_baseline_is_refused_under_a_repeatable_read_default arranges
    # a wait: the rival inserts a tree row with the loser's name and another
    # identity and commits; the loser's store_sample raises TreeMismatch.
    # Second case: same identity under another name -> TreeNamedOtherwise.

@pytest.mark.parametrize("name", ["", "a" * 65, "-a", ".a", "a b", "a/b", "café"])
def test_the_database_rejects_a_malformed_name(db, name):
    # a direct INSERT INTO trackdir.tree raises CheckViolation; valid_tree_name(name) is False

def test_names_differing_in_case_are_two_trees(db):
    # "Proj" and "proj" with different identities: two tree rows
```

In `tests/test_sample.py`:

```python
def test_replay_under_the_name_of_another_tree_is_refused_before_walking(db, dsn, monkeypatch):
    # replay SUBTREE as "live"; then replay a capture with another fs_path as "live"
    # with replay_walk patched to must_not_walk: FatalError whose message contains
    # "fs path" and both paths; dir_state has no tombstones

def test_tree_mismatch_message_names_the_differing_field(...)   # fs name only
def test_tree_named_otherwise_message_names_the_other_tree(...)
def test_sample_requires_a_tree_name(capsys)        # main(["sample", ROOT, "--max-depth", "1"]) == 2
def test_sample_rejects_a_malformed_tree_name(capsys)   # --tree "a b" -> status 2, names the rule
```

- [ ] **Step 3: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_db.py tests/test_sample.py -q`
Expected: FAIL (`store_sample()` takes no `name`; no `TreeMismatch`).

- [ ] **Step 4: Implement the rules in `db.py`**

One private function does the resolution for all three callers:

```python
def _resolve_tree(cur, name: str, key: TreeKey, *, lock: bool, create: bool) -> int | None:
    """Return the id of tree NAME, checked against KEY by the four rules.

    None if neither NAME nor KEY is recorded and CREATE is false.
    """
```

It selects by name (`FOR UPDATE` when `lock`); a row whose identity differs from `key` raises `TreeMismatch`. Without a row it selects by identity (`_TREE_MATCH`); a row raises `TreeNamedOtherwise`. Without either and with `create`, it inserts with `ON CONFLICT DO NOTHING` and starts over once, so a concurrent insert of either unique key is judged by the same rules instead of surfacing as a unique violation. `_lock_tree` is replaced by it.

- [ ] **Step 5: Messages in `dbcli.py`**

`_db_message` gains two cases; both end with `NOTHING_STORED` when a sample was being stored and are worded for someone who has not read the spec:

- `TreeMismatch`: says the tree's name, which fields differ (`cluster`, `fs name`, `fs path`, `depth`, in `tree_identity`'s terms), the recorded and the found value of each, and the three ways out: fix ROOT or the mount; choose another `--tree` name for a different directory; or, for the same directory mounted differently, the section "Changed mount" of `docs/schema.md`.
- `TreeNamedOtherwise`: says the directory and depth are already tree `other`, and to use that name.

- [ ] **Step 6: Thread the name through `sample.py`, the CLI and the callers**

`_walk_live` and `_walk_replay` pass `opts.tree` to `_precheck`, which passes it to `load_baseline`; `run_sample` passes it to `store_sample` and returns it in `SampleResult.name`. `trackdir sample` gets `--tree NAME`, required, validated with `valid_tree_name` by an argparse type function. To keep the suite green before Task 2: `TreeConfig` gets `name: str`, `config._tree` reads a required string key `name` without further checks, and `daemon.py` passes it on. `scripts/loadtest.py` uses the name `loadtest`.

- [ ] **Step 7: `docs/schema.md`**

The `tree` table gains `name`; the reading rule "Pick a tree" says trees are picked by name; a new section "Changed mount" under "Access" explains when the identity may be changed by hand and gives:

```sql
UPDATE trackdir.tree SET fs_name = 'cephfs' WHERE name = 'projects';
```

with the warning that it is right only for the same directory, since the next sample compares against the stored state.

- [ ] **Step 8: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass.

- [ ] **Step 9: Propose the commit**

Subject: `db, sample: trees are named, and a name is checked against the mount`.

---

### Task 2: Names in the daemon's config and startup

**Files:**
- Modify: `src/trackdir/config.py`, `src/trackdir/daemon.py`, `src/trackdir/cli.py` (the daemon's `--help` epilog)
- Test: `tests/test_config.py`, `tests/test_daemon.py`

**Interfaces:**
- Consumes: `db.valid_tree_name`, `db.latest_started(conn, name, key)`, `TreeMismatch`, `TreeNamedOtherwise`, `SampleOptions.tree`.
- Produces: `TreeConfig.name: str`, first field; `config.load` rejects a missing, non-string or malformed `name` and two entries with one name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
def test_tree_name_is_required(...)            # "tree 1: name is required."
@pytest.mark.parametrize("name", ['""', '"a b"', '"-a"', "5", '"' + "a" * 65 + '"'])
def test_malformed_tree_name_is_rejected(...)  # names the key, the tree's index and the rule
def test_two_trees_with_one_name_are_rejected(...)  # names both indexes and the name

# tests/test_daemon.py
def test_startup_fails_when_a_name_stands_for_another_mount(...)
    # the injected inspection raises as latest_started would: one ERROR line with
    # the mismatch message, status 1, no cycle run
def test_mount_changed_between_cycles_stops_the_daemon(...)
    # the fake sampler raises the FatalError of a mismatch in cycle 2: status 1
def test_samples_are_taken_under_the_configured_names(...)
    # the fake sampler's recorded SampleOptions carry tree == each entry's name
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_config.py tests/test_daemon.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`config._tree` validates `name` with `valid_tree_name`; `config.load` rejects duplicates after building the trees. `daemon.inspect_trees` calls `latest_started(conn, tree.name, key)` for each tree, so rules 2 and 4 stop the daemon at startup through the existing `FatalError` path; `_check_distinct` on mount identities stays, since two new names for one directory are both unknown until the first is stored. The daemon's `--help` epilog lists `name` among the required keys and says that the name is checked against the mount at startup and at every sample.

- [ ] **Step 4: Run the whole suite**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Expected: all pass.

- [ ] **Step 5: Propose the commit**

Subject: `daemon: each configured tree has a name, checked at startup`.

---

### Task 3: Names in `db summary`, and the documentation

**Files:**
- Modify: `src/trackdir/db.py` (`TreeStats`, `summarize`), `src/trackdir/summary.py`, `src/trackdir/cli.py` (`db summary --tree`, the `sample` and `db summary` help texts)
- Modify: `README.md`, `docs/terms.md`, `docs/conventions.md`, `docs/superpowers/specs/2026-11-11-schema-v5-design.md`
- Test: `tests/test_summary.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `tree.name`.
- Produces: `TreeStats.name: str` (after `id`); `summarize(conn, tree: str | None = None, recent=10, top=3)`; text heading `Tree NAME (cluster …, depth N)`; JSON trees gain `"name"` and keep `"id"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_summary_names_each_tree(db, initial)            # tree.name == the replay's name
def test_tree_filter_takes_a_name(...)                   # summarize(db, tree="b") -> that tree only
def test_a_numeric_filter_is_a_name_not_an_id(...)       # tree named "3" beside tree id 3
def test_unknown_tree_name_reports_no_tree(...)          # tree_stats == (), database counts intact
def test_render_text_heads_a_tree_with_its_name(...)     # "Tree projects (cluster "
def test_render_json_has_the_tree_name(...)
def test_cli_tree_option_rejects_a_malformed_name(capsys)   # status 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest tests/test_summary.py tests/test_cli.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`summarize` resolves the name to an id with one query and keeps its queries on ids; an unknown name gives no tree statistics. `--tree` takes `NAME` with the same argparse type as `trackdir sample`.

- [ ] **Step 4: Documentation**

- `README.md`: the sample and daemon examples with `--tree` and `name`; the paragraph on tree identity rewritten around names, the four rules in one short table, and what to do after a mount change or a move; the `db summary --tree` example.
- `docs/terms.md`: "tree" (named; identified to the user by its name), new "tree name" and "mount identity", "fs name" (a difference is now refused, not a new tree).
- `docs/conventions.md`: a tree is resolved only by `db._resolve_tree`; no code compares names in place of mount identities.
- The spec: status line (sections 4, 5 and 7 implemented); section 7 notes that names are case-sensitive, that `SCHEMA_VERSION` stayed 5, and the `sample` index added here.
- `trackdir sample --help` and `trackdir db summary --help`.

- [ ] **Step 5: Run the whole suite and try it by hand**

Run: `venv/bin/python -m pytest -q && ruff format . && ruff check .`
Then, on a dev database: `trackdir db init`; replay `tests/data/ceph1-sub-depth4.jsonl.gz` as `--tree a`; replay `tests/data/ceph2-depth3.jsonl.gz` as `--tree a` and read the refusal as a user would; replay it as `--tree b`; `trackdir db summary --tree b`.
Expected: the refusal names the differing fields and the ways out; the summary shows tree `b` only.

- [ ] **Step 6: Propose the commit**

Subject: `summary, docs: show and select trees by name`.
