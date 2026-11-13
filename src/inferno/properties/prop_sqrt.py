"""sqrt(x, epsilon): x >= 1, epsilon > 0; returns a float within epsilon of sqrt(x)."""

import math

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
sqrt = importlib.import_module(f"{_PACKAGE}.sqrt").sqrt

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
