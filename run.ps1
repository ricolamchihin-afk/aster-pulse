<#
    Aster Pulse - live abnormal-move scanner for Aster DEX perps.

        .\run.ps1              live + dashboard at http://127.0.0.1:8787
        .\run.ps1 -NoServe     terminal feed only
        .\run.ps1 -Test        self-check, no network

    Creates .venv and installs dependencies on first run, then launches.
    If PowerShell blocks the script:
        powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
param(
    [switch]$Test,      # run the self-check instead of connecting
    [switch]$NoServe    # skip the dashboard, print to the terminal only
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'creating .venv' -ForegroundColor DarkGray
    uv venv
}
if (-not (Test-Path '.venv\Lib\site-packages\websockets')) {
    Write-Host 'installing dependencies' -ForegroundColor DarkGray
    uv pip install -r requirements.txt
}

$flags = @()
if ($Test) { $flags += '--test' } elseif (-not $NoServe) { $flags += '--serve' }
& '.venv\Scripts\python.exe' aster_pulse.py @flags
