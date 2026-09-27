# 5. Code-Beispiele

[← Technische Umsetzung](04-technische-umsetzung.md) · [Übersicht](../README.md) · [Weiter: Integrationsplan →](06-integrationsplan.md)

Alle Beispiele sind lauffähiger bzw. geprüfter Code im Repository – die Auszüge unten stammen direkt daraus.

## 5.1 Übersicht

| Datei | Sprache | Inhalt | Geprüft durch |
|-------|---------|--------|---------------|
| [`jarvis/orchestrator.py`](../reference/python/src/jarvis/orchestrator.py) | Python | Agent-Loop, zentrale Aktionsschnittstelle, Bestätigungen, Taint-Tracking | Szenario-Tests |
| [`jarvis/policy.py`](../reference/python/src/jarvis/policy.py) | Python | RBAC/ABAC-Policy-Engine mit Risikoklassen | Policy-Matrix-Tests |
| [`jarvis/tools.py`](../reference/python/src/jarvis/tools.py) | Python | Tool-Registry, Schema-Validierung, argumentabhängige Risiken | Unit-Tests |
| [`jarvis/llm/claude.py`](../reference/python/src/jarvis/llm/claude.py) | Python | Claude-Adapter (Streaming, adaptives Denken, Caching, Fallback) | SDK mit simuliertem SSE-Stream |
| [`jarvis/llm/ollama.py`](../reference/python/src/jarvis/llm/ollama.py) | Python | Ollama-Adapter (lokal) | httpx-MockTransport |
| [`jarvis/llm/router.py`](../reference/python/src/jarvis/llm/router.py) | Python | Klassifikation und Routing-Tabelle | Unit-Tests |
| [`jarvis/memory.py`](../reference/python/src/jarvis/memory.py) | Python | Gedächtnis: Ranking, Dedup, Konflikte, pgvector-SQL | Unit-Tests + echte Postgres-Instanz |
| [`jarvis/automation.py`](../reference/python/src/jarvis/automation.py) | Python | Trigger/Bedingungen/Aktionen, Dry-Run | Unit-Tests |
| [`jarvis/connectors/homeassistant.py`](../reference/python/src/jarvis/connectors/homeassistant.py) | Python | HA-WebSocket-Client + Smart-Home-Capabilities | mit `FakeHome` |
| [`jarvis/connectors/mqtt_bridge.py`](../reference/python/src/jarvis/connectors/mqtt_bridge.py) | Python | MQTT-Bridge (aiomqtt) | Import-Prüfung |
| [`jarvis/voice/pipeline.py`](../reference/python/src/jarvis/voice/pipeline.py) | Python | Wyoming-STT/TTS, Satz-Streaming, Barge-in | Segmentierungs-Tests |
| [`jarvis/api.py`](../reference/python/src/jarvis/api.py) | Python | FastAPI: REST + WebSocket | TestClient |
| [`jarvis/info.py`](../reference/python/src/jarvis/info.py) | Python | Wetter (Open-Meteo), Nachrichten (RSS/Atom), Wikipedia | gegen nachgebildete API-Antworten |
| [`jarvis/webhooks.py`](../reference/python/src/jarvis/webhooks.py) | Python | HMAC-Signatur, Replay-Schutz | Unit-Tests |
| [`jarvis/logging_setup.py`](../reference/python/src/jarvis/logging_setup.py) | Python | JSON-Logging, Korrelation, Redaktion | Unit-Tests |
| [`jarvis/demo.py`](../reference/python/src/jarvis/demo.py) | Python | Offline-Demo aller Kernabläufe | CI |
| [`node/jarvis-client.mjs`](../reference/node/jarvis-client.mjs) | Node.js | WebSocket-Client (Desktop/CLI) | `node --check` |
| [`node/webhook-relay.mjs`](../reference/node/webhook-relay.mjs) | Node.js | Webhook-Relay mit Signatur (identisch zu Python) | `node --test` (gemeinsamer Testvektor) |
| [`node/mqtt-device-adapter.mjs`](../reference/node/mqtt-device-adapter.mjs) | Node.js | MQTT-Geräteadapter | `node --check` |
| [`node/plugin-weather/`](../reference/node/plugin-weather) | Node.js | vollständiges Plugin (Manifest + HTTP-Protokoll) | Manifest-Schema |
| [`jarvis/web/`](../reference/python/src/jarvis/web) | HTML/CSS/JS (Browser) | Oberfläche unter `/`: HUD, Spracheingabe/-ausgabe, „Jarvis“-Aktivierung, Chat, Bestätigungen | TestClient, `node --check` |
| [`deploy/windows/`](../deploy/windows) | PowerShell | Autostart: Docker starten, auf JARVIS warten, App-Fenster öffnen | Parser-Prüfung in der CI |
| [`web/voice-widget.js`](../reference/web/voice-widget.js) | JavaScript (Browser) | Push-to-Talk, AudioWorklet, Barge-in | Syntax |
| [`homeassistant/packages/jarvis.yaml`](../integrations/homeassistant/packages/jarvis.yaml) | HA-YAML | rest_command, Webhook-Trigger, MQTT-Sensoren, Skript | YAML-Prüfung |
| [`mqtt/acl`](../integrations/mqtt/acl), [`mosquitto.conf`](../integrations/mqtt/mosquitto.conf) | Mosquitto | Broker, TLS, ACL | – |
| [`node-red/jarvis-flows.json`](../integrations/node-red/jarvis-flows.json) | Node-RED | 3 Flows | Verdrahtungsprüfung |

## 5.2 Python

### 5.2.1 Eine Tür für alle Aktionen

Fast-Path, LLM, Automationen und REST-API landen alle hier – dadurch gilt die Policy überall gleich:

```python
async def request_action(self, *, capability, arguments, principal, correlation_id, session_id, via, ctx,
                         dry_run=False) -> ActionRecord:
    cap = self.registry.get(capability)
    errors = cap.validate(arguments)                  # JSON-Schema
    if errors:
        raise JarvisError("JRV-VAL-001", f"Ungültige Argumente für {capability}", errors=errors)

    risk = cap.risk_for(arguments)                    # z. B. home.lock: locked=R1, unlocked=R3
    decision = self.policy.evaluate(principal, cap.name, cap.domain, risk, arguments, ctx)
    await self.audit.record("policy.decision", correlation_id=correlation_id, actor=principal.actor, …)

    if decision.effect == "deny":
        ...                                           # Problem Details zurück
    if decision.effect == "confirm":
        pending = self.confirmations.create(record, principal, session_id, correlation_id, decision.method, …)
        await self._publish("jarvis.confirmation.requested", …)   # -> App-Push / Sprachrückfrage
        return record
    return await self._execute(cap, record, principal, correlation_id, session_id)  # Timeout, Verifikation, Undo
```

### 5.2.2 Taint-Tracking im Agent-Loop

```python
content = json.dumps({"status": "succeeded", "result": record.result, "verification": record.verification})
if cap.output_trust == "untrusted":
    session.tainted = True   # ab jetzt: jede Aktion >= R2 nur mit Bestätigung
    content = f'<untrusted_content source="{cap.name}">\n{content}\n</untrusted_content>'
```

Bestätigungen löst der Orchestrator **deterministisch** auf, bevor das LLM überhaupt gefragt wird – und nur, wenn die
gesamte Äußerung eine Zustimmung ist und von derselben Person stammt:

```python
pending = self.confirmations.for_session(req.session_id)
if pending is not None:
    reply = confirmation_reply(req.text)          # "Ja" -> True, "Mach das Licht an" -> None
    if reply is not None:
        return await self._resolve_by_voice(pending, req, approve=reply)   # R3: Stimme reicht nicht
```

### 5.2.3 Policy-Engine

```python
if risk == "R4":
    return Decision("deny", risk, "Risikoklasse R4 ist nie autonom zulässig", "risk.R4")
if not _glob_any(capability, role_cfg.get("allow", [])) or _glob_any(capability, role_cfg.get("deny", [])):
    return Decision("deny", …, f"role.{principal.role}.not_allowed")
if ctx.allowed_domains is not None and domain not in ctx.allowed_domains:
    return Decision("deny", …, "intent_binding")
ceiling = self._ceiling(principal, role_cfg)      # min(Rolle, Trust; Stimmkonfidenz < 0,8 -> R1)
if risk_gt(risk, ceiling):
    return Decision("deny", …, f"ceiling.{principal.trust}")
rule = self._first_matching_rule(principal, capability, arguments, ctx)   # config/policies.yaml
...
if risk == "R3":
    return Decision("confirm", risk, "R3 erfordert immer eine starke Bestätigung", "risk.R3", "app_biometric")
```

### 5.2.4 Capability mit argumentabhängigem Risiko (Home Assistant)

```python
registry.register(Capability(
    name="home.lock", domain="home", risk_class="R3", side_effects="reversible",
    risk_rules=[{"when": {"state": "locked"}, "risk_class": "R1"}],        # Verriegeln ist harmlos
    description="Verriegelt (locked) oder entriegelt (unlocked) ein Türschloss. Entriegeln erfordert Bestätigung.",
    input_schema=_obj({"entity_id": _entity("lock"), "state": {"enum": ["locked", "unlocked"]}},
                      ["entity_id", "state"]),
    handler=set_lock, verify=verify_lock, timeout_s=15.0,
))
```

Der WebSocket-Client von Home Assistant im selben Modul authentifiziert sich (`auth_required` → `auth` →
`auth_ok`), lädt `get_states`, abonniert `state_changed` und korreliert Antworten über die Nachrichten-`id`.

### 5.2.5 Claude-Aufruf (Cloud-Modell)

```python
params = {
    "model": "claude-opus-5",
    "max_tokens": 64000,
    "system": [
        {"type": "text", "text": system.static, "cache_control": {"type": "ephemeral"}},  # Tools + Regeln + Persona
        {"type": "text", "text": system.dynamic},                                         # Situation, Memories
    ],
    "messages": self._render_messages(transcript),
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": effort or "medium"},
    "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
               "eager_input_streaming": True} for t in tools],
    "betas": ["server-side-fallback-2026-07-01"],
    "fallbacks": "default",
}
async with self._client.beta.messages.stream(**params) as stream:
    async for event in stream:
        if event.type == "text" and on_text is not None:
            await on_text(event.text)                 # -> Satz-Segmentierer -> TTS
    message = await stream.get_final_message()
```

Die Tool-Eingaben werden anschließend im Orchestrator gegen das Schema geprüft; bei `stop_reason` `max_tokens` oder
`refusal` wird kein Tool ausgeführt. Der Test [`test_llm_providers.py`](../reference/python/tests/test_llm_providers.py)
prüft Request-Form und Stream-Verarbeitung gegen das echte SDK.

### 5.2.6 Gedächtnis-Suche in PostgreSQL

```sql
WITH candidates AS (
    SELECT id, content, kind, importance, created_at,
           1 - (embedding <=> $1::text::vector) AS similarity
    FROM jarvis.memory_items
    WHERE valid_until IS NULL AND embedding IS NOT NULL
      AND (expires_at IS NULL OR expires_at > now())
      AND (user_id = $2::text OR user_id IS NULL)          -- eigene + Haushaltserinnerungen
      AND ($3::boolean OR sensitivity <> 'sensitive')       -- sensibel nur für lokale Modelle
    ORDER BY embedding <=> $1::text::vector
    LIMIT $4::int
)
SELECT id, content, kind,
       $5::float8 * similarity
     + $6::float8 * CASE WHEN kind = 'episodic'
                         THEN power(0.5, extract(epoch FROM now() - created_at)::float8 / 86400 / $7::float8)
                         ELSE 1 END
     + $8::float8 * importance AS score
FROM candidates ORDER BY score DESC LIMIT $9::int;
```

### 5.2.7 Automation mit Policy-Durchgriff

```python
principal = Principal(actor=f"automation:{automation['id']}",
                      role=self.owner_roles.get(owner, "service"), trust="system")
record = await self.gateway.request_action(capability=action["capability"], arguments=arguments,
                                           principal=principal, via="automation", dry_run=dry_run, …)
```

Automationen handeln mit den Rechten ihres Besitzers, aber höchstens mit Trust `system` (Obergrenze R2) – eine
Automation kann also nie selbstständig eine Tür entriegeln.

### 5.2.8 Satzweises Streaming zur Sprachausgabe

```python
seg = SentenceSegmenter()
for delta in ["Sehr wohl. Die Temperatur beträgt z. B. 21", ".5 Grad im Wohnzimmer. Außer", "dem regnet es."]:
    for sentence in seg.feed(delta):
        speak(sentence)   # "Sehr wohl. Die Temperatur beträgt z. B. 21.5 Grad im Wohnzimmer."
speak_all(seg.flush())    # "Außerdem regnet es."
```

## 5.3 JavaScript / Node.js

### 5.3.1 WebSocket-Client

```js
import { createClient } from "./jarvis-client.mjs";

const jarvis = createClient({
  url: "wss://jarvis.home.example",
  token: process.env.JARVIS_TOKEN,
  onEvent: (ev) => {
    if (ev.type === "output.text_delta") process.stdout.write(ev.delta);
    if (ev.type === "output.final" && ev.pending_confirmation) askUser(ev.pending_confirmation);
  },
});
jarvis.say("Wie ist der Status im Haus?");
```

### 5.3.2 Signierter Webhook (identisch zur Python-Seite)

```js
export function sign(secret, timestamp, body) {
  return "sha256=" + createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
}
```

`webhook-relay.test.mjs` prüft gegen einen mit Python berechneten Testvektor – beide Implementierungen bleiben
dadurch kompatibel.

### 5.3.3 Plugin-Protokoll (Wetter-Plugin)

```js
if (req.method === "GET" && req.url === "/manifest") return send(res, 200, manifest);
if (req.method === "GET" && req.url === "/health") return send(res, 200, { status: "ok" });
if (req.method === "POST" && req.url === "/invoke") return send(res, 200, await invoke(await readJson(req)));
// invoke({capability: "info.weather", arguments: {hours: 6}}) -> {ok: true, result: {current, hourly, rain_expected_within_min}}
```

Das Plugin publiziert zusätzlich `jarvis.info.weather_alert`, das die Beispiel-Automation
[`automation.rain-window-warning.json`](../schemas/examples/automation.rain-window-warning.json) auslöst.

### 5.3.4 MQTT-Geräteadapter

```js
const client = mqtt.connect("mqtts://mosquitto:8883", {
  clientId: `dev-${DEVICE_ID}`, username: DEVICE_ID, password: process.env.MQTT_PASSWORD, clean: false,
  will: { topic: `jarvis/v1/state/${DEVICE_ID}/availability`, payload: "offline", qos: 1, retain: true },
});
client.publish(`jarvis/v1/in/sensor/${DEVICE_ID}/co2`, JSON.stringify({ value: 812, unit: "ppm", ts }), { qos: 1 });
```

### 5.3.5 Browser-Voice-Widget

```js
import { JarvisVoice } from "./voice-widget.js";

const jarvis = new JarvisVoice({ url: "wss://jarvis.home.example", token: shortLivedToken,
                                 onText: (t) => bubble.append(t), onState: (s) => orb.dataset.state = s });
talkButton.onpointerdown = () => jarvis.start();   // stoppt laufende Ausgabe (Barge-in)
talkButton.onpointerup = () => jarvis.stop();
```

## 5.4 Home Assistant

```yaml
rest_command:
  jarvis_event:
    url: "http://jarvis-core:8080/v1/events"
    method: POST
    headers:
      authorization: !secret jarvis_ha_bearer
    content_type: "application/json"
    payload: >-
      {"specversion": "1.0", "source": "/homeassistant/automation", "type": "{{ type }}",
       "subject": "{{ subject | default('') }}", "data": {{ data | default({}) | tojson }}}

automation:
  - id: jarvis_forward_doorbell
    alias: "JARVIS: Türklingel melden"
    triggers:
      - trigger: state
        entity_id: binary_sensor.tuerklingel
        to: "on"
    actions:
      - action: rest_command.jarvis_event
        data:
          type: jarvis.home.doorbell
          subject: binary_sensor.tuerklingel
          data:
            camera: camera.haustuer
```

Vollständig: [`integrations/homeassistant/packages/jarvis.yaml`](../integrations/homeassistant/packages/jarvis.yaml)
(Modus-Auswahl, Status-Sensoren, Webhook-Trigger mit Szenen-Allowlist, Skript „JARVIS fragen“).

## 5.5 MQTT

```bash
# Sensorwert einspeisen (Gerät airsensor_buero, ACL erlaubt nur eigene Topics)
mosquitto_pub -h mosquitto -p 8883 --cafile ca.crt -u airsensor_buero -P "$PW" -q 1 \
  -t jarvis/v1/in/sensor/airsensor_buero/co2 -m '{"value": 1240, "unit": "ppm", "ts": "2026-09-26T19:40:00Z"}'

# Alle abgeschlossenen Aktionen beobachten
mosquitto_sub -h mosquitto -p 8883 --cafile ca.crt -u nodered -P "$PW" -t 'jarvis/v1/event/#' -v

# Kommando an ein Gerät (nur jarvis-core darf das)
mosquitto_pub … -u jarvis-core -t jarvis/v1/cmd/airsensor_buero -m '{"command": "calibrate", "request_id": "req_1"}'
```

## 5.6 Node-RED

[`integrations/node-red/jarvis-flows.json`](../integrations/node-red/jarvis-flows.json) enthält drei importierbare Flows:

1. **Aktionen mitschreiben** – `mqtt in jarvis/v1/event/action.completed` → Kurzfassung → Debug/Status.
2. **Waschmaschine fertig** – Zigbee-Steckdose → Leistungsabfall-Erkennung (Kontextspeicher) → `POST /v1/events`
   (`jarvis.home.appliance_finished`) → die Proaktiv-Engine entscheidet über Hinweis und Kanal.
3. **Kommandos ausführen** – `jarvis/v1/cmd/nodered` → Switch nach `command` → Gartenventil schalten (mit
   Sicherheitsgrenze 30 min) + Quittung auf `jarvis/v1/state/nodered/ack`.

## 5.7 Ausführen

```bash
cd reference/python && pip install -e ".[dev]"
pytest                      # 132 Tests: Policy, Orchestrator, Automationen, Memory, API, LLM-Adapter …
python -m jarvis.demo       # Offline-Demo ohne LLM/Home Assistant
cd ../node && node --test   # Webhook-Signatur, Retry-Verhalten
python ../../tools/validate.py
```

Ausgabe der Demo (gekürzt):

```
=== Fast-Path
JARVIS (fast_path): Erledigt.
  Aktion home.set_light {"entity_ids": ["light.kueche"], "on": true, "brightness_pct": 40} -> succeeded [allow, R1, role.adult.default]

=== R3 angefordert
  Aktion home.lock {"entity_id": "lock.haustuer", "state": "unlocked"} -> pending_confirmation [confirm, R3, risk.R3]
=== Sprach-Ja reicht für R3 nicht
JARVIS (confirmation_resolver): Für diese Aktion benötige ich Ihre Bestätigung in der App.
  App-Freigabe (Biometrie) -> succeeded; Haustür: unlocked

=== Prompt-Injection abgewehrt
  Aktion web.fetch {"url": "https://example.org/angebote"} -> succeeded [allow, R0, role.adult.default]
  Aktion home.set_climate {"entity_id": "climate.wohnzimmer", "temperature": 30} -> pending_confirmation [confirm, R2, risk.R2.tainted]

=== Gastrechte
  Aktion home.lock {"entity_id": "lock.haustuer", "state": "unlocked"} -> denied [deny, R3, role.guest.not_allowed]
```
