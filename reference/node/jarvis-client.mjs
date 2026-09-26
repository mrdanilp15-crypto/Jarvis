// Minimaler JARVIS-Client (Desktop/CLI) über WebSocket /v1/stream – ohne Abhängigkeiten (Node >= 22).
//   JARVIS_URL=wss://jarvis.home.example JARVIS_TOKEN=… node jarvis-client.mjs
// Zeigt: Token-Streaming, Bestätigungsdialog (R3 per App), Reconnect mit Backoff, Heartbeat.
import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { randomUUID } from "node:crypto";

const BASE = process.env.JARVIS_URL ?? "ws://localhost:8080";
const TOKEN = process.env.JARVIS_TOKEN;
const SESSION = process.env.JARVIS_SESSION ?? `desktop-${randomUUID().slice(0, 8)}`;

export function createClient({ url, token, onEvent }) {
  let ws;
  let backoff = 1000;
  let heartbeat;
  const queue = [];

  const connect = () => {
    ws = new WebSocket(`${url}/v1/stream?token=${encodeURIComponent(token)}`);
    ws.addEventListener("open", () => {
      backoff = 1000;
      while (queue.length) ws.send(queue.shift());
      heartbeat = setInterval(() => ws.send(JSON.stringify({ type: "ping" })), 25_000);
    });
    ws.addEventListener("message", (msg) => onEvent(JSON.parse(msg.data)));
    ws.addEventListener("close", (ev) => {
      clearInterval(heartbeat);
      if (ev.code === 4401) {
        onEvent({ type: "error", error: { code: "JRV-AUTH-001", title: "Token ungültig" } });
        return; // kein Reconnect bei Auth-Fehler
      }
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30_000);
    });
  };
  connect();

  const send = (obj) => {
    const data = JSON.stringify(obj);
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
    else queue.push(data); // offline gepuffert, beim Reconnect gesendet
  };
  return {
    say: (text) => send({ type: "input.text", text, session_id: SESSION, channel: "desktop" }),
    resolve: (confirmationId, decision, method = "app") =>
      send({ type: "confirmation.resolve", confirmation_id: confirmationId, decision, method }),
    close: () => ws.close(),
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  if (!TOKEN) {
    console.error("JARVIS_TOKEN fehlt");
    process.exit(1);
  }
  const rl = readline.createInterface({ input, output });
  let pending = null;
  let turnDone = () => {};

  const client = createClient({
    url: BASE,
    token: TOKEN,
    onEvent: (ev) => {
      switch (ev.type) {
        case "output.text_delta":
          output.write(ev.delta);
          break;
        case "output.final":
          output.write(ev.route === "fast_path" ? `JARVIS: ${ev.text}\n` : "\n");
          for (const a of ev.actions) console.log(`  ↳ ${a.capability}: ${a.status} (${a.decision.risk_class})`);
          pending = ev.pending_confirmation ?? null;
          if (pending) console.log(`  ⚠ Bestätigung nötig (${pending.method}): ${pending.prompt}  [j/n]`);
          turnDone();
          break;
        case "action.update":
          console.log(`  ↳ ${ev.action.capability}: ${ev.action.status}`);
          turnDone();
          break;
        case "error":
          console.error(`  ✖ ${ev.error.code}: ${ev.error.title}`);
          turnDone();
          break;
      }
    },
  });

  for (;;) {
    const line = (await rl.question("> ")).trim();
    if (!line) continue;
    if (line === "/quit") break;
    const done = new Promise((resolve) => (turnDone = resolve));
    if (pending && /^(j|ja|n|nein)$/i.test(line)) {
      // Desktop gilt als "app"-Kanal; biometrische Freigaben (R3) erfolgen in der Mobile App.
      client.resolve(pending.confirmation_id, /^j/i.test(line) ? "approve" : "reject");
      pending = null;
    } else {
      client.say(line);
    }
    await done;
  }
  client.close();
  rl.close();
}
