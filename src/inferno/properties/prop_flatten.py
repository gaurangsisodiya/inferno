"""flatten(arr): yields arr's non-list objects in order, at any nesting depth."""

import re

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
flatten = importlib.import_module(f"{_PACKAGE}.flatten").flatten

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
