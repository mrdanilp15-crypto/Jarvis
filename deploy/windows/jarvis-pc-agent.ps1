# JARVIS PC-Agent: öffnet auf diesem PC Webseiten, Programme und Ordner, wenn JARVIS darum bittet.
# Verbindet sich selbst mit JARVIS (ws://127.0.0.1:8080/v1/agent) – auf dem PC wird kein Port geöffnet.
# Ausgeführt wird nur, was hier freigegeben ist: Webseiten (nur http/https), Programme aus der Liste unten
# (erweiterbar über %LOCALAPPDATA%\JARVIS\apps.json, z. B. {"steam": "steam:"}) und bekannte Ordner.
# Wird vom Startskript (jarvis-launch.ps1) unsichtbar gestartet. Protokoll: %LOCALAPPDATA%\JARVIS\pc-agent.log
# Kompatibel mit Windows PowerShell 5.1.

$ErrorActionPreference = 'Continue'
$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$logFile = Join-Path $jarvisHome 'pc-agent.log'
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $jarvisHome 'config.json') | ConvertFrom-Json

$mutex = New-Object System.Threading.Mutex($false, 'Local\JarvisPcAgent')
if (-not $mutex.WaitOne(0)) { exit 0 }   # läuft bereits

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
            $name = ([string]$arguments.app).Trim().ToLower()
            if (-not $apps.Contains($name)) {
                throw "Das Programm '$name' ist nicht freigegeben. Verfügbar: $(@($apps.Keys) -join ', ')."
            }
            try {
                Start-Process $apps[$name] -ErrorAction Stop
            } catch {
                throw "'$name' ließ sich nicht starten – ist es auf diesem PC installiert?"
            }
            return @{ opened = $name }
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
        Send-Json $socket @{ type = 'agent.hello'; name = $env:COMPUTERNAME; apps = @($apps.Keys); folders = @($folders.Keys) }
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
        }
    } catch {
        # JARVIS läuft (noch) nicht – später erneut versuchen
    } finally {
        $socket.Dispose()
    }
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 60)
}
