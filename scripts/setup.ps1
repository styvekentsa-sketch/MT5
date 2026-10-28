<#
.SYNOPSIS
    Installe et configure le bot : environnement Python, dépendances, fichier .env, dashboard.

.DESCRIPTION
    - crée .venv (Python 3.12 via uv) s'il n'existe pas, installe requirements-dev.txt
    - crée ou complète .env : API activée avec une API_KEY générée, mode PAPER (DRY_RUN=true),
      chemin du terminal MT5 détecté automatiquement, flux RSS hors service retirés
    - demande les identifiants MT5 (mot de passe masqué) s'ils ne sont pas encore renseignés
    - vérifie la configuration, installe les dépendances du dashboard

    Ré-exécutable sans risque : les valeurs déjà configurées sont conservées.

.PARAMETER SkipCredentials
    Ne pas demander les identifiants MT5 (exécution non interactive).

.PARAMETER SkipDependencies
    Ne pas (ré)installer les dépendances Python et npm.

.EXAMPLE
    .\scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipCredentials,
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$EnvFile = Join-Path $Root ".env"
$EnvExample = Join-Path $Root ".env.example"

# Valeurs d'exemple de .env.example : à remplacer par les vrais identifiants
$PlaceholderAccount = "12345678"
$PlaceholderPassword = "change_me"
$DeadFeeds = @("feeds.reuters.com")
$ApiKeyMinLength = 16

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    !   $Message" -ForegroundColor Yellow }

# --------------------------------------------------------------------- .env

function Read-EnvFile([string]$Path) {
    $values = [ordered]@{}
    foreach ($line in Get-Content $Path -Encoding utf8) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=(.*)$') {
            $values[$Matches[1]] = $Matches[2].Trim().Trim("'").Trim('"')
        }
    }
    return $values
}

function Format-EnvValue([string]$Value) {
    # Guillemets simples (littéraux pour python-dotenv) dès qu'un caractère spécial est présent
    if ($Value -match '^[A-Za-z0-9_.:/\\,@+-]*$') { return $Value }
    if ($Value -notmatch "'") { return "'$Value'" }
    return '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

function Set-EnvValues([string]$Path, [hashtable]$Updates) {
    $lines = [System.Collections.Generic.List[string]](Get-Content $Path -Encoding utf8)
    foreach ($key in $Updates.Keys) {
        $newLine = "$key=$(Format-EnvValue $Updates[$key])"
        $index = $lines.FindIndex([Predicate[string]] { param($l) $l -match "^\s*$key\s*=" })
        if ($index -ge 0) { $lines[$index] = $newLine } else { $lines.Add($newLine) }
    }
    # UTF-8 sans BOM
    [System.IO.File]::WriteAllLines($Path, $lines, [System.Text.UTF8Encoding]::new($false))
}

function Find-Mt5Terminal {
    $candidates = @(
        "$env:ProgramFiles\MetaTrader 5\terminal64.exe",
        "${env:ProgramFiles(x86)}\MetaTrader 5\terminal64.exe"
    ) + @(Get-ChildItem $env:ProgramFiles, ${env:ProgramFiles(x86)} -Filter terminal64.exe -Recurse -Depth 2 `
            -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    return $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function New-ApiKey {
    return (& $Python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
}

# --------------------------------------------------------------------- 1. Python

Write-Step "Environnement Python (.venv)"
if (-not (Test-Path $Python)) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv est introuvable : installez-le (https://docs.astral.sh/uv/) puis relancez ce script."
    }
    uv venv --python 3.12 (Join-Path $Root ".venv")
}
Write-Ok (& $Python --version)

if (-not $SkipDependencies) {
    Write-Step "Dépendances Python (requirements-dev.txt)"
    uv pip install --python $Python -r (Join-Path $Root "requirements-dev.txt") --quiet
    if ($LASTEXITCODE -ne 0) { throw "Échec de l'installation des dépendances Python" }
    Write-Ok "dépendances à jour"
}

# --------------------------------------------------------------------- 2. .env

Write-Step "Fichier .env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Ok ".env créé depuis .env.example"
}
$current = Read-EnvFile $EnvFile
$updates = @{}

# API activée avec une clé valide (conservée si elle existe déjà)
$updates["API_ENABLED"] = "true"
if (-not $current["API_KEY"] -or $current["API_KEY"].Length -lt $ApiKeyMinLength) {
    $updates["API_KEY"] = New-ApiKey
    Write-Ok "API_KEY générée"
}
if (-not $current.Contains("DRY_RUN")) { $updates["DRY_RUN"] = "true" }

# Chemin du terminal : vide si introuvable (sinon la validation de la config échoue)
$terminal = Find-Mt5Terminal
if ($terminal) {
    $updates["MT5_PATH"] = $terminal
    Write-Ok "terminal MT5 trouvé : $terminal"
} elseif ($current["MT5_PATH"] -and -not (Test-Path $current["MT5_PATH"])) {
    $updates["MT5_PATH"] = ""
    Write-Warn "MetaTrader 5 n'est pas installé : MT5_PATH vidé (installez le terminal 64 bits de votre broker)"
}

# Flux RSS hors service
$feeds = @(($current["NEWS_RSS_FEEDS"] -split ",") | Where-Object { $_ })
$alive = @($feeds | Where-Object { $feed = $_; -not ($DeadFeeds | Where-Object { $feed -like "*$_*" }) })
if ($alive.Count -ne $feeds.Count) {
    $updates["NEWS_RSS_FEEDS"] = ($alive -join ",")
    Write-Ok "flux RSS hors service retirés"
}

# Identifiants MT5 (saisis localement, jamais affichés)
$needsCredentials = ($current["MT5_ACCOUNT"] -in @("", $PlaceholderAccount, $null)) -or
                    ($current["MT5_PASSWORD"] -in @("", $PlaceholderPassword, $null))
if ($needsCredentials -and -not $SkipCredentials) {
    Write-Host "`n    Identifiants du compte MetaTrader 5 (de préférence un compte DÉMO) :" -ForegroundColor White
    do { $account = Read-Host "    Numéro de compte" } until ($account -match '^\d+$')
    $server = Read-Host "    Serveur (nom exact affiché dans MT5, ex : MetaQuotes-Demo)"
    $secure = Read-Host "    Mot de passe (masqué)" -AsSecureString
    $updates["MT5_ACCOUNT"] = $account
    $updates["MT5_SERVER"] = $server.Trim()
    $updates["MT5_PASSWORD"] = [System.Net.NetworkCredential]::new("", $secure).Password
    $symbols = Read-Host "    Symboles à trader [$($current["TRADING_SYMBOLS"])] (Entrée = conserver)"
    if ($symbols.Trim()) { $updates["TRADING_SYMBOLS"] = ($symbols -replace '\s', '') }
} elseif ($needsCredentials) {
    Write-Warn "identifiants MT5 à renseigner : relancez .\scripts\setup.ps1 (sans -SkipCredentials)"
}

Set-EnvValues $EnvFile $updates
Write-Ok ".env mis à jour ($($updates.Count) valeur(s))"

# --------------------------------------------------------------------- 3. Vérification

Write-Step "Vérification de la configuration"
Push-Location $Root
try {
    & $Python -c "from config import settings; print(settings.summary())"
    if ($LASTEXITCODE -ne 0) { throw "Configuration invalide : corrigez .env (message ci-dessus)" }
} finally { Pop-Location }

# --------------------------------------------------------------------- 4. Dashboard

if (-not $SkipDependencies) {
    Write-Step "Dashboard (npm)"
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Push-Location (Join-Path $Root "dashboard")
        try {
            npm install --no-fund --no-audit --loglevel=error
            if ($LASTEXITCODE -ne 0) { throw "Échec de npm install" }
            Write-Ok "dépendances du dashboard installées"
        } finally { Pop-Location }
    } else {
        Write-Warn "Node.js introuvable : dashboard ignoré (https://nodejs.org)"
    }
}

# --------------------------------------------------------------------- Résumé

$final = Read-EnvFile $EnvFile
Write-Host "`nConfiguration terminée." -ForegroundColor Green
Write-Host "  API      : http://$($final["API_HOST"]):$($final["API_PORT"])  (documentation : /docs)"
Write-Host "  API_KEY  : $($final["API_KEY"])"
Write-Host "  Mode     : $(if ($final["DRY_RUN"] -eq 'false') { 'LIVE' } else { 'PAPER (simulation)' })"
if (-not (Find-Mt5Terminal)) {
    Write-Warn "MetaTrader 5 absent : installez-le, connectez-vous au compte et activez « Algo Trading »."
}
Write-Host "`nLancement : .\scripts\start.ps1 -Dashboard" -ForegroundColor White
