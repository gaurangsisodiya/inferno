"""Tests for inferno.loop. Both the model and the sandbox are faked: no network, no Docker."""

from pathlib import Path

import pytest

from inferno import loop
from inferno.client import ChatReply, estimate_cost
from inferno.sandbox import SandboxResult

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def quixbugs(tmp_path):
    """A fake QuixBugs checkout containing one buggy program called `add`."""
    progs = tmp_path / "quixbugs" / "python_programs"
    progs.mkdir(parents=True)
    (progs / "add.py").write_text(BUGGY)
    (progs / "node.py").write_text("class Node: pass\n")
    return tmp_path / "quixbugs"


class FakeClient:
    """Returns scripted replies and records the messages it was sent."""

    model = "openai/gpt-oss-120b"

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []  # a copy of the message list at each call
        self.tokens_in = 0
        self.tokens_out = 0

    def chat(self, messages):
        self.sent.append([dict(m) for m in messages])
        self.tokens_in += 100
        self.tokens_out += 50
        return ChatReply(text=self.replies.pop(0), tokens_in=100, tokens_out=50)

    def cost_usd(self):
        return estimate_cost(self.model, self.tokens_in, self.tokens_out)


class FakeSandbox:
    """Passes when the workspace's add.py contains `a + b`. Records what it saw."""

    def __init__(self):
        self.calls = 0
        self.seen_files = []

    def __call__(self, program, workspace: Path) -> SandboxResult:
        self.calls += 1
        progs = workspace / "python_programs"
        self.seen_files.append(sorted(p.name for p in progs.iterdir()))
        source = (progs / f"{program}.py").read_text()
        if "a + b" in source:
            return SandboxResult(passed=True, output="1 passed")
        return SandboxResult(passed=False, output=f"FAILED run {self.calls}")


def test_fixed_on_first_attempt(quixbugs):
    client = FakeClient([FIXED])
    res = loop.repair("add", client, FakeSandbox(), quixbugs_dir=quixbugs)

    assert res.passed and res.attempts == 1
    assert (res.tokens_in, res.tokens_out) == (100, 50)
    assert res.cost_usd == pytest.approx((100 * 0.15 + 50 * 0.60) / 1e6)
    assert res.final_source == FIXED


def test_first_prompt_contains_source_and_failure(quixbugs):
    client = FakeClient([FIXED])
    loop.repair("add", client, FakeSandbox(), quixbugs_dir=quixbugs)

    first = client.sent[0]
    assert first[0]["role"] == "system"
    assert BUGGY in first[1]["content"]
    assert "FAILED run 1" in first[1]["content"]  # output of the initial test run


def test_retries_with_new_feedback_then_succeeds(quixbugs):
    client = FakeClient(["wrong 1", "wrong 2", FIXED])
    sandbox = FakeSandbox()
    res = loop.repair("add", client, sandbox, quixbugs_dir=quixbugs)

    assert res.passed and res.attempts == 3
    assert sandbox.calls == 4  # initial run + 3 candidates
    # Third call sees both earlier replies and the newest failure output.
    third = client.sent[2]
    assert [m["role"] for m in third] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert third[4]["content"] == "wrong 2"
    assert "FAILED run 3" in third[5]["content"]


def test_gives_up_after_max_attempts(quixbugs):
    client = FakeClient(["nope"] * 5)
    res = loop.repair("add", client, FakeSandbox(), quixbugs_dir=quixbugs)

    assert not res.passed
    assert res.attempts == 3
    assert len(client.sent) == 3
    assert (res.tokens_in, res.tokens_out) == (300, 150)


def test_max_attempts_is_configurable(quixbugs):
    client = FakeClient(["nope"] * 5)
    res = loop.repair("add", client, FakeSandbox(), max_attempts=1, quixbugs_dir=quixbugs)
    assert res.attempts == 1 and len(client.sent) == 1


def test_already_passing_program_makes_no_model_call(quixbugs):
    (quixbugs / "python_programs" / "add.py").write_text(FIXED)
    client = FakeClient([])
    res = loop.repair("add", client, FakeSandbox(), quixbugs_dir=quixbugs)
    assert res.passed and res.attempts == 0 and client.sent == []


def test_workspace_holds_only_program_and_node(quixbugs):
    sandbox = FakeSandbox()
    loop.repair("add", FakeClient([FIXED]), sandbox, quixbugs_dir=quixbugs)
    assert sandbox.seen_files[0] == ["add.py", "node.py"]


def test_original_buggy_file_is_not_modified(quixbugs):
    loop.repair("add", FakeClient([FIXED]), FakeSandbox(), quixbugs_dir=quixbugs)
    assert (quixbugs / "python_programs" / "add.py").read_text() == BUGGY


@pytest.mark.parametrize(
    "reply",
    [
        FIXED,
        f"```python\n{FIXED}```",
        f"Here is the fix:\n```python\n{FIXED}```\nDone.",
        f"```\n{FIXED}```",
    ],
)
def test_extract_code_handles_fences(reply):
    assert loop.extract_code(reply) == FIXED


def test_extract_code_picks_longest_block():
    reply = f"```\nx\n```\n```python\n{FIXED}```"
    assert loop.extract_code(reply) == FIXED


def test_trim_keeps_start_and_end():
    text = "START" + "x" * 20_000 + "END"
    out = loop.trim(text, limit=100)
    assert out.startswith("START") and out.endswith("END") and "trimmed" in out
    assert loop.trim("short") == "short"
