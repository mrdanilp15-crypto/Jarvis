# JARVIS starten: Docker Desktop starten, auf JARVIS warten, JARVIS als eigenes Fenster öffnen.
# install-autostart.ps1 kopiert dieses Skript nach %LOCALAPPDATA%\JARVIS und legt Verknüpfungen im Autostart und
# auf dem Desktop an, die es unsichtbar starten. Protokoll: %LOCALAPPDATA%\JARVIS\launcher.log
# Kompatibel mit Windows PowerShell 5.1.

$ErrorActionPreference = 'Continue'   # Fehler von docker/wsl selbst auswerten (PS 5.1 wirft sonst bei stderr)
$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$logFile = Join-Path $jarvisHome 'launcher.log'
$profileDir = Join-Path $jarvisHome 'browser'
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $jarvisHome 'config.json') | ConvertFrom-Json
$base = 'http://127.0.0.1:8080'

function Write-Log([string]$message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $message
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Show-Problem([string]$message) {
    Write-Log "FEHLER: $message"
    Add-Type -AssemblyName PresentationFramework
    [void][System.Windows.MessageBox]::Show("$message`n`nDetails: $logFile", 'JARVIS', 'OK', 'Warning')
}

function Test-Jarvis {
    try {
        $response = Invoke-WebRequest -Uri "$base/v1/system/health" -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-Docker {
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-For([scriptblock]$condition, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $condition) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Start-Jarvis {
    # Die Container starten mit Docker von selbst (restart: unless-stopped). „docker compose up“ holt sie zurück,
    # falls sie mit „start.sh stop“ entfernt wurden, und tut sonst nichts.
    if ($config.mode -eq 'wsl') {
        $wslArgs = @()
        if ($config.distro) { $wslArgs += @('-d', $config.distro) }
        $wslArgs += @('--cd', "$($config.repo)/deploy", '--', 'docker', 'compose', 'up', '-d', 'jarvis-core')
        $output = & wsl.exe @wslArgs 2>&1
    } else {
        Push-Location (Join-Path $config.repo 'deploy')
        try {
            $output = & docker compose up -d jarvis-core 2>&1
        } finally {
            Pop-Location
        }
    }
    Write-Log ('docker compose up: ' + (($output | Out-String).Trim()))
}

function Find-Browser {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { $_ }
    foreach ($relative in @('Google\Chrome\Application\chrome.exe', 'Microsoft\Edge\Application\msedge.exe')) {
        foreach ($root in $roots) {
            $candidate = Join-Path $root $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Open-Jarvis {
    $url = $config.url
    if (Test-Path $profileDir) {
        # Das eigene Browserprofil kennt Token und Einstellungen schon – „wake=1“ nur beim ersten Start setzen,
        # damit ein späteres Ausschalten der „Jarvis“-Aktivierung erhalten bleibt
        $url = $url -replace '[&#]?wake=1', ''
    }
    $browser = Find-Browser
    if (-not $browser) {
        Write-Log 'Chrome/Edge nicht gefunden – öffne den Standardbrowser'
        Start-Process $url
        return
    }
    # Eigenes Profil: eigenes Fenster ohne Adressleiste; die Autoplay-Freigabe erlaubt JARVIS zu sprechen,
    # ohne dass vorher jemand in die Seite klicken muss
    $arguments = @(
        "--app=$url",
        "--user-data-dir=`"$profileDir`"",
        '--autoplay-policy=no-user-gesture-required',
        '--no-first-run',
        '--no-default-browser-check',
        '--window-size=1280,800'
    )
    Start-Process -FilePath $browser -ArgumentList $arguments
    Write-Log "Fenster geöffnet ($browser)"
}

Write-Log "Start (Modus: $($config.mode), Repository: $($config.repo))"
if (-not (Test-Jarvis)) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Show-Problem 'Docker Desktop ist nicht installiert (Befehl "docker" nicht gefunden).'
        exit 1
    }
    if (-not (Test-Docker)) {
        $dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        Write-Log 'Docker läuft nicht – starte Docker Desktop'
        if (Test-Path $dockerDesktop) { Start-Process -FilePath $dockerDesktop -WindowStyle Minimized }
        if (-not (Wait-For { Test-Docker } 300)) {
            Show-Problem 'Docker Desktop ist nach 5 Minuten noch nicht bereit. Bitte Docker Desktop öffnen und prüfen.'
            exit 1
        }
    }
    Start-Jarvis
    if (-not (Wait-For { Test-Jarvis } 300)) {
        Show-Problem 'JARVIS antwortet nicht. Im Jarvis-Ordner "./deploy/start.sh" ausführen und die Meldungen prüfen.'
        exit 1
    }
}
Open-Jarvis
