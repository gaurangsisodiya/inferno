# Image used by src/inferno/sandbox.py to run QuixBugs tests and Hypothesis
# properties in isolation. It contains only Python + test libraries; the program
# and its tests are mounted read-only at run time. The image tag includes a hash
# of this file, so editing it makes ensure_image() build a fresh image.
# Keep hypothesis pinned to the same version as pyproject.toml.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==9.2.0 pytest-timeout==2.4.0 \
    hypothesis==6.170.15 sortedcontainers==2.4.0
