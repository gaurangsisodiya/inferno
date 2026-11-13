"""bucketsort(arr, k): arr holds ints in [0, k); returns them in sorted order."""

from collections import Counter

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
bucketsort = time_limited(importlib.import_module(f"{_PACKAGE}.bucketsort").bucketsort)


@st.composite
def arr_and_k(draw):
    k = draw(st.integers(min_value=1, max_value=30))
    arr = draw(st.lists(st.integers(min_value=0, max_value=k - 1), max_size=50))
    return arr, k


@SETTINGS
@given(arr_and_k())
def test_output_is_ordered(case):
    arr, k = case
    out = bucketsort(list(arr), k)
    assert all(out[i] <= out[i + 1] for i in range(len(out) - 1))


@SETTINGS
@given(arr_and_k())
def test_output_is_a_permutation_of_input(case):
    arr, k = case
    assert Counter(bucketsort(list(arr), k)) == Counter(arr)
