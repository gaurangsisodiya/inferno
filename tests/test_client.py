"""Tests for inferno.client. A fake OpenAI client stands in for the network."""

from types import SimpleNamespace

import pytest

from inferno.client import DEFAULT_MODEL, TogetherClient, estimate_cost


class FakeOpenAI:
    """Mimics `OpenAI().chat.completions.create(...)` and records each call."""

    def __init__(self, replies):
        self.replies = list(replies)  # (text, prompt_tokens, completion_tokens)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text, tin, tout = self.replies.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=tin, completion_tokens=tout),
        )


@pytest.fixture
def no_env(monkeypatch, tmp_path):
    """Clear the relevant env vars and point at an empty .env file."""
    monkeypatch.delenv("INFERNO_MODEL", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    return tmp_path / "missing.env"


def test_chat_returns_text_and_accumulates_tokens(no_env):
    fake = FakeOpenAI([("hello", 10, 5), ("again", 20, 7)])
    client = TogetherClient(env_file=no_env, openai_client=fake)

    reply = client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "hi"}])

    assert reply.text == "hello"
    assert (reply.tokens_in, reply.tokens_out) == (10, 5)
    assert (client.tokens_in, client.tokens_out) == (30, 12)
    assert fake.calls[0]["model"] == DEFAULT_MODEL
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_model_comes_from_env(no_env, monkeypatch):
    monkeypatch.setenv("INFERNO_MODEL", "some/other-model")
    client = TogetherClient(env_file=no_env, openai_client=FakeOpenAI([]))
    assert client.model == "some/other-model"


def test_missing_usage_counts_as_zero(no_env):
    fake = FakeOpenAI([("x", 0, 0)])
    fake.chat.completions.create = lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))], usage=None
    )
    reply = TogetherClient(env_file=no_env, openai_client=fake).chat([])
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ("", 0, 0)


def test_missing_api_key_raises(no_env):
    with pytest.raises(RuntimeError, match="TOGETHER_API_KEY"):
        TogetherClient(env_file=no_env)


def test_api_key_read_from_env_file(no_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TOGETHER_API_KEY=test-key\n")
    client = TogetherClient(env_file=env_file)  # builds a real OpenAI client, no call made
    assert client._client.api_key == "test-key"
    assert str(client._client.base_url).startswith("https://api.together.xyz/v1")


def test_estimate_cost():
    # 1M in at $0.15 + 1M out at $0.60
    assert estimate_cost(DEFAULT_MODEL, 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert estimate_cost("unknown/model", 100, 100) is None
