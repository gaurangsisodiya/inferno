"""gcd(a, b): a, b nonnegative ints; returns the greatest int dividing both."""

import math

from hypothesis import assume

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
gcd = time_limited(importlib.import_module(f"{_PACKAGE}.gcd").gcd)

NONNEG = st.integers(min_value=0, max_value=10**12)


@SETTINGS
@given(NONNEG, NONNEG)
def test_result_divides_both_inputs(a, b):
    assume(a or b)  # gcd(0, 0) has no greatest divisor
    g = gcd(a, b)
    assert g > 0 and a % g == 0 and b % g == 0


@SETTINGS
@given(NONNEG, NONNEG)
def test_no_larger_common_divisor_exists(a, b):
    # If a/g and b/g share no factor, every common divisor of a and b divides g,
    # so none is larger than g.
    assume(a or b)
    g = gcd(a, b)
    assert g > 0 and a % g == 0 and b % g == 0
    assert math.gcd(a // g, b // g) == 1


@SETTINGS
@given(st.integers(min_value=1, max_value=5000), st.integers(min_value=1, max_value=5000))
def test_small_inputs_no_larger_divisor_by_search(a, b):
    g = gcd(a, b)
    assert a % g == 0 and b % g == 0
    assert not any(a % d == 0 and b % d == 0 for d in range(g + 1, min(a, b) + 1))
