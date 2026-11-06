// MQTT-Geräteadapter: bindet ein Gerät ohne Home-Assistant-Integration direkt an JARVIS an.
// Beispiel: ein CO2-/Temperatursensor mit serieller Schnittstelle, hier simuliert.
//   Sensorwerte   -> jarvis/v1/in/sensor/<quelle>/<sensor>   {"value": 812, "unit": "ppm", "ts": "…"}
//   Kommandos     <- jarvis/v1/cmd/<device_id>                {"command": "calibrate", "request_id": "…"}
//   Quittungen    -> jarvis/v1/state/<device_id>/ack          {"request_id": "…", "ok": true}
//   Status (LWT)  -> jarvis/v1/state/<device_id>/availability "online" | "offline" (retained)
import mqtt from "mqtt";

const PREFIX = "jarvis/v1";
const DEVICE_ID = process.env.DEVICE_ID ?? "airsensor_buero";
const SOURCE = DEVICE_ID; // ACL-Pattern jarvis/v1/in/sensor/%u/# erzwingt Quelle = MQTT-Benutzername

const client = mqtt.connect(process.env.MQTT_URL ?? "mqtts://mosquitto:8883", {
  clientId: `dev-${DEVICE_ID}`,
  username: DEVICE_ID, // ACL: Gerät darf nur eigene Topics nutzen (integrations/mqtt/acl)
  password: process.env.MQTT_PASSWORD,
  clean: false, // QoS-1-Kommandos überleben kurze Verbindungsabbrüche
  reconnectPeriod: 2000,
  will: { topic: `${PREFIX}/state/${DEVICE_ID}/availability`, payload: "offline", qos: 1, retain: true },
});

const readSensor = () => ({ co2: 600 + Math.round(Math.random() * 600), temperature: 21 + Math.random() * 2 });

client.on("connect", () => {
  client.publish(`${PREFIX}/state/${DEVICE_ID}/availability`, "online", { qos: 1, retain: true });
  client.subscribe(`${PREFIX}/cmd/${DEVICE_ID}`, { qos: 1 });
});

client.on("message", (topic, payload) => {
  let cmd;
  try {
    cmd = JSON.parse(payload.toString("utf8"));
  } catch {
    return; // ungültige Kommandos ignorieren
  }
  const ok = ["calibrate", "identify"].includes(cmd.command);
  client.publish(`${PREFIX}/state/${DEVICE_ID}/ack`, JSON.stringify({ request_id: cmd.request_id, ok }), { qos: 1 });
});

client.on("error", (err) => console.error(JSON.stringify({ level: "ERROR", msg: "mqtt error", error: err.message })));

setInterval(() => {
  if (!client.connected) return;
  const { co2, temperature } = readSensor();
  const ts = new Date().toISOString();
  client.publish(`${PREFIX}/in/sensor/${SOURCE}/co2`, JSON.stringify({ value: co2, unit: "ppm", ts }), { qos: 1 });
  client.publish(`${PREFIX}/in/sensor/${SOURCE}/temperature`,
    JSON.stringify({ value: Number(temperature.toFixed(1)), unit: "°C", ts }), { qos: 1 });
}, 30_000);
