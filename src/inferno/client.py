"""Thin wrapper around the Together AI chat API.

Together AI exposes an "OpenAI-compatible" API, so we reuse the official
`openai` Python package and just point it at Together's URL. This module:

* reads TOGETHER_API_KEY (and optionally INFERNO_MODEL) from the .env file,
* sends a list of chat messages and returns the model's reply text,
* records how many tokens each call used, and
* estimates the dollar cost of those tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

TOGETHER_BASE_URL = "https://api.together.xyz/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Price in US dollars per 1 million tokens, as (input, output).
# Last checked 2026-11-14. Update this when prices change or models are added.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
}

# This file lives at <repo>/src/inferno/client.py, so the repo root is 3 levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ChatReply:
    """What one model call gave back."""

    text: str
    tokens_in: int  # "prompt tokens": what we sent
    tokens_out: int  # "completion tokens": what the model wrote (includes reasoning)


class TogetherClient:
    """Sends chat messages to Together AI and keeps a running token total."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        env_file: Path = REPO_ROOT / ".env",
        openai_client=None,
    ):
        # load_dotenv copies KEY=value lines from .env into os.environ.
        # It does NOT overwrite variables that are already set in your shell.
        load_dotenv(env_file)

        self.model = model or os.environ.get("INFERNO_MODEL") or DEFAULT_MODEL
        self.tokens_in = 0
        self.tokens_out = 0

        # Tests pass in a fake `openai_client` so no network call is ever made.
        if openai_client is None:
            api_key = api_key or os.environ.get("TOGETHER_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "TOGETHER_API_KEY is not set. Put it in .env (see .env.example)."
                )
            openai_client = OpenAI(api_key=api_key, base_url=TOGETHER_BASE_URL)
        self._client = openai_client

    def chat(self, messages: list[dict[str, str]]) -> ChatReply:
        """Send messages like [{"role": "user", "content": "..."}] and return the reply."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        usage = response.usage  # can be None if the provider doesn't report it
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

        text = response.choices[0].message.content or ""
        return ChatReply(text=text, tokens_in=tokens_in, tokens_out=tokens_out)

    def cost_usd(self) -> float | None:
        """Estimated cost of every call made so far with this client."""
        return estimate_cost(self.model, self.tokens_in, self.tokens_out)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Dollar cost for a token count, or None if we don't know the model's price."""
    if model not in PRICES_PER_MILLION:
        return None
    price_in, price_out = PRICES_PER_MILLION[model]
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
