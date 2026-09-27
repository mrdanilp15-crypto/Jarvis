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

### Variante B – JARVIS-Server mit Docker (lokales KI-Modell)

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
Stimme, Wohnort fürs Wetter, Sprachausgabe und Dauergespräch. Die Spracherkennung nutzt die des Browsers (Chrome/Edge
senden das Audio dafür an Google bzw. Microsoft – mit „Jarvis“-Aktivierung dauerhaft); vollständig lokal über
Whisper/Piper/openWakeWord ist der nächste Ausbauschritt. API-Beschreibung: `http://127.0.0.1:8080/docs`
(oben rechts **Authorize** → Token).

Eingebaut und ohne API-Schlüssel: **Wetter** (Open-Meteo), **Nachrichten** (Tagesschau-RSS, weitere Feeds in
`config/jarvis.example.yaml`) und **Wikipedia**.

**Stimme:** JARVIS spricht mit einer lokalen Männerstimme (Piper, `de_DE-thorsten-high`) und einem KI-Klangeffekt im
Browser (etwas tiefer, leichter synthetischer Schimmer, kurzer Raumhall; Stärke im Zahnrad-Menü). Läuft Piper nicht,
spricht die Browserstimme.

**PC-Steuerung (Windows):** „Öffne den Explorer“, „Öffne meine Downloads“, „Öffne YouTube“, „Such im Internet nach …“,
„Starte Spotify“. Das übernimmt ein kleiner PC-Agent (`deploy/windows/jarvis-pc-agent.ps1`), der mit der
Desktop-Verknüpfung startet, sich selbst mit JARVIS verbindet (kein offener Port) und nur freigegebene Aktionen
ausführt – eigene Programme in `%LOCALAPPDATA%\JARVIS\apps.json`, z. B. `{"steam": "steam:"}`. Oben rechts zeigt
„PC“, ob er verbunden ist.

**Automatisch beim Anmelden starten (Windows):** `./deploy/start.sh autostart` – danach startet Windows Docker
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

- Anderes Modell: `JARVIS_LLM_MODEL` in `deploy/.env` ändern (z. B. `qwen2.5:3b-instruct` = schneller,
  `qwen2.5:14b-instruct` = klüger) und `./deploy/start.sh` erneut ausführen.
- Ohne `ANTHROPIC_API_KEY` arbeitet JARVIS rein lokal; mit Schlüssel gehen komplexe, nicht-sensible Anfragen an Claude.
- Home Assistant verbinden: Token als `JARVIS_SECRET_KV_JARVIS_HOMEASSISTANT_TOKEN` in `.env` eintragen
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
pytest                    # 157 Tests
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
