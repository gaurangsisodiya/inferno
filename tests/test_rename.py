"""Tests for the memorization check: inferno.rename and the --renamed paths.

The model and Docker are always faked. The behaviour test runs the real
QuixBugs tests on the host (trusted dataset code, no model output).
"""

import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from inferno import loop, run_all, validate
from inferno.client import ChatReply, estimate_cost
from inferno.rename import RenameError, rename_program
from inferno.sandbox import QUIXBUGS_DIR, SandboxResult


def words(text):
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


# --- rename_program ---------------------------------------------------------------

SAMPLE = '''import string
from collections import deque

def convert_digits(number, base=10):
    """Converts using the classic algorithm."""
    # the important bug is below
    digits = string.digits
    queue = deque()
    while number > 0:
        queue.appendleft(digits[number % base])
        number = number // base
    return helper_join(queue, sep="")

def helper_join(items, sep):
    return sep.join([item for item in items])

"""
Trailing QuixBugs-style module docstring naming convert_digits.
"""
'''


def test_names_follow_the_scheme():
    r = rename_program(SAMPLE, "convert_digits")
    assert r.mapping == {
        "convert_digits": "f_1", "number": "a_1", "base": "a_2", "digits": "v_1",
        "queue": "v_2", "helper_join": "f_2", "items": "a_3", "sep": "a_4", "item": "v_3",
    }
    assert r.main_new == "f_1"
    assert "def f_1(a_1, a_2=10):" in r.source and "def f_2(a_3, a_4):" in r.source


def test_imports_attributes_and_builtins_are_kept():
    src = rename_program(SAMPLE, "convert_digits").source
    assert "import string" in src and "from collections import deque" in src
    assert "string.digits" in src and "deque()" in src  # imported names
    assert ".appendleft(" in src and ".join(" in src  # attributes


def test_comments_and_docstrings_are_removed():
    src = rename_program(SAMPLE, "convert_digits").source
    assert "#" not in src and "classic" not in src and "Trailing" not in src
    assert not words(src) & {"convert_digits", "number", "queue", "helper_join", "items"}


def test_keyword_arguments_to_own_functions_are_renamed():
    src = rename_program(SAMPLE, "convert_digits").source
    assert "f_2(v_2, a_4='')" in src


def test_scope_aware_builtin_shadowing():
    src = '''def upto(max):
    return list(range(max))

def biggest(xs):
    return max(xs)
'''
    out = rename_program(src, "upto").source
    assert "range(a_1)" in out  # the parameter `max` is renamed...
    assert "return max(a_2)" in out  # ...but the builtin max() in g is not


def test_nested_functions_lambdas_and_comprehensions():
    src = '''def outer(values):
    seen = set()
    def visit(v):
        return v in seen
    key = lambda pair: pair[0]
    return [visit(x) for x in sorted(values, key=key)]
'''
    r = rename_program(src, "outer")
    ns = {}
    exec(r.with_alias(r.source), ns)
    assert ns["outer"]([(3, "a"), (1, "b")]) == [False, False]
    assert not words(r.source) & {"outer", "values", "seen", "visit", "pair", "x"}


def test_renamed_code_behaves_the_same():
    r = rename_program(SAMPLE, "convert_digits")
    ns = {}
    exec(r.with_alias(r.source), ns)
    assert ns["convert_digits"](255) == "255"
    assert ns["convert_digits"](5, 2) == "101"
    assert ns["convert_digits"].__name__ == "convert_digits"  # tests use __name__


@pytest.mark.parametrize("src,why", [
    ("class A:\n    pass\ndef count():\n    pass\n", "ClassDef"),
    ("def count():\n    global x\n    x = 1\n", "Global"),
    ("def count(a_1):\n    return a_1\n", "neutral-style"),
    ("def other():\n    pass\n", "no top-level function"),
    ("def count(:\n", "does not parse"),
    ("def count():\n    return 'count is here'\n", "still appears"),
])
def test_unsupported_programs_raise(src, why):
    with pytest.raises(RenameError, match=why):
        rename_program(src, "count")


def test_scrub_replaces_names_in_test_output():
    r = rename_program(SAMPLE, "convert_digits")
    out = ("FAILED python_testcases/test_convert_digits.py::test_convert_digits[x] - "
           "assert convert_digits(1) == 2 where <function convert_digits at 0x1> "
           "json_testcases/convert_digits.json")
    scrubbed = r.scrub(out)
    assert "convert_digits" not in scrubbed
    assert "test_f_1.py::test_f_1[x]" in scrubbed and "assert f_1(1)" in scrubbed


def test_scrub_leaves_longer_words_alone():
    r = rename_program("def lis(arr):\n    return list(arr)\n", "lis")
    assert r.scrub("list lis() test_lis") == "list f_1() test_f_1"


# --- all 40 real programs ------------------------------------------------------------

needs_quixbugs = pytest.mark.skipif(not QUIXBUGS_DIR.exists(), reason="run scripts/setup_quixbugs.sh")


@needs_quixbugs
def test_every_quixbugs_program_renames_without_leaking_its_name():
    programs = run_all.list_programs()
    assert len(programs) == 40
    for p in programs:
        r = rename_program((QUIXBUGS_DIR / "python_programs" / f"{p}.py").read_text(), p)
        assert r.scrub(r.source) == r.source, p
        assert p not in words(r.source), p


def _quixbugs_copy(root: Path, renamed: bool) -> None:
    for d in ["python_testcases", "json_testcases"]:
        shutil.copytree(QUIXBUGS_DIR / d, root / d, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(QUIXBUGS_DIR / "conftest.py", root)
    (root / "python_programs").mkdir()
    shutil.copy(QUIXBUGS_DIR / "python_programs" / "node.py", root / "python_programs")
    for p in run_all.list_programs():
        src = (QUIXBUGS_DIR / "python_programs" / f"{p}.py").read_text()
        if renamed:
            r = rename_program(src, p)
            src = r.with_alias(r.source)
        (root / "python_programs" / f"{p}.py").write_text(src)


def _start_pytest(root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "python_testcases", "-q", "--tb=no",
         "-p", "no:cacheprovider", "--timeout=2", f"--junitxml={root / 'report.xml'}"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _outcomes(root: Path) -> dict[str, str]:
    out = {}
    for case in ET.parse(root / "report.xml").getroot().iter("testcase"):
        kinds = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
        out[f"{case.get('classname')}::{case.get('name')}"] = kinds[0] if kinds else "passed"
    return out


@needs_quixbugs
@pytest.mark.timeout(300)
def test_renamed_buggy_programs_fail_exactly_the_same_tests(tmp_path):
    """Every QuixBugs test case has the same outcome on the original and renamed+alias buggy code."""
    orig, ren = tmp_path / "orig", tmp_path / "renamed"
    orig.mkdir(), ren.mkdir()
    _quixbugs_copy(orig, renamed=False)
    _quixbugs_copy(ren, renamed=True)
    procs = [_start_pytest(orig), _start_pytest(ren)]  # run both at once
    for proc in procs:
        proc.wait(timeout=280)

    before, after = _outcomes(orig), _outcomes(ren)
    assert len(before) > 250 and "failure" in before.values()
    assert after == before


# --- the repair loop with renaming (fake model + fake sandbox) ------------------------

PROGRAM = "add_numbers"
BUGGY = '''def add_numbers(first_value, second_value):
    """Adds two numbers together."""
    # TODO: this subtraction is the secret bug
    running_total = first_value - second_value
    return running_total
'''
FIX = "def f_1(a_1, a_2):\n    v_1 = a_1 + a_2\n    return v_1\n"
ORIGINAL_WORDS = {"add_numbers", "first_value", "second_value", "running_total",
                  "Adds", "secret", "TODO"}


@pytest.fixture
def quixbugs(tmp_path):
    q = tmp_path / "quixbugs"
    (q / "python_programs").mkdir(parents=True)
    (q / "python_testcases").mkdir()
    (q / "python_programs" / f"{PROGRAM}.py").write_text(BUGGY)
    (q / "python_programs" / "classy.py").write_text("class C:\n    pass\ndef classy():\n    pass\n")
    (q / "python_programs" / "node.py").write_text("")
    for p in [PROGRAM, "classy"]:
        (q / "python_testcases" / f"test_{p}.py").write_text("")
    return q


class FakeModel:
    model = "openai/gpt-oss-120b"

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.tokens_in = self.tokens_out = 0

    def chat(self, messages):
        self.sent.append([dict(m) for m in messages])
        self.tokens_in += 100
        self.tokens_out += 10
        return ChatReply(self.replies.pop(0), 100, 10)

    def cost_usd(self):
        return estimate_cost(self.model, self.tokens_in, self.tokens_out)


class ExecSandbox:
    """Imports the workspace file like the QuixBugs test would, and leaks names in its output."""

    def __init__(self):
        self.files = []

    def __call__(self, program, workspace: Path) -> SandboxResult:
        source = (workspace / "python_programs" / f"{program}.py").read_text()
        self.files.append(source)
        ns = {}
        exec(source, ns)
        fn = ns[program]  # what `from python_programs.<program> import <program>` gets
        ok = fn(2, 3) == 5 and fn.__name__ == program
        # Realistic pytest output that mentions the original names everywhere.
        return SandboxResult(passed=ok, output=(
            f"python_testcases/test_{program}.py F\n"
            f"    assert {program}(*input_data) == expected\n"
            f"E   where -1 = <function {program} at 0x7f>(2, 3)\n"
            f"FAILED python_testcases/test_{program}.py::test_{program}[input_data0-5]\n"
            f"FileNotFoundError: json_testcases/{program}.json\n"
        ))


def _renamed():
    return rename_program(BUGGY, PROGRAM)


def test_no_original_names_reach_the_model(quixbugs):
    client = FakeModel(["def f_1(a_1, a_2):\n    return a_1 * a_2\n", FIX])
    res = loop.repair(PROGRAM, client, ExecSandbox(), quixbugs_dir=quixbugs, renamed=_renamed())
    assert res.passed and res.attempts == 2

    for conversation in client.sent:
        for message in conversation:
            assert not words(message["content"]) & ORIGINAL_WORDS, message["content"]
            assert PROGRAM not in message["content"]
    first = client.sent[0][1]["content"]
    assert "`f_1.py`" in first and "def f_1(a_1, a_2):" in first
    assert "test_f_1.py::test_f_1" in first  # failure output was scrubbed, not dropped
    assert "json_testcases/f_1.json" in client.sent[1][3]["content"]


def test_alias_is_appended_after_the_models_reply(quixbugs):
    sandbox = ExecSandbox()
    rn = _renamed()
    res = loop.repair(PROGRAM, FakeModel([FIX]), sandbox, quixbugs_dir=quixbugs, renamed=rn)

    initial, candidate = sandbox.files
    assert initial == rn.with_alias(rn.source)  # renamed buggy code + alias
    assert candidate == FIX + "\n" + rn.alias_line + "\n"
    assert candidate.rstrip("\n").endswith(rn.alias_line)
    assert res.final_source == FIX  # recorded without the alias
    assert res.passed


def test_original_file_not_modified_in_renamed_mode(quixbugs):
    loop.repair(PROGRAM, FakeModel([FIX]), ExecSandbox(), quixbugs_dir=quixbugs, renamed=_renamed())
    assert (quixbugs / "python_programs" / f"{PROGRAM}.py").read_text() == BUGGY


# --- run_all --renamed and validate on renamed records ----------------------------------

def test_run_all_renamed_records_and_skips(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    outcome = run_all.run_all(
        ["classy", PROGRAM], run_file, lambda: FakeModel([FIX]), ExecSandbox(),
        quixbugs_dir=quixbugs, git_commit="abc", renamed=True,
    )
    assert list(outcome.rename_failed) == ["classy"]
    assert "ClassDef" in outcome.rename_failed["classy"]
    assert outcome.errored == []

    (row,) = run_all.load_results(run_file)  # nothing written for classy
    assert row["program"] == PROGRAM and row["renamed"] is True and row["passed"] is True
    assert row["alias"] == _renamed().alias_line
    assert row["final_source"] == FIX
    for key in ["attempts", "fixed_first_try", "tokens_in", "tokens_out", "cost_usd",
                "seconds", "git_commit", "model"]:
        assert key in row


def test_run_all_normal_mode_records_renamed_false(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    fixed = BUGGY.replace("first_value - second_value", "first_value + second_value")
    run_all.run_all([PROGRAM], run_file, lambda: FakeModel([fixed]), ExecSandbox(),
                    quixbugs_dir=quixbugs)
    (row,) = run_all.load_results(run_file)
    assert row["renamed"] is False and row["alias"] is None


def test_default_run_id_marks_renamed():
    import datetime as dt
    assert run_all.default_run_id("m/x", dt.date(2026, 1, 2), renamed=True) == "m-x_2026-01-02_renamed"


def test_validate_applies_alias_for_renamed_records(quixbugs, tmp_path):
    sandbox = ExecSandbox()
    seen_props = []

    def fake_props(program, workspace, prop_file):
        seen_props.append((workspace / "python_programs" / f"{program}.py").read_text())
        return SandboxResult(passed=True, output="ok")

    props = tmp_path / "props"
    props.mkdir()
    (props / f"prop_{PROGRAM}.py").write_text("")
    record = {"program": PROGRAM, "final_source": FIX, "renamed": True,
              "alias": _renamed().alias_line}
    v = validate.validate(record, sandbox, fake_props, quixbugs, props)

    assert v.passes_original and v.passes_properties and v.valid
    assert sandbox.files[0] == seen_props[0] == FIX + "\n" + _renamed().alias_line + "\n"


def test_validate_without_alias_fails_renamed_code(quixbugs, tmp_path):
    """Sanity: the alias is what makes renamed code importable by the original name."""
    record = {"program": PROGRAM, "final_source": FIX}  # not marked renamed
    with pytest.raises(KeyError):
        validate.validate(record, ExecSandbox(), None, quixbugs, tmp_path)
