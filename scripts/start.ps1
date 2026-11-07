<#
.SYNOPSIS
    Lance le bot de trading et son API ; avec -Dashboard, ouvre aussi le dashboard web.

.DESCRIPTION
    Vérifie l'environnement (.venv, .env, terminal MetaTrader 5), puis exécute main.py.
    Ports dédiés : API_PORT (.env, 47800) et DASHBOARD_PORT (dashboard\.env, 47801), hors de la
    plage dynamique de Windows (49152+) : aucun programme ne les reçoit automatiquement.
    Arrêt propre : Ctrl+C (ou -Uninstall pour le démarrage automatique).

.PARAMETER Dashboard
    Démarre le dashboard (npm run dev) et l'ouvre dans le navigateur.

.PARAMETER Hidden
    Mode arrière-plan (démarrage de session) : dashboard sans fenêtre, journal dans logs\dashboard.log,
    navigateur non ouvert.

.PARAMETER Supervise
    Lance MetaTrader 5 s'il est fermé et relance le bot après chaque arrêt (ex. code 4 : MT5 injoignable).

.PARAMETER Install
    Démarrage automatique à chaque ouverture de session Windows (tâche planifiée « MT5 Trading Bot »,
    à défaut un raccourci dans le dossier Démarrage), en mode -Dashboard -Hidden -Supervise.

.PARAMETER Uninstall
    Supprime le démarrage automatique et l'icône du bureau.

.PARAMETER Launch
    Action de l'icône « MT5 Trading Bot » du bureau : démarre le bot (tâche planifiée) s'il ne tourne
    pas encore, attend le dashboard puis l'ouvre dans le navigateur.

.EXAMPLE
    .\scripts\start.ps1 -Dashboard
    .\scripts\start.ps1 -Install
#>
[CmdletBinding()]
param([switch]$Dashboard, [switch]$Hidden, [switch]$Supervise, [switch]$Install, [switch]$Uninstall, [switch]$Launch)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$TaskName = "MT5 Trading Bot"
$StartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"
$DesktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "$TaskName.lnk"
$Icon = Join-Path $Root "dashboard\public\favicon.ico"

function Get-EnvValue([string]$File, [string]$Name, [string]$Default) {
    if (Test-Path $File) {
        $line = Select-String -Path $File -Pattern "^\s*$Name\s*=" | Select-Object -First 1
        if ($line) { return $line.Line.Split("=", 2)[1].Trim().Trim("'", '"') }
    }
    return $Default
}

$EnvFile = Join-Path $Root ".env"
$ApiPort = Get-EnvValue $EnvFile "API_PORT" "8000"
$DashboardPort = Get-EnvValue (Join-Path $Root "dashboard\.env") "DASHBOARD_PORT" "5173"
$DashboardUrl = "http://localhost:$DashboardPort"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $StartupLink, $DesktopLink -ErrorAction SilentlyContinue
    Write-Host "Démarrage automatique et icône du bureau supprimés." -ForegroundColor Cyan
    exit 0
}

function Test-PortBusy([string]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-Url([string]$Url) {
    try { Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3 | Out-Null; $true } catch { $false }
}

if ($Launch) {
    if (-not (Test-Url "http://127.0.0.1:$ApiPort/docs")) {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $TaskName
        } else {
            $shell = (Get-Process -Id $PID).Path
            Start-Process $shell -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", "`"$PSCommandPath`"", "-Dashboard", "-Hidden", "-Supervise"
        }
    }
    for ($i = 0; $i -lt 60 -and -not (Test-Url $DashboardUrl); $i++) { Start-Sleep 2 }
    Start-Process $DashboardUrl
    exit 0
}

if ($Install) {
    $shell = (Get-Process -Id $PID).Path
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Dashboard -Hidden -Supervise"
    try {
        $action = New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $trigger.Delay = "PT30S"                       # laisse le réseau et le bureau se charger
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Description "Bot MT5 + API (port $ApiPort) + dashboard (port $DashboardPort)" -Force | Out-Null
        Write-Host "Tâche planifiée « $TaskName » installée (ouverture de session)." -ForegroundColor Cyan
    } catch {
        # Sans droits suffisants pour le planificateur : raccourci dans le dossier Démarrage.
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut($StartupLink)
        $link.TargetPath = $shell
        $link.Arguments = $arguments
        $link.WorkingDirectory = $Root
        $link.WindowStyle = 7                           # réduit
        $link.Save()
        Write-Host "Raccourci de démarrage créé : $StartupLink" -ForegroundColor Cyan
    }
    $desktop = (New-Object -ComObject WScript.Shell).CreateShortcut($DesktopLink)
    $desktop.TargetPath = $shell
    $desktop.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Launch"
    $desktop.WorkingDirectory = $Root
    $desktop.WindowStyle = 7
    $desktop.Description = "Démarre le bot MT5 et ouvre le dashboard"
    if (Test-Path $Icon) { $desktop.IconLocation = "$Icon,0" }
    $desktop.Save()
    Write-Host "Icône du bureau créée : $DesktopLink" -ForegroundColor Cyan
    Write-Host "API : http://127.0.0.1:$ApiPort · Dashboard : $DashboardUrl" -ForegroundColor Cyan
    exit 0
}

if (-not (Test-Path $Python)) { throw "Environnement Python absent : lancez d'abord .\scripts\setup.ps1" }
if (-not (Test-Path $EnvFile)) { throw "Fichier .env absent : lancez d'abord .\scripts\setup.ps1" }

function Start-Terminal {
    if (Get-Process terminal64 -ErrorAction SilentlyContinue) { return }
    $terminal = Get-EnvValue $EnvFile "MT5_PATH" ""
    if ($terminal -and (Test-Path $terminal)) {
        Write-Host "Lancement de MetaTrader 5 : $terminal" -ForegroundColor Cyan
        Start-Process $terminal
        Start-Sleep 20                                  # connexion au serveur du courtier
    } else {
        Write-Host "!  Terminal MetaTrader 5 non lancé et MT5_PATH introuvable." -ForegroundColor Yellow
        Write-Host "   Le bot s'arrêtera après 3 tentatives de connexion." -ForegroundColor Yellow
    }
}

if ($Dashboard) {
    $dashboardDir = Join-Path $Root "dashboard"
    if (Test-PortBusy $DashboardPort) {
        Write-Host "Dashboard déjà servi sur le port $DashboardPort." -ForegroundColor Cyan
    } elseif ($Hidden) {
        $logDir = Join-Path $Root "logs"
        New-Item -ItemType Directory -Force $logDir | Out-Null
        Start-Process cmd.exe -ArgumentList "/c", "npm run dev > `"$logDir\dashboard.log`" 2>&1" `
            -WorkingDirectory $dashboardDir -WindowStyle Hidden
        Write-Host "Dashboard : $DashboardUrl (journal logs\dashboard.log)" -ForegroundColor Cyan
    } else {
        $shell = (Get-Process -Id $PID).Path
        Start-Process $shell -ArgumentList "-NoExit", "-Command", "Set-Location '$dashboardDir'; npm run dev"
        Start-Job { Start-Sleep 4; Start-Process $using:DashboardUrl } | Out-Null
        Write-Host "Dashboard : $DashboardUrl (fenêtre séparée)" -ForegroundColor Cyan
    }
}

if (Test-PortBusy $ApiPort) {
    Write-Host "Le port $ApiPort est déjà utilisé : le bot tourne sans doute déjà." -ForegroundColor Yellow
    exit 0
}

Push-Location $Root
try {
    if (-not $Supervise) {
        if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
            Write-Host "!  Terminal MetaTrader 5 non lancé : le bot tentera de le démarrer via MT5_PATH." -ForegroundColor Yellow
        }
        & $Python main.py
        exit $LASTEXITCODE
    }
    while ($true) {
        Start-Terminal
        & $Python main.py
        $code = $LASTEXITCODE
        if ($code -eq 0) { exit 0 }                     # arrêt volontaire (Ctrl+C, arrêt depuis l'API)
        Write-Host "Bot arrêté (code $code) : relance dans 60 s." -ForegroundColor Yellow
        Start-Sleep 60
    }
} finally {
    Pop-Location
}
