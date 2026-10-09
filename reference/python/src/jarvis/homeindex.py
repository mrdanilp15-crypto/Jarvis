"""Räume und Geräte aus Home Assistant – und die Sofortbefehle dafür („Licht im Bad aus“, „Rollläden runter“).

Die Liste baut ``smarthome.py`` nach jeder Verbindung aus den Zuständen und der HA-Registry. Die Befehle hier
brauchen kein Sprachmodell: Räume und Gerätenamen werden unscharf verglichen („Kueche“ = „Küche“, „die Stehlampe“
= „Stehlampe Wohnzimmer“). Was nicht eindeutig ist, bleibt dem Sprachmodell – mit der Geräteliste im Kontext.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any


# Steuerbare Bereiche (Sofortbefehle) und solche, deren Zustand das Sprachmodell kennen sollte
CONTROLLABLE = ("light", "switch", "cover", "climate", "scene", "lock", "media_player")
READABLE = ("sensor", "binary_sensor")
_SENSOR_CLASSES = {"temperature", "humidity", "window", "door", "opening", "motion", "battery"}

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_ARTICLE = re.compile(r"^(?:der|die|das|den|dem|des|mein|meine|meinen|meinem|unser|unsere|unseren|alle|all)\s+")
_PREP = r"(?:in der|im|in dem|in|auf der|auf dem|am|beim|bei der)"


def norm(text: str) -> str:
    """Vergleichsform: klein, Umlaute ausgeschrieben, nur Buchstaben und Ziffern."""
    text = text.lower().translate(_UMLAUTS)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _words(text: str) -> set[str]:
    return set(norm(text).split())


@dataclass(frozen=True)
class Device:
    entity_id: str
    name: str
    area: str | None = None  # area_id
    aliases: tuple[str, ...] = ()
    device_class: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass
class HomeIndex:
    areas: dict[str, str] = field(default_factory=dict)  # area_id -> Anzeigename („Küche“)
    devices: list[Device] = field(default_factory=list)
    location_name: str | None = None

    # -- Aufbau ----------------------------------------------------------------------------------------------------
    @classmethod
    def build(cls, states: list[dict[str, Any]], registry: dict[str, list[dict[str, Any]]] | None = None,
              location_name: str | None = None) -> HomeIndex:
        registry = registry or {}
        areas = {a["area_id"]: a.get("name") or a["area_id"] for a in registry.get("areas", []) if a.get("area_id")}
        device_area = {d["id"]: d.get("area_id") for d in registry.get("devices", []) if d.get("id")}
        entries = {e["entity_id"]: e for e in registry.get("entities", []) if e.get("entity_id")}
        devices = []
        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".", 1)[0]
            attributes = state.get("attributes") or {}
            entry = entries.get(entity_id, {})
            if domain not in CONTROLLABLE + READABLE:
                continue
            if entry.get("disabled_by") or entry.get("hidden_by") or entry.get("entity_category"):
                continue  # Konfigurations- und Diagnose-Entitäten (Firmware, Signalstärke …) nicht anbieten
            device_class = attributes.get("device_class")
            if domain in READABLE and device_class not in _SENSOR_CLASSES:
                continue
            name = entry.get("name") or attributes.get("friendly_name") or entry.get("original_name") or entity_id
            area = entry.get("area_id") or device_area.get(entry.get("device_id") or "")
            aliases = tuple(a for a in entry.get("aliases") or [] if isinstance(a, str) and a.strip())
            devices.append(Device(entity_id, str(name), area if area in areas else None, aliases, device_class))
        devices.sort(key=lambda d: (CONTROLLABLE + READABLE).index(d.domain))
        return cls(areas, devices, location_name)

    # -- Abfragen --------------------------------------------------------------------------------------------------
    def of(self, *domains: str) -> list[Device]:
        return [d for d in self.devices if d.domain in domains]

    def in_area(self, area: str | None, *domains: str) -> list[Device]:
        return [d for d in self.of(*domains) if area is None or d.area == area]

    def area_of(self, spoken: str) -> str | None:
        """„Küche“, „kueche“, „der Küche“, „Wohnzimmers“ -> area_id."""
        wanted = norm(_ARTICLE.sub("", spoken.strip().lower()))
        if not wanted:
            return None
        names = {area_id: norm(name) for area_id, name in self.areas.items()}
        for area_id, name in names.items():
            if wanted in (name, norm(area_id)) or wanted.rstrip("s") == name:
                return area_id
        close = difflib.get_close_matches(wanted, list(names.values()), n=1, cutoff=0.84)
        if close:
            return next(a for a, n in names.items() if n == close[0])
        return None

    def named(self, spoken: str, *domains: str, area: str | None = None) -> list[Device]:
        """Geräte, deren Name (oder Alias) zum Gesagten passt – genau, als Wortteilmenge oder unscharf."""
        wanted = norm(_ARTICLE.sub("", spoken.strip().lower()))
        if not wanted:
            return []
        pool = [d for d in self.of(*domains) if area is None or d.area == area]
        exact, partial, fuzzy = [], [], []
        for device in pool:
            names = [norm(n) for n in (device.name, *device.aliases)]
            if wanted in names:
                exact.append(device)
            elif any(set(wanted.split()) <= set(n.split()) for n in names):
                partial.append(device)
            elif difflib.get_close_matches(wanted, names, n=1, cutoff=0.84):
                fuzzy.append(device)
        return exact or partial or fuzzy

    def area_lights(self) -> dict[str, list[str]]:
        lights: dict[str, list[str]] = {}
        for device in self.of("light"):
            if device.area:
                lights.setdefault(device.area, []).append(device.entity_id)
        return lights

    def area_aliases(self) -> dict[str, str]:
        aliases = {}
        for area_id, name in self.areas.items():
            for variant in (name.lower(), norm(name), area_id):
                aliases[variant] = area_id
        return aliases

    def situation_lines(self, state_of: Any = None, limit: int = 40) -> list[str]:
        """Für das Sprachmodell: Gerät, entity_id, Raum, Zustand – knapp, damit es keine IDs erfinden muss."""
        lines = []
        for device in self.devices[:limit]:
            where = f", {self.areas[device.area]}" if device.area else ""
            current = ""
            if state_of is not None and (state := state_of(device.entity_id)):
                unit = (state.get("attributes") or {}).get("unit_of_measurement") or ""
                current = f": {state.get('state')}{(' ' + unit) if unit else ''}"
            lines.append(f"{device.name} ({device.entity_id}{where}){current}")
        if len(self.devices) > limit:
            lines.append(f"… und {len(self.devices) - limit} weitere Geräte")
        return lines

    def summary(self) -> list[dict[str, Any]]:
        """Für die Oberfläche: Räume mit ihren Geräten (Name und Art)."""
        rooms: dict[str | None, list[dict[str, str]]] = {}
        for device in self.devices:
            rooms.setdefault(device.area, []).append({"name": device.name, "domain": device.domain})
        out = [{"name": self.areas[a], "devices": rooms[a]} for a in self.areas if a in rooms]
        if None in rooms:
            out.append({"name": "Ohne Raum", "devices": rooms[None]})
        return out


# ---------------------------------------------------------------------------------------------------------------
# Sofortbefehle
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class HomeMatch:
    capability: str
    arguments: dict[str, Any]
    grammar: str
    slots: dict[str, Any] = field(default_factory=dict)


_ON = {"an": True, "ein": True, "einschalten": True, "anschalten": True, "anmachen": True,
       "aus": False, "ausschalten": False, "ausmachen": False}
_STATE = r"(?P<state>an|aus|ein|einschalten|ausschalten|anschalten|anmachen|ausmachen)"
_SWITCH_VERB = r"(?:mach|mache|schalt|schalte|dreh|drehe|knips|knipse)"
_LIGHT_WORD = r"(?:licht|lichter|lampe|lampen|beleuchtung)"
_EVERYWHERE = r"(?:überall|im ganzen haus|in der ganzen wohnung|in allen räumen|alle|sämtliche)"

LIGHTS_ALL = [
    re.compile(rf"^(?:{_SWITCH_VERB}\s+)?(?:alle|sämtliche)\s+(?:lichter|lampen)\s+{_STATE}$"),
    re.compile(rf"^{_SWITCH_VERB}\s+{_EVERYWHERE}\s+(?:das\s+)?{_LIGHT_WORD}\s+{_STATE}$"),
    re.compile(rf"^{_SWITCH_VERB}\s+(?:das\s+)?{_LIGHT_WORD}\s+{_EVERYWHERE}\s+{_STATE}$"),
]
LIGHTS_AREA = [
    re.compile(rf"^(?:{_SWITCH_VERB}\s+)?(?:das\s+|die\s+)?{_LIGHT_WORD}\s+(?:(?P<prep>{_PREP})\s+(?P<area>.+?)\s+)?"
               rf"{_STATE}$"),
    re.compile(rf"^{_SWITCH_VERB}\s+{_STATE}\s+(?:das\s+|die\s+)?{_LIGHT_WORD}(?:\s+(?P<prep>{_PREP})\s+(?P<area>.+))?$"),
]
LIGHTS_DIM = re.compile(rf"^(?:mach|mache|stell|stelle|dimm|dimme|setz|setze|dreh|drehe)\s+(?:das\s+|die\s+)?{_LIGHT_WORD}"
                        rf"(?:\s+(?P<prep>{_PREP})\s+(?P<area>.+?))?\s+auf\s+(?P<num>\S+)\s*(?:prozent|%)$")
DEVICE_ON_OFF = [
    re.compile(rf"^{_SWITCH_VERB}\s+(?P<name>.+?)\s+{_STATE}$"),
    re.compile(r"^(?P<name>.+?)\s+(?P<state>einschalten|ausschalten|anschalten|anmachen|ausmachen|an|aus)$"),
]
_COVER_WORD = r"(?P<what>rollläden|rolläden|rollladen|rolladen|rollos|rollo|jalousien|jalousie|markisen|markise|" \
              r"vorhänge|vorhang|rollos)"
COVERS = [
    re.compile(rf"^(?:(?:fahr|fahre|mach|mache|zieh|ziehe|lass|lasse)\s+)?(?:die\s+|den\s+|alle\s+)?{_COVER_WORD}"
               rf"(?:\s+(?P<prep>{_PREP})\s+(?P<area>.+?))?\s+(?P<dir>hoch|rauf|runter|herunter|nach oben|nach unten|"
               rf"auf|zu|halb|auf (?P<num>\S+)\s*(?:prozent|%))$"),
    re.compile(rf"^(?P<verb>öffne|öffnen|schließ|schließe|schliess|schliesse|schließen)\s+(?:die\s+|den\s+|alle\s+)?"
               rf"{_COVER_WORD}(?:\s+(?P<prep>{_PREP})\s+(?P<area>.+))?$"),
]
CLIMATE_SET = re.compile(rf"^(?:(?:stell|stelle|mach|mache|dreh|drehe|setz|setze)\s+)?(?:die\s+|das\s+)?(?:heizung|temperatur|"
                         rf"thermostat|klimaanlage)(?:\s+(?P<prep>{_PREP})\s+(?P<area>.+?))?\s+auf\s+(?P<num>[\d,.]+|\S+)"
                         rf"\s*(?:grad|°c?)?$")
CLIMATE_ON_OFF = re.compile(rf"^(?:{_SWITCH_VERB}\s+)?(?:die\s+)?(?:heizung|klimaanlage)(?:\s+(?P<prep>{_PREP})\s+"
                            rf"(?P<area>.+?))?\s+(?P<state>an|aus|ein|ab)$")
_TARGET = rf"(?:\s+(?:(?P<prep>{_PREP})|auf (?:dem|der|den))\s+(?P<where>.+?))?"
RADIO = [
    re.compile(rf"^(?:spiel|spiele|mach|mache|starte|start)\s+(?:das\s+)?radio\s+(?P<station>.+?){_TARGET}"
               rf"(?:\s+(?:an|ab))?$"),
    re.compile(rf"^(?:spiel|spiele)\s+(?:den\s+)?sender\s+(?P<station>.+?){_TARGET}$"),
    re.compile(rf"^radio\s+(?P<station>.+?)\s+(?P<prep>{_PREP})\s+(?P<where>.+)$"),
]
SCENE = [
    re.compile(r"^(?:aktiviere|aktivier|starte|start|spiel|spiele|mach|mache)\s+(?:die\s+)?szene\s+(?P<name>.+)$"),
    re.compile(r"^szene\s+(?P<name>.+?)(?:\s+(?:an|starten|aktivieren))?$"),
]
SCENE_BY_NAME = re.compile(r"^(?:aktiviere|aktivier|starte|start)\s+(?:den\s+|die\s+|das\s+)?(?P<name>.+)$")
LOCK = [
    re.compile(r"^(?:schließ|schließe|schliess|schliesse|sperr|sperre|mach|mache)\s+(?:die\s+|das\s+|den\s+)?"
               r"(?P<name>.+?)\s+(?P<dir>ab|auf|zu)$"),
    re.compile(r"^(?P<dir>verriegel|verriegle|verriegele|entriegel|entriegle|entriegele)\s+(?:die\s+|das\s+|den\s+)?"
               r"(?P<name>.+)$"),
]
_LOCK_WORDS = re.compile(r"\b(?:tür|haustür|wohnungstür|eingangstür|schloss|garagentor|tor|hintertür)\b")


def _location(prep: str | None, area_id: str, index: HomeIndex) -> str:
    return f"{prep} {index.areas.get(area_id, area_id)}" if prep else ""


def _area(m: re.Match[str], index: HomeIndex) -> tuple[str | None, bool]:
    """(area_id, genannt?) – (None, True) heißt: Raum genannt, aber unbekannt."""
    spoken = (m.groupdict().get("area") or "").strip()
    if not spoken:
        return None, False
    return index.area_of(spoken), True


_ONES = {"null": 0, "ein": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7,
         "acht": 8, "neun": 9}
_TEENS = {"zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16,
          "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_TENS = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70,
         "achtzig": 80, "neunzig": 90, "hundert": 100}


def parse_number(value: str) -> float | None:
    """„21“, „20,5“, „einundzwanzig“, „fünfzig“ -> Zahl."""
    value = value.strip().lower()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        return float(value.replace(",", "."))
    if value in _ONES or value in _TEENS or value in _TENS:
        return float({**_ONES, **_TEENS, **_TENS}[value])
    if m := re.fullmatch(r"(\w+?)und(\w+)", value):
        ones, tens = _ONES.get("eins" if m[1] == "ein" else m[1]), _TENS.get(m[2])
        if ones is not None and tens is not None:
            return float(tens + ones)
    return None


def _percent(value: str | None) -> int | None:
    number = parse_number(value or "")
    return int(number) if number is not None and 0 <= number <= 100 else None


def match_home(text: str, index: HomeIndex | None, default_area: str | None = None) -> HomeMatch | None:
    """Sofortbefehl fürs Haus – oder None (dann versucht es der Rest: PC-Befehle, Sprachmodell).
    Ohne verbundenes Haus liefern eindeutige Hausbefehle ``home_unavailable`` (ehrliche Antwort statt Raten)."""
    t = text.strip().lower()
    connected = index is not None and bool(index.devices)
    unavailable = None
    # -- Licht -------------------------------------------------------------------------------------------------------
    for pattern in LIGHTS_ALL:
        if m := pattern.match(t):
            if not connected:
                return HomeMatch("home.set_light", {}, "home_unavailable")
            lights = [d.entity_id for d in index.of("light")]
            if lights:
                return HomeMatch("home.set_light", {"entity_ids": lights, "on": _ON[m["state"]]}, "lights_all",
                                 {"all": True})
            return None
    if m := LIGHTS_DIM.match(t):
        pct = _percent(m["num"])
        if not connected:
            return HomeMatch("home.set_light", {}, "home_unavailable")
        area, named = _area(m, index)
        area = area or (None if named else default_area)
        lights = [d.entity_id for d in index.in_area(area, "light")] if area else []
        if lights and pct is not None:
            return HomeMatch("home.set_light", {"entity_ids": lights, "on": pct > 0, "brightness_pct": max(pct, 1)},
                             "lights_dim", {"location": _location(m["prep"], area, index) if named else "",
                                            "brightness_pct": pct})
        return None
    for pattern in LIGHTS_AREA:
        if m := pattern.match(t):
            if not connected:
                return HomeMatch("home.set_light", {}, "home_unavailable")
            area, named = _area(m, index)
            if named and area is None:
                break  # „Licht im Hobbyraum“, aber kein solcher Raum: vielleicht ein Gerätename – weiter unten
            area = area or default_area
            lights = [d.entity_id for d in index.in_area(area, "light")] if area else []
            if not lights and not named and len(index.of("light")) == 1:
                lights = [index.of("light")[0].entity_id]  # nur ein Licht im ganzen Haus
            if lights:
                return HomeMatch("home.set_light", {"entity_ids": lights, "on": _ON[m["state"]]}, "lights_area",
                                 {"location": _location(m["prep"], area, index) if named else ""})
            return None
    # -- Rollläden ---------------------------------------------------------------------------------------------------
    for pattern in COVERS:
        if m := pattern.match(t):
            if not connected:
                return HomeMatch("home.set_cover", {}, "home_unavailable")
            groups = m.groupdict()
            if groups.get("verb"):
                position = 100 if groups["verb"].startswith("öffn") else 0
            elif groups.get("num"):
                position = _percent(groups["num"])
            else:
                position = {"halb": 50}.get(groups["dir"], 100 if groups["dir"] in ("hoch", "rauf", "nach oben", "auf")
                                            else 0)
            area, named = _area(m, index)
            if position is None or (named and area is None):
                return None
            covers = [d.entity_id for d in index.in_area(area, "cover")]
            if covers:
                return HomeMatch("home.set_cover", {"entity_ids": covers, "position": position}, "covers",
                                 {"location": _location(groups.get("prep"), area, index) if area else "",
                                  "what": groups["what"]})
            return None
    # -- Heizung -----------------------------------------------------------------------------------------------------
    for pattern, kind in ((CLIMATE_SET, "set"), (CLIMATE_ON_OFF, "switch")):
        if m := pattern.match(t):
            if not connected:
                return HomeMatch("home.set_climate", {}, "home_unavailable")
            area, named = _area(m, index)
            if named and area is None:
                return None
            pool = index.in_area(area or default_area, "climate") if (area or default_area) else []
            if not pool and not named and len(index.of("climate")) == 1:
                pool = index.of("climate")
            if len(pool) != 1:
                return None  # mehrere Thermostate: das Sprachmodell fragt nach
            location = _location(m["prep"], area, index) if named else ""
            if kind == "set":
                degrees = parse_number(m["num"] or "")
                if degrees is None or not 5 <= degrees <= 30:
                    return None
                return HomeMatch("home.set_climate", {"entity_id": pool[0].entity_id, "temperature": degrees},
                                 "climate_set", {"location": location})
            mode = "off" if m["state"] in ("aus", "ab") else "heat"
            return HomeMatch("home.set_climate", {"entity_id": pool[0].entity_id, "hvac_mode": mode}, "climate_mode",
                             {"location": location})
    # -- Radio auf Lautsprecher (Chromecast, Sonos, DLNA über Home Assistant) -----------------------------------
    for pattern in RADIO:
        if (m := pattern.match(t)) and m["station"] not in ("an", "aus", "ab"):
            if not connected:
                return HomeMatch("home.play_radio", {}, "home_unavailable")
            players, location = [], ""
            if where := (m.groupdict().get("where") or "").strip():
                if area := index.area_of(where):
                    players, location = index.in_area(area, "media_player"), _location(m["prep"] or "auf", area, index)
                else:
                    players = index.named(where, "media_player")
                    location = f"auf {players[0].name}" if players else ""
            elif default_area:
                players = index.in_area(default_area, "media_player")
            if not players and not where and len(index.of("media_player")) == 1:
                players = index.of("media_player")
            if not players:
                return None  # kein eindeutiger Lautsprecher: das Sprachmodell fragt nach
            return HomeMatch("home.play_radio", {"station": m["station"].strip(), "entity_id": players[0].entity_id},
                             "radio", {"location": location or f"auf {players[0].name}"})
    # -- Szenen ------------------------------------------------------------------------------------------------------
    for pattern in SCENE:
        if m := pattern.match(t):
            if not connected:
                return HomeMatch("home.activate_scene", {}, "home_unavailable")
            scenes = index.named(m["name"], "scene")
            return HomeMatch("home.activate_scene", {"scene_id": scenes[0].entity_id}, "scene",
                             {"name": scenes[0].name}) if len(scenes) == 1 else None
    if connected and (m := SCENE_BY_NAME.match(t)) and len(scenes := index.named(m["name"], "scene")) == 1 and \
            norm(_ARTICLE.sub("", m["name"])) == norm(scenes[0].name):
        return HomeMatch("home.activate_scene", {"scene_id": scenes[0].entity_id}, "scene", {"name": scenes[0].name})
    # -- Schlösser ---------------------------------------------------------------------------------------------------
    for pattern in LOCK:
        if m := pattern.match(t):
            locks = index.named(m["name"], "lock") if connected else []
            if not locks and connected and _LOCK_WORDS.search(m["name"]) and len(index.of("lock")) == 1:
                locks = index.of("lock")  # „die Tür“ und es gibt genau ein Schloss
            if len(locks) == 1:
                state = "locked" if m["dir"] in ("ab", "zu") or m["dir"].startswith("verrieg") else "unlocked"
                return HomeMatch("home.lock", {"entity_id": locks[0].entity_id, "state": state}, "lock",
                                 {"name": locks[0].name})
            if not connected and _LOCK_WORDS.search(m["name"]):
                unavailable = HomeMatch("home.lock", {}, "home_unavailable")
            break
    # -- Geräte beim Namen („Mach die Kaffeemaschine an“, „Stehlampe aus“) -----------------------------------------
    if connected:
        for pattern in DEVICE_ON_OFF:
            if m := pattern.match(t):
                name, area = m["name"], None
                if place := re.match(rf"^(?P<name>.+?)\s+(?P<prep>{_PREP})\s+(?P<area>.+)$", name):
                    area = index.area_of(place["area"])
                    name = place["name"] if area else name
                devices = index.named(name, "light", "switch", area=area)
                if len(devices) == 1 or (devices and len({d.domain for d in devices}) == 1 and
                                         {norm(d.name) for d in devices} == {norm(devices[0].name)}):
                    domain = devices[0].domain
                    capability = "home.set_light" if domain == "light" else "home.set_switch"
                    return HomeMatch(capability, {"entity_ids": [d.entity_id for d in devices], "on": _ON[m["state"]]},
                                     "device_on_off", {"name": devices[0].name})
                break
    return unavailable
