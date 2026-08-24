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

# Dependency freshness. Informational: an upstream release must not fail the
# gate, but a stale extractor (yt-dlp) silently breaks downloads, so report it.
Write-Host "--- dependency update check ---"
docker run --rm scripper-suite-test python3 scripts/check_updates.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "check-updates: skipped (no network?)" -ForegroundColor Yellow
}

docker compose build browser-controller scripper
exit $LASTEXITCODE
