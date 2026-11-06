// node --test: prüft, dass Node- und Python-Signaturschema identisch sind (Testvektor aus Python berechnet).
import { test } from "node:test";
import assert from "node:assert/strict";
import { sign, verify, forward } from "./webhook-relay.mjs";

test("Signatur entspricht dem Python-Testvektor", () => {
  // python -c "from jarvis.webhooks import sign; print(sign(b's3cr3t', 1790000000, b'{\"event\":\"ring\"}'))"
  assert.equal(sign("s3cr3t", 1790000000, '{"event":"ring"}'),
    "sha256=479d173a5179189bbfe665200755198f8bef96df93c006bb3e76bfea9f702e1e");
});

test("verify lehnt manipulierte oder alte Nachrichten ab", () => {
  const body = '{"event":"ring"}';
  const signature = sign("s3cr3t", 1790000000, body);
  assert.ok(verify("s3cr3t", { body, timestamp: "1790000000", signature, now: 1790000010 }));
  assert.ok(!verify("s3cr3t", { body: '{"event":"open"}', timestamp: "1790000000", signature, now: 1790000010 }));
  assert.ok(!verify("s3cr3t", { body, timestamp: "1790000000", signature, now: 1790000400 }));
});

test("forward wiederholt bei 5xx, nicht bei 4xx", async () => {
  let calls = 0;
  const flaky = async () => (++calls < 2 ? { ok: false, status: 503 } : { ok: true, status: 202 });
  assert.equal(await forward({ coreUrl: "http://x", hookId: "whk", secret: "s", payload: {}, fetchImpl: flaky }), true);
  calls = 0;
  const denied = async () => (++calls, { ok: false, status: 401 });
  assert.equal(await forward({ coreUrl: "http://x", hookId: "whk", secret: "s", payload: {}, fetchImpl: denied }), false);
  assert.equal(calls, 1);
});
