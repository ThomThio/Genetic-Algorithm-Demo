# Pull both repos onto the Windows MT4 box, install, and run read-only tests.
# Run in PowerShell from the folder that holds (or should hold) the repos:
#   powershell -ExecutionPolicy Bypass -File windows_setup.ps1
# Clones into a SEPARATE test folder so the live bot's checkout
# (Desktop\Dev\MT4-TradeSignals, run by "run_signals bot.bat") is never touched.
# Optional: -DevDir <folder>  -SkipMT4 (no terminal checks)
param(
    [string]$DevDir = "C:\Users\Administrator\Desktop\Dev\fx-research-test",
    [string]$Branch = "claude/fx-polling-mt4-orders-818dkg",
    [string]$Instruments = "GBPUSD,USDJPY",
    [switch]$SkipMT4
)
$ErrorActionPreference = "Stop"

function Sync-Repo($name) {
    $path = Join-Path $DevDir $name
    if (-not (Test-Path $path)) {
        git clone "https://github.com/ThomThio/$name.git" $path
    }
    Push-Location $path
    git fetch origin
    git checkout $Branch
    git pull --ff-only origin $Branch
    Pop-Location
    return $path
}

New-Item -ItemType Directory -Force -Path $DevDir | Out-Null
$ga  = Sync-Repo "Genetic-Algorithm-Demo"
$mt4 = Sync-Repo "MT4-TradeSignals"

Write-Host "`n== Python environment (Genetic-Algorithm-Demo\.venv)"
Push-Location $ga
if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python -m pip install -q --upgrade pip
& .\.venv\Scripts\python -m pip install -q -r requirements.txt

Write-Host "`n== Unit tests: scanner"
& .\.venv\Scripts\python -m pytest -q tests
Write-Host "`n== Unit tests: MT4 research hookup"
Push-Location $mt4
& (Join-Path $ga ".venv\Scripts\python") -m pytest -q tests
Pop-Location

Write-Host "`n== Offline scan on the CSVs in MT4-TradeSignals\currency_data"
& .\.venv\Scripts\python -m fx_research.scan --source csv --csv-dir (Join-Path $mt4 "currency_data") --instruments $Instruments --dry-run --seed 1

if (-not $SkipMT4) {
    Write-Host "`n== Live MT4 bridge check (read-only, no orders). MT4 must be open with ZmqCommunicatorEA attached."
    $env:MT4_TRADESIGNALS_PATH = $mt4
    & .\.venv\Scripts\python -m fx_research.mt4_smoke --instruments $Instruments
}
Pop-Location

Write-Host "`nDone. Next: run sql\fx_research_schema.sql in Supabase, fill .env (see .env.example), then:"
Write-Host "  .venv\Scripts\python -m fx_research.scheduler --once"
