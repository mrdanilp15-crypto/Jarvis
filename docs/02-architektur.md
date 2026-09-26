# 2. Komplette Architektur

[← Zusammenfassung](01-zusammenfassung.md) · [Übersicht](../README.md) · [Weiter: Module & Funktionen →](03-module-und-funktionen.md)

## 2.1 Schichtenmodell

| Schicht | Verantwortung | Hauptkomponenten | Kommuniziert über |
|---------|---------------|------------------|-------------------|
| **Frontends** | Erfassen von Eingaben, Darstellen von Ausgaben | Voice-Satelliten, Mobile App, Desktop-Client, Web-UI, Messenger-Bots | WebSocket, MQTT, Wyoming, REST |
| **Input-Layer** | Signalverarbeitung, Normalisierung, Identität, Trust-Tagging | Wake-Word, VAD, STT, Sprecher-ID, Event-Normalizer, Webhook-Gateway | → Event-Bus |
| **KI-Kern** | Verstehen, Planen, Entscheiden, Antworten | Router, Orchestrator, Kontext-Manager, Memory, LLM-Gateway | Event-Bus, interne APIs |
| **Sicherheits-Layer** | Autorisieren, Bestätigen, Protokollieren, Isolieren | Policy-Engine, Confirmation-Service, Audit-Log, Secrets-Vault, Sandbox | synchron (Policy), Event-Bus (Audit) |
| **Ausführung** | Fähigkeiten ausführen, zeitgesteuerte Abläufe | Tool-Registry, Plugin-Host, Automations-Engine, Scheduler, Task-Manager | Event-Bus, Connectoren |
| **Connectoren** | Anbindung externer Systeme | Home Assistant, MQTT, REST/Webhooks, Kalender, Mail, Messenger, System-Agenten | jeweilige Protokolle |
| **Output-Layer** | Antworten formulieren und zustellen | Response-Composer, Persona-Renderer, TTS, Notification-Router | WebSocket, MQTT, Push |
| **Plattform** | Persistenz, Beobachtbarkeit, Betrieb | PostgreSQL/pgvector, Redis, OpenTelemetry, Grafana | – |

## 2.2 Komponentenübersicht

```mermaid
flowchart LR
    subgraph FE["Frontends"]
        SAT["Voice-Satelliten"]
        APP["Mobile App · Desktop · Web"]
        MSG["Messenger-Bots"]
    end

    subgraph INPUT["Input-Layer"]
        WW["Wake-Word"] --> VAD["VAD"] --> STT["STT"] --> SPK["Sprecher-ID"]
        WH["Webhook-Gateway<br/>HMAC"]
        NORM["Event-Normalizer<br/>CloudEvents + Trust"]
        SPK --> NORM
        WH --> NORM
    end

    BUS{{"Event-Bus<br/>Redis Streams"}}

    subgraph CORE["KI-Kern · jarvis-core"]
        RT["Router"]
        FP["Fast-Path-NLU"]
        ORC["Orchestrator<br/>Agent-Loop"]
        CTX["Kontext-Manager"]
        MEM["Memory-Service"]
        GW["LLM-Gateway"]
        PRO["Proaktiv-Engine"]
        RT --> FP
        RT --> ORC
        ORC <--> CTX
        CTX <--> MEM
        ORC <--> GW
        PRO --> ORC
    end

    subgraph MODELS["Modelle"]
        LOC["Lokales LLM · Ollama"]
        CLD["Cloud-LLM · Claude"]
        EMB["Embeddings"]
    end

    subgraph SECL["Sicherheits-Layer"]
        REG["Tool-Registry<br/>Schema-Prüfung"]
        POL["Policy-Engine"]
        CNF["Bestätigungen"]
        AUD["Audit-Log"]
        REG --> POL
        POL <--> CNF
        POL --> AUD
    end

    subgraph EXE["Ausführung · jarvis-worker / plugin-host"]
        AUT["Automations-Engine"]
        SCH["Scheduler"]
        TSK["Task-Manager"]
        PH["Plugin-Host"]
        SBX["Code-Sandbox"]
        SCH --> AUT
        SCH --> TSK
    end

    subgraph CON["Connectoren"]
        HAC["Home Assistant"]
        MQC["MQTT"]
        PIM["Kalender · Mail · Messenger"]
        SYA["System-Agenten · REST"]
    end

    subgraph OUTL["Output-Layer"]
        CMP["Response-Composer<br/>+ Persona"]
        TTS["TTS"]
        NTF["Notification-Router"]
        SPKOUT["Lautsprecher<br/>Satellit"]
        PUSH["App · Push · Messenger"]
        CMP --> TTS --> SPKOUT
        CMP --> NTF --> PUSH
    end

    subgraph DATA["Plattform"]
        DB[("PostgreSQL + pgvector")]
        RD[("Redis")]
        VLT["Secrets-Vault"]
    end

    SAT --> WW
    APP --> NORM
    MSG --> NORM
    NORM --> BUS
    BUS --> RT
    BUS --> AUT
    BUS --> PRO
    GW <--> MODELS
    MEM <--> EMB
    ORC -- "Tool-Aufrufe" --> REG
    FP -- "Aktion" --> REG
    AUT -- "Aktion" --> REG
    POL -- "erlaubt" --> CON
    POL -- "erlaubt" --> PH
    POL -- "erlaubt" --> SBX
    ORC --> CMP
    FP --> CMP
    CORE <--> DATA
    AUD --> DB
    PH --> VLT
```

### Dienste (Deployment-Einheiten)

| Dienst | Inhalt | Skalierung | Zustand |
|--------|--------|-----------|---------|
| `jarvis-core` | API (REST/WS), Router, Orchestrator, Kontext, Memory, Policy, Composer | 1–n Instanzen (zustandslos, Sessions in Redis) | Redis, Postgres |
| `jarvis-voice` | Audio-Sessions, Wake-Word-Verifikation, STT/TTS-Anbindung (Wyoming), Sprecher-ID | pro Standort 1 | flüchtig |
| `jarvis-worker` | Automations-Engine, Scheduler, Task-Manager, Proaktiv-Engine, Memory-Konsolidierung | 1 aktiv (Leader-Election via Redis-Lock) + Standby | Postgres |
| `jarvis-connectors` | Home Assistant, MQTT-Bridge, Mail, Kalender, Messenger | je Connector 1 | Redis (Cursor) |
| `jarvis-plugin-host` | Startet und isoliert Plugins (Prozess/Container), MCP-Clients | 1 | – |
| Infrastruktur | PostgreSQL, Redis, Mosquitto, Ollama, Wyoming-Dienste, SearXNG, OTel-Collector | – | persistent |

## 2.3 KI-Kern

### 2.3.1 Orchestrator (Agent-Loop)

Der Orchestrator führt pro Anfrage (*Turn*) einen begrenzten Agent-Loop aus. Das LLM darf Werkzeuge beantragen;
jede Ausführung läuft durch die Policy-Engine.

```mermaid
flowchart TD
    A["Turn startet<br/>Event mit correlationid"] --> B["Kontext bauen<br/>Persona · Situation · Memory · Verlauf"]
    B --> C["LLM-Aufruf über Gateway<br/>Streaming"]
    C --> D{"stop_reason"}
    D -- "Text fertig" --> Z["Antwort komponieren<br/>→ TTS / Text"]
    D -- "tool_use" --> E["Eingaben gegen JSON-Schema prüfen"]
    D -- "max_tokens / refusal" --> Y["Sicher abbrechen<br/>keine Tools ausführen"]
    E -- "ungültig" --> F["Fehler-Tool-Result an LLM"]
    E -- "gültig" --> G["Policy-Entscheidung"]
    G -- "allow" --> H["Ausführen · Timeout · Verifikation"]
    G -- "confirm" --> I["Bestätigung anlegen<br/>Nutzer fragen"]
    G -- "deny" --> F
    H --> J["Tool-Result + Audit"]
    I --> J
    F --> K{"Iterationen < Limit?"}
    J --> K
    K -- "ja" --> C
    K -- "nein" --> Y
    Y --> Z
```

Kernregeln:

1. **Begrenzung:** max. 8 Tool-Iterationen pro Turn, Gesamt-Timeout 60 s (Hintergrund-Jobs: eigener Task mit Budget).
2. **Parallelität:** Mehrere Tool-Aufrufe einer LLM-Antwort werden parallel ausgeführt; alle Ergebnisse gehen in *einer*
   Nachricht zurück.
3. **Keine Selbstbestätigung:** Bestätigungen werden ausschließlich aus dem Nutzerkanal aufgelöst (Sprach-„Ja“ desselben
   Sprechers, App-Button, PIN) – nie durch ein Tool, das das LLM aufrufen kann.
4. **Taint-Tracking:** Sobald nicht vertrauenswürdige Inhalte (Webseiten, E-Mails, fremde Webhooks) im Kontext sind,
   verlangen alle Aktionen ab R2 eine Bestätigung (siehe [2.8.3](#283-schutz-vor-prompt-injection)).
5. **Verifikation:** Nach Zustandsänderungen prüft der Orchestrator den Zielzustand (z. B. `light.kueche` ist `on`) und
   meldet Abweichungen, statt Erfolg zu behaupten.

### 2.3.2 LLM-Gateway und Modell-Router

Das **LLM-Gateway** abstrahiert Anbieter hinter einer einheitlichen Schnittstelle (`complete(system, messages, tools,
on_text)`), führt ein anbieterneutrales Transkript und rendert es pro Anbieter in dessen Wire-Format. Assistenten-
Antworten eines Anbieters werden unverändert (inkl. Denk-Blöcken) an *denselben* Anbieter zurückgegeben.

Der **Router** entscheidet pro Turn deterministisch:

| Bedingung (erste passende Regel gewinnt) | Route | Begründung |
|------------------------------------------|-------|-----------|
| Grammatik-Treffer mit Konfidenz ≥ 0,9 und Intent in Fast-Path-Liste | **Fast-Path** (kein LLM) | < 300 ms, deterministisch |
| Offene Bestätigung und Antwort ist Ja/Nein | **Confirmation-Resolver** (kein LLM) | Sicherheitsgrund |
| Sensitivität `sensitive`/`secret` (Kamera, Gesundheit, Zugangsdaten) | **Lokales LLM** | Daten verlassen das Haus nicht |
| Kein Internet / Cloud-Circuit-Breaker offen | **Lokales LLM** | Degradierter Modus |
| Komplexität `simple` (Smalltalk, kurze Fakten, einfache Geräteketten) | **Lokales LLM** | Latenz, Kosten |
| Komplexität `complex` (Recherche, Planung, Code, lange Texte, Multi-Step) | **Cloud-LLM** | Qualität |
| Nutzer-Override („frag das große Modell“) | wie angefragt, sofern Sensitivität erlaubt | Transparenz |

Komplexität und Sensitivität liefert ein leichtgewichtiger Klassifikator (Regeln + kleines lokales Modell), Ausgabe
gemäß [`intent.schema.json`](../schemas/intent.schema.json).

**Cloud-Aufrufe (Claude)** laufen mit Streaming, adaptivem Denken (`thinking: {type: "adaptive"}`), Aufwandssteuerung
über `output_config.effort` (Standard `medium` für Dialoge, `high` für Recherche/Code), Prompt-Caching des statischen
Systemprompts inklusive Tool-Definitionen und serverseitigem Refusal-Fallback. Details und Code:
[`reference/python/src/jarvis/llm/claude.py`](../reference/python/src/jarvis/llm/claude.py).

### 2.3.3 Kontext-Management

Der Kontext-Manager baut für jeden LLM-Aufruf ein Kontextpaket mit festem Token-Budget. Stabile Teile stehen vorne
(cachebar), volatile Teile hinten.

| Reihenfolge | Baustein | Budget (Richtwert bei 32 k lokal / 200 k Cloud) | Quelle | Cachebar |
|-------------|----------|-----------------------------------------------|--------|----------|
| 1 | Tool-Definitionen (gefiltert nach Kontext) | 3 k / 8 k | Tool-Registry | ja |
| 2 | Systemprompt: Rolle, Regeln, Sicherheitsleitplanken | 1,5 k | Konfiguration | ja |
| 3 | Persona-Fragment (falls aktiv) | 0,5 k | `persona.*.yaml` | ja |
| 4 | Situationskontext: Zeit, Ort, Sprecher, Raum, relevante Gerätezustände, Modus | 1 k / 3 k | Home-State-Cache, Sessions | nein |
| 5 | Erinnerungen (Top-k nach Relevanz) | 2 k / 10 k | Memory-Service | nein |
| 6 | Zusammenfassung älterer Gesprächsteile | 1 k / 4 k | Kontext-Manager | nein |
| 7 | Letzte Turns im Wortlaut | Rest | Session-Speicher | nein |

Strategien:

- **Relevanzfilter:** Nur Geräte des aktuellen Raums bzw. im Satz genannte Entitäten werden als Zustand eingefügt.
- **Tool-Filter:** Tools werden nach Intent-Domäne vorgefiltert (Smart Home, Kommunikation, Technik …), um Tokens und
  Fehlgriffe zu reduzieren.
- **Rolling Summary:** Überschreitet der Verlauf sein Budget, werden die ältesten Turns durch das lokale LLM
  zusammengefasst; die Zusammenfassung ersetzt sie (Episodic Memory erhält eine Kopie).
- **Sessions:** Eine Session gilt pro Sprecher und Gerät; nach 5 min Inaktivität endet der *Dialog-Kontext*,
  das Gedächtnis bleibt.

### 2.3.4 Memory-Module (Überblick)

| Speicher | Inhalt | Lebensdauer | Technik |
|----------|--------|-------------|---------|
| **Arbeitsgedächtnis** | aktueller Dialog, offene Rückfragen, Slots | Session (Minuten) | Redis, TTL |
| **Episodisch** | Zusammenfassungen von Gesprächen und Ereignissen („Gestern Abend Heizung repariert“) | Monate, mit Verfall | Postgres + pgvector |
| **Semantisch** | Fakten und Präferenzen („Alex trinkt Kaffee schwarz“, „Arbeitszimmer = Büro“) | bis Widerruf/Widerspruch | Postgres + pgvector, Subjekt–Prädikat–Objekt |
| **Prozedural** | gelernte Abläufe, bestätigte Routinen, bevorzugte Tool-Ketten | bis Löschung | Postgres (Automationen, Skills) |

Details: [Abschnitt 3.2.6](03-module-und-funktionen.md#326-memory-system).

### 2.3.5 Reasoning und Planung

- **Einfache Anfragen:** direktes Tool-Use im Agent-Loop (ReAct-Stil: denken → Tool → beobachten → antworten).
- **Mehrschrittige Aufgaben** (z. B. „Plane meinen Umzug“): *Plan-and-Execute* – das LLM erstellt einen Plan als
  strukturierte Liste von Schritten (Task-Manager-Tasks mit Abhängigkeiten); ein Worker arbeitet ihn ab und fragt bei
  Schritten ≥ R2 nach. Zwischenstände sind im Task-Manager sichtbar und fortsetzbar.
- **Verifikation:** Nach jeder Zustandsänderung Soll/Ist-Vergleich; nach Recherche Quellenangabe und Konfliktprüfung.
- **Reflexion:** Fehlgeschlagene Tool-Aufrufe werden mit Fehlercode zurückgegeben; das LLM darf einmal korrigieren,
  danach wird der Nutzer informiert.

## 2.4 Input-Layer

| Quelle | Protokoll | Vorverarbeitung | Event-Typ | Standard-Trust |
|--------|-----------|-----------------|-----------|---------------|
| Sprache (Satellit, App, Browser) | Wyoming, WebSocket (PCM16 16 kHz) | Wake-Word → VAD → STT → Sprecher-ID | `jarvis.input.utterance` | `household` (erkannt) / `guest` |
| Text (App, Desktop, Web) | WebSocket, REST | Auth (OIDC), Sprache erkennen | `jarvis.input.text` | `trusted_user` |
| Messenger (Telegram, Matrix, Signal) | Bot-APIs | Absender-Allowlist | `jarvis.input.text` | `trusted_user` (Allowlist) sonst verworfen |
| API-Events | REST `POST /v1/events` | Scope-geprüfter API-Key | beliebig `jarvis.*` | laut Key (`system`/`external_untrusted`) |
| Webhooks | `POST /v1/webhooks/{id}` | HMAC-Signatur, Zeitstempel, Replay-Schutz | `jarvis.webhook.received` | `external_untrusted` |
| Sensoren (Home Assistant) | HA-WebSocket `subscribe_events` | Filter, Entprellung, Deduplikation | `jarvis.sensor.state_changed` | `system` |
| Sensoren (MQTT direkt) | MQTT `jarvis/v1/in/sensor/#` | Payload-Schema-Prüfung | `jarvis.sensor.reading` | `system` |
| Zeit | Scheduler | – | `jarvis.schedule.fired` | `system` |
| Systemüberwachung | Prometheus Alertmanager Webhook | Mapping | `jarvis.system.alert` | `system` |

Jede Eingabe wird in einen **CloudEvents-1.0-Umschlag** ([`event.schema.json`](../schemas/event.schema.json)) mit
den Erweiterungen `correlationid`, `causationid`, `actor`, `trust` und `priority` überführt. Der Trust-Wert ist der
Ausgangspunkt jeder Policy-Entscheidung.

| Trust-Stufe | Beschreibung | Maximale Risikoklasse ohne Zusatz-Authentifizierung |
|-------------|--------------|------------------------------------------------------|
| `system` | interne Komponenten, Sensoren, Scheduler | laut Automationsfreigabe |
| `trusted_user` | authentifizierter Nutzer (OIDC-Token, App) | R2 |
| `household` | per Stimme erkannter Haushaltsnutzer | R2 (R1 bei Stimmkonfidenz < 0,8) |
| `guest` | unbekannte Stimme, Gastmodus | R1 und nur freigegebene Räume |
| `external_untrusted` | Webhooks Dritter, Web-Inhalte, E-Mail-Inhalte | R0 (nur Information, keine Aktionen) |

## 2.5 Output-Layer

| Kanal | Einsatz | Format | Technik |
|-------|---------|--------|---------|
| **Sprache** | Antworten am Satelliten, Anruf-Modus | Text → SSML-Light → Audio (satzweise gestreamt) | Piper via Wyoming, optional Cloud-TTS |
| **Text** | App, Desktop, Web, Messenger | Markdown, Karten (Rich Cards) | WebSocket, Bot-APIs |
| **Push** | proaktive Hinweise, Bestätigungen R3 | Titel, Text, Aktionen (Buttons) | ntfy / FCM / APNs |
| **Dashboard** | Status, Timer, laufende Tasks | JSON-State | WebSocket, HA-Entitäten |
| **Smart-Home-Befehle** | Geräte steuern | HA-Service-Aufrufe, MQTT-Kommandos | Connectoren |
| **System-Tasks** | Skripte, Diagnosen, Neustarts | Job-Aufträge an System-Agenten / Sandbox | mTLS-RPC |

Der **Response-Composer** trennt *Inhalt* (Fakten, Status, Rückfragen) von *Form* (Persona, Länge, Kanal):

1. Das LLM (oder der Fast-Path) liefert Inhalt plus Metadaten (`kind`: `status`, `answer`, `question`, `warning`,
   `error`).
2. Der Persona-Renderer formt kanalgerecht um (Sprache: kurz, ohne Markdown; Text: strukturiert).
3. Für Sprache wird der Token-Stream an Satzgrenzen gepuffert und satzweise an TTS gegeben (erste Silbe < 1,5 s).
4. Warnungen und Fehler werden nie „weich gezeichnet“ – die Persona darf höflich sein, aber nicht verharmlosen.

## 2.6 Automations-Engine (Überblick)

Regeln nach dem Muster **Trigger → Bedingungen → Aktionen** ([`automation.schema.json`](../schemas/automation.schema.json)):

- **Trigger:** Zustandsänderung, Event-Typ, Uhrzeit, Cron, Sonnenstand, Anwesenheit, Webhook.
- **Bedingungen:** Zustand, numerische Schwellen, Zeitfenster/Wochentage, Anwesenheit, verschachtelt `and`/`or`/`not`.
- **Aktionen:** Capability-Aufruf (durch die Policy-Engine!), Verzögerung, Benachrichtigung, Auswahl (`choose`),
  optionaler LLM-Schritt (z. B. „Fasse die Nacht-Events zusammen“).

Automationen haben eine Herkunft (`user`, `jarvis_suggested`, `imported`). Von JARVIS vorgeschlagene Automationen sind
bis zur Freigabe inaktiv und laufen vorab im **Dry-Run** gegen die Event-Historie der letzten 14 Tage („wäre 9-mal
ausgelöst worden“). Details: [3.2.4](03-module-und-funktionen.md#324-automation-engine).

## 2.7 Entscheidungslogik

```mermaid
flowchart LR
    EV["Event"] --> CL["Klassifizieren<br/>Intent · Sensitivität · Komplexität"]
    CL --> RO{"Route"}
    RO -- "Fast-Path" --> FA["Grammatik-Intent<br/>→ Capability"]
    RO -- "LLM" --> PL["Planen<br/>Agent-Loop"]
    FA --> RI["Risiko bewerten<br/>Risikoklasse · Trust · Kontext"]
    PL --> RI
    RI --> DE{"Entscheidung"}
    DE -- "allow" --> EX["Ausführen"]
    DE -- "confirm" --> CO["Bestätigung<br/>Stimme · App · PIN"]
    DE -- "deny" --> EXP["Erklären"]
    CO -- "ja" --> EX
    CO -- "nein / Timeout" --> EXP
    EX --> VE["Verifizieren"]
    VE --> LE["Lernen<br/>Memory · Statistik"]
```

### 2.7.1 Risikoklassen

| Klasse | Bedeutung | Beispiele | Standardverhalten |
|--------|-----------|-----------|-------------------|
| **R0** | nur lesen/informieren | Wetter, Status abfragen, Suche, Kalender lesen | immer erlaubt (im Rahmen der Datenberechtigung) |
| **R1** | Komfort, sofort umkehrbar | Licht, Musik, Szenen, Timer, Notizen, Erinnerungen | erlaubt ab `guest` (Raum-Freigabe) |
| **R2** | spürbare Wirkung, umkehrbar mit Aufwand oder Kosten | Heizung/Klima, Nachricht an bekannte Kontakte, Kalendereintrag mit Einladung, Automation anlegen | erlaubt für `trusted_user`/`household`; Bestätigung bei Taint, Gast oder Unsicherheit |
| **R3** | sicherheitskritisch oder irreversibel | Türschloss öffnen, Alarmanlage aus, Kamera-Privatsphäre, E-Mail an neue Empfänger, Skript auf Host ausführen, Daten löschen | **immer** Bestätigung, zusätzlich starke Authentifizierung (App-Biometrie oder PIN) |
| **R4** | verboten für Autonomie | Sicherheitsfunktionen dauerhaft deaktivieren, Zahlungen/Überweisungen, Massenlöschung, Policies ändern | **immer verweigert**; nur manuell über Admin-UI |

### 2.7.2 Kontextregeln (Auszug)

| Regel | Wirkung |
|-------|---------|
| Nachtruhe (22–7 Uhr) | Proaktive Sprachausgaben nur bei Priorität `critical`; Lautstärke reduziert |
| Abwesenheitsmodus | Aktionen, die Personen im Haus voraussetzen (Herd, Staubsauger), benötigen Bestätigung |
| Gastmodus | nur R0/R1, nur freigegebene Räume, keine persönlichen Daten in Antworten |
| Mehrdeutigkeit (mehrere Zielgeräte, Konfidenz < 0,7) | Rückfrage statt Ausführung |
| Sicherheitsereignis aktiv (Rauch, Wasser, Einbruch) | Proaktiv-Engine eskaliert, Automationen mit `safety`-Tag haben Vorrang |

### 2.7.3 Proaktive Vorschläge

Die Proaktiv-Engine bewertet Kandidaten (aus Automationsmustern, Kalender, Wetter, Gerätezuständen, Anomalien):

```
score = relevanz × dringlichkeit × konfidenz − störkosten
störkosten = f(Tageszeit, Anwesenheit, Anzahl Hinweise letzte Stunde, Ablehnungsquote dieses Hinweistyps)
```

Nur Kandidaten mit `score ≥ schwelle(kanal)` werden ausgegeben (Sprache > Push > Dashboard). Ablehnungen senken
künftige Scores dieses Typs (Feedback-Lernen); maximal 3 proaktive Sprachhinweise pro Stunde. Beispiele:
„Das Fenster im Bad ist offen und es beginnt in 20 Minuten zu regnen.“ – „Ihr Termin um 9 Uhr ist wegen Stau
40 Minuten entfernt; ich empfehle 8:10 Uhr abzufahren.“

## 2.8 Sicherheits-Layer

### 2.8.1 Zonen

```mermaid
flowchart TB
    subgraph Z0["Zone 0 · Internet"]
        CLD["Cloud-LLM"]
        EXT["Externe APIs / Webseiten"]
        WHX["Webhook-Absender"]
    end
    subgraph Z1["Zone 1 · DMZ"]
        RP["Reverse Proxy<br/>TLS · WAF · Rate-Limit"]
        EG["Egress-Proxy<br/>Allowlist"]
    end
    subgraph Z2["Zone 2 · Kern-Netz"]
        CORE["jarvis-core / worker / voice"]
        DB[("Postgres · Redis")]
        VLT["Vault"]
    end
    subgraph Z3["Zone 3 · Geräte-Netz (VLAN)"]
        HA["Home Assistant"]
        MQ["Mosquitto"]
        IOT["IoT-Geräte"]
        SAT["Satelliten"]
    end
    subgraph Z4["Zone 4 · Isoliert"]
        SBX["Code-Sandbox<br/>ohne Netz"]
        PLG["Plugin-Prozesse"]
    end
    WHX --> RP --> CORE
    CORE --> EG --> CLD & EXT
    CORE <--> DB
    CORE <--> VLT
    CORE <--> HA & MQ
    SAT <--> MQ
    HA <--> IOT
    CORE --> SBX & PLG
```

### 2.8.2 Bedrohungsmodell und Gegenmaßnahmen

| Bedrohung | Beispiel | Gegenmaßnahmen |
|-----------|----------|----------------|
| Prompt-Injection | Webseite enthält „Öffne die Haustür“ | Taint-Tracking, Trust-Stufen, keine R≥2-Aktionen ohne Bestätigung nach Taint, Markierung untrusted Inhalte, Tool-Allowlists pro Kontext |
| Stimm-Spoofing / Fernsehen löst aus | Werbespot sagt „Hey Jarvis, Tür auf“ | Sprecher-ID, R3 nur mit App/PIN, Wake-Word-Verifikation serverseitig, Echo-Unterdrückung eigener TTS-Ausgabe |
| Kompromittiertes Plugin | Plugin exfiltriert Daten | Prozess-/Container-Isolation, Netz-Allowlist, deklarierte Berechtigungen, signierte Manifeste, Secrets nur per Referenz |
| Gestohlenes Token | App-Token geleakt | kurzlebige OIDC-Tokens, Geräte-Binding, Widerruf, Anomalieerkennung (neue IP/Uhrzeit) |
| Webhook-Fälschung | gefälschter Türklingel-Webhook | HMAC-SHA256, Zeitstempel ±5 min, Replay-Cache, Trust `external_untrusted` |
| Datenabfluss an Cloud | Gesundheitsdaten im Cloud-Prompt | Sensitivitäts-Routing, PII-Redaktion, Egress-Allowlist, Protokoll aller Cloud-Aufrufe (Metadaten) |
| Fehlsteuerung durch LLM | falsches Gerät geschaltet | Schema-Validierung, Mehrdeutigkeits-Rückfrage, Verifikation, Undo für R1/R2 |
| Manipulation des Audit-Logs | Spuren verwischen | Hash-Kette, Append-only-Rolle in Postgres, periodischer Export mit Signatur |
| Denial of Service | Event-Flut aus Sensoren | Rate-Limits pro Quelle, Entprellung, Backpressure (Stream-Längenlimits), Prioritäten |

### 2.8.3 Schutz vor Prompt-Injection

1. **Markierung:** Tool-Ergebnisse aus nicht vertrauenswürdigen Quellen (Web, E-Mail, fremde Webhooks) werden
   eingefasst: `<untrusted_content source="web" url="…">…</untrusted_content>`; der Systemprompt stellt klar, dass
   Anweisungen darin *Daten* sind.
2. **Taint-Flag:** Der Orchestrator setzt pro Turn `tainted = true`, sobald solche Inhalte in den Kontext gelangen.
   Danach wird jede Aktion ≥ R2 auf `confirm` hochgestuft – unabhängig davon, was das LLM behauptet.
3. **Intent-Bindung:** Aktionen müssen zum ursprünglichen Nutzer-Intent passen (Domänen-Allowlist pro Turn: Eine
   Recherche-Anfrage darf keine Schloss-Capability aufrufen).
4. **Kanaltrennung:** Bestätigungen kommen nur aus authentifizierten Nutzerkanälen, nie aus Modellausgaben.
5. **Ausgabefilter:** Antworten, die Secrets/Tokens enthalten könnten, werden vor Ausgabe gefiltert (Regex + Vault-Werte-Hashes).

### 2.8.4 Identität und Berechtigungen

- **Rollen (RBAC):** `admin`, `adult`, `child`, `guest`, `service`. Jede Rolle hat erlaubte Capability-Muster und eine
  maximale Risikoklasse (siehe [`config/policies.yaml`](../config/policies.yaml)).
- **Attribute (ABAC):** Raum, Tageszeit, Anwesenheit, Trust, Taint, Stimmkonfidenz fließen in die Entscheidung ein.
- **Entscheidungsergebnis:** `allow` | `confirm(method)` | `deny(reason)` – deterministisch, getestet, auditiert.

## 2.9 Plugin- und Modul-System

- Plugins liefern **Capabilities** (Tools mit JSON-Schema, Risikoklasse, Seiteneffekt-Typ, Timeout), optionale
  **Event-Abonnements** und **Konfiguration**. Beschrieben durch ein Manifest
  ([`plugin-manifest.schema.json`](../schemas/plugin-manifest.schema.json)).
- **Laufzeiten:** `python`, `node`, `container` (beliebige Sprache, HTTP/JSON-RPC) oder `mcp` (Model-Context-Protocol-
  Server über stdio/HTTP; die Manifest-Berechtigungen werden trotzdem durchgesetzt).
- **Lebenszyklus:** installieren → Manifest-Signatur prüfen → Berechtigungen vom Admin freigeben → starten →
  Health-Check → Capabilities registrieren → bei Crash neu starten (Backoff) → deaktivieren/entfernen.
- **Isolation:** eigener Prozess bzw. Container, eigenes Netzwerk-Profil, Secrets werden nur als kurzlebige
  Referenzen injiziert, Aufrufe laufen immer über Tool-Registry → Policy-Engine.

## 2.10 API-Schnittstellen

| Schnittstelle | Zweck | Spezifikation |
|---------------|-------|---------------|
| REST `/v1/*` | Konversationen, Events, Aktionen, Bestätigungen, Automationen, Tasks, Memory, Plugins, System | [`api/openapi.yaml`](../api/openapi.yaml) |
| WebSocket `/v1/stream` | bidirektionales Streaming: Audio, Teil-Transkripte, Token-Stream, Status-Events | [4.2.3](04-technische-umsetzung.md#423-websocket-protokoll-v1stream) |
| MQTT `jarvis/v1/…` | Geräte, Satelliten, Status, Event-Fan-out | [4.2.4](04-technische-umsetzung.md#424-mqtt-topics) |
| Webhooks ein | externe Dienste → JARVIS (HMAC) | [4.2.5](04-technische-umsetzung.md#425-webhooks) |
| Webhooks aus | JARVIS → externe Dienste (signiert, mit Retry) | [4.2.5](04-technische-umsetzung.md#425-webhooks) |
| Plugin-Protokoll | Plugin-Host ↔ Plugin (`/manifest`, `/invoke`, `/health`) oder MCP | [3.2.8](03-module-und-funktionen.md#328-plugin-system) |

Versionierung: URL-Präfix `/v1`, Topic-Präfix `jarvis/v1`, Event-Typen additiv erweiterbar; Breaking Changes nur mit
neuer Hauptversion und 6 Monaten Parallelbetrieb.

## 2.11 Deployment-Topologie

```mermaid
flowchart LR
    subgraph HOME["Zuhause"]
        subgraph SRV["Home-Server<br/>x86 · 32–64 GB RAM · optional GPU 12–24 GB"]
            C1["jarvis-core"]
            C2["jarvis-worker"]
            C3["jarvis-voice"]
            C4["jarvis-connectors"]
            C5["Ollama · Whisper · Piper"]
            C6[("Postgres · Redis")]
            C7["Mosquitto"]
        end
        HAS["Home Assistant<br/>eigener Host oder VM"]
        S1["Satellit Wohnzimmer"]
        S2["Satellit Küche"]
        PC["Desktop-Client"]
    end
    subgraph MOB["Unterwegs"]
        PH["Mobile App"]
    end
    subgraph CLOUD["Cloud"]
        LLM["Claude API"]
        PUSH["Push-Dienst"]
    end
    S1 & S2 <--> C7
    S1 & S2 <--> C3
    PC <--> C1
    PH <-- "VPN (WireGuard) oder Reverse Proxy" --> C1
    C4 <--> HAS
    C1 --> LLM
    C1 --> PUSH --> PH
```

**Hardware-Empfehlung:** Home-Server mit 8+ Kernen, 32–64 GB RAM; für lokale LLMs ≥ 14 B Parameter eine GPU mit
12–24 GB VRAM (alternativ Apple-Silicon-Mac mit 32 GB+). Satelliten: ESP32-S3 mit Mikrofon-Array (ESPHome, Wyoming)
oder Raspberry Pi 5 mit ReSpeaker-HAT.

## 2.12 Ausfallsicherheit und degradierte Modi

| Ausfall | Erkennung | Verhalten |
|---------|-----------|-----------|
| Internet / Cloud-LLM | Circuit-Breaker (5 Fehler / 30 s) | Router nutzt lokales LLM; Antwort-Hinweis „Ich arbeite gerade offline“ bei komplexen Anfragen |
| Lokales LLM | Health-Check | Cloud-LLM für nicht-sensible Anfragen; sensible Anfragen → Fast-Path oder Ablehnung mit Erklärung |
| Home Assistant | WebSocket getrennt | Reconnect mit Backoff; Geräteaktionen werden als fehlgeschlagen gemeldet (kein stilles Queueing für Aktionen ≥ R2) |
| Redis | Health-Check | Sessions flüchtig im Prozess; Event-Bus puffert lokal (begrenzt) |
| Postgres | Health-Check | Memory schreibgeschützt aus Cache; Audit schreibt in lokales Append-Log und synchronisiert nach |
| STT/TTS | Health-Check | Satellit spielt Fehlerton; Text-Kanäle bleiben verfügbar |
