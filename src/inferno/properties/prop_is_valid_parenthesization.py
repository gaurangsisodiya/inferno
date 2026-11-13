"""is_valid_parenthesization(parens): parens is a string of '(' and ')';
returns whether they are properly nested."""

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
is_valid_parenthesization = time_limited(importlib.import_module(
    f"{_PACKAGE}.is_valid_parenthesization"
).is_valid_parenthesization)

# Properly nested strings, built from the grammar  S -> "" | "(" S ")" | S S.
NESTED = st.recursive(
    st.just(""),
    lambda s: st.builds(lambda x: f"({x})", s) | st.builds(lambda x, y: x + y, s, s),
    max_leaves=20,
)


def reduces_to_empty(s: str) -> bool:
    """Repeatedly delete adjacent "()" pairs. Only properly nested strings vanish."""
    while "()" in s:
        s = s.replace("()", "")
    return s == ""


@SETTINGS
@given(NESTED)
def test_nested_strings_are_valid(s):
    assert is_valid_parenthesization(s) is True


@SETTINGS
@given(st.text(alphabet="()", max_size=30))
def test_agrees_with_pair_deletion(s):
    assert bool(is_valid_parenthesization(s)) == reduces_to_empty(s)


@SETTINGS
@given(NESTED, st.integers(min_value=1, max_value=5))
def test_extra_open_parens_are_invalid(s, n):
    assert not is_valid_parenthesization(s + "(" * n)
