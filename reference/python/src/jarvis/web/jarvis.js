// JARVIS – Browser-Oberfläche: HUD, Spracheingabe (Web Speech API), Sprachausgabe (speechSynthesis) und Chat
// über den WebSocket /v1/stream (Protokoll: docs/04-technische-umsetzung.md, Abschnitt 4.2.3).
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const els = {
    hud: $("#hud"), status: $("#status"), caption: $("#caption"), log: $("#log"),
    form: $("#composer"), input: $("#text"), mic: $("#mic-btn"), send: $("#send-btn"),
    conn: $("#conn"), connText: $("#conn-text"),
    login: $("#login"), loginForm: $("#login-form"), loginToken: $("#login-token"), loginError: $("#login-error"),
    settings: $("#settings"), settingsBtn: $("#settings-btn"), voice: $("#voice"), voiceTest: $("#voice-test"),
    optSpeak: $("#opt-speak"), optConvo: $("#opt-convo"), optLocation: $("#opt-location"), logout: $("#logout"),
    wakeToggle: $("#wake-toggle"), wakeText: $("#wake-text"), optEffect: $("#opt-effect"),
    pc: $("#pc-status"), pcText: $("#pc-text"),
  };

  // ---------------------------------------------------------------- Browser-Speicher (nur Komfort)
  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem(`jarvis.${key}`);
        return raw === null ? fallback : JSON.parse(raw);
      } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(`jarvis.${key}`, JSON.stringify(value)); } catch { /* privates Fenster */ }
    },
    remove(key) {
      try { localStorage.removeItem(`jarvis.${key}`); } catch { /* privates Fenster */ }
    },
  };

  const randomId = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");

  // Link von start.sh bzw. dem Autostart: #token=…[&wake=1] übernehmen und aus der Adresszeile entfernen
  function tokenFromLink() {
    const params = new URLSearchParams(location.hash.slice(1));
    const value = params.get("token");
    if (params.get("wake") === "1") store.set("wake", true);
    if (!params.toString()) return null;
    history.replaceState(null, "", location.pathname + location.search);
    if (value) store.set("token", value);
    return value;
  }
  let token = tokenFromLink() || store.get("token", null);
  let sessionId = store.get("session", null);
  if (!sessionId) { sessionId = `web-${randomId()}`; store.set("session", sessionId); }

  const settings = {
    speak: store.get("speak", true), convo: store.get("convo", true), voice: store.get("voice.v2", ""),
    wake: store.get("wake", false), location: store.get("location", ""), effect: store.get("effect", "dezent"),
  };

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canListen = Boolean(SpeechRecognition);
  const canSpeak = "speechSynthesis" in window;
  if (canSpeak) speechSynthesis.getVoices();  // Stimmenliste laden lassen (Chrome liefert sie verzögert)

  // ---------------------------------------------------------------- Zustand & HUD
  const STATUS = {
    offline: "Keine Verbindung zum JARVIS-Server …",
    listening: "Ich höre zu …",
    thinking: "Einen Moment …",
    speaking: "JARVIS spricht – tippen zum Unterbrechen.",
  };
  const LLM_STATUS = {
    loading: "Das Sprachmodell wird geladen – gleich bin ich bereit …",
    missing_model: "Das Sprachmodell fehlt noch – bitte ./deploy/start.sh ausführen.",
    unavailable: "Das Sprachmodell (Ollama) ist noch nicht erreichbar …",
  };
  let state = "offline";
  let llmStatus = "unknown";

  function idleText() {
    if (LLM_STATUS[llmStatus]) return LLM_STATUS[llmStatus];
    if (!canListen) return "Schreiben Sie unten eine Nachricht. (Spracheingabe: Chrome oder Edge)";
    if (settings.wake) return "Sagen Sie „Jarvis“ – oder tippen Sie auf den Kreis.";
    return "Tippen Sie auf den Kreis oder drücken Sie die Leertaste, um zu sprechen.";
  }

  function setState(next, text) {
    const previous = state;
    state = next;
    document.body.dataset.state = next;
    els.status.textContent = text ?? (next === "idle" ? idleText() : STATUS[next]);
    if (next === "speaking" && previous !== "speaking") speakingAnimation.start();
    if (next !== "speaking" && previous === "speaking") speakingAnimation.stop();
    if (next === "idle") wake.schedule();
  }

  function setLevel(value) {
    els.hud.style.setProperty("--level", value.toFixed(3));
  }

  const speakingAnimation = {
    frame: 0,
    boost: 0,
    start() {
      const tick = () => {
        this.boost *= 0.9;
        const base = 0.16 + 0.14 * Math.abs(Math.sin(performance.now() / 150));
        const live = tts.source ? voiceFx.level() : null;  // JARVIS-Stimme: echter Pegel
        setLevel(live ?? Math.max(base, this.boost));
        this.frame = requestAnimationFrame(tick);
      };
      cancelAnimationFrame(this.frame);
      tick();
    },
    stop() { cancelAnimationFrame(this.frame); this.frame = 0; setLevel(0); },
    pulse() { this.boost = 0.85; },
  };

  // ---------------------------------------------------------------- Protokoll
  const timeNow = () => new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });

  function scrollLog() { els.log.scrollTop = els.log.scrollHeight; }

  function addMessage(kind, text, meta) {
    const item = document.createElement("li");
    item.className = `msg ${kind}`;
    if (kind !== "system") {
      const head = document.createElement("div");
      head.className = "meta";
      head.textContent = meta ?? `${kind === "user" ? "Sie" : "JARVIS"} · ${timeNow()}`;
      item.append(head);
    }
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text;
    item.append(body);
    els.log.append(item);
    scrollLog();
    return item;
  }

  function addSystem(text, isError = false) {
    const item = addMessage("system", text);
    if (isError) item.classList.add("error");
    return item;
  }

  const STATUS_DE = {
    succeeded: "erledigt", failed: "fehlgeschlagen", denied: "abgelehnt",
    pending_confirmation: "wartet auf Bestätigung", rejected: "abgebrochen", timed_out: "Zeit abgelaufen",
  };

  function actionChip(action) {
    const chip = document.createElement("span");
    chip.className = "chip";
    updateChip(chip, action);
    return chip;
  }

  function updateChip(chip, action) {
    chip.dataset.actionId = action.action_id;
    chip.dataset.status = action.status;
    chip.textContent = `${action.capability} · ${STATUS_DE[action.status] ?? action.status}`;
    const reason = action.decision?.reason || action.error?.detail || action.error?.title;
    if (reason) chip.title = reason;
  }

  function onActionUpdate(action) {
    document.querySelectorAll(`.chip[data-action-id="${CSS.escape(action.action_id)}"]`)
      .forEach((chip) => updateChip(chip, action));
    document.querySelectorAll(`.confirm[data-action-id="${CSS.escape(action.action_id)}"]`).forEach((card) => {
      if (action.status !== "pending_confirmation") closeConfirmation(card, STATUS_DE[action.status] ?? action.status);
    });
  }

  function routeLabel(route) {
    if (!route) return "";
    if (route === "fast_path") return "Direktbefehl";
    if (route === "confirmation_resolver") return "Bestätigung";
    if (route.startsWith("llm:claude")) return "Claude · Cloud";
    if (route.startsWith("llm:")) return "lokales Modell";
    return route;
  }

  // ---------------------------------------------------------------- Bestätigungen
  function addConfirmation(item, pending, actionId) {
    const open = `.confirm[data-confirmation-id="${CSS.escape(pending.confirmation_id)}"]:not(.done)`;
    if (document.querySelector(open)) return;  // dieselbe Bestätigung steht schon im Protokoll
    const card = document.createElement("div");
    card.className = "confirm";
    card.dataset.confirmationId = pending.confirmation_id;
    if (actionId) card.dataset.actionId = actionId;
    const prompt = document.createElement("p");
    prompt.textContent = pending.prompt;
    const row = document.createElement("div");
    row.className = "row";
    const approve = button("Bestätigen", "btn primary", () => resolveConfirmation(card, "approve"));
    const reject = button("Ablehnen", "btn danger", () => resolveConfirmation(card, "reject"));
    const note = document.createElement("p");
    note.className = "note";
    if (pending.method === "app_biometric") {
      approve.hidden = true;  // R3: nur mit Biometrie in der App – der Browser kann das nicht nachweisen
      note.textContent = "Diese Aktion muss in der JARVIS-App mit Fingerabdruck oder Gesichtserkennung bestätigt werden.";
    } else {
      note.textContent = "Sie können auch einfach „Ja“ oder „Nein“ sagen.";
    }
    row.append(approve, reject);
    card.append(prompt, row, note);
    item.append(card);
    scrollLog();
  }

  function button(label, className, onClick) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = className;
    el.textContent = label;
    el.addEventListener("click", onClick);
    return el;
  }

  let resolvingCard = null;

  function resolveConfirmation(card, decision) {
    if (!send({ type: "confirmation.resolve", confirmation_id: card.dataset.confirmationId, decision, method: "app" })) return;
    resolvingCard = card;
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  }

  function closeConfirmation(card, outcome) {
    card.classList.add("done");
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    card.querySelector(".note").textContent = `Status: ${outcome}`;
    if (resolvingCard === card) resolvingCard = null;
  }

  // ---------------------------------------------------------------- Sprachausgabe
  function forSpeech(text) {
    return text
      .replace(/```[\s\S]*?```/g, " Den Code sehen Sie auf dem Bildschirm. ")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/https?:\/\/\S+/g, "Link")
      .replace(/^\s*(?:[-*+•]|\d+[.)])\s+/gm, "")
      .replace(/^#{1,6}\s*/gm, "")
      .replace(/[*_~>|#]/g, "")
      .replace(/\p{Extended_Pictographic}/gu, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function germanVoices() {
    if (!canSpeak) return [];
    return speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith("de"));
  }

  function pickVoice() {
    if (!canSpeak) return null;
    const voices = speechSynthesis.getVoices();
    const chosen = voices.find((v) => v.name === settings.voice);
    if (chosen) return chosen;
    const german = germanVoices();
    // JARVIS klingt männlich: bekannte männliche Stimmen zuerst, dann natürliche Online-Stimmen, dann irgendeine
    const preferred = [/conrad/i, /killian/i, /florian/i, /stefan/i, /natural/i, /google/i];
    for (const pattern of preferred) {
      const match = german.find((v) => pattern.test(v.name));
      if (match) return match;
    }
    return german[0] ?? null;
  }

  // JARVIS-Stimme über /v1/tts – Microsoft „Conrad“ (Azure) oder lokal Piper – plus Klangeffekt im Browser.
  // „dezent“ hebt nur Klarheit an und gibt einen Hauch Raum; „stark“ klingt hörbar synthetisch.
  const EFFECTS = {
    aus: { rate: 1.0, chorus: 0, reverb: 0, presence: 0 },
    dezent: { rate: 1.0, chorus: 0, reverb: 0.06, presence: 2 },
    stark: { rate: 1.0, chorus: 0.2, reverb: 0.14, presence: 4 },
  };
  const TTS_LABEL = {
    azure: "JARVIS – Microsoft Conrad (Azure)",
    piper: "JARVIS – lokale Stimme (Piper)",
    configured: "JARVIS – Serverstimme",
  };
  let ttsProvider = "off";
  let ttsConfigured = false;
  let jarvisVoiceBroken = false;

  function impulseResponse(context, seconds, decay) {
    const length = Math.floor(context.sampleRate * seconds);
    const buffer = context.createBuffer(2, length, context.sampleRate);
    for (let channel = 0; channel < 2; channel += 1) {
      const data = buffer.getChannelData(channel);
      for (let i = 0; i < length; i += 1) data[i] = (Math.random() * 2 - 1) * (1 - i / length) ** decay;
    }
    return buffer;
  }

  const voiceFx = {
    context: null, input: null, analyser: null, samples: null, nodes: null,
    ensure() {
      if (this.context) return this.context;
      const context = new AudioContext();
      const input = context.createGain();
      const highpass = context.createBiquadFilter();
      highpass.type = "highpass";
      highpass.frequency.value = 110;
      const presence = context.createBiquadFilter();
      presence.type = "peaking";
      presence.frequency.value = 3000;
      presence.Q.value = 0.9;
      const dry = context.createGain();
      const chorusDelay = context.createDelay(0.05);  // kurze, schwankende Verzögerung = synthetischer Schimmer
      chorusDelay.delayTime.value = 0.014;
      const lfo = context.createOscillator();
      lfo.frequency.value = 0.7;
      const lfoDepth = context.createGain();
      lfoDepth.gain.value = 0.0018;
      lfo.connect(lfoDepth).connect(chorusDelay.delayTime);
      lfo.start();
      const chorus = context.createGain();
      const convolver = context.createConvolver();  // kurzer, heller Raum
      convolver.buffer = impulseResponse(context, 0.9, 3.2);
      const reverb = context.createGain();
      const compressor = context.createDynamicsCompressor();
      compressor.threshold.value = -20;
      compressor.ratio.value = 3;
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      input.connect(highpass).connect(presence);
      presence.connect(dry).connect(compressor);
      presence.connect(chorusDelay).connect(chorus).connect(compressor);
      presence.connect(convolver).connect(reverb).connect(compressor);
      compressor.connect(analyser).connect(context.destination);
      Object.assign(this, { context, input, analyser, samples: new Uint8Array(analyser.fftSize),
                            nodes: { presence, dry, chorus, reverb } });
      this.apply();
      return context;
    },
    apply() {
      if (!this.nodes) return;
      const fx = EFFECTS[settings.effect] ?? EFFECTS.dezent;
      this.nodes.presence.gain.value = fx.presence;
      this.nodes.chorus.gain.value = fx.chorus;
      this.nodes.reverb.gain.value = fx.reverb;
      this.nodes.dry.gain.value = 1 - fx.chorus * 0.4;
    },
    level() {
      if (!this.analyser) return null;
      this.analyser.getByteTimeDomainData(this.samples);
      let sum = 0;
      for (const sample of this.samples) { const x = (sample - 128) / 128; sum += x * x; }
      return Math.min(1, Math.sqrt(sum / this.samples.length) * 4);
    },
  };

  // Natürliche Neural-Stimmen des Browsers (Edge: „Microsoft Conrad Online (Natural)“) – kostenlos, sehr gut
  const naturalBrowserVoice = () => germanVoices().find(
    (v) => /conrad|killian|florian/i.test(v.name) && /natural|online/i.test(v.name)) ?? null;

  function useJarvisVoice() {  // Automatik: Azure-Conrad > natürliche Browserstimme > Piper > Browserstimme
    if (!ttsConfigured || jarvisVoiceBroken) return false;
    if (settings.voice === "jarvis") return true;
    if (settings.voice !== "") return false;
    return ttsProvider === "azure" || !naturalBrowserVoice();
  }

  async function fetchSpeech(text) {
    const response = await fetch("/v1/tts", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 1000) }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return voiceFx.ensure().decodeAudioData(await response.arrayBuffer());
  }

  function jarvisVoiceFailed() {
    if (jarvisVoiceBroken) return;
    jarvisVoiceBroken = true;
    addSystem("Die JARVIS-Stimme (Piper) ist gerade nicht erreichbar – ich spreche vorerst mit der Browserstimme.", true);
  }

  const tts = {
    generation: 0,
    pending: 0,
    chain: Promise.resolve(),
    source: null,
    speak(text) {
      const clean = forSpeech(text);
      if (!clean || !settings.speak) return;
      if (useJarvisVoice()) this.speakJarvis(clean);
      else this.speakBrowser(clean);
    },
    done(generation) {  // eine Äußerung ist fertig
      if (generation !== this.generation) return;
      this.pending = Math.max(0, this.pending - 1);
      if (this.pending === 0) onSpeechDone();
    },
    speakJarvis(clean) {
      const generation = this.generation;
      this.pending += 1;
      wake.stop();  // nicht zuhören, während JARVIS spricht – sonst hört er sich selbst
      const audio = fetchSpeech(clean);  // Synthese startet sofort, parallel zur Wiedergabe des Satzes davor
      audio.catch(() => {});
      this.chain = this.chain.then(async () => {
        if (generation !== this.generation) return;
        try {
          const buffer = await audio;
          if (generation === this.generation) await this.play(buffer);
        } catch {
          if (generation !== this.generation) return;
          jarvisVoiceFailed();
          this.speakBrowser(clean);  // Rückfall für diesen Satz
        }
      }).finally(() => this.done(generation));
    },
    play(buffer) {
      const context = voiceFx.ensure();
      voiceFx.apply();
      return new Promise((resolve) => {
        const fx = EFFECTS[settings.effect] ?? EFFECTS.dezent;
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.playbackRate.value = fx.rate;  // etwas langsamer und tiefer
        source.connect(voiceFx.input);
        let finished = false;
        const end = () => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          if (this.source === source) this.source = null;
          resolve();
        };
        const timer = setTimeout(end, (buffer.duration / fx.rate) * 1000 + 3000);
        source.onended = end;
        this.source = source;
        if (context.state === "suspended") {
          context.resume().catch(() => {});
          setTimeout(() => { if (context.state === "suspended") explainBlockedSpeech(); }, 600);
        }
        setState("speaking");
        source.start();
      });
    },
    speakBrowser(clean) {
      if (!canSpeak) return;
      const generation = this.generation;
      const utterance = new SpeechSynthesisUtterance(clean);
      const voice = pickVoice();
      utterance.lang = voice?.lang ?? "de-DE";
      if (voice) utterance.voice = voice;
      utterance.rate = 1.03;
      utterance.pitch = 0.9;
      let settled = false;
      // Chrome verschluckt gelegentlich „end“ – Sicherheitsnetz nach geschätzter Sprechdauer
      const watchdog = setTimeout(() => finish(), 8000 + clean.length * 110);
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(watchdog);
        this.done(generation);
      };
      utterance.onstart = () => { if (generation === this.generation) setState("speaking"); };
      utterance.onboundary = () => speakingAnimation.pulse();
      utterance.onend = finish;
      utterance.onerror = (event) => {
        if (event.error === "not-allowed") explainBlockedSpeech();
        finish();
      };
      this.pending += 1;
      wake.stop();
      speechSynthesis.speak(utterance);
    },
    stop() {
      this.generation += 1;
      this.pending = 0;
      try { this.source?.stop(); } catch { /* schon beendet */ }
      this.source = null;
      if (canSpeak) speechSynthesis.cancel();
    },
    get busy() { return this.pending > 0; },
  };

  // Browser erlauben Ton erst nach einer Interaktion – beim ersten Klick/Tastendruck freischalten
  ["pointerdown", "keydown"].forEach((type) => document.addEventListener(type, () => {
    if (voiceFx.context?.state === "suspended") voiceFx.context.resume().catch(() => {});
  }));

  let speechHintShown = false;

  function explainBlockedSpeech() {  // Chrome spricht erst nach einer ersten Interaktion mit der Seite
    if (speechHintShown) return;
    speechHintShown = true;
    addSystem("Damit JARVIS sprechen darf, klicken Sie einmal irgendwo auf diese Seite.", true);
  }

  // Nach „Jarvis“ antwortet JARVIS kurz („Ja, Sir?“) – mit der JARVIS-Stimme vorab geladen, damit es sofort klingt.
  // Ohne Sprachausgabe bleibt der kurze Ton.
  const ACKS = ["Ja, Sir?", "Sir?", "Ja, Sir?"];
  const acknowledgement = {
    cache: new Map(), preparing: false,
    async prepare() {
      if (this.preparing || this.cache.size || !useJarvisVoice()) return;
      this.preparing = true;
      for (const text of new Set(ACKS)) {
        try { this.cache.set(text, await fetchSpeech(text)); } catch { break; }
      }
      this.preparing = false;
    },
    say(then) {
      let finished = false;
      const done = () => { if (!finished) { finished = true; then(); } };
      const text = ACKS[Math.floor(Math.random() * ACKS.length)];
      if (!settings.speak) { chime(); setTimeout(done, 200); return; }
      const buffer = this.cache.get(text);
      if (buffer && useJarvisVoice()) {
        tts.play(buffer).then(done, done);
        return;
      }
      if (canSpeak) {
        const utterance = new SpeechSynthesisUtterance(text);
        const voice = pickVoice();
        utterance.lang = voice?.lang ?? "de-DE";
        if (voice) utterance.voice = voice;
        utterance.pitch = 0.9;
        utterance.onend = done;
        utterance.onerror = done;
        setState("speaking");
        speechSynthesis.speak(utterance);
        setTimeout(done, 2500);  // Sicherheitsnetz, falls „end“ ausbleibt
        return;
      }
      chime();
      setTimeout(done, 200);
    },
  };

  function chime() {  // kurzer Ton: „Jarvis“ wurde gehört (ohne Sprachausgabe)
    try {
      chime.context = chime.context || new AudioContext();
      const context = chime.context;
      if (context.state === "suspended") context.resume().catch(() => {});
      const start = context.currentTime;
      [[880, 0], [1320, 0.09]].forEach(([frequency, offset]) => {
        const osc = context.createOscillator();
        const gain = context.createGain();
        osc.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, start + offset);
        gain.gain.exponentialRampToValueAtTime(0.18, start + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.16);
        osc.connect(gain).connect(context.destination);
        osc.start(start + offset);
        osc.stop(start + offset + 0.18);
      });
    } catch { /* ohne Ton */ }
  }

  // Satzweise sprechen, während der Text noch gestreamt wird
  // Einzelbuchstaben („z. B.“, „d. h.“), gängige Abkürzungen und Ordnungszahlen („3.“) beenden keinen Satz
  const ABBREVIATION = /(?:(?:^|[\s(„"])\p{L}|\b(?:bzw|ca|usw|etc|nr|dr|hr|fr|st|vgl|ggf|inkl|evtl|bspw|mio|mrd|min|std))\.$|\d\.$|(?:\p{L}\.){2,}$/iu;

  function sentenceEnd(text) {
    const boundary = /[.!?…]+(?=\s)|\n/g;
    let match;
    while ((match = boundary.exec(text))) {
      const end = match.index + match[0].length;
      if (match[0] !== "\n" && ABBREVIATION.test(text.slice(0, end))) continue;
      return end;
    }
    return -1;
  }

  class SentenceStream {
    constructor(onSentence) { this.buffer = ""; this.onSentence = onSentence; }
    push(delta) {
      this.buffer += delta;
      let end;
      while ((end = sentenceEnd(this.buffer)) > 0) {
        const sentence = this.buffer.slice(0, end);
        this.buffer = this.buffer.slice(end);
        if (sentence.trim()) this.onSentence(sentence);
      }
    }
    flush() {
      const rest = this.buffer;
      this.buffer = "";
      if (rest.trim()) this.onSentence(rest);
    }
  }

  // ---------------------------------------------------------------- Gesprächsrunde
  let turn = null;           // aktuelle Anfrage an JARVIS
  let lastTurnSpoken = false;
  let expectReply = false;  // JARVIS hat nachgefragt („Wonach soll ich suchen?“)

  function sendText(raw, spoken) {
    const text = raw.trim();
    if (!text) return;
    if (turn) { els.status.textContent = "Einen Moment, ich antworte noch auf die letzte Frage …"; return; }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addSystem("Keine Verbindung zum Server – ich versuche es weiter.", true);
      return;
    }
    tts.stop();
    wake.stop();
    addMessage("user", text);
    const item = addMessage("jarvis", "");
    item.classList.add("pending");
    turn = {
      spoken, item, body: item.querySelector(".body"), streamed: "", muted: false,
      slowTimer: setTimeout(() => {
        if (state === "thinking") setState("thinking", "Das Sprachmodell rechnet noch – beim ersten Mal kann das eine Weile dauern …");
      }, 12000),
    };
    turn.sentences = new SentenceStream((sentence) => { if (!turn?.muted) tts.speak(sentence); });
    updateComposer();
    setState("thinking");
    // Wenn JARVIS spricht, bittet der Kanal „voice“ um kurze, gesprochene Antworten ohne Markdown
    send({ type: "input.text", text, session_id: sessionId, channel: settings.speak ? "voice" : "web",
           location: settings.location || undefined });
  }

  function onDelta(delta) {
    if (!turn) return;
    turn.streamed += delta;
    turn.body.textContent = turn.streamed;
    turn.sentences.push(delta);
    scrollLog();
  }

  function onFinal(result) {
    if (!turn) return;
    const current = endTurn();
    // Angezeigt wird die Endfassung des Servers – sie ist vollständig durch den Jarvis-Formatter gelaufen.
    // Gesprochen wurde bereits satzweise während des Streamings (ebenfalls formatiert).
    const finalText = (result.text || "").trim();
    const streamed = current.streamed.trim();
    current.body.textContent = finalText || streamed || "(keine Antwort)";

    if (!current.muted) {
      current.sentences.flush();
      if (!streamed) tts.speak(finalText);
    }

    const meta = current.item.querySelector(".meta");
    const route = routeLabel(result.route);
    if (route) meta.textContent += ` · ${route}`;

    const actions = result.actions ?? [];
    if (actions.length) {
      const chips = document.createElement("div");
      chips.className = "chips";
      actions.forEach((action) => chips.append(actionChip(action)));
      current.item.append(chips);
      actions.forEach((action) => onActionUpdate(action));  // per Sprache aufgelöste Bestätigungen schließen
    }
    if (result.pending_confirmation) {
      const waiting = actions.find((a) => a.status === "pending_confirmation");
      addConfirmation(current.item, result.pending_confirmation, waiting?.action_id);
    }
    expectReply = Boolean(result.awaiting_reply) && current.spoken && canListen;
    scrollLog();
    if (!tts.busy) onSpeechDone();
  }

  function failTurn(message) {
    if (!turn) return;
    const current = endTurn();
    current.item.classList.add("error");
    current.body.textContent = current.streamed ? `${current.streamed}\n\n${message}` : message;
    tts.stop();
    tts.speak(message);
    if (!tts.busy) onSpeechDone();
  }

  function endTurn() {
    const current = turn;
    turn = null;
    clearTimeout(current.slowTimer);
    current.item.classList.remove("pending");
    lastTurnSpoken = current.spoken;
    updateComposer();
    return current;
  }

  function onSpeechDone() {
    if (turn) { setState("thinking"); return; }        // Antwort läuft noch, nächster Satz kommt
    if (!ws || ws.readyState !== WebSocket.OPEN) { setState("offline"); return; }
    if (((lastTurnSpoken && settings.convo) || expectReply) && canListen && state !== "listening") {
      lastTurnSpoken = false;
      expectReply = false;
      setTimeout(() => { if (state !== "listening" && !turn) startListening(); }, 300);
      setState("idle");
      return;
    }
    setState("idle");
  }

  function updateComposer() {
    els.send.disabled = Boolean(turn);
  }

  // ---------------------------------------------------------------- Spracheingabe
  let recognition = null;

  const meter = {
    stream: null, context: null, frame: 0,
    async start() {
      if (!navigator.mediaDevices?.getUserMedia) return;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
        if (state !== "listening") { stream.getTracks().forEach((t) => t.stop()); return; }
        this.stream = stream;
        this.context = new AudioContext();
        const analyser = this.context.createAnalyser();
        analyser.fftSize = 512;
        this.context.createMediaStreamSource(stream).connect(analyser);
        const samples = new Uint8Array(analyser.fftSize);
        const tick = () => {
          analyser.getByteTimeDomainData(samples);
          let sum = 0;
          for (const s of samples) { const x = (s - 128) / 128; sum += x * x; }
          setLevel(Math.min(1, Math.sqrt(sum / samples.length) * 5));
          this.frame = requestAnimationFrame(tick);
        };
        tick();
      } catch { /* ohne Pegelanzeige weiter */ }
    },
    stop() {
      cancelAnimationFrame(this.frame);
      this.frame = 0;
      this.stream?.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.context?.close().catch(() => {});
      this.context = null;
      setLevel(0);
    },
  };

  const RECOGNITION_ERRORS = {
    "not-allowed": "Das Mikrofon ist blockiert. Klicken Sie links in der Adressleiste auf das Schloss- bzw. Einstellungssymbol und erlauben Sie das Mikrofon.",
    "service-not-allowed": "Der Browser erlaubt hier keine Spracherkennung. Bitte Chrome oder Edge verwenden.",
    "audio-capture": "Kein Mikrofon gefunden. Ist eines angeschlossen und in Windows freigegeben?",
    network: "Die Spracherkennung des Browsers braucht eine Internetverbindung.",
    "language-not-supported": "Deutsch wird von der Spracherkennung dieses Browsers nicht unterstützt.",
  };

  function startListening() {
    if (!canListen) {
      addSystem("Spracheingabe funktioniert in Chrome und Edge. Hier bitte unten tippen.");
      els.input.focus();
      return;
    }
    if (recognition || turn) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) { addSystem("Noch keine Verbindung zum Server.", true); return; }
    if (wake.rec) {  // Chrome erlaubt nur eine Erkennung zur Zeit: erst die Dauer-Erkennung beenden
      wake.stop();
      setTimeout(startListening, 300);
      return;
    }
    tts.stop();
    let heard = "";
    let failure = null;
    let settle = 0;
    const rec = new SpeechRecognition();
    rec.lang = "de-DE";
    rec.interimResults = true;
    rec.continuous = true;  // Pausen im Satz beenden die Aufnahme nicht – das entscheidet settleDelay()
    rec.maxAlternatives = 1;
    const quiet = setTimeout(() => { if (!heard.trim()) rec.stop(); }, 7000);  // gar nichts gesagt
    rec.onstart = () => { setState("listening", listenHint); listenHint = undefined; meter.start(); };
    rec.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) heard += ` ${result[0].transcript}`;
        else interim += result[0].transcript;
      }
      els.caption.textContent = `${heard} ${interim}`.trim();
      clearTimeout(settle);  // spricht noch – erst nach einer Pause abschicken
      settle = setTimeout(() => rec.stop(), interim ? SETTLE_OPEN_MS + 700 : settleDelay(heard));
    };
    rec.onerror = (event) => { failure = event.error; };
    rec.onend = () => {
      clearTimeout(settle);
      clearTimeout(quiet);
      recognition = null;
      meter.stop();
      els.caption.textContent = "";
      if (heard.trim()) { sendText(heard.trim(), true); return; }
      if (state === "listening") setState(ws?.readyState === WebSocket.OPEN ? "idle" : "offline");
      if (failure === "no-speech" && !settings.wake) {
        els.status.textContent = "Ich habe nichts gehört. Tippen Sie auf den Kreis, um es erneut zu versuchen.";
      }
      else if (failure && RECOGNITION_ERRORS[failure]) addSystem(RECOGNITION_ERRORS[failure], true);
    };
    recognition = rec;
    try {
      rec.start();
    } catch (err) {
      recognition = null;
      addSystem(`Spracheingabe konnte nicht starten: ${err.message}`, true);
    }
  }

  function stopListening() { recognition?.stop(); }

  // Sprechpausen: Chrome meldet schon nach kurzem Zögern ein „fertiges“ Teilstück. Abgeschickt wird erst nach dieser
  // Stille – länger, wenn der Satz hörbar weitergeht („Such mir nach …“, „… auf“).
  const SETTLE_MS = 1300;
  const SETTLE_OPEN_MS = 2800;
  const OPEN_END = /(?:^|\s)(?:nach|auf|zu|zum|zur|von|vom|für|mit|und|oder|bei|in|im|mir|mal|den|die|das|der|dem|des|ein|eine|einen|einem|über|an|am|namens|such|suche|öffne|spiel|zeig|schreib|tippe?|schließe?)$/i;
  const settleDelay = (text) => (OPEN_END.test(text.trim()) ? SETTLE_OPEN_MS : SETTLE_MS);
  let listenHint;

  function onHudActivate() {
    if (recognition) { stopListening(); return; }
    if (turn) {  // Antwort läuft: Stimme stummschalten, Text läuft weiter ins Protokoll
      turn.muted = true;
      tts.stop();
      setState("thinking");
      return;
    }
    if (tts.busy) tts.stop();
    startListening();
  }

  // ---------------------------------------------------------------- Aktivierungswort „Jarvis“
  // Dauer-Erkennung über den Browser (Chrome/Edge senden das Audio dafür an Google bzw. Microsoft). Läuft nur,
  // solange JARVIS bereit ist – nicht während er denkt, spricht oder einem Befehl zuhört.
  // Aktivierungswort unscharf erkennen: Die Spracherkennung schreibt „Jarvis“ oft als „Jarwis“, „Javis“, „Jervis“,
  // „Charvis“ oder in zwei Silben („Jar wies“). Verglichen wird eine Lautschrift mit höchstens einem Unterschied.
  const hasWords = (text) => /[\p{L}\p{N}]{2,}/u.test(text);
  const phonetic = (word) => word.toLowerCase()
    .replace(/^(?:dsch|tsch|sch|ch|dj|dz|j|y|g(?=[ae]))/, "j")
    .replace(/ph|w|f/g, "v").replace(/[zßc]/g, "s").replace(/ie/g, "i").replace(/e(?=r)/g, "a")
    .replace(/h/g, "").replace(/(.)\1+/g, "$1");
  function distance(a, b) {
    const row = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i += 1) {
      let previous = row[0];
      row[0] = i;
      for (let j = 1; j <= b.length; j += 1) {
        const current = row[j];
        row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1));
        previous = current;
      }
    }
    return row[b.length];
  }
  function findWake(text) {  // -> Textende des Aktivierungsworts oder -1
    const words = [...text.matchAll(/\p{L}+/gu)];
    for (let i = 0; i < words.length; i += 1) {
      const single = words[i];
      const pair = words[i + 1] && `${single[0]}${words[i + 1][0]}`;
      for (const [candidate, end] of [[single[0], single.index + single[0].length],
                                      [pair, pair && words[i + 1].index + words[i + 1][0].length]]) {
        if (!candidate) continue;
        const sound = phonetic(candidate);
        if (sound.length >= 4 && sound.length <= 8 && sound[0] === "j" && distance(sound, "jarvis") <= 1) return end;
      }
    }
    return -1;
  }
  window.jarvisFindWake = findWake;  // für Tests

  const wake = {
    rec: null, timer: 0, settle: 0, failures: 0, collecting: false, buffer: "",
    wanted() {
      return settings.wake && canListen && state === "idle" && !turn && !recognition && !tts.busy
        && ws?.readyState === WebSocket.OPEN;
    },
    schedule(delay = 300) {
      clearTimeout(this.timer);
      if (settings.wake) this.timer = setTimeout(() => this.start(), delay);
    },
    start() {
      if (this.rec || !this.wanted()) return;
      const rec = new SpeechRecognition();
      rec.lang = "de-DE";
      rec.continuous = true;
      rec.interimResults = true;
      rec.maxAlternatives = 3;  // „Jarvis“ steht oft nur in einer der Alternativen
      const startedAt = Date.now();
      let failure = null;
      rec.onresult = (event) => {
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const result = event.results[i];
          if (this.collecting) {  // Befehl im selben Atemzug („Jarvis, such mir nach …“): Pausen abwarten
            this.collect(result[0].transcript.trim(), result.isFinal);
            continue;
          }
          let hit = null;
          for (let k = 0; k < result.length && !hit; k += 1) {
            const text = result[k].transcript.trim();
            const end = findWake(text);
            if (end >= 0) hit = { text, end };
          }
          if (!hit) continue;
          this.failures = 0;
          if (!result.isFinal) { document.body.classList.add("wake-heard"); continue; }
          const command = hit.text.slice(hit.end).replace(/^[\s,.!?:;-]+/, "");
          if (hasWords(command)) {
            this.collecting = true;
            this.collect(command, true);
            continue;
          }
          this.acknowledge();  // nur „Jarvis“: „Ja, Sir?“ – dann zuhören
          return;
        }
      };
      rec.onerror = (event) => { failure = event.error; };
      rec.onend = () => {
        if (this.rec !== rec) return;  // bewusst gestoppt
        this.rec = null;
        if (this.collecting && hasWords(this.buffer)) { this.hand(this.buffer); return; }
        this.reset();
        if (["not-allowed", "service-not-allowed", "audio-capture"].includes(failure)) {
          setWake(false);
          addSystem(RECOGNITION_ERRORS[failure], true);
          return;
        }
        // Chrome beendet die Dauer-Erkennung regelmäßig (Stille, Netz) – neu starten, bei schnellen Abbrüchen
        // mit wachsender Pause
        const quick = Date.now() - startedAt < 3000;
        this.failures = quick ? this.failures + 1 : 0;
        if (failure === "network" && this.failures === 3) addSystem(RECOGNITION_ERRORS.network, true);
        if (state === "listening") setState("idle");
        this.schedule(quick ? Math.min(30000, 500 * 2 ** this.failures) : 250);
      };
      this.rec = rec;
      try {
        rec.start();
      } catch {
        this.rec = null;
        this.schedule(2000);
      }
    },
    collect(text, final) {
      document.body.classList.remove("wake-heard");
      if (state !== "listening") setState("listening");
      clearTimeout(this.settle);
      if (final) {
        this.buffer = `${this.buffer} ${text}`.trim();
        els.caption.textContent = this.buffer;
        this.settle = setTimeout(() => this.hand(this.buffer), settleDelay(this.buffer));
      } else {
        els.caption.textContent = `${this.buffer} ${text}`.trim();
        this.settle = setTimeout(() => this.hand(`${this.buffer} ${text}`.trim()), SETTLE_OPEN_MS + 700);
      }
    },
    acknowledge() {
      this.stop();
      document.body.classList.remove("wake-heard");
      acknowledgement.say(() => {
        if (turn || recognition) return;
        listenHint = "Ja? Ich höre …";
        startListening();
      });
    },
    hand(command) {  // Befehl an JARVIS übergeben
      this.stop();
      els.caption.textContent = "";
      if (hasWords(command)) sendText(command, true);
      else setState("idle");
    },
    reset() {
      this.collecting = false;
      this.buffer = "";
      clearTimeout(this.settle);
      document.body.classList.remove("wake-heard");
    },
    stop() {
      clearTimeout(this.timer);
      this.reset();
      const rec = this.rec;
      this.rec = null;
      if (rec) {
        rec.onresult = null;
        rec.onend = null;
        rec.abort();
      }
    },
  };

  function setWake(on) {
    settings.wake = on;
    store.set("wake", on);
    els.wakeToggle.setAttribute("aria-pressed", String(on));
    els.wakeText.textContent = on ? "Hört auf „Jarvis“" : "„Jarvis“-Aktivierung aus";
    if (on) {
      if (state === "idle") setState("idle");
    } else {
      wake.stop();
      if (state === "listening" && !recognition) setState("idle");
      else if (state === "idle") setState("idle");
    }
  }

  // ---------------------------------------------------------------- Status des Sprachmodells
  let healthTimer = 0;

  async function checkHealth() {
    clearTimeout(healthTimer);
    try {
      const response = await fetch("/v1/system/health", { cache: "no-store" });
      const health = await response.json();
      llmStatus = health.local_llm ?? "ready";
      ttsProvider = health.tts ?? "off";
      ttsConfigured = ttsProvider !== "off";
      setPcStatus(health.pc_agent === "connected");
      acknowledgement.prepare();
    } catch {
      llmStatus = "unknown";
    }
    if (state === "idle") setState("idle");
    if (ws) healthTimer = setTimeout(checkHealth, LLM_STATUS[llmStatus] ? 3000 : 30000);
  }

  function setPcStatus(connected) {
    els.pc.dataset.conn = connected ? "online" : "none";
    els.pc.title = connected ? "PC-Steuerung verbunden: Programme, Ordner und Webseiten öffnen"
      : "PC-Steuerung nicht verbunden – JARVIS über die Desktop-Verknüpfung starten (./deploy/start.sh autostart)";
  }

  // ---------------------------------------------------------------- Verbindung
  let ws = null;
  let reconnectDelay = 500;
  let reconnectTimer = 0;
  let greeted = false;

  function setConnection(kind, text) {
    els.conn.dataset.conn = kind;
    els.connText.textContent = text;
  }

  function send(message) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addSystem("Keine Verbindung zum Server.", true);
      return false;
    }
    ws.send(JSON.stringify(message));
    return true;
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (!token) { setConnection("offline", "Nicht angemeldet"); showLogin(); return; }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/v1/stream?token=${encodeURIComponent(token)}`);
    ws = socket;
    setConnection("connecting", "Verbinde …");
    socket.addEventListener("open", () => {
      reconnectDelay = 500;
      jarvisVoiceBroken = false;
      setConnection("online", "Online");
      if (state === "offline") setState("idle");
      checkHealth();
      if (!greeted) {
        greeted = true;
        addSystem("JARVIS ist online. Sprechen Sie mit mir oder schreiben Sie unten.");
      }
    });
    socket.addEventListener("message", (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      onServerMessage(message);
    });
    socket.addEventListener("close", (event) => {
      if (ws !== socket) return;
      ws = null;
      if (turn) failTurn("Die Verbindung zum Server wurde unterbrochen.");
      if (recognition) recognition.abort();
      wake.stop();
      setState("offline");
      if (event.code === 4401) {  // Token ungültig: nicht erneut verbinden, neu anmelden
        setConnection("offline", "Nicht angemeldet");
        token = null;
        store.remove("token");
        showLogin("Dieses Token wurde nicht akzeptiert. Bitte prüfen Sie es.");
        return;
      }
      setConnection("offline", "Offline – verbinde erneut …");
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    });
  }

  function onServerMessage(message) {
    switch (message.type) {
      case "output.text_delta": onDelta(message.delta ?? ""); break;
      case "output.final": onFinal(message); break;
      case "action.update": onActionUpdate(message.action); break;
      case "notification": onNotification(message); break;
      case "error": onServerError(message.error ?? {}); break;
      default: break;
    }
  }

  // Timer abgelaufen, Erinnerung, Termin: JARVIS meldet sich von selbst – auch mitten in einer Antwort
  function onNotification(message) {
    const text = (message.text || "").trim();
    if (!text) return;
    const item = addMessage("jarvis", text, `JARVIS · ${timeNow()} · ${NOTICE_LABELS[message.kind] ?? "Hinweis"}`);
    item.classList.add("notice");
    if (settings.speak && !turn?.muted) {
      if (!tts.busy && !turn) wake.stop();
      chime();
      setTimeout(() => tts.speak(text), 350);
    }
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      try { new Notification("JARVIS", { body: text, icon: "favicon.svg" }); } catch { /* ohne Desktop-Hinweis */ }
    }
  }
  const NOTICE_LABELS = { timer: "Timer", reminder: "Erinnerung", event: "Termin" };

  function onServerError(problem) {
    const text = problem.user_message || problem.detail || problem.title || "Unbekannter Fehler";
    if (turn) { failTurn(text); return; }
    if (resolvingCard) {
      const card = resolvingCard;
      resolvingCard = null;
      card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      card.querySelector(".note").textContent = text;
      return;
    }
    addSystem(text, true);
  }

  // ---------------------------------------------------------------- Dialoge
  function showLogin(error) {
    els.loginError.hidden = !error;
    els.loginError.textContent = error ?? "";
    if (!els.login.open) els.login.showModal();
    els.loginToken.focus();
  }

  els.loginForm.addEventListener("submit", (event) => {
    const value = els.loginToken.value.trim().replace(/^bearer\s+/i, "");
    if (!value) { event.preventDefault(); return; }
    token = value;
    store.set("token", value);
    els.loginToken.value = "";
    connect();
  });
  els.login.addEventListener("cancel", (event) => { if (!token) event.preventDefault(); });

  function fillVoices() {
    const jarvis = useJarvisVoice();
    const auto = jarvis ? TTS_LABEL[ttsProvider] ?? TTS_LABEL.configured : pickVoice()?.name ?? "Browserstimme";
    els.voice.replaceChildren(new Option(`Automatisch (jetzt: ${auto})`, "", false, settings.voice === ""));
    if (ttsConfigured) {
      els.voice.append(new Option(TTS_LABEL[ttsProvider] ?? TTS_LABEL.configured, "jarvis", false,
                                  settings.voice === "jarvis"));
    }
    germanVoices().forEach((voice) => {
      const label = `${voice.name}${voice.localService ? "" : " (online)"}`;
      els.voice.append(new Option(label, voice.name, false, settings.voice === voice.name));
    });
    els.voice.disabled = els.voice.options.length < 2;
    els.optEffect.value = settings.effect;
    els.optEffect.disabled = !jarvis;
  }

  els.settingsBtn.addEventListener("click", () => {
    els.optSpeak.checked = settings.speak;
    els.optConvo.checked = settings.convo;
    els.optConvo.disabled = !canListen;
    els.optLocation.value = settings.location;
    fillVoices();
    els.settings.showModal();
  });
  els.optSpeak.addEventListener("change", () => {
    settings.speak = els.optSpeak.checked;
    store.set("speak", settings.speak);
    if (!settings.speak) tts.stop();
  });
  els.optConvo.addEventListener("change", () => { settings.convo = els.optConvo.checked; store.set("convo", settings.convo); });
  els.voice.addEventListener("change", () => {
    settings.voice = els.voice.value;
    store.set("voice.v2", settings.voice);
    els.optEffect.disabled = !useJarvisVoice();
  });
  els.optEffect.addEventListener("change", () => {
    settings.effect = els.optEffect.value;
    store.set("effect", settings.effect);
    voiceFx.apply();
  });
  els.optLocation.addEventListener("change", () => {
    settings.location = els.optLocation.value.trim();
    store.set("location", settings.location);
  });
  els.voiceTest.addEventListener("click", () => {
    const speakSetting = settings.speak;
    settings.speak = true;
    tts.stop();
    tts.speak("Guten Tag. Alle Systeme sind einsatzbereit.");
    settings.speak = speakSetting;
  });
  els.logout.addEventListener("click", () => {
    store.remove("token");
    token = null;
    els.settings.close();
    const socket = ws;
    ws = null;
    socket?.close();
    wake.stop();
    setConnection("offline", "Nicht angemeldet");
    setState("offline");
    showLogin();
  });
  if (canSpeak) speechSynthesis.addEventListener("voiceschanged", () => { if (els.settings.open) fillVoices(); });

  // ---------------------------------------------------------------- Bedienung
  els.hud.addEventListener("click", onHudActivate);
  els.wakeToggle.hidden = !canListen;
  els.wakeToggle.addEventListener("click", () => setWake(!settings.wake));
  els.mic.addEventListener("click", onHudActivate);
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = els.input.value;
    if (!text.trim() || turn) return;
    els.input.value = "";
    sendText(text, false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest?.("input, select, textarea, button, dialog")) return;
    if (event.code === "Space" && !event.repeat) { event.preventDefault(); onHudActivate(); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
    if (recognition) recognition.abort();
    if (wake.collecting) { wake.reset(); els.caption.textContent = ""; }
    if (turn) { turn.muted = true; tts.stop(); return; }
    tts.stop();
    if (state !== "offline") setState("idle");
  });

  window.addEventListener("hashchange", () => {  // Link mit Token in einem bereits offenen Tab
    const value = tokenFromLink();
    setWake(store.get("wake", settings.wake));
    if (!value) return;
    token = value;
    if (els.login.open) els.login.close();
    const socket = ws;
    ws = null;
    socket?.close();
    connect();
  });

  updateComposer();
  setWake(settings.wake);
  setState("offline", "Verbinde …");
  connect();
})();
