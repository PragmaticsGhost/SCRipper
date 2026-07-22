$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

docker compose config --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker buildx build --check -f Dockerfile .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker buildx build --check -f browser-controller.Dockerfile .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker build --target test -t scripper-suite-test .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker run --rm scripper-suite-test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker compose build browser-controller scripper
exit $LASTEXITCODE

