# Änderungsprotokoll

## Jarvis-Modul 2.3.0 – 2026-09-28

Suchbefehle verstehen, mehr PC-Steuerung, Timer, Kalender, E-Mails und ein Aktivierungswort, das antwortet.

### Behoben
- **„Suche mir nach Arteriion auf Spotify“ öffnete eine Google-Suche nach „Arteriion auf Spotify“.**
  - Suchbefehle trennen jetzt den Dienst vom Suchbegriff („auf/bei/in X“, vor oder nach dem Begriff).
  - Bekannt sind 24 Dienste: Spotify, YouTube, Amazon, Netflix, Google Maps, eBay, Kleinanzeigen und weitere.
  - Spotify sucht in der installierten App, sonst im Web-Player.
  - Unbekannte Orte bleiben Teil der Suche („Urlaub auf Mallorca“).
- **Befehle wurden zu früh abgeschickt.**
  - Die Oberfläche schickte bei der ersten kurzen Sprechpause ab („Jarvis, such mir nach …“).
  - Jetzt wartet sie etwa 1,3 s Stille ab. Endet der Satz hörbar offen („… nach“, „… auf“), wartet sie etwa 2,8 s.
- **Unvollständige Befehle** („Such mal“, „Öffne“, „Erinnere mich an den Müll“, „Trag Zahnarzt ein“):
  - JARVIS fragt nach („Wonach soll ich suchen, Sir?“) und hört danach direkt zu.
  - Die Antwort ergänzt den Befehl.
- **Formatter:** „mit ./deploy/start.sh“ wurde zu „mit./deploy/start.sh“.

### Neu
- **Aktivierungswort:**
  - Auf „Jarvis“ folgt „Ja, Sir?“ bzw. „Sir?“ statt des Pieptons, mit der JARVIS-Stimme vorab geladen.
  - Die Erkennung ist unscharf: Lautschrift mit höchstens einer Abweichung, drei Erkennungsalternativen, auch
    getrennte Silben („Char wies“).
  - „Davis“, „Travis“ oder „Service“ lösen nicht aus.
- **PC-Steuerung:**
  - Programme sanft schließen (`pc.close_app`, wie das X; Explorer-Fenster ohne die Taskleiste; JARVIS bleibt offen).
  - Text ins aktive Fenster einfügen (`pc.type_text`, nie in Konsolen).
  - Tasten, Kürzel und Medientasten (`pc.press_key`: Enter, Tab, Kopieren, Rückgängig, Tabs, Zoom, Wiedergabe,
    nächster Titel, Lautstärke, stumm).
  - Klicken per Beschriftung (`pc.click`, UI Automation, auch auf Webseiten).
  - E-Mail-Entwürfe (`pc.compose_mail`):
    - im Mailprogramm oder in Gmail/Outlook im Web;
    - Empfänger als diktierte Adresse („max punkt mustermann at gmail punkt com“) oder als Kontakt;
    - JARVIS versendet nie selbst.
  - Anmelden:
    - „Melde mich bei Netflix an“ öffnet die Anmeldeseite.
    - Passwörter tippt JARVIS grundsätzlich nicht, auch nicht auf Nachfrage.
  - Windows-Hinweise für Meldungen.
- **Timer und Erinnerungen** (`timer.start`, `timer.list`, `timer.cancel`, `reminder.create`):
  - Deutsche Zeitangaben („in einer halben Stunde“, „morgen um halb acht“, „am Freitag um 3“, „heute Abend“).
  - Timer und Erinnerungen überstehen Neustarts. Was während einer Abschaltung fällig war, meldet JARVIS als verspätet.
  - Meldungen kommen in der Oberfläche (gesprochen) und als Windows-Hinweis. Ohne offenes Fenster bleiben sie bis zum
    nächsten Öffnen liegen.
- **Kalender** (`calendar.list`, `calendar.add`, `calendar.delete`):
  - Eigene Termine werden gespeichert; JARVIS erinnert 15 Minuten vorher.
  - Abonnierte Kalender per ICS (Google, Outlook, iCloud) werden nur gelesen, samt Wiederholungen, Ausnahmen und
    verschobenen Terminen.
  - Der Tagesplan („Plan für morgen?“) nennt jetzt die Termine.
- **E-Mails lesen** (`mail.list_unread`, `mail.read`):
  - Optional über IMAP mit App-Passwort.
  - Nachrichten bleiben ungelesen markiert.
  - Auswahl mit „die zweite“.
  - Der Mailtext wird wörtlich vorgelesen; der Jarvis-Formatter schreibt Fremdtext nicht um.
- Betrieb: Docker-Volume `jarvis-data`, neue Einstellungen in `deploy/.env.example`, Persona 2.3.0 mit Regeln für
  die neuen Werkzeuge.

### Tests
- 170 neue Tests: Zeitangaben, Timer-Planer und Speicher, ICS-Kalender, IMAP, Suchdienste, Rückfragen, Tasten und
  Kürzel, Klicken, Tippen, Mail-Entwürfe, Push-Meldungen über die API, Jarvis-Antworten.
- In Chromium geprüft:
  - Sprechpausen, unscharfes Aktivierungswort mit „Ja, Sir?“, Rückfrage mit automatischem Zuhören.
  - Timer-Meldung vom Server.
- PowerShell: alle Skripte ohne Parserfehler; Hilfsfunktionen des Agenten (Fenstertitel-Abgleich, Tasten-Escaping)
  in PowerShell 7 geprüft.

## Jarvis-Modul 2.2.0 – 2026-09-28

JARVIS sucht Links und Dateien selbst heraus und öffnet sie.

### Neu
- **Link heraussuchen und öffnen** (`pc.open_link`): „Such mir einen Link zu … und öffne ihn“, „Öffne die Webseite
  von …“, „Geh auf …“, „Öffne den ersten Treffer für …“, „Spiel … auf YouTube“ (erstes Video).
  - JARVIS sucht serverseitig über DuckDuckGo oder eine eigene SearXNG-Instanz (`JARVIS_SEARXNG_URL`) und öffnet den
    ersten Treffer. Die Antwort nennt Titel und Seite.
  - Ist die Suche nicht erreichbar, leitet DuckDuckGo den Browser direkt zum ersten Treffer weiter.
  - R2: Hat das Gespräch vorher Fremdinhalte gelesen, fragt JARVIS erst nach.
- **Websuche mit Auswahl** (`web.search`): „Such mir ein paar Links zu …“ nennt die besten Treffer. „Den zweiten“
  öffnet den gewählten Link.
- **Dateien finden und öffnen** (`pc.find_files`, `pc.open_file`):
  - „Öffne die Datei Bewerbung“, „Öffne die PDF Rechnung“, „Öffne Bewerbung.pdf“, „Öffne den Ordner Projekte“,
    „Wo ist meine Steuererklärung?“.
  - Der PC-Agent sucht im Benutzerordner über den Windows-Suchindex (Rückfall: Durchsuchen der üblichen Ordner) und
    findet dabei auch Umlaut-Schreibweisen („Steuererklaerung.pdf“).
  - Er öffnet den besten und neuesten Treffer mit dem Standardprogramm.
  - Programme, Skripte und Verknüpfungen startet er nie, sondern markiert sie im Explorer.
- **Auswahl im Gespräch**: Nach einer Trefferliste genügt „die zweite“, „Nummer 3“ oder „den letzten“; bei einem
  einzelnen Treffer „ja“. Die Auswahl gilt nur für den direkt folgenden Satz. „Ja“ zählt nur, wenn JARVIS selbst
  gefragt hat.
- **Unbekannte Namen**: „Öffne Chefkoch“ startet ein installiertes Programm dieses Namens, sonst öffnet JARVIS die
  passende Webseite. Mit Artikel („Öffne die Einkaufsliste“) übernimmt weiter das Sprachmodell.
- **Hörfehler**: Programmnamen werden unscharf abgeglichen („Discort“ → Discord, „Minecraf“ → Minecraft Launcher).
- Die Explorer-Suche („Such die Datei …“) nennt zusätzlich den neuesten Treffer.
- Persona 2.2.0: Das Sprachmodell soll zum Öffnen und Finden die PC-Tools nutzen, statt nur zu beschreiben.

### Tests
- 47 neue Tests:
  - Auslesen der DuckDuckGo-Ergebnisseite (ohne Anzeigen), SearXNG, Bot-Sperre und Netzfehler.
  - Link öffnen samt Rückfall, Datei-Capabilities, Formulierungen.
  - Auswahl aus Datei- und Linklisten, „ja“ nur nach eigener Rückfrage, Webseiten-Rückfall für unbekannte Namen,
    unscharfer Abgleich.
- Die Dateisuche des PowerShell-Agenten wurde mit einem nachgebildeten Benutzerordner geprüft: Suche, Öffnen per
  Nummer und Name, Umlaute, Ordner, markierte Programme.

## Jarvis-Modul 2.1.0 – 2026-09-27

PC-Befehle zuverlässig gemacht. Vorher klappten die meisten Befehle nicht, aus drei Gründen:
1. **Nur die Befehlsform wurde erkannt.** „Öffne den Explorer“ ging, „Kannst du den Explorer öffnen?“ oder „Explorer
   öffnen“ landeten beim Sprachmodell, das das Werkzeug oft nicht aufrief (15 von 22 geprüften Formulierungen).
2. **Nur 12 fest eingetragene Programme.** Steam, Discord, Spiele usw. lehnte der PC-Agent ab.
3. **Keine Datei- und Seitensuche.** „Such die Datei …“ und „… auf YouTube“ gab es nicht.

### Neu
- **Natürliche Formulierungen** für PC-Befehle: Fragen („Kannst du …?“, „Könnten Sie …?“), Wünsche („Ich möchte
  Minecraft spielen“), Verb am Ende („Steam starten“), Füllwörter („bitte“, „mal“). Suchbegriffe behalten ihre
  Schreibweise.
- **Jedes installierte Programm**: Der PC-Agent liest das Windows-Startmenü (`Get-StartApps`) und startet Programme
  und Spiele per Name (exakter Name vor Namensanfang vor ganzem Wort: „Minecraft“ → „Minecraft Launcher“). Er
  überspringt Deinstallations-, Setup- und Hilfe-Einträge. JARVIS kennt die Liste (`agent.hello`/`agent.apps`) und
  startet „Öffne Steam“ ohne Sprachmodell. Für unbekannte Namen („Öffne die Einkaufsliste“) übernimmt weiter das
  Sprachmodell, statt zu raten.
- **Suchen**: `pc.search_web` mit `site` (Google, YouTube, Amazon, Wikipedia, eBay); neue Capability
  `pc.search_files` öffnet die Explorer-Suche im Benutzerordner.
- Weitere feste Programme: Systemsteuerung, Kamera, Uhr, Microsoft Store, Ausschneidewerkzeug.
- `./deploy/start.sh autostart` ersetzt einen laufenden älteren PC-Agenten sofort. Mit einem veralteten Agenten
  sagt JARVIS, dass ein Update nötig ist, statt nur „unbekannte Aktion“ zu melden.
- Antworten nennen, was tatsächlich gestartet wurde („Sehr wohl. Steam ist geöffnet.“, „Die YouTube-Suche nach
  „Katzenvideos“ ist geöffnet.“).
- README: Übersicht „Was JARVIS kann – und was (noch) nicht“.

### Sicherheit
- Der Kern schickt dem Agenten nur Namen und Suchbegriffe. `pc.open_app` lehnt Pfade, Pipes und Steuerzeichen schon
  im Schema ab; was gestartet wird, entscheidet der Agent anhand seiner Liste und des Startmenüs.

### Tests
- 55 neue Tests: Formulierungen, Programmauflösung, Weiterleitung unbekannter Namen an das Sprachmodell, Such-URLs,
  Dateisuche, veralteter Agent, Weg „Kannst du Steam starten?“ über die API bis zum Agenten. Die Suchlogik des
  PowerShell-Agenten wurde mit einem nachgebildeten Startmenü geprüft.

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
