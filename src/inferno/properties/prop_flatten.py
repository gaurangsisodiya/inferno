"""flatten(arr): yields arr's non-list objects in order, at any nesting depth."""

import re

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
flatten = time_limited(importlib.import_module(f"{_PACKAGE}.flatten").flatten)

NESTED = st.lists(
    st.recursive(st.integers(-99, 99), lambda inner: st.lists(inner, max_size=4), max_leaves=25),
    max_size=6,
)


@SETTINGS
@given(NESTED)
def test_output_has_no_lists(arr):
    assert all(isinstance(x, int) for x in flatten(arr))


@SETTINGS
@given(NESTED)
def test_output_is_leaves_in_written_order(arr):
    # The integers, read left to right in the printed nested list, are the leaves in order.
    leaves = [int(t) for t in re.findall(r"-?\d+", repr(arr))]
    assert list(flatten(arr)) == leaves
