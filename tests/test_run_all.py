"""Tests for inferno.run_all. Fake model + fake sandbox: no network, no Docker."""

import datetime as dt
import json
from pathlib import Path

import pytest

from inferno import run_all
from inferno.client import ChatReply, estimate_cost
from inferno.sandbox import SandboxResult

# Each fake program is "fixed" when its source contains the marker FIXED_<name>.
PROGRAMS = ["alpha", "beta", "gamma"]


@pytest.fixture
def quixbugs(tmp_path):
    """Fake QuixBugs tree: 3 programs with tests, plus one without a test and node.py."""
    q = tmp_path / "quixbugs"
    (q / "python_programs").mkdir(parents=True)
    (q / "python_testcases").mkdir()
    for name in PROGRAMS:
        (q / "python_programs" / f"{name}.py").write_text(f"# buggy {name}\n")
        (q / "python_testcases" / f"test_{name}.py").write_text("")
    (q / "python_programs" / "no_test.py").write_text("")
    (q / "python_programs" / "node.py").write_text("")
    (q / "python_testcases" / "node.py").write_text("")
    return q


class FakeModel:
    """Stands in for TogetherClient. `script` maps program -> list of replies."""

    model = "openai/gpt-oss-120b"

    def __init__(self, script):
        self.script = script
        self.tokens_in = 0
        self.tokens_out = 0

    def chat(self, messages):
        prompt = messages[1]["content"]
        program = next(p for p in self.script if f"`{p}.py`" in prompt)
        attempt = (len(messages) - 1) // 2  # 0 on the first call
        self.tokens_in += 100
        self.tokens_out += 10
        return ChatReply(self.script[program][attempt], 100, 10)

    def cost_usd(self):
        return estimate_cost(self.model, self.tokens_in, self.tokens_out)


def fake_sandbox(program, workspace: Path) -> SandboxResult:
    source = (workspace / "python_programs" / f"{program}.py").read_text()
    ok = f"FIXED_{program}" in source
    return SandboxResult(passed=ok, output="ok" if ok else "FAILED")


SCRIPT = {
    "alpha": ["FIXED_alpha"],  # first try
    "beta": ["wrong", "FIXED_beta"],  # second try
    "gamma": ["wrong", "wrong", "wrong"],  # never
}


def make_factory(script=SCRIPT):
    """Returns a make_client() that hands out a fresh FakeModel each call, and the list of them."""
    made = []

    def make():
        made.append(FakeModel(script))
        return made[-1]

    return make, made


def go(quixbugs, run_file, **kw):
    make, made = make_factory(kw.pop("script", SCRIPT))
    errored = run_all.run_all(
        kw.pop("programs", PROGRAMS), run_file, make, fake_sandbox,
        quixbugs_dir=quixbugs, git_commit="abc123", git_dirty=False, **kw,
    )
    return errored, made


def test_list_programs_needs_a_test_file(quixbugs):
    assert run_all.list_programs(quixbugs) == PROGRAMS


def test_writes_one_line_per_program_with_all_fields(quixbugs, tmp_path):
    run_file = tmp_path / "runs" / "r1.jsonl"
    errored, _ = go(quixbugs, run_file)
    assert errored == []

    rows = [json.loads(line) for line in run_file.read_text().splitlines()]
    by = {r["program"]: r for r in rows}
    assert list(by) == PROGRAMS

    a, b, g = by["alpha"], by["beta"], by["gamma"]
    assert (a["passed"], a["attempts"], a["fixed_first_try"]) == (True, 1, True)
    assert (b["passed"], b["attempts"], b["fixed_first_try"]) == (True, 2, False)
    assert (g["passed"], g["attempts"], g["fixed_first_try"]) == (False, 3, False)

    # Per-program token counts, not cumulative across the batch.
    assert (a["tokens_in"], a["tokens_out"]) == (100, 10)
    assert (g["tokens_in"], g["tokens_out"]) == (300, 30)
    assert a["cost_usd"] == pytest.approx((100 * 0.15 + 10 * 0.60) / 1e6)

    assert a["final_source"].strip() == "FIXED_alpha"
    assert g["final_source"].strip() == "wrong"
    for r in rows:
        assert r["git_commit"] == "abc123" and r["git_dirty"] is False
        assert r["run_id"] == "r1" and r["seconds"] >= 0 and r["model"] == FakeModel.model


def test_resume_skips_programs_already_done(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    go(quixbugs, run_file, limit=1)
    assert [json.loads(l)["program"] for l in run_file.read_text().splitlines()] == ["alpha"]

    _, made = go(quixbugs, run_file)
    assert len(made) == 2  # only beta and gamma ran the second time
    programs = [json.loads(l)["program"] for l in run_file.read_text().splitlines()]
    assert programs == PROGRAMS

    _, made = go(quixbugs, run_file)  # everything done: nothing runs
    assert made == []


def test_resume_ignores_a_half_written_last_line(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    go(quixbugs, run_file, programs=["alpha"])
    with run_file.open("a") as f:
        f.write('{"program": "beta", "pas')  # killed mid-write

    _, made = go(quixbugs, run_file)
    assert len(made) == 2  # beta is retried, gamma runs
    results = run_all.load_results(run_file)
    assert [r["program"] for r in results] == PROGRAMS


def test_programs_option_runs_only_those(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    go(quixbugs, run_file, programs=["gamma", "alpha"])
    assert [r["program"] for r in run_all.load_results(run_file)] == ["gamma", "alpha"]


def test_limit_counts_only_programs_not_yet_done(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    go(quixbugs, run_file, limit=2)
    go(quixbugs, run_file, limit=2)
    assert len(run_all.load_results(run_file)) == 3


def test_exception_is_not_recorded_so_it_is_retried(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    broken = dict(SCRIPT, beta=[])  # IndexError when beta asks the model
    errored, _ = go(quixbugs, run_file, script=broken)
    assert errored == ["beta"]
    assert [r["program"] for r in run_all.load_results(run_file)] == ["alpha", "gamma"]

    errored, made = go(quixbugs, run_file)
    assert errored == [] and len(made) == 1
    assert len(run_all.load_results(run_file)) == 3


def test_summary(quixbugs, tmp_path):
    run_file = tmp_path / "r.jsonl"
    go(quixbugs, run_file)
    text = run_all.summarize(run_all.load_results(run_file))
    assert "Fixed first try:    1/3 (33%)" in text
    assert "Fixed within 3 tries: 2/3 (67%)" in text
    assert "Tokens in / out:    600 / 60" in text
    assert "$0.0001" in text


def test_summary_with_unknown_price():
    rows = [dict(fixed_first_try=True, passed=True, attempts=1, tokens_in=1, tokens_out=1, cost_usd=None)]
    assert "unknown" in run_all.summarize(rows)


def test_default_run_id_is_filename_safe():
    rid = run_all.default_run_id("openai/gpt-oss-120b", dt.date(2026, 11, 14))
    assert rid == "openai-gpt-oss-120b_2026-11-14"


def test_git_info_on_this_repo():
    commit, dirty = run_all.git_info()
    assert commit is not None and len(commit) == 40 and isinstance(dirty, bool)


def test_git_info_outside_a_repo(tmp_path):
    assert run_all.git_info(tmp_path) == (None, None)
