"""Tests for inferno.sandbox. Docker is mocked except in the last, optional test."""

import shutil
import subprocess
from pathlib import Path

import pytest

from inferno import sandbox


@pytest.fixture
def quixbugs(tmp_path):
    """A tiny fake QuixBugs checkout with the files one program's tests need."""
    q = tmp_path / "quixbugs"
    (q / "python_testcases").mkdir(parents=True)
    (q / "json_testcases").mkdir()
    for f in ["conftest.py", "python_testcases/test_gcd.py",
              "python_testcases/load_testdata.py", "python_testcases/node.py",
              "json_testcases/gcd.json"]:
        (q / f).write_text("")
    return q


def mounts_in(cmd):
    """Pull the --mount values out of a docker command."""
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--mount"]


def test_command_is_locked_down(quixbugs, tmp_path):
    cmd = sandbox.build_docker_command("gcd", tmp_path / "ws", "c1", quixbugs,
                                       private_dir=tmp_path / "private")
    joined = " ".join(cmd)
    assert "--network none" in joined
    assert "--memory 512m" in joined
    assert "--read-only" in cmd
    assert "--cap-drop ALL" in joined
    assert "--timeout=5" in cmd
    assert cmd[cmd.index("--name") + 1] == "c1"


def test_mounts_only_workspace_and_needed_files_readonly(quixbugs, tmp_path):
    cmd = sandbox.build_docker_command("gcd", tmp_path / "ws", "c1", quixbugs,
                                       private_dir=tmp_path / "private")
    mounts = mounts_in(cmd)
    assert all(m.endswith(",readonly") for m in mounts)
    sources = sorted(m.split(",")[1].removeprefix("source=") for m in mounts)
    assert sources == sorted([
        str(tmp_path / "ws" / "python_programs"),
        str(quixbugs / "conftest.py"),
        str(quixbugs / "python_testcases" / "test_gcd.py"),
        str(quixbugs / "python_testcases" / "load_testdata.py"),
        str(quixbugs / "python_testcases" / "node.py"),
        str(quixbugs / "json_testcases" / "gcd.json"),
    ])


def test_json_mount_skipped_when_missing(quixbugs):
    (quixbugs / "json_testcases" / "gcd.json").unlink()
    targets = [t for _, t in sandbox.needed_test_files("gcd", quixbugs)]
    assert not any("json_testcases" in t for t in targets)


@pytest.mark.parametrize("bad", ["private", "private/correct_python_programs", "."])
def test_private_dir_never_mounted(tmp_path, bad):
    private = tmp_path / "private"
    (private / "correct_python_programs").mkdir(parents=True)
    with pytest.raises(PermissionError):
        sandbox._assert_not_private(tmp_path / bad, private)  # "." = a parent of private


def test_workspace_inside_private_is_refused(quixbugs, tmp_path):
    with pytest.raises(PermissionError):
        sandbox.build_docker_command("gcd", tmp_path / "private" / "ws", "c1", quixbugs,
                                     private_dir=tmp_path / "private")


def test_default_private_dir_is_not_the_quixbugs_checkout():
    sandbox._assert_not_private(sandbox.QUIXBUGS_DIR)  # must not raise


def test_run_tests_pass_and_fail(monkeypatch, quixbugs, tmp_path):
    codes = iter([0, 1])

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, next(codes), stdout="out", stderr="err")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    ok = sandbox.run_tests("gcd", tmp_path, quixbugs)
    bad = sandbox.run_tests("gcd", tmp_path, quixbugs)
    assert ok.passed and not bad.passed
    assert bad.output == "outerr"


def test_hard_timeout_kills_container(monkeypatch, quixbugs, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            assert kwargs["timeout"] == 7
            raise subprocess.TimeoutExpired(cmd, 7, output="partial")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = sandbox.run_tests("gcd", tmp_path, quixbugs, hard_timeout=7)

    assert res.timed_out and not res.passed
    assert "partial" in res.output and "Hard timeout" in res.output
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "kill", name]


def test_missing_docker_gives_clear_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Docker is not available"):
        sandbox.ensure_image()


def test_broken_docker_gives_clear_error(monkeypatch):
    """e.g. WSL's placeholder `docker` script, or a stopped daemon."""
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    with pytest.raises(RuntimeError, match="Docker is not available"):
        sandbox.ensure_image()


def test_image_built_only_when_missing(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[:2])
        missing_image = cmd[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(cmd, 1 if missing_image else 0)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.ensure_image()
    assert calls == [["docker", "info"], ["docker", "image"], ["docker", "build"]]


def _docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="Docker is not available")
@pytest.mark.skipif(not sandbox.QUIXBUGS_DIR.exists(), reason="run scripts/setup_quixbugs.sh")
def test_real_docker_run_on_buggy_gcd(tmp_path):
    """Integration test: the buggy gcd really fails inside the real container."""
    from inferno.loop import make_workspace

    sandbox.ensure_image()
    make_workspace("gcd", tmp_path, sandbox.QUIXBUGS_DIR)
    res = sandbox.run_tests("gcd", tmp_path)
    assert not res.passed and not res.timed_out
    assert "failed" in res.output
