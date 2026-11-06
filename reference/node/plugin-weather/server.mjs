// JARVIS-Plugin "org.example.weather" – Plugin-Protokoll über HTTP (siehe docs/03, Abschnitt 3.2.8).
//   GET  /manifest  -> Manifest
//   GET  /health    -> {"status":"ok"}
//   POST /invoke    -> {"capability","arguments","context","deadline_ms"} -> {"ok":true,"result":…}
// Zusätzlich pollt das Plugin periodisch die Vorhersage und publiziert jarvis.info.weather_alert,
// wenn innerhalb der nächsten 30 Minuten Regen erwartet wird.
import http from "node:http";
import { readFile } from "node:fs/promises";

const manifest = JSON.parse(await readFile(new URL("./plugin.json", import.meta.url), "utf8"));
const config = JSON.parse(process.env.JARVIS_PLUGIN_CONFIG ?? '{"home_latitude":52.52,"home_longitude":13.40}');
const PORT = Number(process.env.PORT ?? manifest.runtime.port);
const CORE_URL = process.env.JARVIS_CORE_URL ?? "http://jarvis-core:8080";
const PLUGIN_TOKEN = process.env.JARVIS_PLUGIN_TOKEN; // vom Plugin-Host injiziert, Scope: events:publish

function problem(code, title, detail, retryable = false, status = 400) {
  return { type: `https://docs.jarvis.local/errors/${code.toLowerCase()}`, title, detail, status,
           code, category: status >= 500 ? "integration" : "validation", retryable };
}

async function fetchForecast({ latitude, longitude, hours = 12 }) {
  const url = new URL("https://api.open-meteo.com/v1/forecast");
  url.searchParams.set("latitude", String(latitude));
  url.searchParams.set("longitude", String(longitude));
  url.searchParams.set("current", "temperature_2m,precipitation,weather_code,wind_speed_10m");
  url.searchParams.set("minutely_15", "precipitation");
  url.searchParams.set("hourly", "temperature_2m,precipitation_probability,precipitation");
  url.searchParams.set("forecast_hours", String(hours));
  url.searchParams.set("timezone", "auto");
  const res = await fetch(url, { signal: AbortSignal.timeout(4000) });
  if (!res.ok) throw Object.assign(new Error(`Open-Meteo HTTP ${res.status}`), { retryable: res.status >= 500 });
  const data = await res.json();

  const hourly = data.hourly.time.map((time, i) => ({
    time,
    temperature_c: data.hourly.temperature_2m[i],
    precipitation_probability: data.hourly.precipitation_probability[i],
    precipitation_mm: data.hourly.precipitation[i],
  }));
  const rainIndex = (data.minutely_15?.precipitation ?? []).slice(0, 8).findIndex((mm) => mm >= 0.1);
  return {
    current: {
      temperature_c: data.current.temperature_2m,
      precipitation_mm: data.current.precipitation,
      weather_code: data.current.weather_code,
      wind_kmh: data.current.wind_speed_10m,
    },
    hourly,
    rain_expected_within_min: rainIndex >= 0 ? rainIndex * 15 : null,
  };
}

async function invoke({ capability, arguments: args = {} }) {
  if (capability !== "info.weather") {
    return { ok: false, error: problem("JRV-NFD-001", "Nicht gefunden", `Unbekannte Capability ${capability}`, false, 404) };
  }
  const latitude = args.latitude ?? config.home_latitude;
  const longitude = args.longitude ?? config.home_longitude;
  try {
    return { ok: true, result: await fetchForecast({ latitude, longitude, hours: args.hours ?? 12 }) };
  } catch (err) {
    return { ok: false, error: problem("JRV-INT-001", "Wetterdienst nicht erreichbar", err.message, err.retryable ?? true, 502) };
  }
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function send(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/manifest") return send(res, 200, manifest);
    if (req.method === "GET" && req.url === "/health") return send(res, 200, { status: "ok" });
    if (req.method === "POST" && req.url === "/invoke") return send(res, 200, await invoke(await readJson(req)));
    send(res, 404, problem("JRV-NFD-001", "Nicht gefunden", req.url, false, 404));
  } catch (err) {
    send(res, 400, problem("JRV-VAL-001", "Ungültige Anfrage", err.message));
  }
});

// Proaktiver Regenhinweis: nur Hinweis-Event, die Entscheidung über Ausgabe trifft die Proaktiv-Engine.
let lastAlertAt = 0;
async function pollAlerts() {
  if (!PLUGIN_TOKEN) return;
  try {
    const forecast = await fetchForecast({ latitude: config.home_latitude, longitude: config.home_longitude, hours: 2 });
    const soon = forecast.rain_expected_within_min;
    if (soon !== null && soon <= 30 && Date.now() - lastAlertAt > 3 * 3600_000) {
      lastAlertAt = Date.now();
      await fetch(`${CORE_URL}/v1/events`, {
        method: "POST",
        headers: { authorization: `Bearer ${PLUGIN_TOKEN}`, "content-type": "application/json" },
        body: JSON.stringify({
          specversion: "1.0",
          source: "/plugin/org.example.weather",
          type: "jarvis.info.weather_alert",
          time: new Date().toISOString(),
          data: { kind: "rain_soon", minutes: soon },
        }),
      });
    }
  } catch (err) {
    console.error(JSON.stringify({ level: "WARNING", msg: "weather alert poll failed", error: err.message }));
  }
}
setInterval(pollAlerts, (config.alert_poll_minutes ?? 15) * 60_000).unref();

server.listen(PORT, () => console.log(JSON.stringify({ level: "INFO", msg: "weather plugin listening", port: PORT })));
