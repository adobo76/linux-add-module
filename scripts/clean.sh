#!/usr/bin/env bash
# Remove all build artifacts and test caches.
# All build outputs now live under ./build/, so this is mostly an rm -rf.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

# Build tree: contains everything Kbuild produced (the .ko plus all
# intermediate .o/.cmd/.mod files) plus the source-file symlinks we set up.
rm -rf "${ROOT}/build"

# Defensive: clean any stale artifacts left in module/ from an
# older in-place build (pre-./build.sh layout).
if compgen -G "${ROOT}/module/.*.cmd" > /dev/null \
   || compgen -G "${ROOT}/module/*.o" > /dev/null; then
    make -C "${ROOT}/module" clean >/dev/null
fi

# Python bytecode caches
find "$ROOT" -path "${ROOT}/.git" -prune -o -type d -name __pycache__ -print0 \
    | xargs -0 -r rm -rf

# pytest cache
rm -rf "${ROOT}/.pytest_cache"

echo "Clean."
