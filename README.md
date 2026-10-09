# JARVIS – Modulares KI-Assistenzsystem

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem – als echtes, implementierbares System definiert,
> nicht als Film-Imitation.

JARVIS ist eine Referenzarchitektur für einen persönlichen KI-Assistenten, der Sprache, Text, API-Events und
Sensordaten versteht, über mehrere Schritte planen kann, das Smart Home steuert, recherchiert, kommuniziert,
Systeme diagnostiziert und proaktiv handelt – abgesichert durch eine Policy-Engine mit Risikoklassen,
Bestätigungs-Workflows und lückenlosem Audit-Log. Die Jarvis-Persona (höflich, britisch-präzise, subtil humorvoll)
ist eine optional aktivierbare Schicht über dem technischen Kern.

Dieses Repository enthält das **vollständige technische Konzept**, maschinenlesbare **Schemas und API-Blueprints**
sowie ein **Referenz-Skelett** in Python und Node.js, dessen Kernlogik offline getestet wird.

## Architektur auf einen Blick

```mermaid
flowchart LR
    subgraph IN["Input-Layer"]
        V["Sprache<br/>Wake-Word · VAD · STT"]
        T["Text<br/>App · Desktop · Messenger"]
        E["API-Events<br/>REST · Webhooks"]
        S["Sensoren<br/>Home Assistant · MQTT"]
    end

    subgraph CORE["KI-Kern"]
        N["Normalisierung<br/>CloudEvents + Trust-Tagging"]
        R["Router<br/>Fast-Path · lokales LLM · Cloud-LLM"]
        O["Orchestrator<br/>Agent-Loop · Planner"]
        C["Kontext-Manager"]
        M[("Memory<br/>kurz · episodisch · semantisch · prozedural")]
    end

    subgraph SEC["Sicherheits-Layer"]
        P["Policy-Engine<br/>RBAC/ABAC · Risikoklassen R0–R4"]
        A["Audit-Log<br/>hash-verkettet"]
    end

    subgraph EXEC["Ausführung"]
        AE["Automations-Engine"]
        TM["Task-Manager"]
        PL["Plugin-Host<br/>Tools · MCP"]
    end

    subgraph OUT["Output-Layer"]
        TTS["Sprache (TTS)"]
        UI["Text · Push · Dashboard"]
        HA["Smart-Home-Befehle"]
        SYS["System-Tasks"]
    end

    V & T & E & S --> N --> R --> O
    O <--> C <--> M
    O -- "Tool-Aufruf" --> P
    P -- "erlaubt" --> PL & AE & TM
    P --> A
    PL & AE & TM --> HA & SYS
    O --> TTS & UI
```

## Dokumentation

| # | Abschnitt | Inhalt |
|---|-----------|--------|
| 1 | [Kurze Zusammenfassung](docs/01-zusammenfassung.md) | Vision, Designprinzipien, Kennzahlen, Technologie-Stack |
| 2 | [Komplette Architektur](docs/02-architektur.md) | KI-Kern, Input/Output, Automations-Engine, Entscheidungslogik, Sicherheit, Plugins, APIs, Deployment |
| 3 | [Module & Funktionen](docs/03-module-und-funktionen.md) | Vollständiger Funktionsumfang, Modul-Design mit Schnittstellen und Datenfluss |
| 4 | [Technische Umsetzung](docs/04-technische-umsetzung.md) | API-Blueprints, JSON-Schemas, Requests/Responses, Datenbank, Event-Flow, Error-Handling, Logging |
| 5 | [Code-Beispiele](docs/05-code-beispiele.md) | Python, JavaScript/Node, Home Assistant, MQTT, Node-RED – geführte Tour durch den Referenzcode |
| 6 | [Integrationsplan](docs/06-integrationsplan.md) | Home Assistant, Node-RED, MQTT, REST, Webhooks, lokale & Cloud-Modelle, Apps, Voice-Frontend, Roadmap |
| 7 | [Jarvis-Persona (optional)](docs/07-jarvis-persona.md) | Persona-Parameter, Voice-Style-Guide, Antwort-Beispiele |
| 8 | [Erweiterungen & Zukunft](docs/08-erweiterungen.md) | Ausbaustufen, Forschungsthemen, Risiken |

## Repository-Struktur

```
.
├── CHANGELOG.md              Änderungen des Jarvis-Moduls (Versionen)
├── docs/                     Konzept in 8 Abschnitten
├── api/openapi.yaml          REST/WebSocket-Blueprint (OpenAPI 3.1)
├── schemas/                  JSON-Schemas (Draft 2020-12) + validierte Beispiel-Payloads
├── config/                   Systemkonfiguration, Policies, Persona
├── db/schema.sql             PostgreSQL 16 + pgvector
├── reference/
│   ├── python/               Kern-Skelett: Orchestrator, LLM-Gateway, Policy, Memory, Automationen, Connectoren
│   ├── node/                 WebSocket-Client, Webhook-Relay, MQTT-Geräteadapter, Plugin-Beispiel
│   └── web/                  Browser-Voice-Widget
├── integrations/             Home Assistant, MQTT (Mosquitto), Node-RED
├── deploy/                   docker-compose Referenz-Deployment
└── tools/validate.py         Validiert Schemas, Beispiele, YAML/JSON-Artefakte
```

## Schnellstart

### Variante A – Offline-Demo (ohne Docker, ohne KI-Modell)

Voraussetzung: Python ≥ 3.11.

```bash
git clone https://github.com/mrdanilp15-crypto/Jarvis.git
cd Jarvis/reference/python
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m jarvis.demo              # spielt Fast-Path, Tool-Use, Bestätigungen, Injection-Abwehr durch
```

### Variante B – Als Windows-Programm, ohne Docker (empfohlen unter Windows)

Im Jarvis-Ordner **„JARVIS installieren.cmd“ doppelklicken** – sonst ist nichts zu tun:

- Fehlen Python 3.12 oder Ollama, installiert das Skript sie selbst (über winget, ohne Administratorrechte).
- JARVIS kommt in eine eigene Python-Umgebung (`%LOCALAPPDATA%\JARVIS`). Das Sprachmodell wird passend zur Hardware
  gewählt und geladen; Ollama nutzt eine NVIDIA- oder AMD-Grafikkarte direkt.
- Lief JARVIS bisher mit Docker, übernimmt das Skript Token, Name, Claude-Schlüssel und die übrigen Einstellungen aus
  `deploy/.env` und beendet die Docker-Fassung. Timer und eigene Termine bleiben im Docker-Volume.
- Danach startet JARVIS beim Anmelden automatisch; auf dem Desktop liegt die Verknüpfung „JARVIS“. Die PC-Steuerung läuft
  mit. Kein Redis, kein Postgres, kein Docker Desktop – deutlich weniger Arbeitsspeicher und ein schnellerer Start.

Aktualisieren: `git pull`, dann „JARVIS installieren.cmd“ erneut. Beenden: `JARVIS installieren.cmd -Stop`, Autostart
entfernen: `-Uninstall`. Einstellungen stehen in `%LOCALAPPDATA%\JARVIS\jarvis.env` (wie `deploy/.env`), das
Protokoll in `server.log` daneben. Die lokale Piper-Stimme gibt es nur in der Docker-Fassung; als Programm spricht
JARVIS mit „Microsoft Conrad“ (Edge oder Azure) bzw. der Browserstimme.

### Variante C – JARVIS-Server mit Docker (lokales KI-Modell)

Voraussetzung: Docker mit Compose v2; für flüssige Antworten 32 GB RAM oder eine GPU (siehe
[Hardware-Tabelle](docs/06-integrationsplan.md#65-lokale-ki-modelle)).

**Ein Befehl** (macOS, Linux, Windows über WSL/Git Bash) – legt `deploy/.env` mit zufälligem Passwort und API-Token
an, wählt das Sprachmodell passend zur Hardware (NVIDIA-GPU wird automatisch genutzt), startet Kern, Postgres, Redis
und Ollama, lädt die Modelle, wärmt sie vor und zeigt am Ende den Link zur Oberfläche:

```bash
git clone https://github.com/mrdanilp15-crypto/Jarvis.git
cd Jarvis
./deploy/start.sh          # stoppen: ./deploy/start.sh stop
```

**Mit JARVIS sprechen:** den angezeigten Link `http://127.0.0.1:8080/#token=…` in Chrome oder Edge öffnen, auf den
leuchtenden Kreis tippen (oder Leertaste), sprechen – JARVIS antwortet mit Stimme und schreibt mit. Tippen geht auch.
Mit **„Jarvis“-Aktivierung** (Schalter unter dem Kreis) reicht „Jarvis, wie wird das Wetter morgen?“. Im Zahnrad-Menü:
**Allgemein** (Sprachausgabe, Dauergespräch, Wohnort fürs Wetter, visuelle Effekte), **Stimme & Hören** (Stimme,
„Jarvis“ einlernen) und **KI-Modell** (siehe unten). Die Spracherkennung nutzt die des Browsers (Chrome/Edge
senden das Audio dafür an Google bzw. Microsoft – mit „Jarvis“-Aktivierung dauerhaft); vollständig lokal über
Whisper/Piper/openWakeWord ist der nächste Ausbauschritt. API-Beschreibung: `http://127.0.0.1:8080/docs`
(oben rechts **Authorize** → Token).

**Smart Home** (Zahnrad → **Smart Home**): JARVIS sucht Home Assistant beim Start selbst – unter
homeassistant.local, auf dem PC und im Heimnetz. Ist er gefunden, erscheint oben „Haus verbinden“. Ein Klick öffnet die
Anmeldung von Home Assistant; danach legt JARVIS sich einen eigenen, dauerhaften Zugang an (in Home Assistant unter
Profil → Sicherheit sichtbar) und übernimmt Räume, Geräte und deren Aliasse – alle fünf Minuten neu. Licht, Schalter,
Rollläden, Heizung, Szenen und Schlösser schaltet JARVIS dann ohne Sprachmodell, sofort; das Aufschließen einer Tür
muss in der App bestätigt werden. Freiere Fragen („Ist im Bad noch Licht an?“, „Wie warm ist es im Wohnzimmer?“)
beantwortet das Sprachmodell mit der aktuellen Geräteliste. Wer lieber ein Token einfügt: „Stattdessen ein Token
einfügen“ im selben Reiter. Noch kein Home Assistant? Am einfachsten auf einem Raspberry Pi oder Mini-PC
(home-assistant.io → Installation), Geräte dort hinzufügen und Räumen zuordnen – den Rest erledigt JARVIS.

Eingebaut und ohne API-Schlüssel: **Wetter** (Open-Meteo), **Nachrichten** (Tagesschau-RSS, weitere Feeds in
`config/jarvis.example.yaml`) und **Wikipedia**.

**Wissensfragen schlägt JARVIS nach, statt zu raten** („Wer ist …?“, „Was ist …?“, „Kennst du …?“):
- Gibt es einen passenden Wikipedia-Artikel, liest JARVIS dessen Anfang vor, ohne Sprachmodell und damit sofort.
- Gibt es nur Web-Treffer, die den Namen enthalten, fasst das Sprachmodell genau diese Treffer zusammen.
- Findet er nichts, sagt er das und bietet die Suche im Browser an.
- Ähnlich geschriebene Artikel zählen nicht: Bei „ARTERIION“ liefert die Wikipedia-Suche „Arterie“, und daraus hat ein
  kleines Modell früher eine Pharmafirma erfunden.
- Das Thema bleibt stehen: „Erzähl mir mehr“, „Nein, das ist ein Künstler“ (sucht neu) und „Such nach mehr Infos“
  (Browser-Suche zum Thema) beziehen sich darauf.

**Stimme:** „Automatisch“ (Zahnrad-Menü) nimmt die beste verfügbare – eine tiefe, ruhige Männerstimme im Stil des
Film-JARVIS (die Stimme eines realen Sprechers wird bewusst nicht nachgebildet):

1. **Microsoft Conrad über Azure** – beste Qualität in jedem Browser, mit einstellbarem KI-Effekt. Kostenloses
   Kontingent (F0, 500 000 Zeichen/Monat): im [Azure-Portal](https://portal.azure.com) eine Ressource
   **„Speech“** (Tarif *Free F0*) anlegen → **„Schlüssel und Endpunkt“** → *Schlüssel 1* und *Region* als
   `AZURE_SPEECH_KEY` und `AZURE_SPEECH_REGION` in `deploy/.env` eintragen → `./deploy/start.sh`. Der Antworttext
   geht dafür an Microsoft.
2. **Microsoft Conrad in Edge** – dieselbe Stimme kostenlos und ohne Einrichtung, wenn JARVIS in Microsoft Edge läuft
   (Autostart: `./deploy/start.sh autostart edge`).
3. **Piper** (`de_DE-thorsten-high`) – läuft komplett lokal; Rückfall, wenn nichts anderes verfügbar ist.

**Jarvis-Ton (Stil-Engine 2.0):** Jede Antwort läuft durch einen Formatter – höflich, britisch-präzise, ohne
Umgangssprache; Alltagsfragen („Status?“, „Wie spät ist es?“, „Kannst du mir helfen?“) beantwortet JARVIS sofort ohne
Sprachmodell. Ihren Namen fragt `./deploy/start.sh` ab (oder `JARVIS_USER_NAME` in `deploy/.env`). Details:
[Abschnitt 7.9](docs/07-jarvis-persona.md#79-stil-engine-20--neukalibrierung), Änderungen: [CHANGELOG](CHANGELOG.md).

**PC-Steuerung (Windows):** Ein kleiner PC-Agent (`deploy/windows/jarvis-pc-agent.ps1`) startet mit der
Desktop-Verknüpfung, verbindet sich selbst mit JARVIS (kein offener Port) und führt nur freigegebene Aktionen aus.
Er startet jedes Programm und Spiel aus dem **Windows-Startmenü** per Name, also Steam, Discord, Minecraft usw.
(ohne Deinstallations- oder Setup-Einträge). Zusätzliche eigene Einträge kommen in
`%LOCALAPPDATA%\JARVIS\apps.json`. Oben rechts zeigt „PC“, ob er verbunden ist.

**Was JARVIS kann – und was (noch) nicht:**

| Bereich | Beispiele (auch als Frage: „Kannst du …?“) | Voraussetzung |
|---|---|---|
| Programme & Spiele | „Öffne den Explorer“, „Kannst du Steam starten?“, „Ich möchte Minecraft spielen“ | PC-Agent verbunden |
| Ordner | „Öffne meine Downloads“, „Zeig mir die Bilder“ | PC-Agent |
| Webseiten | „Öffne YouTube“, „Öffne heise.de“, „Öffne Chefkoch“, „Geh auf Media Markt“ | PC-Agent |
| Link heraussuchen & öffnen | „Such mir einen Link zu einem Lasagne-Rezept und öffne ihn“, „Öffne die Webseite von Ikea“, „Spiel Lofi-Musik auf YouTube“ (öffnet das erste Video) | PC-Agent, Internet |
| Links zur Auswahl | „Such mir ein paar Links zu Kürbissuppe“ → „den zweiten“ | PC-Agent, Internet |
| Dateien finden & öffnen | „Öffne die Datei Bewerbung“, „Öffne die PDF Rechnung“, „Wo ist meine Steuererklärung?“ → „die zweite“ / „ja“, „Öffne den Ordner Projekte“, „Ordner Minecraft“ | PC-Agent |
| Suchen | „Such nach Pizza“, „Suche mir nach Arteriion auf Spotify“ (in der Spotify-App), „Such auf Amazon nach Kopfhörern“, „Schau auf Netflix nach Dark“, „Such die Datei Rechnung“ (Explorer-Suche) | PC-Agent |
| Korrigieren | Direkt nach einer Suche nur das richtige Wort sagen („Arteriion“), „Nein, ich meinte Spotify“, „Such das so, wie ich es geschrieben habe“ (nimmt das zuletzt getippte Wort), „Such nach mehr Infos“ (zum aktuellen Thema) | – |
| Programme schließen | „Schließ Steam“, „Mach den Browser zu“, „Schließ den Tab“ | PC-Agent |
| Tippen & Klicken | „Tippe Pizza Berlin und drück Enter“, „Klick auf Anmelden“, „Klick auf Alle akzeptieren“, „Drück zweimal Tab“ | PC-Agent |
| Musik & Lautstärke | „Pause“, „Nächstes Lied“, „Mach lauter“, „Etwas leiser“, „Ton aus“ | PC-Agent |
| Tastenkürzel | „Kopieren“, „Einfügen“, „Mach das rückgängig“, „Neuer Tab“, „Scroll runter“, „Geh zurück“ | PC-Agent |
| Timer & Erinnerungen | „Stell einen Nudel-Timer auf 8 Minuten“, „Wie lange läuft der Timer noch?“, „Erinnere mich morgen um 8 an den Müll“, „Weck mich um 7“ | – |
| Kalender | „Trag morgen um 15 Uhr Zahnarzt ein“, „Welche Termine habe ich am Freitag?“, „Wann ist mein nächster Termin?“, „Sag den Friseur ab“ | – (Abos: ICS-Adresse) |
| E-Mails | „Schreib eine E-Mail“ – JARVIS fragt nach Empfänger (Kontakt oder diktiert: „max punkt mustermann at gmx punkt de“), liest die Adresse vor, fragt Betreff und Text und öffnet den Entwurf. Korrigieren: „Nein“, „Die Adresse ist falsch“, „Der Betreff ist falsch“; „ohne Betreff“, „fertig“, „abbrechen“. In einem Satz: „Schreib eine Mail an Mama mit dem Betreff Sonntag“. Lesen: „Habe ich neue Mails?“ → „Lies die erste vor“ | Mailprogramm; Lesen: IMAP |
| Anmelden | „Melde mich bei Netflix an“ öffnet die Anmeldeseite – Passwörter gibt JARVIS nie ein (das übernimmt der Passwortmanager des Browsers) | PC-Agent |
| Wissen | „Wie wird das Wetter morgen?“, „Was gibt es Neues?“, „Wer war Ada Lovelace?“ (nachgeschlagen) → „Erzähl mir mehr“, „Wer ist ARTERIION?“ → „Nein, das ist ein Künstler“ | Internet |
| Assistenz | „Status?“, „Plan für morgen?“ (mit Terminen und Wetter), „Wie spät ist es?“, „Was kannst du?“, „Kannst du mich verstehen?“ (sagt, was angekommen ist) | – |
| Gedächtnis | „Merk dir, dass ich meinen Kaffee schwarz trinke“, „Was weißt du über mich?“, „Vergiss, dass …“ – bleibt über Neustarts erhalten (`data/memory.db`) | – |
| PC-Zustand | „Wie hoch ist die CPU-Auslastung?“, „Wie viel Arbeitsspeicher ist frei?“, „Wie voll ist die Festplatte?“, „Wie warm ist die Grafikkarte?“, „Systemmonitor“ | PC-Agent (sonst der JARVIS-Rechner) |
| Systemaktionen | „Sperr den Bildschirm“, „Leere den Papierkorb“, „Räum die temporären Dateien auf“, „Such nach Updates“, „Starte den PC neu“ / „Fahr den Rechner herunter“ (Bestätigung in der App), „Brich das Herunterfahren ab“ – feste Liste, keine freien Befehle | PC-Agent |
| Börse | „Wie steht Apple?“, „Wie steht der DAX?“, „Was kostet Bitcoin?“, „Aktienkurs von SAP“ (Tagesschluss) | Internet |
| Sehen | „Was siehst du?“, „Was halte ich in der Hand?“, „Lies mir das Etikett vor“ – ein Einzelbild, Kamera danach aus, Bild bleibt auf dem Rechner | Kamera, Bildmodell (lädt JARVIS selbst) |
| Anwesenheit | Zahnrad → Allgemein → „Anwesenheit über die Kamera“: JARVIS merkt, wenn Sie nach einer Abwesenheit (10–60 min) zurückkommen, und begrüßt Sie („Willkommen zurück, Sir. Ihr nächster Termin: …“). Nur Bewegung, keine Gesichtserkennung, Bilder bleiben im Browser | Webcam |
| Raum-Satelliten | Altes Tablet oder Handy als JARVIS in Küche, Bad, Flur: Zahnrad → Geräte → Raum wählen → „Koppeln“ → QR-Code mit dem Tablet scannen. Es hört auf „Jarvis“, bleibt an, und „Licht an“ meint seinen Raum | Tablet mit Chrome (Android) bzw. Safari (iPad), Ladegerät, selbes WLAN |
| Durchsagen | „Sag in der Küche, dass das Essen fertig ist“, „Durchsage an alle: Abfahrt in fünf Minuten“. Timer auf dem Küchen-Tablet klingeln in der Küche; hören mehrere Geräte „Jarvis“, antwortet nur das nächste | gekoppelte Raumgeräte |
| Radio & Medien | „Spiel Radio Bob im Wohnzimmer“, „Spiel den Sender 1Live in der Küche“ – auf Chromecast, Sonos, DLNA-Fernsehern | Home Assistant mit Mediaplayern |
| Routinen | JARVIS bemerkt Gewohnheiten („Küchenlicht werktags gegen 6:45 an“) und übernimmt sie nach Ihrer Zustimmung | Home Assistant |
| Haus | „Licht im Wohnzimmer aus“, „Alle Lichter aus“, „Dimm das Licht in der Küche auf 30 Prozent“, „Mach die Stehlampe an“, „Rollläden runter“, „Heizung im Bad auf 21 Grad“, „Starte Filmabend“, „Schließ die Haustür ab“ – Räume und Gerätenamen kommen aus Home Assistant | Home Assistant (Zahnrad → Smart Home) |
| Noch nicht | E-Mails selbst versenden (bewusst: nur Entwürfe), Termine in Google/Outlook eintragen (nur lesen; Nextcloud/iCloud über CalDAV schon), Formulare mit Passwörtern ausfüllen, Personen am Gesicht erkennen (bewusst nicht), beliebige Terminal-Befehle (bewusst nicht) | – |

**Module und Voraussetzungen** (alles läuft lokal; Cloud nur, wo ausdrücklich genannt):

| Modul | Hardware | Software / Einrichtung |
|---|---|---|
| Sprachmodell | ab 16 GB RAM; flüssig mit NVIDIA/AMD-GPU ab 6 GB (7b) bzw. 11 GB (14b) | Ollama – installiert der Windows-Installer bzw. `start.sh` |
| Gedächtnis | – | eingebaut (SQLite); Embedding-Modell `bge-m3` lädt der Installer, ohne es sucht JARVIS nach Wörtern |
| Spracherkennung lokal | Mikrofon; CPU mit 4+ Kernen (Whisper „small“, ~1–2 s je Satz); GPU optional (`JARVIS_STT_DEVICE=cuda`, `JARVIS_STT_MODEL=medium`) | Extra `voice-local` (faster-whisper, openWakeWord) – Windows-Installer nimmt es mit; Modell lädt beim ersten Start (~500 MB). Zahnrad → Stimme & Hören → Spracherkennung |
| Stimme | Lautsprecher | Piper lokal (Programm: im Prozess, Docker: Container); optional Microsoft Conrad (Edge kostenlos, Azure F0) |
| Sehen, Anwesenheit | Webcam | Bildmodell `qwen2.5vl:3b` (~3 GB, lädt JARVIS beim ersten „Was siehst du?“), anderes per `JARVIS_VISION_MODEL` |
| Raum-Satelliten | Tablet/Handy (Android 8+ mit Chrome oder iPad mit iPadOS 16.4+), Netzteil | Windows-Programm: automatisch (Port 8443, einmal Windows-Freigabe bestätigen; das WLAN muss in Windows „Privat“ sein). Docker: `JARVIS_LAN=on`, `JARVIS_LAN_ADDRESS=<IP des Rechners>` in `deploy/.env` |
| Smart Home, Radio, Routinen | Home Assistant (Raspberry Pi 4/5 oder Mini-PC); Zigbee/Z-Wave/Matter-Stick dort | Zahnrad → Smart Home (findet und verbindet selbst) |
| Systemmonitor & -aktionen | Windows-PC | PC-Agent (läuft mit JARVIS); Temperaturen nur, wo Windows sie herausgibt |
| Online-Kalender | – | `JARVIS_CALDAV_URL`, `JARVIS_CALDAV_USER`, `JARVIS_CALDAV_PASSWORD` (App-Passwort) in `jarvis.env` bzw. `deploy/.env` |
| Börse, Radio-Verzeichnis, Wetter, Wikipedia | Internet | ohne Schlüssel |

Befehle erkennt JARVIS ohne Sprachmodell (sofort). Freie Fragen beantwortet das Sprachmodell. Unvollständige
Befehle („Such mal …“) beantwortet JARVIS mit einer Rückfrage und hört danach direkt zu.

- **Dateien:** Der PC-Agent sucht im Benutzerordner über den Windows-Suchindex. Programme und Skripte unter den
  Treffern startet er nie, sondern markiert sie nur im Explorer.
- **Tippen:** In Konsolen (Eingabeaufforderung, PowerShell, Terminal) tippt er nicht.
- **Links:** JARVIS sucht über DuckDuckGo. Wer eine eigene SearXNG-Instanz betreibt, trägt sie als
  `JARVIS_SEARXNG_URL` ein.

**Optional in `deploy/.env`:**

| Einstellung | Wofür |
|---|---|
| `JARVIS_CALENDAR_ICS` | Kalender abonnieren, zum Beispiel Google Kalender → Einstellungen → „Privatadresse im iCal-Format“ |
| `JARVIS_CONTACTS` | Kontakte für Mail-Entwürfe (`Mama=mama@example.de; Max=max@example.de`) |
| `JARVIS_MAIL_COMPOSE` | Wo Mail-Entwürfe aufgehen: `mailto` (Mailprogramm), `gmail` oder `outlook` |
| `JARVIS_MAIL_IMAP_HOST`, `JARVIS_MAIL_USER`, `JARVIS_MAIL_PASSWORD` | E-Mails vorlesen (IMAP). Immer ein App-Passwort, nie das Hauptpasswort |

Timer, Erinnerungen und eigene Termine liegen im Docker-Volume `jarvis-data` und überstehen Neustarts. Ist der
Timer abgelaufen, spricht JARVIS die Meldung und Windows zeigt einen Hinweis.

**Was die Anzeige bedeutet:**
- Der Kreis wechselt weich die Farbe: Cyan = bereit, Weiß = hört zu, Gold = denkt, Violett mit Radar = schlägt nach,
  Blau = führt ein Werkzeug oder einen Befehl aus. Grün leuchtet er kurz auf, wenn etwas gefunden oder erledigt ist;
  Gold heißt „nichts gefunden“, Orange „wartet auf Ihre Bestätigung“, Rot mit kurzem Rütteln „Fehler“.
- Unter dem Kreis steht, was JARVIS gerade tut („Ich schlage nach: „Albert Einstein“ …“, „Ich rufe die Wetterdaten
  ab …“).
- **Karten:**
  - Wetter: animiertes Symbol, Temperatur, gefühlt, Wind, Luftfeuchte, 7-Tage-Vorschau mit Temperaturspanne und
    Regenwahrscheinlichkeit. Bei Regen oder Schnee fallen im Hintergrund Tropfen bzw. Flocken, bei Gewitter
    wetterleuchtet es.
  - Nachgeschlagenes: Wikipedia mit Bild und Link; Web-Quellen als Links unter der Antwort.
  - Timer mit Countdown-Ring, der beim Ablauf pulsiert.
- **Kopfzeile:** Uhr und Datum, das zuletzt abgefragte Wetter (Klick zeigt die Karte wieder) und das aktive Modell
  (Klick öffnet die Modellwahl).
- Wer es ruhiger mag oder einen langsamen Rechner hat: Zahnrad → Allgemein → Visuelle Effekte „dezent“ oder „aus“.
  Die Systemeinstellung „Bewegung reduzieren“ wird beachtet.

**Aktivierungswort:**
- Auf „Jarvis“ antwortet JARVIS mit „Ja, Sir?“. Ohne Sprachausgabe bleibt der kurze Ton.
- Das Wort wird auch in abweichenden Schreibweisen der Spracherkennung erkannt („Jarwis“, „Javis“, „Charvis“).
- Kurze Sprechpausen beenden einen Befehl nicht mehr.
- Hört JARVIS nicht auf seinen Namen, zeigt er unter dem Schalter, was er gehört hat (etwa „Gehört: „Service““). Ein
  Klick auf **Das war „Jarvis“** merkt sich diese Schreibweise.
- Im Zahnrad-Menü gibt es **„Jarvis“ einlernen**: Man sagt viermal „Jarvis“, und JARVIS merkt sich, was die
  Spracherkennung bei dieser Stimme daraus macht. Gelerntes lässt sich dort wieder löschen.
- Hört Chrome „Jarvis“ zunächst richtig und schreibt es danach um (oder verschluckt das kurze Wort), reagiert JARVIS
  trotzdem.

**Automatisch beim Anmelden starten (Windows):** `./deploy/start.sh autostart` (oder `… autostart edge`) – danach startet Windows Docker
Desktop, den PC-Agenten und JARVIS als eigenes Fenster, ohne Konsole; zusätzlich liegt eine Verknüpfung „JARVIS“ auf
dem Desktop. Nach einem Update erneut ausführen. Entfernen: `./deploy/start.sh autostart-remove`.

Update auf eine neue Version: `git pull` und erneut `./deploy/start.sh` (baut den Kern neu; geladene Modelle bleiben).

Dasselbe Schritt für Schritt:

```bash
cd Jarvis/deploy
cp .env.example .env               # POSTGRES_PASSWORD und den Dev-Token in JARVIS_DEV_TOKENS ändern;
                                   # optional ANTHROPIC_API_KEY für komplexe Anfragen über Claude
docker compose up -d --build jarvis-core      # startet auch Postgres, Redis und Ollama

# Modelle einmalig laden (Name muss zu llm.providers.local.model in config/jarvis.example.yaml passen)
docker compose exec ollama ollama pull qwen2.5:14b-instruct
docker compose exec ollama ollama pull bge-m3

curl http://127.0.0.1:8080/v1/system/health
curl -X POST http://127.0.0.1:8080/v1/conversations/test/messages \
  -H "Authorization: Bearer dev-alex-token" -H "Content-Type: application/json" \
  -d '{"text": "Hallo Jarvis, was kannst du?"}'
```

Chatten im Terminal (Node ≥ 22):

```bash
cd Jarvis/reference/node
JARVIS_URL=ws://127.0.0.1:8080 JARVIS_TOKEN=dev-alex-token node jarvis-client.mjs
```

- **Modell in der Oberfläche wählen:** Zahnrad → **KI-Modell**.
  - Dort stehen die installierten und die empfohlenen Modelle. **Herunterladen** lädt eines mit Fortschrittsanzeige
    und schaltet danach um, **Verwenden** wechselt ohne Neustart. Das alte Modell wird aus dem Speicher genommen.
  - **Claude:** API-Schlüssel eintragen (erstellt auf platform.claude.com unter „API Keys“) → **Prüfen & speichern**.
    JARVIS prüft ihn kostenlos und speichert ihn nur auf dem Server (Datei `llm.json` im Volume `jarvis-data`, nur
    für den Dienst lesbar); die Oberfläche zeigt danach nur die letzten vier Zeichen.
  - Wählbar sind Claude Opus 5.5 (empfohlen), Sonnet 5.5 (günstiger) und Fable 5.1 (am stärksten).
  - **Wer antwortet:** automatisch (schwierige Fragen an Claude), immer Claude oder nur lokal. Sensibles bleibt immer
    lokal.
  - Die Auswahl gilt auch nach einem Neustart. Oben rechts zeigt JARVIS, welches Modell gerade antwortet.
- Anderes Modell per Datei: `JARVIS_LLM_MODEL` in `deploy/.env` ändern (z. B. `qwen2.5:3b-instruct` = schneller,
  `qwen2.5:14b-instruct` = klüger) und `./deploy/start.sh` erneut ausführen. Das 3b-Modell (ohne Grafikkarte und mit
  wenig Arbeitsspeicher) versteht freie Fragen deutlich schlechter; wenn möglich mindestens `qwen2.5:7b-instruct`.
- Ohne Claude-Schlüssel arbeitet JARVIS rein lokal; ein Schlüssel in der Oberfläche hat Vorrang vor
  `ANTHROPIC_API_KEY` in `deploy/.env`.
- Home Assistant verbinden: Zahnrad → Smart Home (oder Token als `JARVIS_SECRET_KV_JARVIS_HOMEASSISTANT_TOKEN` und
  Adresse als `JARVIS_HA_URL` in `.env`)
  ([Anleitung](docs/06-integrationsplan.md#61-home-assistant)).
- Sprache, MQTT, Node-RED und Plugins kommen schrittweise dazu: [Inbetriebnahme](docs/06-integrationsplan.md#610-inbetriebnahme-referenz-deployment).
- Stoppen: `docker compose down` (Daten bleiben in Docker-Volumes erhalten).

## Prüfen & Ausprobieren

```bash
# Artefakte validieren (Schemas, Beispiel-Payloads, Konfiguration, Flows)
pip install jsonschema pyyaml
python tools/validate.py

# Kernlogik des Python-Skeletts testen (läuft offline, ohne LLM/Home Assistant)
cd reference/python
pip install -e ".[dev]"
pytest                    # 585 Tests
python -m jarvis.demo     # Fast-Path, Tool-Use, R3-Bestätigung, Prompt-Injection-Abwehr, Gastrechte

# Node-Beispiele (Node >= 22)
cd ../node && node --test
```

Dieselben Prüfungen laufen in der CI ([`.github/workflows/validate.yml`](.github/workflows/validate.yml)),
zusätzlich der Import von `db/schema.sql` in PostgreSQL 16 + pgvector und `docker compose config`.

Das Referenz-Deployment (`deploy/docker-compose.yml`) startet Postgres/pgvector, Redis, Mosquitto, Ollama,
Wyoming-STT/TTS/Wake-Word, SearXNG und optional Home Assistant und Node-RED; siehe
[Integrationsplan](docs/06-integrationsplan.md#65-lokale-ki-modelle).

## Status

Konzept vollständig; der Referenzcode deckt die sicherheitskritische Kernlogik ab (Policy-Engine,
Tool-Validierung, Bestätigungen, Taint-Tracking, Automations-Bedingungen, Memory-Ranking, Event-Schemas) und
dient als Ausgangspunkt der Implementierung gemäß Roadmap in [Abschnitt 6](docs/06-integrationsplan.md#611-roadmap).
