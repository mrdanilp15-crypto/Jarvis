# JARVIS PC-Agent: öffnet auf diesem PC Programme, Spiele, Ordner, Webseiten und die Dateisuche, wenn JARVIS darum bittet.
# Verbindet sich selbst mit JARVIS (ws://127.0.0.1:8080/v1/agent) – auf dem PC wird kein Port geöffnet.
# Ausgeführt wird nur, was hier freigegeben ist: Webseiten (nur http/https), Programme aus der Liste unten
# (erweiterbar über %LOCALAPPDATA%\JARVIS\apps.json, z. B. {"mein tool": "C:\\Tools\\tool.exe"}), Programme aus dem
# Windows-Startmenü (per Name, ohne Deinstallations- und Setup-Einträge), bekannte Ordner und die Explorer-Suche.
# JARVIS schickt nur Namen und Suchbegriffe – nie Pfade oder Befehle.
# Wird vom Startskript (jarvis-launch.ps1) unsichtbar gestartet. Protokoll: %LOCALAPPDATA%\JARVIS\pc-agent.log
# Kompatibel mit Windows PowerShell 5.1.

$ErrorActionPreference = 'Continue'
$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$logFile = Join-Path $jarvisHome 'pc-agent.log'
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $jarvisHome 'config.json') | ConvertFrom-Json

$agentVersion = '2.1.0'
$mutex = New-Object System.Threading.Mutex($false, 'Local\JarvisPcAgent')
try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }   # Vorgänger beendet
if (-not $owned) { exit 0 }   # läuft bereits

function Write-Log([string]$message) {
    if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 1MB) { Remove-Item $logFile }
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $message
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# Programme: Name -> was gestartet wird (Programm, Adresse oder Windows-Protokoll)
$apps = [ordered]@{
    'explorer'      = 'explorer.exe'
    'browser'       = 'https://www.google.de'
    'editor'        = 'notepad.exe'
    'rechner'       = 'calc.exe'
    'paint'         = 'mspaint.exe'
    'einstellungen' = 'ms-settings:'
    'taskmanager'   = 'taskmgr.exe'
    'systemsteuerung' = 'control.exe'
    'kamera'        = 'microsoft.windows.camera:'
    'uhr'           = 'ms-clock:'
    'store'         = 'ms-windows-store:'
    'snipping'      = 'ms-screenclip:'
    'spotify'       = 'spotify:'
    'word'          = 'winword.exe'
    'excel'         = 'excel.exe'
    'powerpoint'    = 'powerpnt.exe'
    'outlook'       = 'outlook.exe'
}
$customApps = Join-Path $jarvisHome 'apps.json'
if (Test-Path $customApps) {
    try {
        $custom = Get-Content -Raw -Encoding UTF8 $customApps | ConvertFrom-Json
        foreach ($entry in $custom.PSObject.Properties) { $apps[$entry.Name.ToLower()] = [string]$entry.Value }
    } catch {
        Write-Log "apps.json nicht lesbar: $($_.Exception.Message)"
    }
}

$folders = @{
    'desktop'   = [Environment]::GetFolderPath('Desktop')
    'documents' = [Environment]::GetFolderPath('MyDocuments')
    'downloads' = 'shell:Downloads'
    'pictures'  = [Environment]::GetFolderPath('MyPictures')
    'music'     = [Environment]::GetFolderPath('MyMusic')
    'videos'    = [Environment]::GetFolderPath('MyVideos')
    'home'      = [Environment]::GetFolderPath('UserProfile')
    'pc'        = 'shell:MyComputerFolder'
}

# Startmenü: alle installierten Programme und Spiele (Steam, Discord, Minecraft …) mit Namen und App-ID.
# Deinstallations-, Setup- und Hilfe-Einträge werden nie gestartet.
$skipApps = '(?i)uninstall|deinstall|entfernen|setup|installer|readme|liesmich|release notes|handbuch|manual|' +
            'dokumentation|documentation|support|website|lizenz|license'
$script:startApps = @()
$script:startAppsAt = [datetime]::MinValue
$script:appsChanged = $false

function Get-StartAppNames { return @($script:startApps | ForEach-Object { [string]$_.Name }) }

function Update-StartApps([switch]$Force) {
    $age = ((Get-Date) - $script:startAppsAt).TotalMinutes
    if (-not $Force -and $script:startApps.Count -gt 0 -and $age -lt 10) { return }
    $before = (Get-StartAppNames) -join '|'
    try {
        $script:startApps = @(Get-StartApps | Where-Object { $_.Name -and $_.AppID -and $_.Name -notmatch $skipApps } |
            Sort-Object -Property Name -Unique)
    } catch {
        Write-Log "Startmenü nicht lesbar: $($_.Exception.Message)"
    }
    $script:startAppsAt = Get-Date
    if ($before -and ((Get-StartAppNames) -join '|') -ne $before) { $script:appsChanged = $true }
}

function ConvertTo-AppKey([string]$text) {
    # „Counter-Strike 2“ -> „counter strike 2“ (gleiche Regel wie im JARVIS-Kern)
    return (($text.ToLower() -replace '[^\p{L}\p{Nd}]+', ' ').Trim())
}

function Find-StartApp([string]$name) {
    # exakter Name vor Namensanfang („minecraft“ -> „Minecraft Launcher“) vor ganzem Wort („chrome“ -> „Google Chrome“)
    $wanted = ConvertTo-AppKey $name
    if (-not $wanted) { return $null }
    $best = $null
    $bestRank = 9
    foreach ($entry in $script:startApps) {
        $key = ConvertTo-AppKey $entry.Name
        $rank = 9
        if ($key -eq $wanted -or $key.Replace(' ', '') -eq $wanted.Replace(' ', '')) { $rank = 0 }
        elseif ($key.StartsWith($wanted + ' ')) { $rank = 1 }
        elseif ((' ' + $key + ' ').Contains(' ' + $wanted + ' ')) { $rank = 2 }
        if ($rank -eq 9) { continue }
        if ($rank -lt $bestRank -or ($rank -eq $bestRank -and $entry.Name.Length -lt $best.Name.Length)) {
            $best = $entry
            $bestRank = $rank
        }
    }
    return $best
}

function Start-StartApp($entry) {
    $id = [string]$entry.AppID
    if ($id -match '^[a-z][a-z0-9+.-]+://') {
        Start-Process $id -ErrorAction Stop   # Verknüpfung auf eine Adresse, z. B. steam://rungameid/…
    } else {
        # Startet Desktop-Programme und Store-Apps gleichermaßen über ihre App-ID
        Start-Process explorer.exe -ArgumentList ('"shell:AppsFolder\' + $id + '"') -ErrorAction Stop
    }
}

function Invoke-Action([string]$action, $arguments) {
    switch ($action) {
        'open_url' {
            $uri = $null
            $valid = [Uri]::TryCreate([string]$arguments.url, [UriKind]::Absolute, [ref]$uri)
            if (-not $valid -or @('http', 'https') -notcontains $uri.Scheme) { throw 'Ich öffne nur http- und https-Adressen.' }
            Start-Process $uri.AbsoluteUri -ErrorAction Stop
            return @{ opened = $uri.AbsoluteUri }
        }
        'open_app' {
            $name = ([string]$arguments.app).Trim()
            $key = $name.ToLower()
            if ($apps.Contains($key)) {
                try {
                    Start-Process $apps[$key] -ErrorAction Stop
                    return @{ opened = $key }
                } catch {
                    Write-Log "open_app $key über die Liste fehlgeschlagen – versuche das Startmenü"
                }
            }
            Update-StartApps
            $entry = Find-StartApp $name
            if ($null -eq $entry) {
                Update-StartApps -Force   # vielleicht gerade erst installiert
                $entry = Find-StartApp $name
            }
            # Strings mit „…“ nur in einfachen Anführungszeichen: PowerShell liest „ und “ sonst als Stringende
            if ($null -eq $entry) { throw ('Ein Programm namens „' + $name + '“ finde ich auf diesem PC nicht.') }
            try {
                Start-StartApp $entry
            } catch {
                throw ('„' + $entry.Name + '“ ließ sich nicht starten.')
            }
            return @{ opened = [string]$entry.Name }
        }
        'search_files' {
            $query = ([string]$arguments.query).Trim()
            if (-not $query) { throw 'Wonach soll ich suchen?' }
            $location = [Uri]::EscapeDataString([Environment]::GetFolderPath('UserProfile'))
            $search = 'search-ms:displayname=' + [Uri]::EscapeDataString('Suche nach ' + $query) +
                      '&query=' + [Uri]::EscapeDataString($query) + '&crumb=location:' + $location
            Start-Process explorer.exe -ArgumentList ('"' + $search + '"') -ErrorAction Stop
            return @{ searched = $query }
        }
        'open_folder' {
            $key = [string]$arguments.folder
            if (-not $folders.ContainsKey($key)) { throw "Unbekannter Ordner '$key'." }
            Start-Process explorer.exe -ArgumentList ('"' + $folders[$key] + '"') -ErrorAction Stop
            return @{ opened = $key }
        }
        default { throw "Unbekannte Aktion '$action'." }
    }
}

function Send-Json($socket, $object) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($object | ConvertTo-Json -Compress -Depth 5))
    $segment = [ArraySegment[byte]]::new($bytes)
    $socket.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true,
        [Threading.CancellationToken]::None).GetAwaiter().GetResult()
}

function Receive-Json($socket, [byte[]]$buffer) {
    $stream = New-Object System.IO.MemoryStream
    do {
        $segment = [ArraySegment[byte]]::new($buffer)
        $result = $socket.ReceiveAsync($segment, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
        if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { return $null }
        $stream.Write($buffer, 0, $result.Count)
    } while (-not $result.EndOfMessage)
    return ([System.Text.Encoding]::UTF8.GetString($stream.ToArray()) | ConvertFrom-Json)
}

$url = 'ws://127.0.0.1:8080/v1/agent?token=' + [Uri]::EscapeDataString([string]$config.token)
$buffer = New-Object byte[] 65536
$delay = 5
Write-Log 'PC-Agent gestartet'
while ($true) {
    $socket = New-Object System.Net.WebSockets.ClientWebSocket
    $socket.Options.Proxy = $null   # 127.0.0.1 nie über einen Proxy
    try {
        $socket.ConnectAsync([Uri]$url, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
        Update-StartApps
        Send-Json $socket @{
            type = 'agent.hello'; name = $env:COMPUTERNAME; version = $agentVersion; apps = @($apps.Keys)
            start_apps = @(Get-StartAppNames); folders = @($folders.Keys)
            actions = @('open_url', 'open_app', 'open_folder', 'search_files')
        }
        $script:appsChanged = $false
        Write-Log 'Mit JARVIS verbunden'
        $delay = 5
        while ($socket.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
            $message = Receive-Json $socket $buffer
            if ($null -eq $message) { break }
            if ($message.type -ne 'agent.invoke') { continue }
            try {
                $result = Invoke-Action $message.action $message.arguments
                Send-Json $socket @{ type = 'agent.result'; id = $message.id; ok = $true; result = $result }
                Write-Log "$($message.action): ok"
            } catch {
                Send-Json $socket @{ type = 'agent.result'; id = $message.id; ok = $false; error = $_.Exception.Message }
                Write-Log "$($message.action): $($_.Exception.Message)"
            }
            if ($script:appsChanged) {
                # Neu installierte oder entfernte Programme an JARVIS melden
                Send-Json $socket @{ type = 'agent.apps'; start_apps = @(Get-StartAppNames) }
                $script:appsChanged = $false
            }
        }
    } catch {
        # JARVIS läuft (noch) nicht – später erneut versuchen
    } finally {
        $socket.Dispose()
    }
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 60)
}
