#!/usr/bin/env pwsh
# PredictOps — Full Setup & Run Script
# Run from: c:\Users\madhu\OneDrive\Desktop\PredictOps\
# Usage: .\start.ps1

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BACKEND = Join-Path $ROOT "backend"
$FRONTEND = Join-Path $ROOT "frontend"
$DATA = Join-Path $ROOT "data"
$VENV_PYTHON = Join-Path $BACKEND ".venv\Scripts\python.exe"
$VENV_ACTIVATE = Join-Path $BACKEND ".venv\Scripts\Activate.ps1"

Write-Host "=== PredictOps Startup ===" -ForegroundColor Cyan

# ── Step 1: Check venv ────────────────────────────────────────────────────────
if (-not (Test-Path $VENV_PYTHON)) {
    Write-Host "[1/6] Creating Python 3.13 virtual environment..." -ForegroundColor Yellow
    Set-Location $BACKEND
    py -3.13 -m venv .venv
    & $VENV_ACTIVATE
    pip install -r requirements.txt
} else {
    Write-Host "[1/6] Virtual environment found. ✓" -ForegroundColor Green
}

# ── Step 2: Generate data ─────────────────────────────────────────────────────
$DB_PATH = Join-Path $BACKEND "predictops.db"
if (-not (Test-Path $DB_PATH)) {
    Write-Host "[2/6] Generating synthetic data (20 machines, 90 days)..." -ForegroundColor Yellow
    Set-Location $BACKEND
    & $VENV_PYTHON (Join-Path $DATA "generate_synthetic_data.py")
    & $VENV_PYTHON (Join-Path $DATA "seed_demo.py")
} else {
    Write-Host "[2/6] Database already exists. Skipping data generation. ✓" -ForegroundColor Green
}

# ── Step 3: Train ML model ────────────────────────────────────────────────────
$MODEL_PATH = Join-Path $BACKEND "app\ml\model.joblib"
if (-not (Test-Path $MODEL_PATH)) {
    Write-Host "[3/6] Training ML model (GradientBoostingClassifier)..." -ForegroundColor Yellow
    Set-Location $BACKEND
    & $VENV_PYTHON "app\ml\train.py"
} else {
    Write-Host "[3/6] Model artifact found. Skipping training. ✓" -ForegroundColor Green
}

# ── Step 4: Validate health ───────────────────────────────────────────────────
Write-Host "[4/6] Starting backend server..." -ForegroundColor Yellow
Set-Location $BACKEND
$backendJob = Start-Job -ScriptBlock {
    param($backend, $activate)
    Set-Location $backend
    & $activate
    uvicorn app.main:app --host 0.0.0.0 --port 8000
} -ArgumentList $BACKEND, $VENV_ACTIVATE

Start-Sleep -Seconds 4

try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 5
    Write-Host "[4/6] Backend: $($health.status) | DB: $($health.database) | Demo: $($health.demo_mode) ✓" -ForegroundColor Green
} catch {
    Write-Host "[4/6] Backend starting... check http://localhost:8000/api/health" -ForegroundColor Yellow
}

# ── Step 5: Frontend ─────────────────────────────────────────────────────────
Write-Host "[5/6] Starting frontend dev server..." -ForegroundColor Yellow
Set-Location $FRONTEND
$frontendJob = Start-Job -ScriptBlock {
    param($frontend)
    Set-Location $frontend
    npm run dev
} -ArgumentList $FRONTEND

Start-Sleep -Seconds 3

# ── Step 6: Open browser ──────────────────────────────────────────────────────
Write-Host "[6/6] Opening PredictOps in browser..." -ForegroundColor Yellow
Start-Process "http://localhost:5173"

Write-Host ""
Write-Host "=== PredictOps is running! ===" -ForegroundColor Green
Write-Host "  Frontend:  http://localhost:5173" -ForegroundColor Cyan
Write-Host "  Backend:   http://localhost:8000" -ForegroundColor Cyan
Write-Host "  API Docs:  http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop all services." -ForegroundColor Gray

# Keep running
try {
    while ($true) { Start-Sleep -Seconds 5 }
} finally {
    Stop-Job $backendJob, $frontendJob
    Remove-Job $backendJob, $frontendJob
    Write-Host "Services stopped." -ForegroundColor Yellow
}
