"""gcd(a, b): a, b nonnegative ints; returns the greatest int dividing both."""

import math

from hypothesis import assume

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
gcd = importlib.import_module(f"{_PACKAGE}.gcd").gcd

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
