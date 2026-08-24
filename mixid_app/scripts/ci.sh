#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

docker compose config --quiet
docker buildx build --check -f Dockerfile .
docker buildx build --check -f browser-controller.Dockerfile .
docker build --target test -t scripper-suite-test .
docker run --rm scripper-suite-test

# Dependency freshness. Informational: an upstream release must not fail the
# gate, but a stale extractor (yt-dlp) silently breaks downloads, so report it.
echo "--- dependency update check ---"
docker run --rm scripper-suite-test python3 scripts/check_updates.py || \
    echo "check-updates: skipped (no network?)"

docker compose build browser-controller scripper
