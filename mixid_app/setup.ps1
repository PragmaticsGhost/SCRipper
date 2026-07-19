# SCRipper Suite — one-shot Windows setup.
# Checks/install Docker Desktop, starts the engine, builds & starts the
# app, then opens http://localhost:8080 in your browser.
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== SCRipper Suite setup ===" -ForegroundColor Cyan
Write-Host ""

function Test-DockerCli {
    try { docker --version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}
function Test-DockerEngine {
    try { docker info *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# --- 1. Docker Desktop installed? ---
if (-not (Test-DockerCli)) {
    Write-Host "Docker Desktop is not installed." -ForegroundColor Yellow
    $ans = Read-Host "Install it now with winget? (y/n)"
    if ($ans -ne "y") {
        Write-Host "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and re-run this script."
        exit 1
    }
    winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
    Write-Host ""
    Write-Host "Docker Desktop installed. Windows may need a sign-out or restart to finish." -ForegroundColor Green
    Write-Host "After that, re-run this script to continue."
    exit 0
}

# --- 2. Docker engine running? ---
if (-not (Test-DockerEngine)) {
    Write-Host "Starting Docker Desktop..."
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
        Start-Process $dd
    } else {
        Write-Host "Could not find Docker Desktop.exe — start it manually, then press Enter."
        Read-Host | Out-Null
    }
    Write-Host "Waiting for the Docker engine (this can take a minute)..."
    $up = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        if (Test-DockerEngine) { $up = $true; break }
    }
    if (-not $up) {
        Write-Host "The Docker engine did not come up. Open Docker Desktop, wait for it to say 'running', then re-run this script." -ForegroundColor Red
        exit 1
    }
}
Write-Host "Docker engine is running." -ForegroundColor Green

# --- 3. Build and start the app ---
Set-Location $PSScriptRoot
Write-Host ""
Write-Host "Building and starting SCRipper Suite."
Write-Host "The FIRST build compiles the fingerprinting engine and takes ~10 minutes;"
Write-Host "later runs start in seconds."
Write-Host ""
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker compose failed — scroll up for the error." -ForegroundColor Red
    exit 1
}

# --- 4. Wait for the app, then open the browser ---
Write-Host "Waiting for the app to come up..."
$healthy = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080/api/library" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}
if ($healthy) {
    Write-Host ""
    Write-Host "SCRipper Suite is running:  http://localhost:8080" -ForegroundColor Green
    Start-Process "http://localhost:8080"
} else {
    Write-Host "The containers started but the app isn't answering yet." -ForegroundColor Yellow
    Write-Host "Give it a minute, then open http://localhost:8080"
}
