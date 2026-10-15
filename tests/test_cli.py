import os
import signal
import time
from collections import Counter
from pathlib import Path

import pytest

from trackdir import cli
from trackdir.anonymize import key_recipe
from trackdir.capture import load_capture
from trackdir.cli import (
    CaptureOptions,
    CaptureResult,
    FatalError,
    main,
    render_summary,
    run_capture,
)
from trackdir.model import RStats
from trackdir.rstats import RStatMode


def collapse(text: str) -> str:
    return " ".join(text.split())


def must_not_walk(*args, **kwargs):
    pytest.fail("walked although an earlier check should have failed")


@pytest.fixture
def fake_ceph(monkeypatch):
    """Make local directories look like CephFS mounted with dirstat."""
    monkeypatch.setattr(cli, "detect_mode", lambda fd: RStatMode.DIRSTAT)
    monkeypatch.setattr(cli, "cluster_fsid", lambda fd: "fsid")
    monkeypatch.setattr(
        cli, "read_rstats", lambda fd, mode: RStats(1_000_000_000, 10, 2, 1)
    )


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "z").mkdir()
    return root


def options(tree, tmp_path, **changes):
    fields = {"root": tree, "max_depth": 2, "output": tmp_path / "out.jsonl.gz"}
    return CaptureOptions(**(fields | changes))


def test_capture_writes_the_file_and_reports_counts(tree, tmp_path, fake_ceph):
    warned = []
    result = run_capture(options(tree, tmp_path), warned.append)
    capture = load_capture(tmp_path / "out.jsonl.gz")
    paths = [p.path for p in capture.probes.values()]
    assert paths == [b".", b"a", b"a/b", b"z"]
    assert capture.header.root == str(tree.resolve())
    assert capture.header.max_depth == 2
    assert capture.header.dirs == 4
    assert capture.header.rstat_mode == "dirstat"
    assert capture.header.cluster_fsid == "fsid"
    assert not capture.header.anonymized
    assert result.dirs == 4
    assert not result.errors
    assert warned == []


def test_anonymized_capture(tree, tmp_path, fake_ceph):
    key = tmp_path / "k.key"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o600)
    result = run_capture(options(tree, tmp_path, key_file=key), print)
    capture = load_capture(tmp_path / "out.jsonl.gz")
    assert result.anonymized
    assert capture.header.anonymized
    assert capture.header.host is None
    names = [n for p in capture.probes if p != b"." for n in p.split(b"/")]
    assert names
    assert all(len(name) == 16 for name in names)


def test_summary_text(tree, tmp_path, fake_ceph):
    result = run_capture(options(tree, tmp_path), print)
    expected = (
        f"Captured 4 directories under {tree.resolve()} to depth 2 into "
        f"{tmp_path / 'out.jsonl.gz'} in "
    )
    assert collapse(render_summary(result)).startswith(expected)
    assert collapse(render_summary(result)).endswith("s; none had errors.")


def test_summary_lists_errors_by_stage_and_code():
    result = CaptureResult(
        root="/r",
        output=Path("o"),
        max_depth=1,
        dirs=5,
        errors=Counter({("open", "ENOENT"): 1, ("open", "EACCES"): 2}),
        elapsed=2.0,
        rstat_mode=RStatMode.DIRSTAT,
        openat2=True,
        anonymized=False,
    )
    assert collapse(render_summary(result)) == (
        "Captured 5 directories under /r to depth 1 into o in 2.0 s; "
        "3 had errors: open EACCES 2, open ENOENT 1."
    )


def test_existing_output_is_refused_before_walking(tree, tmp_path, monkeypatch):
    (tmp_path / "out.jsonl.gz").write_bytes(b"x")
    monkeypatch.setattr(cli, "walk", must_not_walk)
    with pytest.raises(FatalError, match="already exists"):
        run_capture(options(tree, tmp_path), print)


def test_output_inside_root_is_refused_with_relative_paths(tree, monkeypatch):
    monkeypatch.chdir(tree.parent)
    opts = CaptureOptions(
        root=Path("root/"), max_depth=1, output=Path("root/a/out.jsonl.gz")
    )
    with pytest.raises(FatalError, match="inside ROOT"):
        run_capture(opts, print)
    assert sorted(os.listdir(tree / "a")) == ["b"]


def test_missing_output_directory_fails_before_walking(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "walk", must_not_walk)
    with pytest.raises(FatalError, match="does not exist"):
        run_capture(options(tree, tmp_path, output=tmp_path / "no" / "o.gz"), print)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")
def test_unsearchable_output_directory_reports_why(tree, tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    locked.chmod(0)
    monkeypatch.setattr(cli, "walk", must_not_walk)
    try:
        with pytest.raises(FatalError, match="Permission denied"):
            output = locked / "sub" / "o.gz"
            run_capture(options(tree, tmp_path, output=output), print)
    finally:
        locked.chmod(0o700)


def test_output_directory_that_is_a_file_is_refused(tree, tmp_path, monkeypatch):
    (tmp_path / "file").touch()
    monkeypatch.setattr(cli, "walk", must_not_walk)
    with pytest.raises(FatalError, match="not a directory"):
        run_capture(options(tree, tmp_path, output=tmp_path / "file" / "o.gz"), print)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_output_directory_fails_before_walking(
    tree, tmp_path, fake_ceph, monkeypatch
):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setattr(cli, "walk", must_not_walk)
    try:
        with pytest.raises(FatalError, match="cannot create"):
            run_capture(options(tree, tmp_path, output=readonly / "o.gz"), print)
    finally:
        readonly.chmod(0o700)


def test_short_key_is_refused(tree, tmp_path):
    key = tmp_path / "short.key"
    key.write_bytes(b"x" * 31)
    key.chmod(0o600)
    with pytest.raises(FatalError, match="has 31 bytes"):
        run_capture(options(tree, tmp_path, key_file=key), print)


def test_key_readable_by_others_is_refused(tree, tmp_path):
    key = tmp_path / "open.key"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o644)
    with pytest.raises(FatalError, match="chmod 600"):
        run_capture(options(tree, tmp_path, key_file=key), print)


def test_help_shows_a_private_key_recipe(capsys):
    with pytest.raises(SystemExit):
        main(["capture", "--help"])
    assert key_recipe("trackdir.key") in capsys.readouterr().out


def test_symlink_root_is_refused(tree, tmp_path, fake_ceph):
    link = tmp_path / "link"
    link.symlink_to(tree)
    with pytest.raises(FatalError, match="cannot open ROOT"):
        run_capture(options(link, tmp_path), print)


def test_local_directory_is_not_cephfs(tree, tmp_path):
    with pytest.raises(FatalError, match="not on CephFS"):
        run_capture(options(tree, tmp_path), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_dir_limit_leaves_nothing(tree, tmp_path, fake_ceph):
    with pytest.raises(FatalError, match="more than 2 directories"):
        run_capture(options(tree, tmp_path, max_dirs=2), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_interrupt_leaves_nothing(tree, tmp_path, fake_ceph, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "walk", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_capture(options(tree, tmp_path), print)
    assert sorted(os.listdir(tmp_path)) == ["root"]


def test_missing_openat2_warns_when_not_root(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "openat2_supported", lambda: False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    warned = []
    run_capture(options(tree, tmp_path), warned.append)
    assert len(warned) == 1
    assert "openat2" in warned[0]


def test_missing_openat2_is_fatal_for_root(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "openat2_supported", lambda: False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(FatalError, match="openat2"):
        run_capture(options(tree, tmp_path), print)


def test_getxattr_mode_warns(tree, tmp_path, fake_ceph, monkeypatch):
    monkeypatch.setattr(cli, "detect_mode", lambda fd: RStatMode.GETXATTR)
    warned = []
    run_capture(options(tree, tmp_path), warned.append)
    assert len(warned) == 1
    assert "dirstat" in warned[0]


def test_main_success(tree, tmp_path, fake_ceph, capsys):
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 0
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert collapse(stderr).startswith("Captured 3 directories")


def test_main_error(tree, tmp_path, capsys):
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 1
    assert collapse(capsys.readouterr().err).startswith("ERROR: ROOT")


def test_main_interrupted(tree, tmp_path, fake_ceph, monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "walk", interrupted)
    out = tmp_path / "o.jsonl.gz"
    assert main(["capture", str(tree), "--max-depth", "1", "-o", str(out)]) == 130
    assert "interrupted" in capsys.readouterr().err


class UnhandledSignal(BaseException):
    """Raised by the test's guard handler if main() did not handle the signal."""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_termination_signal_leaves_nothing(
    tree, tmp_path, fake_ceph, monkeypatch, capsys, signum
):
    def guard(signum, frame):
        raise UnhandledSignal(signum)

    def terminated(*args, **kwargs):
        os.kill(os.getpid(), signum)
        time.sleep(5)  # the signal handler raises long before this returns
        pytest.fail("the signal did not interrupt the walk")

    previous = signal.signal(signum, guard)  # keeps pytest alive if main() fails
    monkeypatch.setattr(cli, "walk", terminated)
    out = tmp_path / "o.jsonl.gz"
    try:
        status = main(["capture", str(tree), "--max-depth", "1", "-o", str(out)])
    finally:
        signal.signal(signum, previous)
    assert status == 128 + signum
    assert sorted(os.listdir(tmp_path)) == ["root"]
    assert signal.Signals(signum).name in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["capture", "r", "-o", "o"],
        ["capture", "r", "--max-depth", "1"],
        ["capture", "r", "--max-depth", "-1", "-o", "o"],
        ["capture", "r", "--max-depth", "x", "-o", "o"],
        ["capture", "r", "--max-depth", "1", "-o", "o", "--jobs", "0"],
        ["capture", "r", "--max-depth", "1", "-o", "o", "--max-dirs", "0"],
        ["frobnicate"],
    ],
)
def test_usage_errors_exit_2(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
