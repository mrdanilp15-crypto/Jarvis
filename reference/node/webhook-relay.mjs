// Webhook-Relay: nimmt Webhooks von Diensten ohne JARVIS-Signatur an (z. B. Türklingel mit Shared-Token),
// normalisiert sie und leitet sie signiert an JARVIS weiter (POST /v1/webhooks/{id}).
// Signaturschema (identisch zu reference/python/src/jarvis/webhooks.py):
//   X-Jarvis-Timestamp: <unix-sekunden>
//   X-Jarvis-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<body>")
//   X-Jarvis-Delivery:  <eindeutige ID>
import http from "node:http";
import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

export function sign(secret, timestamp, body) {
  return "sha256=" + createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
}

export function verify(secret, { body, timestamp, signature, now = Math.floor(Date.now() / 1000), maxSkewS = 300 }) {
  if (!timestamp || !signature) return false;
  if (Math.abs(now - Number(timestamp)) > maxSkewS) return false;
  const expected = Buffer.from(sign(secret, timestamp, body));
  const given = Buffer.from(signature);
  return expected.length === given.length && timingSafeEqual(expected, given);
}

export async function forward({ coreUrl, hookId, secret, payload, fetchImpl = fetch }) {
  const body = JSON.stringify(payload);
  const timestamp = Math.floor(Date.now() / 1000);
  for (let attempt = 1; attempt <= 4; attempt++) {
    const res = await fetchImpl(`${coreUrl}/v1/webhooks/${hookId}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-jarvis-timestamp": String(timestamp),
        "x-jarvis-signature": sign(secret, timestamp, body),
        "x-jarvis-delivery": `dlv_${randomUUID()}`,
      },
      body,
    }).catch((err) => ({ ok: false, status: 0, err }));
    if (res.ok) return true;
    if (res.status >= 400 && res.status < 500 && res.status !== 429) return false; // nicht wiederholbar
    await new Promise((r) => setTimeout(r, 2 ** attempt * 250 + Math.random() * 250));
  }
  return false;
}

// Mapping eingehender Quellen -> JARVIS-Webhook-ID + normalisierte Payload
const SOURCES = {
  doorbell: {
    hookId: "whk_doorbell",
    normalize: (raw) => ({ event: "ring", camera: raw.camera ?? "haustuer", at: raw.timestamp ?? new Date().toISOString() }),
  },
  paket: {
    hookId: "whk_parcel",
    normalize: (raw) => ({ event: "parcel_status", carrier: raw.carrier, status: raw.status, eta: raw.eta ?? null }),
  },
};

if (import.meta.url === `file://${process.argv[1]}`) {
  const CORE = process.env.JARVIS_CORE_URL ?? "http://localhost:8080";
  const SECRETS = JSON.parse(process.env.JARVIS_WEBHOOK_SECRETS ?? "{}"); // {"whk_doorbell": "…"}
  const INBOUND_TOKEN = process.env.RELAY_INBOUND_TOKEN; // Shared-Token der Quelle

  http.createServer(async (req, res) => {
    const source = SOURCES[req.url?.split("/")[2] ?? ""];
    if (req.method !== "POST" || !req.url?.startsWith("/in/") || !source) {
      res.writeHead(404).end();
      return;
    }
    if (!INBOUND_TOKEN || req.headers.authorization !== `Bearer ${INBOUND_TOKEN}`) {
      res.writeHead(401).end();
      return;
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let raw;
    try {
      raw = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {
      res.writeHead(400).end();
      return;
    }
    const ok = await forward({ coreUrl: CORE, hookId: source.hookId, secret: SECRETS[source.hookId], payload: source.normalize(raw) });
    res.writeHead(ok ? 202 : 502, { "content-type": "application/json" }).end(JSON.stringify({ forwarded: ok }));
  }).listen(Number(process.env.PORT ?? 7320));
}
