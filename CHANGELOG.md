# Änderungsprotokoll

## Jarvis-Modul 2.0.0 – 2026-09-27

Neukalibrierung des Antwortsystems auf den Jarvis-Ton (Details und Fehleranalyse:
[docs/07, Abschnitt 7.9](docs/07-jarvis-persona.md#79-stil-engine-20--neukalibrierung)).

### Neu
- **Stil-Engine** (`reference/python/src/jarvis/style.py`): Phrasenbank mit den Pflicht-Phrasen, Vorlagen je Aktion
  und Status, Gesprächs-Intents, satzweiser Freitext-Filter (Umgangssprache, Duzen → Siezen, Emojis,
  Ausrufezeichen, Floskeln, Anrede höchstens einmal), Streaming-Formatter.
- **Pipeline** im Orchestrator: Intent-Erkennung → Kontext-Interpretation → Ausführung/LLM → Formatter → Ausgabe;
  ein einziger Ausgang, keine Antwort bleibt ungefiltert (auch Fehler- und Zeitüberschreitungstexte).
- **Gesprächs-Intents** ohne Sprachmodell: Hilfe, Dank, Gruß, Befinden, Identität, Fähigkeiten, Name, Uhrzeit,
  Datum, Abschied.
- **Skills** `system.status` (echte Prüfung von Sprachmodell, PC-Steuerung, Stimme) und `assistant.day_plan`
  (Termine, sobald ein Kalender verbunden ist; Wetter für heute/morgen).
- **Persona 2.0.0**: Pflicht-Regeln, Pflicht-Phrasen und Beispiele im Systemprompt; Schema um `version`,
  `lexicon.mandatory_phrases` und `lexicon.replace` erweitert.
- Health-Check meldet die aktive Stil-Version (`"style": "jarvis 2.0.0"`).

### Behoben
- JARVIS nannte den Nutzer „Alex“: Die interne Actor-ID (`user:alex`) wurde als Name gelesen. Jetzt gilt
  `Principal.name` bzw. `JARVIS_USER_NAME` (fragt `start.sh`); ohne Namen spricht JARVIS nur mit „Sir“ an.
- „Mach das Licht an“ ohne Raumzuordnung führte zu einem Validierungsfehler; ohne verbundenes Haus antwortet JARVIS
  jetzt ehrlich statt zu raten.
- Technische Bestätigungsfragen („… {"entity_id": …} – ausführen?“) sind natürlichen Rückfragen gewichen.
- Die Oberfläche zeigt die formatierte Endfassung statt des Rohstreams.

### Tests
- `tests/test_style.py`: Test-Suite „Eingabe → erwartete Jarvis-Antwort“ über die echte Pipeline, Filterregeln,
  Idempotenz des Formatters, Bestätigungsdialog, Status bei Störungen, Namensbehandlung.
