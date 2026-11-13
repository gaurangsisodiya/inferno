#!/usr/bin/env bash
# Reproducibly fetch QuixBugs into data/quixbugs and move the reference
# (correct) Python and Java programs out of the repo so the repair agent can never read them.
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

# 2. Move the reference solutions (Python and Java) out of the repo.
for name in correct_python_programs correct_java_programs; do
    src="$DEST/$name"
    dst="$PRIVATE_DIR/$name"
    if [[ -d "$src" ]]; then
        if [[ -e "$dst" ]]; then
            echo "$dst already exists; removing the in-repo copy instead of overwriting it."
            rm -rf "$src"
        else
            mkdir -p "$PRIVATE_DIR"
            mv "$src" "$dst"
            echo "Moved $name -> $dst"
        fi
    else
        echo "$name not in repo (already moved)."
    fi
done
