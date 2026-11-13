"""powerset(arr): arr has no duplicates; returns every subset of arr, each once."""

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
powerset = time_limited(importlib.import_module(f"{_PACKAGE}.powerset").powerset)

ARRS = st.lists(st.integers(-50, 50), unique=True, max_size=8)


@SETTINGS
@given(ARRS)
def test_has_two_to_the_n_subsets(arr):
    assert len(powerset(list(arr))) == 2 ** len(arr)


@SETTINGS
@given(ARRS)
def test_each_item_is_a_subset_without_repeats(arr):
    for subset in powerset(list(arr)):
        assert len(set(subset)) == len(subset)
        assert set(subset) <= set(arr)


@SETTINGS
@given(ARRS)
def test_subsets_are_all_different_and_include_empty_and_full(arr):
    subsets = [frozenset(s) for s in powerset(list(arr))]
    assert len(set(subsets)) == len(subsets)
    assert frozenset() in subsets and frozenset(arr) in subsets
