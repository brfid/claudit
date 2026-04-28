#!/bin/sh
# Install the pre-commit hook into .git/hooks/.
# Run once per clone.

set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
cp "$REPO_ROOT/scripts/pre-commit" "$REPO_ROOT/.git/hooks/pre-commit"
chmod +x "$REPO_ROOT/.git/hooks/pre-commit"
echo "Installed pre-commit hook at .git/hooks/pre-commit"
