# 8. Erweiterungen & Zukunfts-Features

[← Jarvis-Persona](07-jarvis-persona.md) · [Übersicht](../README.md)

Alle Erweiterungen nutzen die bestehenden Verträge (Events, Capabilities, Policy, Plugins) – der Kern muss dafür
nicht umgebaut werden.

## 8.1 Ausbaustufen

| Feature | Beschreibung | Voraussetzungen | Andockpunkt |
|---------|--------------|-----------------|-------------|
| **Multi-Agenten-Spezialisten** | Orchestrator delegiert an spezialisierte Sub-Agenten (Recherche, Haus, Technik, Kommunikation) mit eigenem Tool-Set und Budget; Ergebnisse als strukturierte Berichte | Task-Manager-Jobs, Tool-Filter je Domäne | `task.plan`, Intent-Domänen |
| **Vision (lokal)** | Kamera-Ereignisse verstehen („Paket liegt vor der Tür“, „Herd ist an und niemand in der Küche“), Dokumente fotografieren und ablegen | lokales Vision-Modell, Frigate/NVR, Sensitivität `sensitive` | Capability `vision.describe_local`, Events |
| **Präsenz-Fusion** | mmWave-Radar, BLE-Beacons, WLAN, Türsensoren → wer ist in welchem Raum | Sensorik, Wahrscheinlichkeitsmodell | `jarvis.presence.changed`, Kontext |
| **Energie-Optimierung** | PV-Prognose, dynamische Stromtarife, Batteriespeicher, Wallbox, Wärmepumpe → Lastverschiebung mit Komfortgrenzen | Tarif-/PV-Plugins, Zeitreihen (TimescaleDB) | `energy.schedule_load` (R2), Automationen |
| **Digitaler Zwilling** | Grundriss + Geräte + Zustände als Graph; „Welche Fenster sind im Obergeschoss offen?“, Simulation von Automationen | Raum-/Gerätemodell, 3D-/2D-Plan | Kontext, Dry-Run |
| **Selbstlernende Skills** | Wiederholte Tool-Ketten werden als prozedurale Erinnerung vorgeschlagen („Filmabend“ = 4 Schritte) und nach Freigabe zur Szene/Automation | Suggestion-Miner, Freigabe-Workflow | `memory` (procedural), Automationen |
| **MCP-Ökosystem** | beliebige MCP-Server als Plugins, mit Manifest-Rechten und Risikoklassen | Plugin-Host | Laufzeit `mcp` |
| **Anrufe (real)** | SIP-Anbindung (Asterisk/FreeSWITCH): Anrufe annehmen, filtern, Termine vereinbaren – mit Ansage, dass eine KI spricht | SIP-Trunk, Anruf-Modus | `call.*` (R2/R3) |
| **Gesundheit & Wohlbefinden** | Schlaf, Luftqualität, Bewegungserinnerungen, Medikamentenerinnerungen – streng lokal | Wearables/Sensoren, Einwilligung | Sensitivität `sensitive`, lokales LLM |
| **Fahrzeug & Mobilität** | Ladezustand, Vorklimatisierung, Abfahrtsempfehlungen | Fahrzeug-API-Plugin | `vehicle.*` (R2) |
| **AR/HUD & Wearables** | Hinweise auf Smartwatch/Brille, Sprachsteuerung unterwegs | App-Erweiterung | Notification-Router |
| **Multi-Home** | Ferienwohnung, Büro, Eltern: mehrere Häuser mit getrennten Policies | Mandantenfähigkeit | `site_id` in Events/Entitäten |
| **Roboter & Aktoren** | Saugroboter, Rasenmäher, Rollos als koordinierte Abläufe („Wohnzimmer vor Besuch vorbereiten“) | Geräte-Plugins | Plan-Ausführung |
| **Föderiertes Lernen** | Verbesserung von Wake-Word/Intent-Modellen aus lokalen Korrekturen ohne Datenabfluss | Trainingspipeline | Eval-Set |

## 8.2 Technische Weiterentwicklung

| Thema | Richtung |
|-------|----------|
| Latenz | spekulatives Ausführen des Fast-Path parallel zum STT-Endpunkt; Streaming-STT mit Teilergebnissen; TTS mit Satz-Vorabberechnung |
| Lokale Modelle | kleinere, spezialisierte Modelle für Routing/Extraktion; Distillation aus Cloud-Antworten (nur nicht-sensible Daten, mit Einwilligung) |
| Gedächtnis | Wissensgraph über Tripeln (Personen, Orte, Geräte, Beziehungen); zeitliche Abfragen („Wann wurde zuletzt …?“) |
| Planung | explizite Pläne mit Vor-/Nachbedingungen, Wiederaufnahme nach Neustart, Kosten-/Risikoabschätzung vor Ausführung |
| Sicherheit | formale Prüfung der Policy (Property-Tests: „keine Automation erreicht R3“), signierte Plugins mit Reproducible Builds, Remote-Attestation der Satelliten |
| Beobachtbarkeit | Qualitäts-Dashboards (Tool-Genauigkeit, Ablehnungsquote, Nutzerkorrekturen) als Frühwarnsystem |

## 8.3 Risiken und Gegenmaßnahmen

| Risiko | Auswirkung | Gegenmaßnahme |
|--------|------------|---------------|
| Fehlhandlung durch Sprachmodell | falsches Gerät, unerwünschte Nachricht | Schema-Validierung, Risikoklassen, Bestätigungen, Verifikation, Undo |
| Prompt-Injection über Fremdinhalte | ungewollte Aktionen | Taint-Tracking, Intent-Bindung, Kanaltrennung für Bestätigungen, Injection-Tests |
| Überwachungsgefühl im Haushalt | Akzeptanzverlust | Hardware-Mute, sichtbare Aufnahmeanzeige, keine Audio-Speicherung per Default, Gedächtnis einsehbar/löschbar |
| Abhängigkeit von Cloud-Anbietern | Ausfall, Kosten, Datenschutz | Local-first, Router mit Fallback, austauschbare Adapter |
| Nervige Proaktivität | Abschalten der Funktion | Score mit Störkosten, Feedback-Lernen, Ruhezeiten, Budget pro Stunde |
| Komplexität des Systems | Wartungsaufwand | klare Verträge (Schemas), Tests, Referenz-Deployment, Observability |
| Rechtliche Anforderungen (DSGVO) | Bußgeld, Vertrauensverlust | Datenminimierung, Aufbewahrungsfristen, Auskunft/Löschung/Export, Einwilligung für Stimme und Gesundheit |

## 8.4 Leitlinien für neue Features

1. Jede neue Fähigkeit ist eine **Capability** mit Schema, Risikoklasse und Test – nie ein Sonderweg am Policy-Gate vorbei.
2. Neue Datenquellen erhalten eine **Trust-Stufe**; Fremdinhalte sind `untrusted`.
3. Sensible Daten bleiben **lokal**; Cloud-Nutzung wird im Router, nicht im Feature entschieden.
4. Proaktive Funktionen melden sich über die **Proaktiv-Engine** (Score, Budget, Feedback) – nicht direkt.
5. Die **Persona** bleibt Präsentation: Features liefern Inhalt + `kind`, die Form entsteht im Composer.
