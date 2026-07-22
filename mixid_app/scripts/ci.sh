#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

docker compose config --quiet
docker buildx build --check -f Dockerfile .
docker buildx build --check -f browser-controller.Dockerfile .
docker build --target test -t scripper-suite-test .
docker run --rm scripper-suite-test
docker compose build browser-controller scripper
