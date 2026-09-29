// Browser-Voice-Frontend (Push-to-Talk) für JARVIS – ES-Modul ohne Abhängigkeiten.
// Protokoll auf /v1/stream (bedient von jarvis-voice, siehe docs/04 Abschnitt 4.2.3):
//   -> {"type":"audio.start","session_id","format":{"rate":16000,"width":2,"channels":1}}
//   -> Binärframes: PCM16 little-endian, 16 kHz, mono (je ~20 ms)
//   -> {"type":"audio.stop"}
//   <- {"type":"transcript.final","text"} · {"type":"output.text_delta","delta"}
//   <- Binärframes: TTS-Audio (PCM16, Rate aus {"type":"audio.format"}) · {"type":"output.final",…}
//
// Verwendung:
//   import { JarvisVoice } from "./voice-widget.js";
//   const jarvis = new JarvisVoice({ url: "wss://jarvis.home.example", token, onText: (t) => … });
//   button.onpointerdown = () => jarvis.start(); button.onpointerup = () => jarvis.stop();

const WORKLET = `
class Pcm16Capture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.ratio = sampleRate / 16000; this.pos = 0; }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    // einfaches Downsampling auf 16 kHz (für Sprache ausreichend)
    for (; this.pos < ch.length; this.pos += this.ratio) {
      const s = Math.max(-1, Math.min(1, ch[Math.floor(this.pos)]));
      this.buf.push(s < 0 ? s * 0x8000 : s * 0x7fff);
    }
    this.pos -= ch.length;
    if (this.buf.length >= 320) {               // 20 ms bei 16 kHz
      const out = Int16Array.from(this.buf).buffer;
      this.port.postMessage(out, [out]);        // Puffer übertragen statt kopieren
      this.buf = [];
    }
    return true;
  }
}
registerProcessor("pcm16-capture", Pcm16Capture);
`;

export class JarvisVoice {
  constructor({ url, token, sessionId = crypto.randomUUID(), onText = () => {}, onTranscript = () => {}, onState = () => {} }) {
    Object.assign(this, { url, token, sessionId, onText, onTranscript, onState });
    this.ctx = null;
    this.playhead = 0;
    this.outputRate = 22050;
    this.connect();
  }

  connect() {
    this.ws = new WebSocket(`${this.url}/v1/stream?token=${encodeURIComponent(this.token)}`);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (msg) => {
      if (msg.data instanceof ArrayBuffer) return this.play(msg.data);
      const ev = JSON.parse(msg.data);
      if (ev.type === "audio.format") this.outputRate = ev.rate;
      if (ev.type === "transcript.final") this.onTranscript(ev.text);
      if (ev.type === "output.text_delta") this.onText(ev.delta);
      if (ev.type === "output.final") this.onState("idle", ev);
    };
    this.ws.onclose = (ev) => {
      if (ev.code !== 4401) setTimeout(() => this.connect(), 2000);
    };
  }

  async start() {
    this.ctx ??= new AudioContext();
    await this.ctx.resume();
    this.bargeIn(); // eigene Ausgabe stoppen, sobald der Nutzer spricht
    if (!this.node) {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await this.ctx.audioWorklet.addModule(url);
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      this.source = this.ctx.createMediaStreamSource(stream);
      this.node = new AudioWorkletNode(this.ctx, "pcm16-capture");
      this.node.port.onmessage = (e) => this.recording && this.ws.send(e.data);
    }
    this.ws.send(JSON.stringify({ type: "audio.start", session_id: this.sessionId,
                                  format: { rate: 16000, width: 2, channels: 1 } }));
    this.source.connect(this.node);
    this.recording = true;
    this.onState("listening");
  }

  stop() {
    if (!this.recording) return;
    this.recording = false;
    this.source.disconnect(this.node);
    this.ws.send(JSON.stringify({ type: "audio.stop" }));
    this.onState("thinking");
  }

  play(arrayBuffer) {
    const pcm = new Int16Array(arrayBuffer);
    const buffer = this.ctx.createBuffer(1, pcm.length, this.outputRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    this.playhead = Math.max(this.playhead, this.ctx.currentTime);
    src.start(this.playhead);                   // lückenlos aneinanderreihen
    this.playhead += buffer.duration;
    (this.playing ??= new Set()).add(src);
    src.onended = () => this.playing.delete(src);
    this.onState("speaking");
  }

  bargeIn() {
    for (const src of this.playing ?? []) src.stop();
    this.playing?.clear();
    this.playhead = 0;
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "output.cancel" }));
  }
}
