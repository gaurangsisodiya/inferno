"""bitcount(n): n a nonnegative int; returns the number of 1-bits in n."""

import importlib
import os

from hypothesis import given, settings
from hypothesis import strategies as st

SETTINGS = settings(max_examples=200, deadline=None, derandomize=True, database=None)
_PACKAGE = os.environ.get("INFERNO_PROGRAM_PACKAGE", "python_programs")
bitcount = importlib.import_module(f"{_PACKAGE}.bitcount").bitcount

NONNEG = st.integers(min_value=0, max_value=2**64)


@SETTINGS
@given(NONNEG)
def test_matches_binary_representation(n):
    assert bitcount(n) == bin(n).count("1")


@SETTINGS
@given(NONNEG)
def test_doubling_keeps_count_and_adding_one_bit_adds_one(n):
    assert bitcount(2 * n) == bitcount(n)
    assert bitcount(2 * n + 1) == bitcount(n) + 1
