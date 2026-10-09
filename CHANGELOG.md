# Änderungsprotokoll

## Jarvis-Modul 2.9.1 – 2026-10-09

### Behoben
- **Dateisuche:** „Such im Explorer ein Bild namens Heizung“ sucht jetzt nach „Heizung“ statt nach „ein Bild namens
  Heizung“. Ebenso „ein Foto, das Urlaub heißt“ und „mein Foto mit dem Namen Urlaub“. Ein nachgeschobenes
  „suche Heizung“ sucht nach „Heizung“ statt nach „suche Heizung“.
- **Windows-Programm startete nicht** („No time zone found with key Europe/Berlin“): Python bringt unter Windows
  keine Zeitzonen-Datenbank mit. Das Paket `tzdata` wird jetzt mitinstalliert.
- **Geöffnete Fenster landen vorne** (PC-Agent 2.9.1):
  - Bisher öffneten sich Ordner, Programme, Dateien und Webseiten oft hinter anderen Fenstern oder nur in der
    Taskleiste. Besonders betroffen: ein zweiter Ordner bei schon offenem Explorer. Ursache war die Fokus-Sperre
    von Windows für Hintergrundprozesse.
  - Der Agent hebt die Sperre jetzt auf und holt das Fenster nach vorne; ein minimiertes Fenster wird
    wiederhergestellt.
  - Ist ein Ordner schon offen, holt er dieses Fenster nach vorne, statt ein weiteres zu öffnen.

## Jarvis-Modul 2.9.0 – 2026-10-09

### Neu
- **Durchsagen:** „Sag in der Küche, dass das Essen fertig ist“, „Durchsage an alle: Abfahrt in fünf Minuten“,
  „Sag allen Bescheid, dass …“.
  - Die Geräte des Raums spielen einen Hinweiston und sprechen die Durchsage.
  - JARVIS sagt, ob sie angekommen ist oder ob dort gerade kein Gerät verbunden ist.
  - Fähigkeit `message.announce` (R0); Kinderprofile dürfen sie auch.
- **Timer und Erinnerungen bleiben im Raum:**
  - Was auf dem Küchen-Tablet gestellt wurde, meldet sich in der Küche, nicht am PC (auch kein Windows-Hinweis).
  - „Wie lange läuft der Timer?“ und „Stopp den Timer“ gelten für alle Timer des Raums.
- **Nur das nächste Gerät antwortet:**
  - Hören mehrere Geräte „Jarvis“, meldet jedes, wie laut es ankam: Abstand der Stimme zum Grundrauschen in dB, aus
    der lokalen Spracherkennung.
  - JARVIS sammelt die Meldungen höchstens 0,6 s und lässt nur das lauteste antworten. Die anderen bleiben still.
  - Ist nur ein Gerät aktiv, antwortet es ohne Wartezeit.
  - Bei Browser-Spracherkennung fehlt die Lautstärke; dann gewinnt ein Gerät mit lokaler Erkennung.

## Jarvis-Modul 2.8.2 – 2026-10-09

### Neu
- **Raum-Satelliten: Tablet oder Handy als JARVIS im Raum** (Zahnrad → Geräte):
  - **Heimnetz:** Mit `JARVIS_LAN=on` ist JARVIS zusätzlich unter `https://<PC>:8443` erreichbar. Browser geben
    Mikrofon und Kamera nur über HTTPS frei. Das Zertifikat stellt JARVIS selbst aus (`data/tls`, 10 Jahre, alle
    Heimnetz-Adressen) und erneuert es, wenn sich die Adresse ändert. Das Windows-Programm schaltet das von selbst ein,
    der Installer gibt Port 8443 einmal für private Netze frei. Docker: `JARVIS_LAN=on` und
    `JARVIS_LAN_ADDRESS=<IP>`.
  - **Koppeln:** Raum wählen (aus Home Assistant oder frei) → „Koppeln“ → QR-Code mit dem Tablet scannen. Jedes Gerät
    hat einen eigenen Zugang (`data/devices.json`, Dateirechte 0600), der jederzeit widerrufbar ist. Ein Tablet kann
    selbst keine weiteren Geräte koppeln.
  - **Raum-Modus:** Das Tablet zeigt seinen Raum in der Kopfzeile, hört auf „Jarvis“ und hält den Bildschirm an.
    „Licht an“ meint dort diesen Raum.

## Jarvis-Modul 2.8.1 – 2026-10-09

### Neu
- **Anwesenheit über die Kamera** (Zahnrad → Allgemein, standardmäßig aus):
  - **Erkennung:** Die Oberfläche vergleicht einmal pro Sekunde ein 64×48-Graustufenbild. Die Schwelle folgt dem
    Bildrauschen; ein dunkles oder abgedecktes Bild zählt nicht.
  - **Begrüßung:** Nach 10, 20, 30 oder 60 Minuten ohne Bewegung gilt man als abwesend. Bei der nächsten Bewegung
    begrüßt JARVIS („Willkommen zurück, Sir.“), nach langer Abwesenheit mit Uhrzeit bzw. „Guten Morgen“, dazu der
    nächste Termin der kommenden drei Stunden.
  - **Datenschutz:** Bilder verlassen den Browser nie. Nur „angekommen/gegangen“ geht an `POST /v1/presence` und wird
    als Bus-Ereignis `jarvis.presence.changed` für Automationen veröffentlicht. Keine Gesichtserkennung.
  - Solange die Erkennung läuft, zeigt die Kopfzeile „Kamera“.
  - „Was siehst du?“ nutzt die laufende Kamera mit, statt sie ein zweites Mal zu öffnen.

## Jarvis-Modul 2.8.0 – 2026-10-09

Alle offenen Module der Roadmap – lokal zuerst: Gedächtnis, Spracherkennung, Stimme, Sehen, Systemmonitor, Börse,
Online-Kalender, Radio auf Lautsprechern und lernende Routinen.

### Neu
- **Gedächtnis dauerhaft** (`SqliteMemoryStore`, `data/memory.db`):
  - Übersteht Neustarts. Konflikte und Duplikate werden wie bisher aufgelöst.
  - Ohne Embedding-Modell oder nach einem Modellwechsel sucht JARVIS über Wörter.
  - Sofortbefehle: „Merk dir, dass …“ (der Nebensatz wird zum Hauptsatz), „Was weißt du über mich?“, „Vergiss, dass …“.
- **Systemmonitor** (`system.monitor`):
  - Prozessor, Arbeitsspeicher, Laufwerke, Netz, Temperaturen, Grafikkarte und Akku – vom PC-Agenten (WMI,
    nvidia-smi) oder per psutil.
  - Antwortet passend zur Frage und zeigt eine Karte mit Balken.
- **Sichere Systemaktionen** (`pc.system_action`, feste Freigabeliste statt freier Befehle):
  - sperren, Energiesparen, Papierkorb, temporäre Dateien, Updates, Task-Manager;
  - Neustart und Herunterfahren nur nach Bestätigung in der App, mit einer Minute Vorlauf zum Abbrechen.
- **Lokale Spracherkennung** (`voice/local.py`, `WS /v1/audio`):
  - Whisper (faster-whisper) mit Wörterbuch aus Name, Räumen und Geräten. „Jarvis“ wird unscharf erkannt,
    optional schon „Hey Jarvis“ per openWakeWord.
  - Das Mikrofon sendet nur, solange JARVIS zuhört oder auf „Jarvis“ wartet. Kein Audio verlässt den Rechner.
  - Standard, sobald installiert (Windows-Installer); umschaltbar unter Zahnrad → Stimme & Hören.
- **Lokale Piper-Stimme ohne Docker:** im JARVIS-Prozess, die Stimme lädt beim ersten Start.
- **Sehen** („Was siehst du?“, „Was halte ich in der Hand?“, „Lies mir das Etikett vor“):
  - ein Einzelbild der Kamera, danach geht sie sofort aus;
  - lokales Bildmodell (`qwen2.5vl:3b`, lädt sich selbst nach);
  - keine Gesichtserkennung, Bilder werden nicht gespeichert, Gäste ausgeschlossen.
- **Börse** (`info.stock`): Aktien, Indizes, Bitcoin, Gold und Devisen (Tagesschluss, ohne Schlüssel) – „Wie steht
  Apple?“.
- **Online-Kalender** über CalDAV (Nextcloud, iCloud, mailbox.org …):
  - eintragen, löschen und lesen; eigene Termine erscheinen nicht doppelt;
  - ohne Verbindung bleibt der Termin lokal, und JARVIS sagt es.
- **Radio und Medien auf Lautsprechern** über Home Assistant (Chromecast, Sonos, DLNA): `home.play_media`,
  `home.play_radio` (radio-browser.info) – „Spiel Radio Bob im Wohnzimmer“.
- **Lernende Routinen** (`patterns.py`):
  - JARVIS erkennt wiederkehrende Schaltungen (mindestens fünf Tage in drei Wochen, ±20 Minuten) und kündigt sie
    einmal an.
  - Erst nach Zustimmung im Reiter Smart Home werden sie zur Routine. Die Uhrzeit folgt danach gleitend den
    Gewohnheiten.
  - Ausgeführt wird über die Policy; ist das Gerät schon im gewünschten Zustand, passiert nichts.
- README: Übersicht der Module mit Hardware- und Software-Voraussetzungen.

## Jarvis-Modul 2.7.0 – 2026-10-09

Smart Home ohne Handarbeit und JARVIS als normales Windows-Programm – ohne Docker.

### Neu
- **Smart Home automatisch** (`smarthome.py`, Zahnrad → **Smart Home**):
  - **Finden:** Beim Start sucht JARVIS Home Assistant selbst – homeassistant.local, der eigene PC, der Docker-Host und
    das Heimnetz (/24, Port 8123; erkannt an `/auth/providers`). Gefunden → in der Kopfzeile „Haus verbinden“.
  - **Verbinden mit einem Klick:** „Bei Home Assistant anmelden“ öffnet die Anmeldung von Home Assistant (OAuth/
    IndieAuth). Danach legt JARVIS sich einen eigenen, dauerhaften Zugang an (`auth/long_lived_access_token`, in HA
    unter Profil → Sicherheit sichtbar). Alternativ ein Token einfügen; es wird vor dem Speichern geprüft.
    Gespeichert in `data/home.json` (Dateirechte 0600), nie an die Oberfläche zurückgegeben; nur Erwachsene.
  - **Räume und Geräte übernehmen:** aus der HA-Registry, inklusive Aliasse, alle fünf Minuten neu. Diagnose- und
    Konfigurations-Entitäten bleiben außen vor. Die Einstellungen zeigen die Räume mit ihren Geräten.
  - **Sofortbefehle ohne Sprachmodell** (`homeindex.py`): „Licht im Wohnzimmer aus“, „Alle Lichter aus“, „Dimm das
    Licht in der Küche auf dreißig Prozent“, „Mach die Stehlampe an“, „Kaffeemaschine an“, „Rollläden runter“, „Fahr
    die Rollos im Schlafzimmer hoch“, „Heizung im Bad auf 21 Grad“, „Mach die Heizung aus“, „Starte Filmabend“,
    „Schließ die Haustür ab“. Raum- und Gerätenamen werden unscharf verglichen („Kueche“ = „Küche“).
    Mehrdeutiges bleibt dem Sprachmodell.
  - **Sprachmodell kennt das Haus:** Es bekommt eine kurze Geräteliste mit entity_ids, Räumen und Zuständen (bisher
    leer – es hätte IDs raten müssen).
  - Ohne verbundenes Haus sagt JARVIS bei Hausbefehlen, wo man es einrichtet. Der Systemstatus meldet, wenn
    Home Assistant nicht erreichbar ist.
  - Neue Endpunkte `GET/DELETE /v1/settings/home`, `POST /v1/settings/home/discover`, `POST /v1/settings/home/oauth`,
    `PUT /v1/settings/home/token`; Health-Feld `smart_home`.
  - `deploy/.env` geht weiterhin: `JARVIS_HA_URL` plus `JARVIS_SECRET_KV_JARVIS_HOMEASSISTANT_TOKEN`.
- **JARVIS als Windows-Programm** – „JARVIS installieren.cmd“ doppelklicken (`deploy/windows/jarvis-install.ps1`):
  - Installiert fehlendes Python 3.12 und Ollama über winget.
  - Richtet JARVIS in `%LOCALAPPDATA%\JARVIS\venv` ein und wählt das Sprachmodell nach Grafik- bzw.
    Arbeitsspeicher.
  - Übernimmt die Einstellungen aus `deploy/.env` und beendet eine laufende Docker-Fassung.
  - Richtet Autostart, Desktop-Verknüpfung und PC-Steuerung ein und öffnet JARVIS.
  - Erneut ausführen aktualisiert; `-Stop` beendet, `-Uninstall` entfernt den Autostart.
  - Der Server läuft dann ohne Redis und Postgres. Dafür gibt es neue Umgebungsvariablen: `JARVIS_BUS=memory`,
    `JARVIS_OLLAMA_URL`, `JARVIS_PIPER=off`, `JARVIS_HOST`, `JARVIS_PORT`.

### Geändert
- Docker: `host.docker.internal` zeigt auch unter Linux auf den Rechner, damit Home Assistant auf demselben Gerät
  gefunden wird.
- Der Autostart startet die PC-Steuerung nicht doppelt.

## Jarvis-Modul 2.6.0 – 2026-09-29

E-Mails schreiben, ohne dass das Sprachmodell Adressen verdreht – und ehrliche Antworten auf „Verstehst du mich?“.

### Neu
- **E-Mail-Assistent** (`maildialog.py`, ohne Sprachmodell): „Schreib eine E-Mail“, „Dir eine E-Mail“, „Neue E-Mail“,
  „Ich will eine Mail an Max schreiben“ oder „E-Mail an Mama“ starten ihn. JARVIS fragt nacheinander:
  1. **Empfänger:** Kontakt („Mama“) oder diktierte Adresse. JARVIS liest die erkannte Adresse vor
     („An hegemann.daniel@gmx.de, Sir. Wie lautet der Betreff?“). Die Stimme buchstabiert sie so, wie man sie
     diktiert („… Punkt daniel at gmx Punkt de“).
  2. **Betreff** – oder „ohne Betreff“.
  3. **Text.** Anrede („Hallo,“) und Gruß („Viele Grüße, Daniel“) ergänzt JARVIS, wenn sie fehlen.
  4. Danach öffnet sich der Entwurf im Mailprogramm. Abschicken tut der Nutzer selbst.
- **Korrekturen im Assistenten:**
  - „Nein“ direkt nach dem Vorlesen, „Die E-Mail ist falsch“ oder „Die Adresse stimmt nicht“ fragen die Adresse neu ab.
  - Die Adresse einfach noch einmal zu diktieren, ersetzt sie ebenfalls.
  - „Der Betreff ist falsch“ fragt den Betreff neu.
  - „fertig“ oder „den Rest schreib ich selbst“ öffnen den Entwurf sofort; „abbrechen“ verwirft ihn.
  - „weiter“ überspringt einen unbekannten Kontakt – die Adresse trägt der Nutzer dann im Entwurf ein.
  - Wer statt einer Adresse etwas anderes fragt („Wie spät ist es?“), bekommt die Antwort; der Entwurf wird verworfen.
    Nach drei Minuten Pause ist er vergessen.
- **Mail-Karte in der Oberfläche:**
  - Der Entwurf füllt sich live (An, Betreff, Text), mit Schrittanzeige und blinkender Schreibmarke im aktuellen Feld.
  - Eben Verstandenes leuchtet kurz auf; Hinweise nennen die möglichen Korrekturen.
  - Die Karte wird grün, sobald der Entwurf geöffnet ist.
  - Die Antwort trägt dafür das neue Feld `card` (OpenAPI `TurnResult`, ebenso das bisher undokumentierte
    `awaiting_reply`).
- **„Kannst du mich (jetzt) verstehen?“**, „Hörst du mich?“ und „Test“ beantwortet JARVIS sofort und ehrlich – mit
  dem, was bei ihm angekommen ist: „Laut und deutlich, Sir. Bei mir angekommen ist: „…““.

### Behoben
- Diktierte Adressen wurden vom lokalen Modell falsch zusammengesetzt („Hegemann@punkt.daniel.at.gmx.de“). Der
  Adress-Erkenner (`pc.spoken_email`) versteht jetzt:
  - Satzzeichen der Spracherkennung („GMX, Punkt. DE.“);
  - Füllwörter am Ende („… und“, „bitte“) und Einleitungen („die Adresse ist …“);
  - „ät“, „at-Zeichen“, „Klammeraffe“, „Minus“, „Bindestrich“, „Unterstrich“.
  Auch Tool-Aufrufe des Sprachmodells laufen über ihn (`pc.resolve_recipient`).
- „Ich überwache stets die Kommunikation“: JARVIS behauptet keine Dauerüberwachung mehr. Die Stil-Engine streicht
  solche Sätze, und die Persona nennt „Ich überwache die Situation.“ nicht mehr als Baustein. Im Statusbericht bleibt
  der Satz, dort stimmt er.

## Jarvis-Modul 2.5.0 – 2026-09-28

KI-Modell und Claude-Schlüssel direkt in der Oberfläche wählen – und eine Anzeige, die zeigt, was JARVIS gerade tut.

### Neu
- **KI-Modell in der Oberfläche** (Zahnrad → KI-Modell, `llm_settings.py`, Endpunkte `/v1/settings/llm`):
  - **Lokales Modell:**
    - Die Seite listet installierte und empfohlene Ollama-Modelle (qwen2.5 3b/7b/14b, llama3.1 8b) mit Größe und
      Einordnung.
    - **Herunterladen** zeigt einen Fortschrittsbalken (GB, Prozent) und schaltet danach um. Auch andere Modelle
      lassen sich per Name laden.
    - **Verwenden** wechselt ohne Neustart. Das alte Modell wird aus dem Speicher genommen, das neue vorgewärmt.
  - **Claude-Schlüssel:**
    - Eintragen, kostenlos über die Models-API prüfen und nur bei Erfolg speichern. Verständliche Meldungen bei
      falschem Schlüssel, fehlender Berechtigung oder fehlendem Netz.
    - Gespeichert wird serverseitig in `data/llm.json` (Dateirechte 0600). Die Oberfläche sieht nur „…abcd“.
    - Der Schlüssel hat Vorrang vor `ANTHROPIC_API_KEY`; nach dem Entfernen gilt wieder `deploy/.env`.
  - **Claude-Modell:** Opus 5.5 (empfohlen), Sonnet 5.5 (günstiger), Fable 5.1 (am stärksten), jeweils mit Preis je
    Million Tokens. Beim Wechsel wird geprüft, ob der Schlüssel das Modell nutzen darf.
  - **Wer antwortet:** automatisch (schwierige Fragen an Claude), immer Claude oder nur lokal. Sensibles bleibt immer
    lokal.
  - Nur Erwachsene des Haushalts dürfen das ändern (Gäste erhalten 403).
  - Die Auswahl übersteht Neustarts. Die Kopfzeile zeigt das aktive Modell (`/v1/system/health` → `llm`).
- **Anzeige, die mitdenkt:**
  - **Farbsprache mit weichen Übergängen** (registrierte CSS-Farbvariablen): Cyan bereit, Weiß hört, Gold denkt,
    Violett mit Radar-Schwenk schlägt nach, Blau führt aus. Grün leuchtet kurz bei „gefunden/erledigt“, Gold bei
    „nichts gefunden“, Orange pulsiert bei offener Bestätigung, Rot mit Rütteln bei Fehlern. Hintergrund,
    Statuszeile und Uhr färben sich mit.
  - **Zwischenstände vom Server:** Die neue WebSocket-Nachricht `status` meldet `research`, `tool` bzw. `action`.
    Die Statuszeile sagt, was passiert („Ich schlage nach: „Albert Einstein“ …“, „Ich rufe die Wetterdaten ab …“).
  - **Wetter-Karte:**
    - animiertes Symbol (Sonne mit drehenden Strahlen, ziehende Wolken, Regen, Schnee, Blitz, Nebel, Mond bei
      Nacht), Temperatur, gefühlt, Wind, Luftfeuchte, Regenwahrscheinlichkeit;
    - 7-Tage-Vorschau mit Symbolen und Temperaturspanne;
    - bei Regen, Schnee oder Gewitter fallen Tropfen bzw. Flocken hinter dem Kreis, bei Gewitter wetterleuchtet es;
    - das Wetter liefert dafür jetzt den WMO-Code und Tag/Nacht.
  - **Wissens-Karte:** Wikipedia-Artikel mit Vorschaubild (nur von upload.wikimedia.org, per Content-Security-Policy
    erzwungen), Beschreibung und Link. Web-Quellen stehen als Links unter der Antwort, auch wenn das Modell aus
    ihnen antwortet.
  - **Timer-Karte** mit Countdown-Ring; beim Ablauf pulsiert sie und zeigt „Abgelaufen“.
  - **Kopfzeile:** Uhr und Datum, das letzte Wetter (3 Stunden gültig, Klick zeigt die Karte) und das aktive Modell
    (Klick öffnet die Modellwahl).
  - Bei offenen Karten wird der Kreis kleiner, Karten schließen sich nach zwei Minuten.
  - **Einstellungen mit Reitern** (Allgemein · Stimme & Hören · KI-Modell, per Pfeiltasten bedienbar).
  - **Visuelle Effekte** „voll“, „dezent“ (ohne Partikel) oder „aus“. „Bewegung reduzieren“ des Systems wird
    beachtet.
  - **Handy:** kompakte Kopfzeile, die Seite scrollt, die Wochenvorschau scrollt in ihrer Karte. Getestet bei
    390 px Breite ohne seitliches Scrollen.

### Behoben
- **Claude mit neuen API-Konten:**
  - Konten ab dem 31.08.2026 lehnen Denk-Blöcke ab, deren Verlauf sich geändert hat. Bei JARVIS ändert sich der
    Situationsteil (Uhrzeit) jede Runde, und alte Runden werden gekürzt. Ein neuer Schlüssel wäre deshalb ab der
    zweiten Frage mit Fehler 400 gescheitert.
  - Denk-Blöcke gehen jetzt nur innerhalb der laufenden Runde zurück. Zusätzlich verwirft die API unpassende Blöcke,
    statt abzulehnen (`prefix_mismatch_behavior: drop_block`, Beta `thinking-binding-controls-2026-08-01`).
  - Assistenten-Nachrichten, die nur aus Denken bestanden, entfallen, statt leer gesendet zu werden.
- Standardmodell für Claude ist jetzt `claude-opus-5-5`.

### Tests
- 14 neue Tests, insgesamt 585:
  - Einstellungs-API: nur Erwachsene, Übersicht, falscher Schlüssel wird weder gespeichert noch zurückgegeben,
    Dateirechte 0600, Neustart übernimmt die Auswahl, Rückfall auf `deploy/.env`, Download mit Fortschritt und
    Fehlern, nicht installiertes Modell, Modus steuert den Router, Health nennt die Modelle;
  - Claude-Adapter: Denk-Blöcke nur aus der laufenden Runde, Schlüsselprüfung über die Models-API;
  - Statusmeldungen und Wetter-Codes.
- In Chromium geprüft (Desktop und 390 px): alle Farbzustände mit gemessenen Farbwerten, Wetter-Karte mit
  Regen- und Schneepartikeln, Radar beim Nachschlagen, Wissens-Karte, Quellen-Links, Timer bis „Abgelaufen“,
  Fehler-Blitz, Modell-Download mit Fortschritt, falscher und richtiger Schlüssel, Moduswechsel und
  Modellanzeige.

## Jarvis-Modul 2.4.0 – 2026-09-28

Nachschlagen statt raten, beim Thema bleiben, und ein Aktivierungswort, das sich die eigene Stimme merkt.

### Behoben
- **„ARTERIION ist ein deutsches Unternehmen, das Medikamente gegen Herz-Kreislauf-Erkrankungen herstellt …“ war
  erfunden.**
  - Ursache: Die Wikipedia-Suche lieferte den ähnlich geschriebenen Artikel „Arterie“, und das kleine Modell machte
    daraus eine Firma.
  - `info.wikipedia` nimmt jetzt nur Artikel, deren Titel zum Namen passt, und meldet sonst `found: false`.
    Begriffsklärungsseiten gelten als mehrdeutig.
- **„Nein, das ist ein Künstler“** führte zu einer Rückfrage des Modells statt zu einer neuen Suche. Jetzt schlägt
  JARVIS mit der Art erneut nach („ARTERIION Künstler“) und weist das Modell darauf hin, dass die vorige Antwort falsch
  war.
- **„Such nach mehr Infos“** suchte wörtlich nach „mehr Infos“.
  - Suchen ohne eigenen Begriff („mehr Infos“, „das“, „darüber“, „diesen Künstler“) meinen jetzt das aktuelle Thema.
  - Ohne Thema fragt JARVIS nach („Wonach soll ich suchen, Sir?“).
- **Das Sprachmodell wusste nichts von Direktbefehlen.** Befehle, Gesprächs-Antworten und Nachgeschlagenes stehen
  jetzt im Verlauf. Folgefragen wie „Wie alt ist er geworden?“ oder „Was hast du gerade gemacht?“ haben damit Kontext.

### Neu
- **Wissensfragen werden nachgeschlagen** (`knowledge.py`), z. B. „Wer ist …?“, „Was ist …?“, „Kennst du …?“,
  „Erzähl mir etwas über …“, „Was bedeutet …?“ und „Wer ist der Rapper …?“.
  - Wikipedia (mit Titelprüfung) und Websuche laufen parallel.
  - **Passender Wikipedia-Artikel:** Die ersten Sätze werden wörtlich vorgelesen („Laut Wikipedia: …“), ohne
    Sprachmodell und damit sofort. Klammern mit Lautschrift und Lebensdaten fallen weg.
  - **Nur Web-Treffer, die den Namen enthalten:** Das Sprachmodell bekommt genau diese Treffer als markierte
    Fremdinhalte vor die Frage, dazu die Regel, nur daraus zu antworten und die Quelle zu nennen. Das funktioniert
    lokal wie mit Claude (neues Feld `UserTurn.sources`).
  - **Nichts Passendes:** „Zu „…“ finde ich nichts Verlässliches, Sir – weder in der Wikipedia noch in der
    Websuche. Soll ich im Browser danach suchen?“ – „Ja“ öffnet die Suche. Ist die Websuche blockiert, sagt JARVIS
    genau das.
  - **Ohne Internet** antwortet das Modell mit dem Hinweis, nur Sicheres zu sagen.
  - „Wer ist der Bundeskanzler?“ fragt nach einer Person, nicht nach dem Amt. Hier fasst das Modell die Quellen
    zusammen, statt den Artikelanfang vorzulesen.
- **Gesprächsthema:**
  - „Erzähl mir mehr“ liest die nächsten Sätze. Danach bietet JARVIS an, den Wikipedia-Artikel zu öffnen.
  - „Wer ist das?“ nach einer Suche schlägt den gesuchten Namen nach.
  - „Nein, ich meinte …“ und ein einzelnes korrigiertes Wort schlagen erneut nach.
  - Ausrufe wie „Krass“ oder „Interessant“ gelten nicht als neuer Begriff.
- **Oberfläche:** Antworten tragen „nachgeschlagen“ bzw. „lokales Modell · nachgeschlagen“ in der Kopfzeile.
- **Aktivierungswort „Jarvis“:**
  - Unter dem Schalter steht kurz, was die Spracherkennung gehört hat (etwa „Gehört: „Service““). **Das war
    „Jarvis“** merkt sich diese Schreibweise.
  - **„Jarvis“ einlernen** im Zahnrad-Menü: Man sagt viermal „Jarvis“, und JARVIS merkt sich, was die Erkennung bei
    dieser Stimme daraus macht. Gelerntes lässt sich wieder löschen.
  - Gespeichert wird nur im Browser. Gewöhnliche Wörter („ja“, „das“) lassen sich nicht lernen.
  - Enthielt ein Zwischenergebnis „Jarvis“ und schreibt Chrome es in der Endfassung um („Davis, such …“), gilt das
    Zwischenergebnis. Kommt gar keine Endfassung, antwortet JARVIS nach 2 s trotzdem.
  - Fünf statt drei Erkennungsalternativen.
  - Wo Chrome es unterstützt, wird „Jarvis“ als erwartetes Wort vorgegeben (`SpeechRecognitionPhrase`). Lehnt Chrome
    das ab, geht es ohne weiter.

### Persona
- Stehen nachgeschlagene Quellen vor der Frage, antwortet JARVIS nur daraus und nennt sie.
- `found=false` von Wikipedia heißt „kein passender Artikel“: Ähnlich geschriebene Begriffe meinen etwas anderes.

### Tests
- 51 neue Tests (`tests/test_knowledge.py`), insgesamt 571:
  - Fragen, Folgesätze und vage Suchbegriffe erkennen;
  - Titelprüfung und Satzgrenzen („17. Juli“, „z. B.“, „Dr.“);
  - das Protokoll als Dialog über die echte Pipeline: unbekannter Künstler, Klarstellung, „Such nach mehr Infos“;
  - Wikipedia-Antwort mit „mehr“ und Artikel-Angebot, nichts gefunden, Websuche blockiert, offline;
  - Befehle im Verlauf.
- In Chromium geprüft: „Gehört: …“ und Lernen per Klick, Einlernen und Vergessen, Zwischenergebnis ohne Endfassung,
  umgeschriebene Endfassung, abgelehnte Wortvorgabe.

## Jarvis-Modul 2.3.1 – 2026-09-28

Korrekturen verstehen und keine Tool-Aufrufe mehr als Text – aus einem echten Gesprächsprotokoll.

### Behoben
- **Korrekturen nach einer Suche landeten beim Sprachmodell, das Bedeutungen erfand.**
  - Beispiel: „ARTERII.“ oder „ARTERIION“ direkt nach „Suche auf Spotify nach Atherion“.
  - Ein einzelner Begriff (1–3 Wörter) im direkt folgenden Satz wiederholt jetzt dieselbe Suche mit dem neuen
    Wort, beim selben Dienst.
  - Ebenso „Nein, ich meinte …“, „Es schreibt sich …“ und „Ich meinte … auf YouTube“ (anderer Dienst).
  - Ein falsch verstandener Programmname lässt sich mit „Nein, ich meinte Spotify“ korrigieren.
  - Füllwörter („Gab es.“, „Danke“) und Fragen bleiben normales Gespräch. Nach „Öffne den Explorer“ gilt ein
    einzelnes Wort nicht als Korrektur.
- **„Suche nach Arterien, so wie ich es dir gerade geschrieben habe, mit 2 i“ suchte den ganzen Satz bei Google.**
  - Rückbezüge und Buchstabierhinweise („mit 2 i“, „mit doppel i“) werden aus dem Suchbegriff entfernt.
  - Bei einem Rückbezug nimmt JARVIS das zuletzt getippte oder gesagte Wort und bleibt beim Dienst der letzten Suche.
- **Ordnerbefehle ohne Verb gingen ans Sprachmodell.**
  - Betroffen waren „Nach dem Ordner Minecraft (ähnlich)“, „Ordner mit dem Namen Minecraft“ und „Die Datei
    Bewerbung“.
  - Sie suchen jetzt gezielt nach Ordnern bzw. Dateien. Bekannte Ordner („Ordner Dokumente“) öffnen sich direkt.
- **„Suche im Detail Explorer nach Minecraft“** (verhörtes „Datei-Explorer“) sucht jetzt im Explorer.
- **Tool-Aufrufe als Text:** Kleine lokale Modelle schrieben Aufrufe manchmal als Text in die Antwort
  („Dorf. {"name": "pc.open_folder", …}“).
  - Der Ollama-Adapter erkennt solche Aufrufe (JSON, `<tool_call>`, ```` ```json ````) und führt bekannte
    Werkzeuge richtig aus.
  - Weder JSON noch das Beiwerk davor wird vorgelesen: die ersten 40 Zeichen werden kurz zurückgehalten, längere
    Antworten streamen danach wie bisher.
- **Initialen** („A.R.T.E.R.I.I.“) gelten nicht mehr als Satzende, weder im Formatter noch in der Sprachausgabe.

### Persona
- Unbekannte Namen oder Begriffe: sagen und eine Suche anbieten, nie Bedeutungen oder Abkürzungen erfinden.
- Tool-Aufrufe nur über die Tool-Schnittstelle, nie als JSON im Antworttext.
- „Ich überwache die Situation.“ nur, wenn JARVIS tatsächlich etwas überwacht.

### Tests
- 32 neue Tests aus dem Protokoll: Formulierungen, Rückbezüge, Korrekturbegriffe, der Dialogablauf mit mehreren
  Korrekturen, Text-Tool-Aufrufe im Stream, Initialen und „Ordner erstellen“ (keine Suche). Insgesamt 520 Tests.

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
