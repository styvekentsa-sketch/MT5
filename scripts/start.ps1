<#
.SYNOPSIS
    Lance le bot de trading et son API ; avec -Dashboard, ouvre aussi le dashboard web.

.DESCRIPTION
    Vérifie l'environnement (.venv, .env, terminal MetaTrader 5), puis exécute main.py.
    Arrêt propre : Ctrl+C.

.PARAMETER Dashboard
    Démarre le dashboard (npm run dev) dans une nouvelle fenêtre et l'ouvre dans le navigateur.

.EXAMPLE
    .\scripts\start.ps1 -Dashboard
#>
[CmdletBinding()]
param([switch]$Dashboard)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$DashboardUrl = "http://localhost:5173"

if (-not (Test-Path $Python)) { throw "Environnement Python absent : lancez d'abord .\scripts\setup.ps1" }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw "Fichier .env absent : lancez d'abord .\scripts\setup.ps1" }

if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    Write-Host "!  Terminal MetaTrader 5 non lancé : le bot tentera de le démarrer via MT5_PATH." -ForegroundColor Yellow
    Write-Host "   S'il n'est pas installé, le bot s'arrêtera après 3 tentatives de connexion." -ForegroundColor Yellow
}

if ($Dashboard) {
    $shell = (Get-Process -Id $PID).Path
    $dashboardDir = Join-Path $Root "dashboard"
    Start-Process $shell -ArgumentList "-NoExit", "-Command", "Set-Location '$dashboardDir'; npm run dev"
    Start-Job { Start-Sleep 4; Start-Process $using:DashboardUrl } | Out-Null
    Write-Host "Dashboard : $DashboardUrl (fenêtre séparée)" -ForegroundColor Cyan
}

Push-Location $Root
try {
    & $Python main.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
