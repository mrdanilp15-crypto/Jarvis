# JARVIS PC-Agent: öffnet auf diesem PC Programme, Spiele, Ordner, Webseiten und die Dateisuche, wenn JARVIS darum bittet.
# Verbindet sich selbst mit JARVIS (ws://127.0.0.1:8080/v1/agent) – auf dem PC wird kein Port geöffnet.
# Ausgeführt wird nur, was hier freigegeben ist: Webseiten (nur http/https), Programme aus der Liste unten
# (erweiterbar über %LOCALAPPDATA%\JARVIS\apps.json, z. B. {"mein tool": "C:\\Tools\\tool.exe"}), Programme aus dem
# Windows-Startmenü (per Name, ohne Deinstallations- und Setup-Einträge), bekannte Ordner, Dateien im Benutzerordner
# (Suche über den Windows-Suchindex; Programme und Skripte darunter werden nur im Explorer markiert, nie gestartet)
# und die Explorer-Suche. Dazu: Programme sanft schließen (wie das X oben rechts), Text in das aktive Fenster
# einfügen und Tasten drücken (nie in Konsolen), Schaltflächen per Beschriftung anklicken (UI Automation),
# E-Mail-Entwürfe öffnen (senden muss der Nutzer selbst) und Windows-Hinweise anzeigen.
# JARVIS schickt nur Namen, Suchbegriffe, Texte und Trefferummern – nie Pfade oder Befehle.
# Wird vom Startskript (jarvis-launch.ps1) unsichtbar gestartet. Protokoll: %LOCALAPPDATA%\JARVIS\pc-agent.log
# Kompatibel mit Windows PowerShell 5.1.

$ErrorActionPreference = 'Continue'
$jarvisHome = Join-Path $env:LOCALAPPDATA 'JARVIS'
$logFile = Join-Path $jarvisHome 'pc-agent.log'
$config = Get-Content -Raw -Encoding UTF8 (Join-Path $jarvisHome 'config.json') | ConvertFrom-Json

$agentVersion = '2.8.0'
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

# Dateien im Benutzerordner: Windows-Suchindex (schnell), sonst Durchsuchen der üblichen Ordner.
# Die letzte Trefferliste bleibt gemerkt, damit JARVIS „die zweite“ öffnen kann (per Nummer, nie per Pfad).
$script:found = @()
$noLaunch = @('.exe', '.bat', '.cmd', '.com', '.ps1', '.psm1', '.vbs', '.vbe', '.js', '.jse', '.wsf', '.wsh', '.msi',
              '.msp', '.scr', '.hta', '.cpl', '.msc', '.jar', '.reg', '.lnk', '.pif', '.url', '.inf', '.application',
              '.appref-ms', '.settingcontent-ms', '.dll', '.sys', '.vbscript', '.psd1', '.ps1xml')

function Get-SearchWords([string]$query) {
    $clean = $query.ToLower() -replace "[%_\[\]'`"*?<>|]", ' '
    return @($clean -split '\s+' | Where-Object { $_ })
}

function Get-WordVariants([string]$word) {
    # „steuererklärung“ findet auch „Steuererklaerung.pdf“
    $alt = $word.Replace('ä', 'ae').Replace('ö', 'oe').Replace('ü', 'ue').Replace('ß', 'ss')
    if ($alt -ne $word) { return @($word, $alt) }
    return @($word)
}

function Test-NameMatch([string]$name, $words) {
    $lower = $name.ToLower()
    foreach ($word in $words) {
        $hit = $false
        foreach ($variant in (Get-WordVariants $word)) { if ($lower.Contains($variant)) { $hit = $true } }
        if (-not $hit) { return $false }
    }
    return $true
}

function Find-Files([string]$query, [string]$kind = 'any', [int]$limit = 20) {
    $words = Get-SearchWords $query
    if ($words.Count -eq 0) { return @() }
    $userHome = [Environment]::GetFolderPath('UserProfile')
    $items = New-Object System.Collections.ArrayList
    try {
        $conditions = foreach ($word in $words) {
            '(' + ((Get-WordVariants $word | ForEach-Object { "System.FileName LIKE '%$_%'" }) -join ' OR ') + ')'
        }
        $sql = "SELECT TOP 60 System.ItemPathDisplay, System.DateModified FROM SYSTEMINDEX WHERE SCOPE='file:" +
               $userHome.Replace('\', '/') + "' AND " + (@($conditions) -join ' AND ') + ' ORDER BY System.DateModified DESC'
        $connection = New-Object -ComObject ADODB.Connection
        $connection.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        try {
            $records = $connection.Execute($sql)
            while (-not $records.EOF) {
                [void]$items.Add([pscustomobject]@{
                    Path = [string]$records.Fields.Item('System.ItemPathDisplay').Value
                    Modified = $records.Fields.Item('System.DateModified').Value
                })
                $records.MoveNext()
            }
            $records.Close()
        } finally {
            $connection.Close()
        }
    } catch {
        Write-Log "Windows-Suchindex nicht verfügbar ($($_.Exception.Message)) – durchsuche die Benutzerordner"
        $items.Clear()
        $roots = @('Desktop', 'MyDocuments', 'MyPictures', 'MyMusic', 'MyVideos' | ForEach-Object { [Environment]::GetFolderPath($_) })
        $roots += (Join-Path $userHome 'Downloads')
        if ($env:OneDrive) { $roots += $env:OneDrive }
        foreach ($root in ($roots | Select-Object -Unique)) {
            if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
            foreach ($variant in (Get-WordVariants $words[0])) {
                Get-ChildItem -LiteralPath $root -Recurse -Depth 6 -Filter ('*' + $variant + '*') -ErrorAction SilentlyContinue |
                    Where-Object { Test-NameMatch $_.Name $words } | Select-Object -First 100 |
                    ForEach-Object { [void]$items.Add([pscustomobject]@{ Path = $_.FullName; Modified = $_.LastWriteTime }) }
            }
        }
    }
    $hits = @($items | Where-Object { $_.Path -and $_.Path -notmatch '\\AppData\\' } | Sort-Object Path -Unique |
        Sort-Object Modified -Descending)
    if ($kind -eq 'folder') { $hits = @($hits | Where-Object { Test-Path -LiteralPath $_.Path -PathType Container }) }
    elseif ($kind -eq 'file') { $hits = @($hits | Where-Object { Test-Path -LiteralPath $_.Path -PathType Leaf }) }
    return @($hits | Select-Object -First $limit)
}

function Get-DisplayFolder([string]$path) {
    $userHome = [Environment]::GetFolderPath('UserProfile')
    $dir = Split-Path -Parent $path
    if ($dir.StartsWith($userHome, [StringComparison]::OrdinalIgnoreCase)) { $dir = $dir.Substring($userHome.Length).TrimStart('\') }
    return $dir
}

function Get-FoundList([int]$count) {
    $list = @()
    for ($i = 0; $i -lt [Math]::Min($count, $script:found.Count); $i++) {
        $item = $script:found[$i]
        $modified = ''
        if ($item.Modified -is [datetime]) { $modified = $item.Modified.ToString('yyyy-MM-dd') }
        $kind = 'file'
        if (Test-Path -LiteralPath $item.Path -PathType Container) { $kind = 'folder' }
        $list += @{ id = $i + 1; name = (Split-Path -Leaf $item.Path); folder = (Get-DisplayFolder $item.Path)
                    modified = $modified; kind = $kind }
    }
    return $list
}

function Select-BestFile($items, [string]$query) {
    # exakter Name vor Namensanfang vor Namensteil; bei Gleichstand die neueste (Liste ist nach Datum sortiert)
    $wanted = ConvertTo-AppKey $query
    $best = $null
    $bestRank = 9
    foreach ($item in $items) {
        $leaf = Split-Path -Leaf $item.Path
        $key = ConvertTo-AppKey ([IO.Path]::GetFileNameWithoutExtension($leaf))
        $rank = 3
        if ($key -eq $wanted -or (ConvertTo-AppKey $leaf) -eq $wanted) { $rank = 0 }
        elseif ($wanted -and $key.StartsWith($wanted)) { $rank = 1 }
        elseif ($wanted -and $key.Contains($wanted)) { $rank = 2 }
        if ($rank -lt $bestRank) {
            $best = $item
            $bestRank = $rank
        }
    }
    return $best
}

function Open-FoundItem($item, [bool]$show) {
    $path = [string]$item.Path
    $name = Split-Path -Leaf $path
    if (-not (Test-Path -LiteralPath $path)) { throw ('„' + $name + '“ ist nicht mehr vorhanden.') }
    $isFolder = Test-Path -LiteralPath $path -PathType Container
    $kind = 'file'
    if ($isFolder) { $kind = 'folder' }
    $result = @{ opened = $name; folder = (Get-DisplayFolder $path); kind = $kind; shown = $false; blocked = $false }
    $program = (-not $isFolder) -and ($noLaunch -contains [IO.Path]::GetExtension($path).ToLower())
    if ($show -or $program) {
        # Programme, Skripte und Verknüpfungen nie starten – nur im Explorer zeigen
        Start-Process explorer.exe -ArgumentList ('/select,"' + $path + '"') -ErrorAction Stop
        $result.shown = $true
        $result.blocked = [bool]$program -and -not $show
        return $result
    }
    if ($isFolder) {
        Start-Process explorer.exe -ArgumentList ('"' + $path + '"') -ErrorAction Stop
    } else {
        Invoke-Item -LiteralPath $path -ErrorAction Stop   # Standardprogramm (PDF-Anzeige, Word, Fotos …)
    }
    return $result
}

function Get-Kind($arguments) {
    $kind = [string]$arguments.kind
    if (@('file', 'folder') -notcontains $kind) { $kind = 'any' }
    return $kind
}

# ---------------------------------------------------------------- Fenster, Tastatur, Maus
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class JarvisNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    public static void Key(byte vk) { keybd_event(vk, 0, 0, UIntPtr.Zero); keybd_event(vk, 0, 2, UIntPtr.Zero); }
    public static void Click(int x, int y) {
        SetCursorPos(x, y);
        mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
        mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    }
    public static string Title(IntPtr hWnd) { var text = new StringBuilder(512); GetWindowText(hWnd, text, 512); return text.ToString(); }
}
'@

# Programme, deren Fenster nie Tastatureingaben von JARVIS bekommen: Konsolen führen Getipptes als Befehl aus
$noTyping = @('cmd', 'powershell', 'pwsh', 'powershell_ise', 'windowsterminal', 'openconsole', 'conhost', 'wt', 'mintty',
              'bash', 'wsl', 'wslhost', 'ubuntu', 'regedit', 'mmc', 'putty', 'kitty', 'alacritty', 'wezterm-gui')
# Nie schließen: Windows selbst, JARVIS und seine Helfer
$noClose = @('explorer', 'csrss', 'winlogon', 'wininit', 'services', 'lsass', 'svchost', 'smss', 'dwm', 'system', 'idle',
             'fontdrvhost', 'sihost', 'ctfmon', 'runtimebroker', 'searchhost', 'startmenuexperiencehost',
             'shellexperiencehost', 'textinputhost', 'powershell', 'pwsh', 'docker desktop', 'com.docker.backend',
             'vmmem', 'vmmemwsl', 'wsl', 'wslservice')
$processAliases = @{
    'browser' = @('chrome', 'msedge', 'firefox', 'opera', 'brave', 'vivaldi'); 'chrome' = @('chrome'); 'google chrome' = @('chrome')
    'edge' = @('msedge'); 'microsoft edge' = @('msedge'); 'firefox' = @('firefox'); 'editor' = @('notepad')
    'rechner' = @('calculatorapp', 'calc'); 'paint' = @('mspaint'); 'taskmanager' = @('taskmgr')
    'einstellungen' = @('systemsettings'); 'word' = @('winword'); 'excel' = @('excel'); 'powerpoint' = @('powerpnt')
    'outlook' = @('outlook', 'olk'); 'spotify' = @('spotify'); 'discord' = @('discord'); 'steam' = @('steam', 'steamwebhelper')
    'teams' = @('ms-teams', 'teams'); 'visual studio code' = @('code'); 'vs code' = @('code'); 'vlc' = @('vlc')
}

function Get-Foreground {
    $hwnd = [JarvisNative]::GetForegroundWindow()
    $processId = [uint32]0
    [void][JarvisNative]::GetWindowThreadProcessId($hwnd, [ref]$processId)
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Handle = $hwnd; Title = [JarvisNative]::Title($hwnd)
                              Process = $(if ($process) { $process.ProcessName.ToLower() } else { '' }) }
}

function Assert-TypingAllowed {
    $window = Get-Foreground
    if ($noTyping -contains $window.Process) {
        throw 'In Konsolen und Systemwerkzeugen tippe ich aus Sicherheitsgründen nicht.'
    }
    if ($window.Title -match 'J\.A\.R\.V\.I\.S') {
        throw 'Gerade ist das JARVIS-Fenster aktiv – klicken Sie bitte zuerst in das Fenster, in das ich schreiben soll.'
    }
    return $window
}

function ConvertTo-SendKeys([string]$text) {
    return [regex]::Replace($text, '[+^%~(){}\[\]]', { param($m) '{' + $m.Value + '}' })
}

function Send-Text([string]$text, [bool]$enter) {
    $window = Assert-TypingAllowed
    $text = ($text -replace "[\r\n]+", ' ').Trim()
    # Über die Zwischenablage einfügen: funktioniert mit allen Zeichen und Tastaturlayouts; alter Text kommt zurück
    $previous = $null
    try { if ([System.Windows.Forms.Clipboard]::ContainsText()) { $previous = [System.Windows.Forms.Clipboard]::GetText() } } catch { }
    [System.Windows.Forms.Clipboard]::SetText($text)
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 250
    if ($enter) { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
    if ($null -ne $previous) { try { [System.Windows.Forms.Clipboard]::SetText($previous) } catch { } }
    return @{ typed = $text.Length; window = $window.Title; enter = $enter }
}

$keys = @{
    'enter' = '{ENTER}'; 'tab' = '{TAB}'; 'escape' = '{ESC}'; 'space' = ' '; 'backspace' = '{BACKSPACE}'
    'delete' = '{DELETE}'; 'up' = '{UP}'; 'down' = '{DOWN}'; 'left' = '{LEFT}'; 'right' = '{RIGHT}'
    'page_up' = '{PGUP}'; 'page_down' = '{PGDN}'; 'home' = '{HOME}'; 'end' = '{END}'; 'refresh' = '{F5}'
    'fullscreen' = '{F11}'; 'copy' = '^c'; 'paste' = '^v'; 'cut' = '^x'; 'undo' = '^z'; 'redo' = '^y'
    'select_all' = '^a'; 'save' = '^s'; 'find' = '^f'; 'print' = '^p'; 'new_tab' = '^t'; 'close_tab' = '^w'
    'reopen_tab' = '^+t'; 'next_tab' = '^{TAB}'; 'previous_tab' = '^+{TAB}'; 'back' = '%{LEFT}'; 'forward' = '%{RIGHT}'
    'zoom_in' = '^{ADD}'; 'zoom_out' = '^{SUBTRACT}'; 'switch_window' = '%{TAB}'; 'close_window' = '%{F4}'
}
# Medien- und Lautstärketasten wirken systemweit, unabhängig vom aktiven Fenster
$mediaKeys = @{ 'play_pause' = 0xB3; 'next_track' = 0xB0; 'previous_track' = 0xB1; 'stop_media' = 0xB2
                'volume_up' = 0xAF; 'volume_down' = 0xAE; 'mute' = 0xAD }

function Send-Key([string]$key, [int]$times) {
    $times = [Math]::Max(1, [Math]::Min($times, 10))
    if ($mediaKeys.ContainsKey($key)) {
        for ($i = 0; $i -lt $times; $i++) { [JarvisNative]::Key([byte]$mediaKeys[$key]); Start-Sleep -Milliseconds 40 }
        return @{ pressed = $key; times = $times }
    }
    if (-not $keys.ContainsKey($key)) { throw "Die Taste '$key' kenne ich nicht." }
    $window = Get-Foreground
    if ($noTyping -contains $window.Process -and @('enter', 'paste') -contains $key) {
        throw 'In Konsolen drücke ich Enter nicht – das würde einen Befehl ausführen.'
    }
    for ($i = 0; $i -lt $times; $i++) { [System.Windows.Forms.SendKeys]::SendWait($keys[$key]); Start-Sleep -Milliseconds 60 }
    return @{ pressed = $key; times = $times; window = $window.Title }
}

function Test-TitleMatch([string]$title, [string]$key) {
    # „Rechnung.docx - Word“ gehört zu „word“; kurze Namen („uhr“) nie über den Titel, zu leicht verwechselt
    if ($key.Length -lt 4 -or -not $title) { return $false }
    $titleKey = ConvertTo-AppKey $title
    return $titleKey -eq $key -or $titleKey.EndsWith(' ' + $key)
}

function Close-App([string]$name) {
    $key = ConvertTo-AppKey $name
    if (@('explorer', 'datei explorer', 'dateiexplorer', 'windows explorer') -contains $key) {
        # Nur die Ordnerfenster schließen – explorer.exe selbst ist auch die Taskleiste
        $shell = New-Object -ComObject Shell.Application
        $windows = @($shell.Windows() | Where-Object { $_.FullName -like '*explorer.exe' })
        foreach ($window in $windows) { $window.Quit() }
        if ($windows.Count -eq 0) { throw 'Es ist kein Explorer-Fenster geöffnet.' }
        return @{ closed = 'explorer'; count = $windows.Count }
    }
    $names = @()
    if ($processAliases.ContainsKey($key)) { $names += $processAliases[$key] }
    $names += $key.Replace(' ', '')
    $entry = Find-StartApp $name   # Startmenü: „Minecraft Launcher“ -> MinecraftLauncher.exe
    if ($entry -and ([string]$entry.AppID) -match '([^\\/]+)\.exe$') { $names += $Matches[1].ToLower() }
    $candidates = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $process = $_.ProcessName.ToLower()
        $_.Id -ne $PID -and $noClose -notcontains $process -and $_.MainWindowHandle -ne 0 -and
            (($names -contains $process) -or (Test-TitleMatch $_.MainWindowTitle $key))
    })
    # Das JARVIS-Fenster (eigenes Browserprofil) bleibt offen
    $candidates = @($candidates | Where-Object { $_.MainWindowTitle -notmatch 'J\.A\.R\.V\.I\.S' })
    if ($candidates.Count -eq 0) { throw ('„' + $name + '“ ist gerade nicht geöffnet.') }
    foreach ($process in $candidates) { [void]$process.CloseMainWindow() }   # wie das X – ungespeicherte Arbeit fragt nach
    return @{ closed = $name; count = $candidates.Count }
}

function Invoke-ClickByName([string]$label) {
    $window = Get-Foreground
    if ($window.Title -match 'J\.A\.R\.V\.I\.S') { throw 'Gerade ist das JARVIS-Fenster aktiv – bitte zuerst das Zielfenster anklicken.' }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($window.Handle)
    $wanted = ConvertTo-AppKey $label
    $types = @('Button', 'Hyperlink', 'MenuItem', 'ListItem', 'TabItem', 'CheckBox', 'RadioButton', 'TreeItem', 'SplitButton')
    $conditions = [System.Windows.Automation.Condition[]]@($types | ForEach-Object {
        New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::$_) })
    $clickable = [System.Windows.Automation.OrCondition]::new($conditions)
    $best = $null
    $bestRank = 9
    for ($attempt = 0; $attempt -lt 2 -and $null -eq $best; $attempt++) {
        # Browser bauen ihre Bedienhilfen-Struktur erst beim ersten Zugriff auf – dann ein zweiter Versuch
        if ($attempt -gt 0) { Start-Sleep -Milliseconds 800 }
        foreach ($element in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $clickable)) {
            $current = $element.Current
            if ($current.IsOffscreen -or -not $current.IsEnabled) { continue }
            $key = ConvertTo-AppKey $current.Name
            if (-not $key) { continue }
            $rank = 9
            if ($key -eq $wanted) { $rank = 0 } elseif ($key.StartsWith($wanted)) { $rank = 1 } elseif ($key.Contains($wanted)) { $rank = 2 }
            if ($rank -lt $bestRank) { $best = $element; $bestRank = $rank }
        }
    }
    if ($null -eq $best) { throw ('Im aktiven Fenster finde ich nichts mit der Beschriftung „' + $label + '“.') }
    $name = $best.Current.Name
    $pattern = $null
    if ($best.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        $pattern.Invoke()
    } else {
        $point = $best.GetClickablePoint()
        [JarvisNative]::Click([int]$point.X, [int]$point.Y)
    }
    return @{ clicked = $name; window = $window.Title }
}

function Open-MailDraft($arguments) {
    $parts = @()
    if ($arguments.subject) { $parts += 'subject=' + [Uri]::EscapeDataString([string]$arguments.subject) }
    if ($arguments.body) { $parts += 'body=' + [Uri]::EscapeDataString([string]$arguments.body) }
    $to = ([string]$arguments.to).Trim()
    if ($to -and $to -notmatch '^[^@\s<>"]+@[^@\s<>"]+\.[A-Za-z]{2,}$') { throw 'Die E-Mail-Adresse ist ungültig.' }
    $uri = 'mailto:' + $to
    if ($parts.Count) { $uri += '?' + ($parts -join '&') }
    Start-Process $uri -ErrorAction Stop   # Standard-Mailprogramm: Entwurf öffnen, senden muss der Nutzer
    return @{ draft = $true; to = $to }
}

$script:trayIcon = $null
function Show-Notification([string]$title, [string]$text) {
    # Windows-Hinweis (Timer, Erinnerungen) – auch wenn das JARVIS-Fenster nicht offen ist
    if ($null -eq $script:trayIcon) {
        $script:trayIcon = New-Object System.Windows.Forms.NotifyIcon
        $script:trayIcon.Icon = [System.Drawing.SystemIcons]::Information
        $jarvisIcon = Join-Path $jarvisHome 'jarvis.ico'
        if (Test-Path $jarvisIcon) { try { $script:trayIcon.Icon = New-Object System.Drawing.Icon($jarvisIcon) } catch { } }
        $script:trayIcon.Text = 'JARVIS PC-Steuerung'
        $script:trayIcon.Visible = $true
    }
    $script:trayIcon.ShowBalloonTip(10000, $title, $text, [System.Windows.Forms.ToolTipIcon]::Info)
    return @{ shown = $true }
}

function Get-SystemInfo {
    # Systemmonitor: Auslastung, Speicher, Laufwerke, Akku, Temperaturen und Grafikkarte dieses PCs
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = @(Get-CimInstance Win32_Processor)
    $load = ($cpu | Measure-Object -Property LoadPercentage -Average).Average
    $disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' | ForEach-Object {
        @{ name = $_.DeviceID; total_gb = [math]::Round($_.Size / 1GB, 1); free_gb = [math]::Round($_.FreeSpace / 1GB, 1) }
    })
    $info = @{
        source = 'pc'; host = $env:COMPUTERNAME
        cpu_percent = [math]::Round([double]$load, 0); cpu_name = $cpu[0].Name.Trim()
        cpu_cores = ($cpu | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
        memory_total_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
        memory_used_gb = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 1)
        uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
        disks = $disks; temperatures = @(); gpus = @(); battery = $null
    }
    try {
        # Nur mit Administratorrechten bzw. auf manchen Geräten verfügbar – sonst bleibt die Liste leer
        $zones = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
        $info.temperatures = @($zones | ForEach-Object { @{ label = 'Mainboard'; celsius = [math]::Round($_.CurrentTemperature / 10 - 273.15, 0) } })
    } catch { }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        try {
            $rows = & $smi.Source --query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total --format=csv,noheader,nounits
            $info.gpus = @($rows | ForEach-Object {
                $f = $_.Split(',') | ForEach-Object { $_.Trim() }
                @{ name = $f[0]; percent = [int]$f[1]; celsius = [int]$f[2]; memory_used_gb = [math]::Round([double]$f[3] / 1024, 1); memory_total_gb = [math]::Round([double]$f[4] / 1024, 1) }
            })
        } catch { }
    }
    $battery = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($battery) { $info.battery = @{ percent = [int]$battery.EstimatedChargeRemaining; plugged = ($battery.BatteryStatus -eq 2) } }
    return $info
}

function Invoke-SystemAction([string]$name) {
    # Feste Freigabeliste – nie ein frei formulierter Befehl
    switch ($name) {
        'lock_screen' { Start-Process rundll32.exe -ArgumentList 'user32.dll,LockWorkStation' }
        'sleep' { Start-Process rundll32.exe -ArgumentList 'powrprof.dll,SetSuspendState 0,1,0' }
        'empty_recycle_bin' { Clear-RecycleBin -Force -ErrorAction SilentlyContinue }
        'open_task_manager' { Start-Process taskmgr.exe }
        'check_updates' { Start-Process 'ms-settings:windowsupdate-action' }
        'clean_temp' {
            $limit = (Get-Date).AddDays(-1)
            $files = @(Get-ChildItem -Path $env:TEMP -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt $limit })
            $bytes = ($files | Measure-Object -Property Length -Sum).Sum
            $removed = 0
            foreach ($file in $files) {
                try { Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop; $removed++ } catch { }
            }
            return @{ action = $name; files = $removed; freed_mb = [math]::Round([double]$bytes / 1MB, 0) }
        }
        'restart' { & shutdown.exe /r /t 60 /c 'JARVIS: Neustart in einer Minute (abbrechen: shutdown /a)' }
        'shutdown' { & shutdown.exe /s /t 60 /c 'JARVIS: Herunterfahren in einer Minute (abbrechen: shutdown /a)' }
        'cancel_shutdown' { & shutdown.exe /a }
        default { throw "Unbekannte Systemaktion '$name'." }
    }
    return @{ action = $name; done = $true }
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
            $script:found = @(Find-Files $query 'any' 20)
            return @{ searched = $query; total = $script:found.Count; results = @(Get-FoundList 5) }
        }
        'find_files' {
            $query = ([string]$arguments.query).Trim()
            if (-not $query) { throw 'Wonach soll ich suchen?' }
            $script:found = @(Find-Files $query (Get-Kind $arguments) 20)
            return @{ query = $query; total = $script:found.Count; results = @(Get-FoundList 5) }
        }
        'open_file' {
            if ($arguments.id) {
                $index = [int]$arguments.id - 1
                if ($index -lt 0 -or $index -ge $script:found.Count) { throw 'Diese Nummer gibt es in der letzten Suche nicht.' }
                $item = $script:found[$index]
            } else {
                $query = ([string]$arguments.query).Trim()
                if (-not $query) { throw 'Welche Datei soll ich öffnen?' }
                $script:found = @(Find-Files $query (Get-Kind $arguments) 20)
                if ($script:found.Count -eq 0) { throw ('In Ihren Ordnern finde ich nichts zu „' + $query + '“.') }
                $item = Select-BestFile $script:found $query
            }
            $result = Open-FoundItem $item ([bool]$arguments.show)
            $result.total = $script:found.Count
            return $result
        }
        'open_folder' {
            $key = [string]$arguments.folder
            if (-not $folders.ContainsKey($key)) { throw "Unbekannter Ordner '$key'." }
            Start-Process explorer.exe -ArgumentList ('"' + $folders[$key] + '"') -ErrorAction Stop
            return @{ opened = $key }
        }
        'app_search' {
            # „Such Arteriion auf Spotify“: in der installierten App suchen (sonst öffnet JARVIS die Webseite)
            $query = ([string]$arguments.query).Trim()
            if ([string]$arguments.app -ne 'spotify' -or -not $query) { return @{ opened = $false } }
            if (-not (Test-Path 'Registry::HKEY_CLASSES_ROOT\spotify')) { return @{ opened = $false } }
            Start-Process ('spotify:search:' + [Uri]::EscapeDataString($query)) -ErrorAction Stop
            return @{ opened = $true; app = 'spotify' }
        }
        'close_app' { return Close-App ([string]$arguments.app) }
        'type_text' { return Send-Text ([string]$arguments.text) ([bool]$arguments.enter) }
        'press_key' { return Send-Key ([string]$arguments.key) ([int]$(if ($arguments.times) { $arguments.times } else { 1 })) }
        'click' { return Invoke-ClickByName ([string]$arguments.label) }
        'compose_mail' { return Open-MailDraft $arguments }
        'notify' { return Show-Notification ([string]$arguments.title) ([string]$arguments.text) }
        'system_info' { return Get-SystemInfo }
        'system_action' { return Invoke-SystemAction ([string]$arguments.name) }
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
            actions = @('open_url', 'open_app', 'open_folder', 'search_files', 'find_files', 'open_file', 'app_search',
                        'close_app', 'type_text', 'press_key', 'click', 'compose_mail', 'notify', 'system_info',
                        'system_action')
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
