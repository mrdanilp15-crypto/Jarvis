# Richtet den JARVIS-Autostart unter Windows ein (oder entfernt ihn mit -Remove).
# Normalerweise über "./deploy/start.sh autostart" aufgerufen (Git Bash oder WSL). Legt an:
#   %LOCALAPPDATA%\JARVIS\  Startskript, Symbol, Einstellungen, eigenes Browserprofil
#   Verknüpfung "JARVIS" im Autostart-Ordner und auf dem Desktop
# Kompatibel mit Windows PowerShell 5.1.
param(
    [ValidateSet('windows', 'wsl')] [string]$Mode = 'windows',
    [string]$Repo = '',       # Windows-Pfad (Modus windows) bzw. Linux-Pfad (Modus wsl) des Jarvis-Ordners
    [string]$Distro = '',     # WSL-Distribution (leer = Standard)
    [string]$Token = '',      # API-Token aus deploy/.env
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$links = @(
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'JARVIS.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'JARVIS.lnk')
)

if ($Remove) {
    foreach ($link in $links) {
        if (Test-Path $link) { Remove-Item $link }
    }
    Write-Host "Autostart und Desktop-Verknüpfung entfernt. Einstellungen bleiben in $jarvisHome."
    exit 0
}

if (-not $Repo) { $Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }   # deploy\windows -> Repository

New-Item -ItemType Directory -Force -Path $jarvisHome | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'jarvis-launch.ps1') $jarvisHome
Copy-Item -Force (Join-Path $PSScriptRoot 'jarvis-pc-agent.ps1') $jarvisHome
Copy-Item -Force (Join-Path $PSScriptRoot 'jarvis.ico') $jarvisHome

# Beim ersten Öffnen: Token übernehmen und die „Jarvis“-Aktivierung einschalten (danach merkt sich das Fenster
# die Einstellungen selbst)
$fragment = @()
if ($Token) { $fragment += "token=$Token" }
$fragment += 'wake=1'
$settings = [ordered]@{
    mode   = $Mode
    repo   = $Repo
    distro = $Distro
    token  = $Token
    url    = 'http://127.0.0.1:8080/#' + ($fragment -join '&')
}
$settings | ConvertTo-Json | Set-Content -Encoding UTF8 -Path (Join-Path $jarvisHome 'config.json')

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$launcher = Join-Path $jarvisHome 'jarvis-launch.ps1'
$shell = New-Object -ComObject WScript.Shell
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $powershell
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $shortcut.WorkingDirectory = $jarvisHome
    $shortcut.WindowStyle = 7   # minimiert: kein aufblitzendes Konsolenfenster
    $shortcut.IconLocation = (Join-Path $jarvisHome 'jarvis.ico') + ',0'
    $shortcut.Description = 'JARVIS starten'
    $shortcut.Save()
}

Write-Host 'Fertig: JARVIS startet ab jetzt beim Anmelden automatisch.'
Write-Host 'Zusätzlich liegt eine Verknüpfung "JARVIS" auf dem Desktop.'
Write-Host 'Die PC-Steuerung (Programme, Ordner, Webseiten öffnen) startet mit JARVIS mit.'
Write-Host 'Entfernen: ./deploy/start.sh autostart-remove'
