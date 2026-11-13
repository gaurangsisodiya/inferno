"""Hypothesis properties, one file per QuixBugs program: prop_<program>.py.

Each file is a standalone pytest module. It is mounted read-only into the
sandbox next to the candidate program, so it must not import anything from
`inferno`. It imports the program under test from the package named by
INFERNO_PROGRAM_PACKAGE: `python_programs` by default (the candidate), or
`correct_python_programs` when scripts/check_properties.py runs it against
the reference version.

The properties describe the output from the program's spec. They are not
copies of the reference solutions. Every test uses
@settings(max_examples=200, deadline=None, derandomize=True, database=None).
derandomize makes a validation run reproducible: the same candidate always
gets the same examples. database=None keeps Hypothesis from writing a
.hypothesis/ directory.
"""
