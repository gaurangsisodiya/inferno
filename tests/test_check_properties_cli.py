"""The property self-check's command line. Never reaches the reference programs."""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_properties.py"


def run(*args, tmp_path):
    env = dict(os.environ, INFERNO_PRIVATE=str(tmp_path / "no-such-private-dir"))
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_help(tmp_path):
    r = run("--help", tmp_path=tmp_path)
    assert r.returncode == 0
    assert "usage:" in r.stdout and "bitcount" in r.stdout


def test_unknown_program_is_rejected(tmp_path):
    r = run("gcd", "not_a_program", tmp_path=tmp_path)
    assert r.returncode == 2
    assert "no property file for: not_a_program" in r.stderr
