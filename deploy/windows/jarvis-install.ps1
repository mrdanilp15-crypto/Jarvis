# JARVIS als Windows-Programm installieren und starten – ohne Docker.
# Normalerweise per Doppelklick auf "JARVIS installieren.cmd" im Jarvis-Ordner. Erledigt alles selbst:
#   1. Python 3.12 und Ollama installieren, falls sie fehlen (über winget, ohne Administratorrechte)
#   2. JARVIS in eine eigene Python-Umgebung installieren (%LOCALAPPDATA%\JARVIS\venv)
#   3. Zugang anlegen bzw. aus deploy\.env übernehmen, Sprachmodell passend zur Hardware wählen und laden
#   4. Autostart und Desktop-Verknüpfung "JARVIS" anlegen, PC-Steuerung starten, JARVIS öffnen
# Erneut ausführen = aktualisieren (nach "git pull"). Parameter:
#   -Browser chrome|edge   Fenster in Chrome oder Edge (Standard: Edge mit der Stimme „Microsoft Conrad“)
#   -Stop                  JARVIS beenden
#   -Uninstall             Autostart und Verknüpfungen entfernen (Daten bleiben in %LOCALAPPDATA%\JARVIS)
# Kompatibel mit Windows PowerShell 5.1.
param(
    [ValidateSet('chrome', 'edge')] [string]$Browser = 'edge',
    [switch]$Stop,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest ist mit Fortschrittsbalken in PS 5.1 sehr langsam

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # deploy\windows -> Jarvis-Ordner
$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$venv = Join-Path $jarvisHome 'venv'
$envFile = Join-Path $jarvisHome 'jarvis.env'
$embedModel = 'bge-m3'   # Erinnerungen (config\jarvis.example.yaml -> llm.embeddings.model)

function Say([string]$text) { Write-Host "-> $text" -ForegroundColor Cyan }
function Fail([string]$text) {
    Write-Host "X $text" -ForegroundColor Red
    Read-Host 'Enter zum Schließen' | Out-Null
    exit 1
}

function Stop-JarvisServer {
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*-m jarvis.app*' })
    foreach ($process in $running) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
    return $running.Count
}

if ($Stop) {
    $count = Stop-JarvisServer
    Write-Host "JARVIS beendet ($count Prozess(e))."
    exit 0
}
if ($Uninstall) {
    & (Join-Path $PSScriptRoot 'install-autostart.ps1') -Remove
    [void](Stop-JarvisServer)
    exit 0
}

Write-Host ''
Write-Host '  J.A.R.V.I.S – Installation als Windows-Programm (ohne Docker)' -ForegroundColor Cyan
Write-Host ''
New-Item -ItemType Directory -Force -Path $jarvisHome, (Join-Path $jarvisHome 'data') | Out-Null

# ---------------------------------------------------------------- Python und Ollama
function Test-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }

function Install-WithWinget([string]$id, [string]$name) {
    if (-not (Test-Winget)) {
        Fail "$name fehlt und winget ist nicht verfügbar. Bitte $name selbst installieren und das Skript erneut starten."
    }
    Say "Installiere $name (einmalig, einige Minuten) …"
    & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
}

function Find-Python {
    # Der Platzhalter "python" aus dem Microsoft Store zählt nicht – nur echte Installationen ab 3.11
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
    foreach ($version in @('313', '312', '311')) {
        $candidates += , @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"))
        $candidates += , @((Join-Path $env:ProgramFiles "Python$version\python.exe"))
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*WindowsApps*') { $candidates += , @($onPath.Source) }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        if ($exe -ne 'py' -and -not (Test-Path $exe)) { continue }
        $arguments = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.version_info >= (3, 11))')
        try {
            $ok = & $exe @arguments 2>$null
            if ($ok -eq 'True') { return , $candidate }
        } catch { }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
    $python = Find-Python
    if (-not $python) { Fail 'Python wurde installiert, ist aber noch nicht auffindbar. Bitte das Skript erneut starten.' }
}
Say "Python gefunden: $($python -join ' ')"

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $path = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $path) { return $path }
    return $null
}

$ollama = Find-Ollama
if (-not $ollama) {
    Install-WithWinget 'Ollama.Ollama' 'Ollama (lokales Sprachmodell)'
    $ollama = Find-Ollama
    if (-not $ollama) { Fail 'Ollama wurde installiert, ist aber noch nicht auffindbar. Bitte das Skript erneut starten.' }
}

function Test-Ollama {
    try { return (Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}
if (-not (Test-Ollama)) {
    Say 'Starte Ollama …'
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (Test-Ollama); $i++) { Start-Sleep -Seconds 1 }
    if (-not (Test-Ollama)) { Fail 'Ollama startet nicht. Bitte Ollama einmal über das Startmenü öffnen und erneut versuchen.' }
}

# ---------------------------------------------------------------- Docker-Fassung ablösen
# Läuft JARVIS noch im Docker-Container, belegt er Port 8080. Daten (Timer, Termine) bleiben dort im Volume.
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        $containers = & docker ps --filter 'name=jarvis-core' --format '{{.Names}}' 2>$null
        if ($LASTEXITCODE -eq 0 -and $containers) {
            Say 'Beende die Docker-Fassung von JARVIS (Daten bleiben im Docker-Volume erhalten) …'
            Push-Location (Join-Path $repo 'deploy')
            try { & docker compose down | Out-Host } finally { Pop-Location }
        }
    } catch {
        # Docker Desktop läuft nicht – dann läuft auch die Docker-Fassung von JARVIS nicht: nichts zu tun
    }
}
[void](Stop-JarvisServer)   # ältere Programmfassung beenden, damit das Update greift

# ---------------------------------------------------------------- JARVIS installieren
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Say 'Lege die Python-Umgebung für JARVIS an …'
    $arguments = @($python | Select-Object -Skip 1) + @('-m', 'venv', $venv)
    & $python[0] @arguments
    if ($LASTEXITCODE -ne 0) { Fail 'Die Python-Umgebung konnte nicht angelegt werden.' }
}
$project = Join-Path $repo 'reference\python'
$hashFile = Join-Path $venv 'jarvis-installed.txt'
$hash = (Get-FileHash (Join-Path $project 'pyproject.toml') -Algorithm SHA256).Hash
$installed = ''
if (Test-Path $hashFile) { $installed = (Get-Content $hashFile -Raw).Trim() }
if ($installed -ne $hash) {
    Say 'Installiere JARVIS und seine Bausteine (einmalig, 1–3 Minuten) …'
    & $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip | Out-Host
    # voice-local: Spracherkennung (Whisper, openWakeWord) und Stimme (Piper) direkt auf diesem PC
    & $venvPython -m pip install --disable-pip-version-check --quiet -e "$project[all,voice-local]" | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'Installation fehlgeschlagen (Internetverbindung?). Details stehen oben.' }
    Set-Content -Path $hashFile -Value $hash -Encoding ASCII
}

# Raum-Satelliten (Tablets) erreichen JARVIS im Heimnetz über HTTPS-Port 8443 – Freigabe nur für private Netze
$ruleName = 'JARVIS Heimnetz (Tablets)'
& netsh advfirewall firewall show rule name="$ruleName" *> $null
if ($LASTEXITCODE -ne 0) {
    Say 'Gebe JARVIS im Heimnetz frei (Windows fragt einmal nach Administratorrechten) …'
    try {
        $rule = "advfirewall firewall add rule name=`"$ruleName`" dir=in action=allow protocol=TCP localport=8443 profile=private"
        Start-Process -FilePath netsh -ArgumentList $rule -Verb RunAs -WindowStyle Hidden -Wait
    } catch {
        Say 'Freigabe übersprungen – Tablets können JARVIS erst erreichen, wenn Windows den Zugriff erlaubt.'
    }
}

# ---------------------------------------------------------------- Einstellungen (jarvis.env)
function Read-EnvFile([string]$path) {
    $values = [ordered]@{}
    if (Test-Path $path) {
        foreach ($line in [System.IO.File]::ReadAllLines($path)) {
            if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $values[$matches[1]] = $matches[2] }
        }
    }
    return $values
}

$settings = Read-EnvFile $envFile
$dockerEnv = Read-EnvFile (Join-Path $repo 'deploy\.env')
# Bisherige Docker-Einstellungen übernehmen (Token, Name, Claude-Schlüssel, Stimme, Kontakte …)
$skip = @('POSTGRES_PASSWORD', 'COMPOSE_FILE', 'COMPOSE_PATH_SEPARATOR', 'JARVIS_CONFIG_FILE', 'JARVIS_GPU', 'NODERED_JARVIS_TOKEN')
foreach ($key in $dockerEnv.Keys) {
    if (-not $settings.Contains($key) -and $skip -notcontains $key -and $dockerEnv[$key]) { $settings[$key] = $dockerEnv[$key] }
}
if (-not $settings['JARVIS_DEV_TOKENS'] -or $settings['JARVIS_DEV_TOKENS'] -like '*dev-alex-token*') {
    $bytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $random = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
    $settings['JARVIS_DEV_TOKENS'] = '{"jarvis-' + $random + '": {"actor": "user:owner", "role": "adult", "trust": "trusted_user"}}'
}
$token = ([regex]::Match($settings['JARVIS_DEV_TOKENS'], '"([^"]+)"\s*:\s*\{')).Groups[1].Value

if (-not $settings['JARVIS_USER_NAME']) {
    $name = Read-Host 'Wie soll JARVIS Sie nennen? (Vorname, Enter = überspringen)'
    $name = ($name -replace '[^\p{L}\p{M} .''-]', '').Trim()
    if ($name.Length -gt 40) { $name = $name.Substring(0, 40) }
    if ($name) { $settings['JARVIS_USER_NAME'] = $name }
}

if (-not $settings['JARVIS_LLM_MODEL']) {
    # Wie start.sh: Grafikspeicher (NVIDIA) bzw. Arbeitsspeicher entscheidet über die Modellgröße
    $vram = 0
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        try {
            $vram = ((& $smi.Source --query-gpu=memory.total --format=csv,noheader,nounits) | ForEach-Object { [int]$_.Trim() } |
                Measure-Object -Maximum).Maximum
        } catch { $vram = 0 }
    }
    $ramGb = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    $model = 'qwen2.5:3b-instruct'
    if ($vram -ge 6000 -or $ramGb -ge 16) { $model = 'qwen2.5:7b-instruct' }
    if ($vram -ge 11000) { $model = 'qwen2.5:14b-instruct' }
    $settings['JARVIS_LLM_MODEL'] = $model
    $gpuText = if ($vram) { "NVIDIA-Grafikkarte mit $vram MiB" } else { 'keine NVIDIA-Grafikkarte' }
    Say "Sprachmodell passend zur Hardware ($gpuText, $ramGb GB RAM): $model"
}

$lines = @('# JARVIS als Windows-Programm – Einstellungen (wie deploy\.env). Nach Änderungen: JARVIS neu starten.')
foreach ($key in $settings.Keys) { $lines += "$key=$($settings[$key])" }
[System.IO.File]::WriteAllLines($envFile, $lines, (New-Object System.Text.UTF8Encoding $false))

# ---------------------------------------------------------------- Modelle laden
foreach ($model in @($settings['JARVIS_LLM_MODEL'], $embedModel)) {
    Say "Lade Modell $model (beim ersten Mal einige Minuten) …"
    & $ollama pull $model
    if ($LASTEXITCODE -ne 0) { Write-Host "! Modell $model konnte nicht geladen werden – später erneut: ollama pull $model" -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- Autostart, Verknüpfung, Start
Say 'Richte Autostart, Desktop-Verknüpfung und PC-Steuerung ein …'
& (Join-Path $PSScriptRoot 'install-autostart.ps1') -Mode native -Repo $repo -Token $token -Browser $Browser
Say 'Starte JARVIS …'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
& $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $jarvisHome 'jarvis-launch.ps1')

Write-Host ''
Write-Host 'Fertig. JARVIS läuft als Programm – ohne Docker.' -ForegroundColor Green
Write-Host "  Öffnen:        Desktop-Verknüpfung „JARVIS“ (startet auch beim Anmelden automatisch)"
Write-Host "  Im Browser:    http://127.0.0.1:8080/#token=$token"
Write-Host "  Einstellungen: $envFile"
Write-Host "  Protokoll:     $jarvisHome\server.log"
Write-Host '  Aktualisieren: git pull, dann „JARVIS installieren.cmd“ erneut ausführen'
Write-Host ''
Read-Host 'Enter zum Schließen' | Out-Null
