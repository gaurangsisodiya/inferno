"""Tests for inferno.validate, inferno.validate_run, the property command and
scripts/check_properties.py. Docker and the model are never used."""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from inferno import sandbox, validate, validate_run
from inferno.sandbox import SandboxResult

GOOD = "def add(a, b):\n    return a + b\n"
PLAUSIBLE = "def add(a, b):\n    return a + b if a < 10 else 0\n"  # passes tests, fails props
BAD = "def add(a, b):\n    return a - b\n"


@pytest.fixture
def quixbugs(tmp_path):
    q = tmp_path / "quixbugs"
    (q / "python_programs").mkdir(parents=True)
    for name in ["add", "noprops"]:
        (q / "python_programs" / f"{name}.py").write_text(BAD)
    (q / "python_programs" / "node.py").write_text("")
    (q / "conftest.py").write_text("")
    return q


@pytest.fixture
def props_dir(tmp_path):
    d = tmp_path / "props"
    d.mkdir()
    (d / "prop_add.py").write_text("# fake property file\n")
    return d


class FakeSandbox:
    """Original tests pass unless the source has `a - b`; properties pass only on GOOD."""

    def __init__(self):
        self.test_calls, self.prop_calls = [], []

    def run_tests(self, program, workspace: Path):
        src = (workspace / "python_programs" / f"{program}.py").read_text()
        self.test_calls.append(program)
        ok = "a - b" not in src
        return SandboxResult(passed=ok, output="ok" if ok else "FAILED test")

    def run_properties(self, program, workspace: Path, prop_file: Path):
        src = (workspace / "python_programs" / f"{program}.py").read_text()
        self.prop_calls.append((program, prop_file.name))
        ok = src == GOOD
        return SandboxResult(passed=ok, output="ok" if ok else "Falsifying example: add(a=10, b=0)")


def check(record, quixbugs, props_dir, fake=None):
    fake = fake or FakeSandbox()
    return validate.validate(record, fake.run_tests, fake.run_properties, quixbugs, props_dir)


# --- validate() -----------------------------------------------------------------

def test_valid_fix(quixbugs, props_dir):
    v = check({"program": "add", "final_source": GOOD}, quixbugs, props_dir)
    assert (v.passes_original, v.passes_properties, v.valid) == (True, True, True)
    assert v.property_output is None


def test_plausible_but_wrong_fix(quixbugs, props_dir):
    v = check({"program": "add", "final_source": PLAUSIBLE}, quixbugs, props_dir)
    assert (v.passes_original, v.passes_properties, v.valid) == (True, False, False)
    assert "Falsifying example" in v.property_output


def test_failing_fix_still_runs_properties(quixbugs, props_dir):
    fake = FakeSandbox()
    v = check({"program": "add", "final_source": BAD}, quixbugs, props_dir, fake)
    assert (v.passes_original, v.passes_properties, v.valid) == (False, False, False)
    assert fake.prop_calls == [("add", "prop_add.py")]


def test_no_property_file(quixbugs, props_dir):
    fake = FakeSandbox()
    v = check({"program": "noprops", "final_source": GOOD}, quixbugs, props_dir, fake)
    assert v.passes_properties is None and v.valid is None and v.passes_original
    assert fake.prop_calls == []


def test_candidate_is_tested_not_the_buggy_file(quixbugs, props_dir):
    check({"program": "add", "final_source": GOOD}, quixbugs, props_dir)
    assert (quixbugs / "python_programs" / "add.py").read_text() == BAD


# --- validate_run ---------------------------------------------------------------

def records():
    return [
        {"program": "add", "final_source": PLAUSIBLE, "passed": True, "attempts": 1,
         "model": "m", "run_id": "r", "git_commit": "run111"},
        {"program": "noprops", "final_source": GOOD, "passed": True, "attempts": 2,
         "model": "m", "run_id": "r", "git_commit": "run111"},
    ]


def go(recs, out_file, quixbugs, props_dir, fake=None, **kw):
    fake = fake or FakeSandbox()
    errored = validate_run.validate_run(
        recs, out_file, fake.run_tests, fake.run_properties, quixbugs, props_dir,
        git_commit="val222", git_dirty=False, **kw,
    )
    return errored, fake


def test_validate_run_writes_lines_with_commits(quixbugs, props_dir, tmp_path):
    out = tmp_path / "validated" / "r.jsonl"
    errored, _ = go(records(), out, quixbugs, props_dir)
    assert errored == []
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["program"] for r in rows] == ["add", "noprops"]
    add = rows[0]
    assert add["passes_original"] and add["passes_properties"] is False and add["valid"] is False
    assert add["git_commit"] == "val222" and add["run_git_commit"] == "run111"
    assert add["run_passed"] is True and add["attempts"] == 1 and add["model"] == "m"
    assert rows[1]["passes_properties"] is None


def test_validate_run_resumes(quixbugs, props_dir, tmp_path):
    out = tmp_path / "r.jsonl"
    go(records(), out, quixbugs, props_dir, limit=1)
    _, fake = go(records(), out, quixbugs, props_dir)
    assert fake.test_calls == ["noprops"]  # "add" was already done
    assert len(validate_run.load_results(out)) == 2


def test_validate_run_exception_not_recorded(quixbugs, props_dir, tmp_path):
    out = tmp_path / "r.jsonl"
    fake = FakeSandbox()

    def boom(program, workspace, prop_file):
        raise RuntimeError("docker died")

    fake.run_properties = boom
    errored, _ = go(records(), out, quixbugs, props_dir, fake=fake)
    assert errored == ["add"]
    assert [r["program"] for r in validate_run.load_results(out)] == ["noprops"]


def test_summary_lists_plausible_but_wrong(quixbugs, props_dir, tmp_path):
    out = tmp_path / "r.jsonl"
    recs = records() + [{"program": "add2", "final_source": GOOD}]
    (props_dir / "prop_add2.py").write_text("")
    (quixbugs / "python_programs" / "add2.py").write_text(BAD)
    go(recs, out, quixbugs, props_dir)
    results = validate_run.load_results(out)

    assert validate_run.plausible_but_wrong(results) == ["add"]
    text = validate_run.summarize(results)
    assert "Programs with properties:               2" in text
    assert "passed originals but FAILED properties: 1" in text
    assert "Plausible-but-wrong fixes: add" in text


# --- sandbox property command -----------------------------------------------------

def test_property_command_is_locked_down_and_mounts_little(tmp_path):
    q = tmp_path / "quixbugs"
    prop = tmp_path / "props" / "prop_gcd.py"
    cmd = sandbox.build_property_command("gcd", tmp_path / "ws", prop, "c1", q,
                                         private_dir=tmp_path / "private")
    joined = " ".join(cmd)
    assert "--network none" in joined and "--read-only" in cmd and "--cap-drop ALL" in joined
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert all(m.endswith(",readonly") for m in mounts)
    assert sorted(m.split(",")[1].removeprefix("source=") for m in mounts) == sorted([
        str(tmp_path / "ws" / "python_programs"), str(q / "conftest.py"), str(prop),
    ])
    assert "properties/prop_gcd.py" in cmd
    assert f"--timeout={sandbox.PROPERTY_TEST_TIMEOUT_S}" in cmd
    assert "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/.hypothesis" in cmd


def test_property_file_in_private_dir_is_refused(tmp_path):
    with pytest.raises(PermissionError):
        sandbox.build_property_command("gcd", tmp_path / "ws", tmp_path / "private" / "p.py",
                                       "c1", tmp_path / "q", private_dir=tmp_path / "private")


def test_image_tag_tracks_dockerfile():
    assert sandbox.IMAGE.startswith("inferno-sandbox:py3.12-")
    assert "hypothesis==" in sandbox.DOCKERFILE.read_text()


def test_dockerfile_and_pyproject_pin_same_hypothesis():
    import re
    import tomllib

    repo = sandbox.DOCKERFILE.parents[1]
    deps = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["dependencies"]
    pinned = next(d for d in deps if d.startswith("hypothesis=="))
    docker = re.search(r"hypothesis==[\w.]+", sandbox.DOCKERFILE.read_text()).group()
    assert pinned == docker


# --- the real property files ----------------------------------------------------

def test_ten_property_files_exist():
    assert len(validate.programs_with_properties()) == 10


@pytest.mark.parametrize("path", sorted(validate.PROPERTIES_DIR.glob("prop_*.py")), ids=lambda p: p.stem)
def test_property_file_is_standalone(path):
    """Mounted alone into the sandbox, so it can't import inferno."""
    tree = ast.parse(path.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "inferno" not in imported
    assert any(n.name.startswith("test_") for n in tree.body if isinstance(n, ast.FunctionDef))


# --- scripts/check_properties.py --------------------------------------------------

def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_properties.py"
    spec = importlib.util.spec_from_file_location("check_properties", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_script_detects_pass_and_fail(tmp_path):
    """Runs prop_bitcount.py on host against two tiny stand-in packages (no Docker)."""
    script = load_script()
    for pkg, body in [("good_pkg", "    return bin(n).count('1')\n"),
                      ("bad_pkg", "    return bin(n).count('1') + (n % 3 == 0)\n")]:
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "bitcount.py").write_text("def bitcount(n):\n" + body)

    good = script.run_properties("bitcount", "good_pkg", tmp_path)
    bad = script.run_properties("bitcount", "bad_pkg", tmp_path)
    assert good and all(good.values())
    assert not all(bad.values())


def test_check_script_reports_import_failure(tmp_path):
    script = load_script()
    res = script.run_properties("bitcount", "no_such_pkg", tmp_path)
    assert not all(res.values())
