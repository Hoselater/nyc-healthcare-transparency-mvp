# Launches the public app without needing the virtualenv activated.
#
# `streamlit run ...` only works if the venv is active, and activating it in
# PowerShell can also trip the script execution policy. Calling the venv's own
# python with -m sidesteps both.
#
#   .\run_app.ps1            # public value-index app
#   .\run_app.ps1 review     # crosswalk review console

param([string]$Which = "app")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv found. Create it first:" -ForegroundColor Yellow
    Write-Host "    python -m venv venv"
    Write-Host "    venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

$target = if ($Which -eq "review") { "review_crosswalk.py" } else { "app.py" }
$port   = if ($Which -eq "review") { "8502" } else { "8501" }

Write-Host "Starting $target on http://localhost:$port" -ForegroundColor Green
Write-Host "Leave this window open. Ctrl+C to stop." -ForegroundColor DarkGray
& $python -m streamlit run $target --server.port $port
