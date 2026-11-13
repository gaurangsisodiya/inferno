"""sqrt(x, epsilon): x >= 1, epsilon > 0; returns a float within epsilon of sqrt(x)."""

import math

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
sqrt = time_limited(importlib.import_module(f"{_PACKAGE}.sqrt").sqrt)

# Bounded so float rounding (about x * 1e-16) stays far below epsilon.
XS = st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
EPS = st.floats(min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False)


@SETTINGS
@given(XS, EPS)
def test_within_epsilon_of_true_root(x, epsilon):
    assert abs(sqrt(x, epsilon) - math.sqrt(x)) <= epsilon


@SETTINGS
@given(st.integers(min_value=1, max_value=1000), EPS)
def test_perfect_squares(n, epsilon):
    assert abs(sqrt(float(n * n), epsilon) - n) <= epsilon
