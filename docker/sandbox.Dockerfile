# Image used by src/inferno/sandbox.py to run QuixBugs tests in isolation.
# It contains only Python + pytest; the program and its tests are mounted
# read-only at run time. Built automatically on first use as inferno-sandbox:py3.12.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==9.2.0 pytest-timeout==2.4.0
