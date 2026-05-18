#!/usr/bin/env bash
# Remove all build/test artifacts so the working tree contains only source.
# As more components get added, extend the lists below.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

# 1. Kernel module artifacts (Kbuild knows the full list, so defer to it).
make -C "${ROOT}/server/module" clean

# 2. Python bytecode caches scattered through the tree.
#    -path ./.git -prune skips the git dir; -print0/xargs -0 handles odd names.
find "$ROOT" -path "${ROOT}/.git" -prune -o -type d -name __pycache__ -print0 \
    | xargs -0 -r rm -rf

# 3. pytest's cache (created at the rootdir each run).
rm -rf "${ROOT}/.pytest_cache"

echo "Clean."
