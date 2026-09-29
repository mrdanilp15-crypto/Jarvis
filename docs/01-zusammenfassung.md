# 1. Kurze Zusammenfassung

[← Übersicht](../README.md) · [Weiter: Architektur →](02-architektur.md)

## 1.1 Was JARVIS ist

JARVIS ist ein **lokal zentriertes, ereignisgesteuertes KI-Assistenzsystem** für Haushalt, Arbeit und Technik.
Es kombiniert:

- einen **KI-Kern** aus Orchestrator (Agent-Loop), Modell-Router, Kontext-Manager und mehrschichtigem Gedächtnis,
- einen **Input-Layer**, der Sprache, Text, API-Events und Sensordaten in ein einheitliches Event-Format
  (CloudEvents 1.0) überführt und jede Quelle mit einer **Vertrauensstufe** versieht,
- einen **Output-Layer**, der Antworten als Sprache, Text, Push-Nachricht oder Dashboard-Update ausgibt und
  Aktionen an Home Assistant, MQTT, REST-Dienste und Systemagenten verteilt,
- eine **Automations-Engine** (Trigger → Bedingung → Aktion) plus Scheduler und Task-Manager,
- einen **Sicherheits-Layer**, der *jede* Aktion – ob vom LLM, einer Automation oder einem Plugin angestoßen – durch
  eine Policy-Engine mit **Risikoklassen R0–R4** leitet, Bestätigungen einholt und alles hash-verkettet protokolliert,
- ein **Plugin-System**, dessen Erweiterungen Fähigkeiten (Tools) mit deklarierten Berechtigungen bereitstellen
  (eigenes Protokoll oder MCP-Server),
- eine **optionale Jarvis-Persona**, die nur Ton und Stil verändert, nie Sicherheitsentscheidungen.

## 1.2 Designprinzipien

| Prinzip | Bedeutung für die Umsetzung |
|---------|-----------------------------|
| **Local-first** | Wake-Word, STT, TTS, Fast-Path-Befehle und ein lokales LLM laufen im Haus. Das System bleibt ohne Internet bedienbar (degradierter Modus). |
| **Privacy by Design** | Sensible Daten (Kamera, Gesundheit, Zugangsdaten) verlassen das Haus nie; der Router erzwingt lokale Verarbeitung. Datenminimierung vor jedem Cloud-Aufruf. |
| **Ereignisgesteuert** | Alles ist ein Event mit `correlationid`. Komponenten sind lose über einen Event-Bus gekoppelt und einzeln austauschbar. |
| **Capability-basierte Sicherheit** | Das LLM hat keine direkte Macht. Es *beantragt* Aktionen; die Policy-Engine entscheidet deterministisch. |
| **Human-in-the-Loop** | Irreversible oder sicherheitskritische Aktionen verlangen eine Bestätigung über einen Kanal, den das LLM nicht beeinflussen kann. |
| **Deterministisch, wo möglich** | Häufige Befehle („Licht aus“) laufen über einen regelbasierten Fast-Path ohne LLM: schneller, billiger, reproduzierbar. |
| **Beobachtbarkeit** | Strukturierte Logs, verteilte Traces (OpenTelemetry), Metriken und ein unveränderliches Audit-Log. |
| **Erweiterbarkeit** | Neue Fähigkeiten kommen als Plugin mit Manifest, JSON-Schema und Berechtigungen – ohne Änderungen am Kern. |
| **Persona als Schicht** | Die Jarvis-Persönlichkeit ist Konfiguration, kein Code-Pfad. Warnungen und Fehler werden immer eindeutig formuliert. |

## 1.3 Zielwerte (nicht-funktionale Anforderungen, Auszug)

| Kennzahl | Ziel | Messpunkt |
|----------|------|-----------|
| Fast-Path-Befehl (z. B. „Licht an“) | ≤ 500 ms nach Ende der Spracheingabe bis Aktion | Event `jarvis.input.utterance` → `jarvis.action.completed` |
| Erste gesprochene Silbe bei LLM-Antworten | ≤ 1,5 s (Streaming satzweise an TTS) | STT-Ende → erster TTS-Audio-Chunk |
| Wake-Word-Fehlauslösungen | < 1 pro 24 h | Satelliten-Telemetrie |
| Verfügbarkeit Kern (lokal) | ≥ 99,5 % / Monat | Health-Checks |
| Offline-Funktionsumfang | Smart Home, Timer, Erinnerungen, lokale Q&A | Chaos-Test „WAN aus“ |
| Audit-Abdeckung | 100 % aller Aktionen ≥ R1 | Audit-Log-Konsistenzprüfung |

## 1.4 Technologie-Stack (Referenz)

| Bereich | Wahl | Alternativen |
|---------|------|-------------|
| Kern-Services | Python 3.12, asyncio, FastAPI, Pydantic v2 | Go, Rust (für Edge-Komponenten) |
| Event-Bus | Redis Streams (intern) + MQTT/Mosquitto (Geräte) | NATS JetStream |
| Datenbank | PostgreSQL 16 + pgvector | + TimescaleDB für Zeitreihen |
| Cloud-LLM | Anthropic Claude (`claude-opus-5-5`, adaptives Denken, Streaming, Tool-Use) | beliebiger Provider über Adapter |
| Lokales LLM | Ollama (z. B. Llama/Qwen/Mistral-Familie, 7–32 B) | llama.cpp-Server, vLLM |
| Embeddings | lokal (z. B. `bge-m3` via Ollama/sentence-transformers) | – |
| Wake-Word | openWakeWord („hey_jarvis“) | Porcupine |
| STT | faster-whisper (large-v3 / turbo) über Wyoming | Vosk (sehr klein), Cloud-STT |
| TTS | Piper (lokal) über Wyoming | Coqui XTTS, Cloud-TTS |
| Smart Home | Home Assistant (WebSocket-API), MQTT, Zigbee2MQTT | openHAB, ioBroker |
| Web-Recherche | SearXNG (selbst gehostet) + Fetcher/Extractor | Such-APIs |
| Secrets | HashiCorp Vault oder SOPS/age | Docker Secrets |
| Identität | OIDC (Authelia/Keycloak) + Sprecher-Erkennung | – |
| Observability | OpenTelemetry → Loki/Tempo/Prometheus → Grafana | ELK |
| Clients | Flutter (Mobile), Tauri (Desktop), ESP32-S3/Raspberry Pi (Satelliten) | React Native, Electron |

## 1.5 Umfang in Ausbaustufen

| Stufe | Inhalt | Ergebnis |
|-------|--------|----------|
| **MVP** | Text + Sprache, Fast-Path, Home-Assistant-Steuerung, Timer/Erinnerungen, Policy R0–R2, Audit | Alltagstauglicher Sprachassistent fürs Haus |
| **v1** | Cloud-/Lokal-Routing, Memory, Kalender/Mail, Recherche, Automationsvorschläge, Mobile App | Persönlicher Assistent mit Gedächtnis |
| **v2** | Plugins/MCP, Systemdiagnosen, Code-Sandbox, proaktive Engine, Anruf-Modus, Desktop-Client | Vollständiges „JARVIS“ |
| **v3** | Multi-Agenten, Vision, Energie-Optimierung, digitaler Zwilling | Siehe [Abschnitt 8](08-erweiterungen.md) |
