"""Welche Werkzeuge bekommt das Sprachmodell in dieser Runde?

Kleine lokale Modelle rufen bei 40 angebotenen Werkzeugen gern ein beliebiges auf („Mir geht es nicht gut“ -> Tagesplan,
„Ja, bitte“ -> Systemmonitor) und erzählen dann, sie hätten etwas getan. Darum bekommt das Modell nur die Werkzeuge der
Bereiche, die der Satz überhaupt anspricht. Ohne Treffer gar keine – dann kann es nur antworten, nicht handeln.
Das spart nebenbei viel Text im Prompt und macht die Antworten schneller.
"""

from __future__ import annotations

import re

# Bereich (Werkzeug-Domäne) -> Wortanfänge, die ihn ansprechen (Kleinschreibung, Teilwort genügt)
KEYWORDS: dict[str, str] = {
    "home": r"licht|lampe|leucht|heiz|thermostat|rollo|rolläden|rollläden|jalousie|steckdose|szene|schloss|abschließ|tür\b|"
            r"haustür|garage|hell|dunkel|dimm|wohnzimmer|küche|schlafzimmer|bad\b|flur|kinderzimmer|zimmer|radio|lautsprecher|"
            r"sonos|fernseher|grad\b|wärmer|kälter|haus\b|alle lichter",
    "info": r"prozent|rechne|rechnen|umrechn|wie viel (?:sind|ist)|dollar|euro\b|meilen|kilometer|wurzel|wetter|regen|schirm|temperatur|draußen|nachricht|news|schlagzeile|wikipedia|aktie|börse|kurs\b|dax|bitcoin|"
            r"wer (?:ist|war)|was (?:ist|bedeutet|sind)|wie (?:alt|groß|hoch|weit|viel)|wann (?:ist|war|wurde)|wo (?:ist|liegt)|"
            r"hauptstadt|einwohner|erklär",
    "web": r"such|google|recherch|internet|im netz|nachschlag|online|link|webseite|website",
    "calendar": r"termin|kalender|verabred|besprechung|meeting|geburtstag|was steht|plan\b|tagesplan|heute an|morgen an",
    "assistant": r"tagesplan|briefing|plan für|was steht|status|wie läuft",
    "timer": r"timer|wecker|stoppuhr|minuten?\b|sekunden?\b|stunden?\b|countdown",
    "reminder": r"erinner|vergiss nicht|nicht vergessen",
    "mail": r"mail|e-mail|posteingang|postfach|schreib (?:an|eine)|antworte",
    "memory": r"merk|merke|vergiss|gemerkt|weißt du (?:noch|über)|was weißt du|erinnerst du",
    "pc": r"öffne|öffnen|starte|schließ|beende|datei|ordner|programm|app\b|fenster|bildschirm|tippe|schreib|klick|taste|"
          r"drück|browser|explorer|chrome|edge|spotify|youtube|steam|word|excel|lauter|leiser|stumm|musik|pause|nächster titel|"
          r"download|dokument|foto|bild\b|sperr|zeig",
    "system": r"modell|prüf dich|selbsttest|sicherung|backup|daten sichern|akku|prozessor|cpu|arbeitsspeicher|ram\b|festplatte|laufwerk|grafikkarte|gpu|auslastung|systemmonitor|"
              r"neustart|herunterfahren|ausschalten|temp-|papierkorb|aufräumen|updates?",
    "automation": r"beobacht|bescheid|protokoll|routine|automati|wenn .* dann",
    "message": r"durchsage|sag (?:in|im|allen)|richte aus",
    "vision": r"kamera|siehst|sehen|schau mal|was ist das|auf dem bild|halte ich",
    "stocks": r"aktie|börse|kurs\b",
    "lists": r"einkaufs|liste\b|todo|to-do|aufgabe|kaufen|streich",
    "media": r"radio|sender\b|musik|hör",
    "knowledge": r"notiz|dokument|unterlagen|vertrag|meine pdf|mein wissen|rechnung|kaution|miete",
}
_COMPILED = {domain: re.compile(pattern) for domain, pattern in KEYWORDS.items()}
_QUESTION = re.compile(r"\?|^(?:wie|was|wer|wann|wo|warum|wieso|weshalb|welche[rsmn]?|gibt es|kennst du|weißt du)\b")


def wanted_domains(text: str, known: frozenset[str], previous: frozenset[str] = frozenset()) -> frozenset[str]:
    """Bereiche, deren Werkzeuge das Modell für diesen Satz bekommt. ``previous``: die der letzten Runde – gilt nur für
    kurze Anschlusssätze („Und morgen?“, „Ja, bitte“, „Mach das“)."""
    lower = text.lower()
    found = {domain for domain, pattern in _COMPILED.items() if pattern.search(lower)}
    if "info" in found or (_QUESTION.search(lower) and not found):
        found |= {"info", "web"}  # Wissensfragen: Wikipedia und Websuche
    if not found and len(lower.split()) <= 5:
        found |= set(previous)
    return frozenset(found) & known
