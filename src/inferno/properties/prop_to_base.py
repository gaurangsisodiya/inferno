"""to_base(num, b): num > 0, 2 <= b <= 36; returns num written in base b
using the digits 0-9 then A-Z."""

import string

import importlib
import itertools
import os
import signal
import types

from hypothesis import Phase, given, settings
from hypothesis import strategies as st

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    # No shrinking: the first failing example is enough, and shrinking would
    # re-run slow or looping candidates many more times.
    phases=[Phase.explicit, Phase.generate],
)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")

CALL_LIMIT_S = 2.0  # CPU seconds per call; buggy versions often loop forever
MAX_ITEMS = 100_000  # most items taken from a returned generator


class CallTimeout(Exception):
    """One call ran too long. A plain Exception, so Hypothesis reports a normal failure."""


def time_limited(fn):
    """Cap each call to fn (including draining a returned generator) at CALL_LIMIT_S of CPU.

    Uses SIGVTALRM, so it doesn't clash with pytest-timeout's SIGALRM.
    """

    def _raise(signum, frame):
        raise CallTimeout(f"{fn.__name__} used more than {CALL_LIMIT_S}s of CPU")

    def wrapper(*args):
        old = signal.signal(signal.SIGVTALRM, _raise)
        signal.setitimer(signal.ITIMER_VIRTUAL, CALL_LIMIT_S)
        try:
            out = fn(*args)
            if isinstance(out, types.GeneratorType):
                out = list(itertools.islice(out, MAX_ITEMS + 1))
                if len(out) > MAX_ITEMS:
                    raise CallTimeout(f"{fn.__name__} yielded more than {MAX_ITEMS} items")
            return out
        finally:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            signal.signal(signal.SIGVTALRM, old)

    return wrapper
to_base = time_limited(importlib.import_module(f"{_PACKAGE}.to_base").to_base)

NUMS = st.integers(min_value=1, max_value=10**15)
BASES = st.integers(min_value=2, max_value=36)


@SETTINGS
@given(NUMS, BASES)
def test_parses_back_to_the_same_number(num, b):
    assert int(to_base(num, b), b) == num


@SETTINGS
@given(NUMS, BASES)
def test_uses_only_valid_uppercase_digits_without_leading_zero(num, b):
    out = to_base(num, b)
    digits = (string.digits + string.ascii_uppercase)[:b]
    assert out and all(c in digits for c in out)
    assert out[0] != "0"
