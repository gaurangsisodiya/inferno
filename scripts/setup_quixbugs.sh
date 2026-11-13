#!/usr/bin/env bash
# Reproducibly fetch QuixBugs into data/quixbugs and move the reference
# (correct) programs out of the repo so the repair agent can never read them.
#
# Usage: scripts/setup_quixbugs.sh
# Env overrides:
#   QUIXBUGS_URL     (default: https://github.com/jkoppel/QuixBugs)
#   QUIXBUGS_REF     (optional commit/tag to check out for exact reproducibility)
#   INFERNO_PRIVATE  (default: ~/inferno-private)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/data/quixbugs"
URL="${QUIXBUGS_URL:-https://github.com/jkoppel/QuixBugs}"
PRIVATE_DIR="${INFERNO_PRIVATE:-$HOME/inferno-private}"

# 1. Clone (skip if already present).
if [[ -d "$DEST/.git" ]]; then
    echo "QuixBugs already cloned at $DEST"
else
    mkdir -p "$(dirname "$DEST")"
    git clone "$URL" "$DEST"
fi
if [[ -n "${QUIXBUGS_REF:-}" ]]; then
    git -C "$DEST" checkout --quiet "$QUIXBUGS_REF"
fi
echo "QuixBugs at commit $(git -C "$DEST" rev-parse HEAD)"

# 2. Move correct_python_programs out of the repo.
SRC_CORRECT="$DEST/correct_python_programs"
DST_CORRECT="$PRIVATE_DIR/correct_python_programs"
if [[ -d "$SRC_CORRECT" ]]; then
    if [[ -e "$DST_CORRECT" ]]; then
        echo "$DST_CORRECT already exists; removing the in-repo copy instead of overwriting it."
        rm -rf "$SRC_CORRECT"
    else
        mkdir -p "$PRIVATE_DIR"
        mv "$SRC_CORRECT" "$DST_CORRECT"
        echo "Moved correct_python_programs -> $DST_CORRECT"
    fi
else
    echo "correct_python_programs not in repo (already moved)."
fi
