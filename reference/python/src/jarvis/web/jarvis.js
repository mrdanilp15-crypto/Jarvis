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
    wakeToggle: $("#wake-toggle"), wakeText: $("#wake-text"), optEffect: $("#opt-effect"), optStt: $("#opt-stt"),
    sttState: $("#stt-state"),
    pc: $("#pc-status"), pcText: $("#pc-text"), homeChip: $("#home-chip"), homeChipText: $("#home-chip-text"),
    homeNow: $("#home-now"), homeError: $("#home-error"), homeSetup: $("#home-setup"), homeFound: $("#home-found"),
    homeUrl: $("#home-url"), homeSearch: $("#home-search"), homeLogin: $("#home-login"), homeToken: $("#home-token"),
    homeTokenSave: $("#home-token-save"), homeConnected: $("#home-connected"), homeRooms: $("#home-rooms"),
    homeRemove: $("#home-remove"), camChip: $("#cam-chip"), optPresence: $("#opt-presence"),
    optPresenceMin: $("#opt-presence-min"), homeRoutines: $("#home-routines"), homeRoutinesEmpty: $("#home-routines-empty"),
    heard: $("#heard"), heardText: $("#heard-text"), heardLearn: $("#heard-learn"),
    wakeTrain: $("#wake-train"), wakeForget: $("#wake-forget"), wakeTrainStatus: $("#wake-train-status"),
    clockTime: $("#clock-time"), clockDate: $("#clock-date"), weatherChip: $("#weather-chip"),
    modelChip: $("#model-chip"), modelText: $("#model-text"), ambient: $("#ambient"), cards: $("#cards"),
    optFx: $("#opt-fx"), tabs: [...document.querySelectorAll('.tabs [role="tab"]')],
    aiNow: $("#ai-now"), aiError: $("#ai-error"), localModels: $("#local-models"), pullProgress: $("#pull-progress"),
    pullBar: $("#pull-bar"), pullText: $("#pull-text"), customModel: $("#custom-model"), customPull: $("#custom-pull"),
    claudeState: $("#claude-state"), claudeKey: $("#claude-key"), claudeSave: $("#claude-save"),
    claudeHttp: $("#claude-http"), claudeError: $("#claude-error"), claudeRemove: $("#claude-remove"),
    claudeModel: $("#claude-model"), llmModes: $("#llm-modes"),
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
    if (params.get("home")) homeReturn = params.get("home");  // Rückkehr von der Home-Assistant-Anmeldung
    if (!params.toString()) return null;
    history.replaceState(null, "", location.pathname + location.search);
    if (value) store.set("token", value);
    return value;
  }
  let homeReturn = null;
  let token = tokenFromLink() || store.get("token", null);
  let sessionId = store.get("session", null);
  if (!sessionId) { sessionId = `web-${randomId()}`; store.set("session", sessionId); }

  const settings = {
    speak: store.get("speak", true), convo: store.get("convo", true), voice: store.get("voice.v2", ""),
    wake: store.get("wake", false), location: store.get("location", ""), effect: store.get("effect", "dezent"),
    fx: store.get("fx", "voll"), stt: store.get("stt", "auto"),
    presence: store.get("presence", false), presenceMinutes: Number(store.get("presence.minutes", 20)) || 20,
  };
  document.body.dataset.fx = settings.fx;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let canListen = Boolean(SpeechRecognition);  // wird true, sobald die lokale Erkennung bereit ist
  let sttLocal = "none";  // Zustand der lokalen Spracherkennung (Health: stt = local:ready | local:loading | …)
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
    const phase = next === "thinking" ? turn?.phaseText : null;  // „Ich schlage nach …“ bleibt stehen
    els.status.textContent = text ?? phase ?? (next === "idle" ? idleText() : STATUS[next]);
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
      if (action.status !== "pending_confirmation") {
        closeConfirmation(card, STATUS_DE[action.status] ?? action.status);
        flash(action.status === "succeeded" ? "done" : action.status === "rejected" ? null : "error");
      }
    });
    setAlert(Boolean(document.querySelector(".confirm:not(.done)")));
  }

  function routeLabel(route) {
    if (!route) return "";
    if (route === "fast_path") return "Direktbefehl";
    if (route === "research") return "nachgeschlagen";
    if (route === "conversation") return "Gespräch";
    if (route === "confirmation_resolver") return "Bestätigung";
    const sourced = route.endsWith(":research") ? " · nachgeschlagen" : "";  // Modell antwortet aus Quellen
    if (route.startsWith("llm:claude")) return `Claude · Cloud${sourced}`;
    if (route.startsWith("llm:")) return `lokales Modell${sourced}`;
    return route;
  }

  // ---------------------------------------------------------------- Farben, Karten, Wetter
  // Farbsprache (jarvis.css): Cyan bereit · Weiß hört · Gold denkt · Violett schlägt nach · Blau führt aus ·
  // Grün gefunden/erledigt · Orange wartet auf Bestätigung · Rot Fehler. Die Phasen meldet der Server („status“).
  const TOOL_TEXT = {
    "info.weather": "Ich rufe die Wetterdaten ab …", "info.news": "Ich lade die Nachrichten …",
    "info.wikipedia": "Ich sehe in der Wikipedia nach …", "web.search": "Ich suche im Internet …",
    "web.fetch": "Ich lese die Seite …", "pc.search_files": "Ich durchsuche Ihre Dateien …",
    "pc.find_files": "Ich durchsuche Ihre Dateien …", "pc.open_link": "Ich suche den passenden Link …",
    calendar: "Ich sehe in Ihren Kalender …", mail: "Ich sehe ins Postfach …", timer: "Timer wird gestellt …",
    reminder: "Erinnerung wird angelegt …", pc: "Befehl an Ihren PC …", home: "Haussteuerung …",
    memory: "Einen Moment …", system: "Systemprüfung läuft …", assistant: "Ich stelle Ihren Tag zusammen …",
  };
  const toolText = (capability = "") => TOOL_TEXT[capability] ?? TOOL_TEXT[capability.split(".")[0]] ?? "Einen Moment …";

  function onStatus(message) {
    if (!turn) return;
    document.body.dataset.phase = message.phase;
    turn.phaseText = message.phase === "research"
      ? (message.query ? `Ich schlage nach: „${message.query}“ …` : "Ich schlage nach …")
      : toolText(message.capability);
    if (state === "thinking") els.status.textContent = turn.phaseText;
  }

  let flashTimer = 0;
  function flash(kind) {  // found | done | miss | error | alert
    if (!kind) return;
    clearTimeout(flashTimer);
    delete document.body.dataset.flash;
    void document.body.offsetWidth;  // Animation neu starten, auch bei gleicher Art
    document.body.dataset.flash = kind;
    flashTimer = setTimeout(() => { delete document.body.dataset.flash; }, kind === "alert" ? 3800 : 1500);
  }

  function setAlert(on) {
    if (on) document.body.dataset.alert = "confirm";
    else delete document.body.dataset.alert;
  }

  function outcomeOf(result, text) {
    const actions = result.actions ?? [];
    if (result.pending_confirmation) return "alert";
    const failed = actions.some((a) => ["failed", "denied", "timed_out"].includes(a.status));
    const succeeded = actions.some((a) => a.status === "succeeded");
    if (/finde ich nichts Verlässliches|steht nichts in der Wikipedia|Mehr steht in meinen Quellen nicht/.test(text)) return "miss";
    if (failed && !succeeded) return "error";
    if (result.route === "research" || result.route?.endsWith(":research")) return "found";
    return succeeded ? "done" : null;
  }

  // Karten unter dem Kreis (Wetter, Nachgeschlagenes, Timer) – der Kreis wird dafür kleiner
  const cards = {
    items: new Map(),
    show(key, card, lifetimeMs) {
      this.remove(key, true);
      card.dataset.key = key;
      const close = button("×", "close", () => this.remove(key));
      close.setAttribute("aria-label", "Schließen");
      card.prepend(close);
      els.cards.prepend(card);
      const timer = lifetimeMs ? setTimeout(() => this.remove(key), lifetimeMs) : 0;
      this.items.set(key, { card, timer });
      document.body.classList.add("has-cards");
      return card;
    },
    remove(key, instant = false) {
      const item = this.items.get(key);
      if (!item) return;
      this.items.delete(key);
      clearTimeout(item.timer);
      if (key === "weather") ambient.stop();
      const done = () => {
        item.card.remove();
        if (!this.items.size) document.body.classList.remove("has-cards");
      };
      if (instant) { done(); return; }
      item.card.classList.add("leaving");
      setTimeout(done, 340);
    },
    touch(key, lifetimeMs) {  // Karte bleibt stehen und wird nur aktualisiert – Ablaufzeit neu setzen
      const item = this.items.get(key);
      if (!item) return;
      clearTimeout(item.timer);
      item.timer = lifetimeMs ? setTimeout(() => this.remove(key), lifetimeMs) : 0;
    },
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // -- Wetter: WMO-Code -> Himmel, animierte Symbole, Partikel -------------------------------------------------
  function skyOf(code, conditions = "", isDay = true) {
    let sky;
    if (code === null || code === undefined) {
      const c = conditions.toLowerCase();
      sky = /gewitter/.test(c) ? "thunder" : /schnee/.test(c) ? "snow" : /niesel/.test(c) ? "drizzle"
        : /regen|schauer/.test(c) ? "rain" : /nebel/.test(c) ? "fog" : /bedeckt/.test(c) ? "cloud"
          : /teilweise|überwiegend/.test(c) ? "partly" : "sun";
    } else if (code === 0) sky = "sun";
    else if (code <= 2) sky = "partly";
    else if (code === 3) sky = "cloud";
    else if (code <= 48) sky = "fog";
    else if (code <= 57) sky = "drizzle";
    else if (code <= 67 || (code >= 80 && code <= 82)) sky = "rain";
    else if (code <= 77 || code === 85 || code === 86) sky = "snow";
    else sky = "thunder";
    if (!isDay && sky === "sun") return "night";
    if (!isDay && sky === "partly") return "partly-night";
    return sky;
  }

  const CLOUD = '<g class="cloud{c}" transform="{t}"><circle cx="-14" cy="4" r="14"/><circle cx="4" cy="-5" r="18"/>'
    + '<circle cx="20" cy="6" r="12"/><rect x="-28" y="4" width="60" height="16" rx="8"/></g>';
  const cloud = (extra = "", transform = "") => CLOUD.replace("{c}", extra ? ` ${extra}` : "").replace("{t}", transform);
  const SUN = '<g transform="{t}"><g class="rays">' + Array.from({ length: 8 }, (_, i) => {
    const a = (i * Math.PI) / 4;
    return `<line x1="${(30 * Math.cos(a)).toFixed(1)}" y1="${(30 * Math.sin(a)).toFixed(1)}" x2="${(40 * Math.cos(a)).toFixed(1)}" y2="${(40 * Math.sin(a)).toFixed(1)}"/>`;
  }).join("") + '</g><circle class="sun-core" r="21"/></g>';
  const MOON = '<path class="moon" transform="{t}" d="M 6 -24 A 24 24 0 1 0 24 8 A 19 19 0 1 1 6 -24 Z"/>';
  const drops = (cls, n) => [[-16, 24], [-2, 26], [12, 24], [-9, 30]].slice(0, n)
    .map(([x, y], i) => `<line class="drop d${i + 1}" x1="${x}" y1="${y}" x2="${x - 3}" y2="${y + 8}"/>`).join("").replace(/drop/g, cls);
  const flakes = () => [[-15, 26], [0, 30], [14, 26]].map(([x, y], i) => `<circle class="flake d${i + 1}" cx="${x}" cy="${y}" r="3"/>`).join("");

  function weatherIcon(sky) {
    const parts = {
      sun: SUN.replace("{t}", ""),
      night: MOON.replace("{t}", ""),
      partly: SUN.replace("{t}", "translate(-12 -12) scale(0.72)") + cloud("", "translate(6 8) scale(0.85)"),
      "partly-night": MOON.replace("{t}", "translate(-10 -12) scale(0.7)") + cloud("", "translate(6 8) scale(0.85)"),
      cloud: cloud("back", "translate(-10 -10) scale(0.7)") + cloud("", "translate(4 4)"),
      fog: cloud("back", "translate(0 -10)") + '<line class="fogline" x1="-30" y1="22" x2="26" y2="22"/>'
        + '<line class="fogline d2" x1="-22" y1="31" x2="32" y2="31"/><line class="fogline d3" x1="-32" y1="40" x2="18" y2="40"/>',
      drizzle: cloud("", "translate(0 -8)") + drops("drop", 3),
      rain: cloud("dark", "translate(0 -8)") + drops("drop", 4),
      snow: cloud("", "translate(0 -8)") + flakes(),
      thunder: cloud("dark", "translate(0 -10)") + '<polygon class="bolt" points="-2,16 -12,34 -2,34 -8,50 10,26 0,26 6,16"/>'
        + drops("drop", 2),
    };
    return `<svg class="wx" viewBox="-50 -50 100 100" aria-hidden="true">${parts[sky] ?? parts.cloud}</svg>`;
  }

  const WEEKDAY = new Intl.DateTimeFormat("de-DE", { weekday: "short" });
  const round = (value) => (typeof value === "number" ? Math.round(value) : "–");

  function showWeather(data) {
    const current = data.current ?? {};
    const sky = skyOf(current.code, current.conditions, current.is_day !== false);
    const card = el("section", "card weather");
    card.dataset.sky = sky.replace("partly-night", "night").replace("partly", "cloud");
    card.setAttribute("aria-label", `Wetter ${data.location ?? ""}`);
    card.append(el("p", "kicker", `Wetter · ${data.location ?? ""}`));
    const now = el("div", "now");
    now.insertAdjacentHTML("afterbegin", weatherIcon(sky));
    const info = el("div");
    const temp = el("div", "temp", `${round(current.temperature_c)}°`);
    if (typeof current.feels_like_c === "number") temp.append(el("small", "", `gefühlt ${round(current.feels_like_c)}°`));
    info.append(temp, el("div", "cond", current.conditions ?? ""));
    const facts = el("div", "facts");
    const fact = (label, value) => { if (value !== null && value !== undefined) { const f = el("span", "", `${label} `); f.append(el("b", "", value)); facts.append(f); } };
    fact("Wind", typeof current.wind_kmh === "number" ? `${round(current.wind_kmh)} km/h` : null);
    fact("Luftfeuchte", typeof current.humidity_pct === "number" ? `${round(current.humidity_pct)} %` : null);
    const today = (data.forecast ?? [])[0];
    fact("Regen", today && typeof today.precipitation_probability_pct === "number" ? `${today.precipitation_probability_pct} %` : null);
    info.append(facts);
    now.append(info);
    card.append(now);

    const forecast = data.forecast ?? [];
    if (forecast.length > 1) {
      const lows = forecast.map((d) => d.temp_min_c).filter((v) => typeof v === "number");
      const highs = forecast.map((d) => d.temp_max_c).filter((v) => typeof v === "number");
      const min = Math.min(...lows), max = Math.max(...highs), span = Math.max(1, max - min);
      const days = el("div", "days");
      forecast.slice(0, 7).forEach((day, i) => {
        const cell = el("div", "day");
        const name = i === 0 ? "Heute" : i === 1 ? "Morgen" : WEEKDAY.format(new Date(`${day.date}T12:00:00`)).replace(".", "");
        cell.append(el("div", "", name));
        cell.insertAdjacentHTML("beforeend", weatherIcon(skyOf(day.code, day.conditions)));
        const temps = el("div");
        temps.append(el("b", "", `${round(day.temp_max_c)}°`), document.createTextNode(` ${round(day.temp_min_c)}°`));
        cell.append(temps);
        if (typeof day.temp_min_c === "number" && typeof day.temp_max_c === "number") {
          const bar = el("div", "bar");  // Spanne des Tages im Verhältnis zur ganzen Woche
          bar.style.marginLeft = `${6 + ((day.temp_min_c - min) / span) * 40}%`;
          bar.style.marginRight = `${6 + ((max - day.temp_max_c) / span) * 40}%`;
          cell.append(bar);
        }
        if (day.precipitation_probability_pct >= 30) cell.append(el("div", "rain", `☂ ${day.precipitation_probability_pct} %`));
        days.append(cell);
      });
      card.append(days);
    }
    cards.show("weather", card, 120000);
    ambient.start(sky);
    weatherChip.set(data, sky);
  }

  // Kleines Wetter in der Kopfzeile (letzte Abfrage, 3 Stunden gültig) – Klick zeigt die Karte wieder
  const weatherChip = {
    set(data, sky) {
      store.set("weather.last", { at: Date.now(), data });
      els.weatherChip.innerHTML = weatherIcon(sky);
      els.weatherChip.append(document.createTextNode(`${round(data.current?.temperature_c)}°`));
      els.weatherChip.title = `${data.location ?? ""}: ${data.current?.conditions ?? ""} – anzeigen`;
      els.weatherChip.hidden = false;
    },
    restore() {
      const last = store.get("weather.last", null);
      if (!last || Date.now() - last.at > 3 * 3600 * 1000 || !last.data?.current) return;
      const current = last.data.current;
      this.set(last.data, skyOf(current.code, current.conditions, current.is_day !== false));
    },
  };
  els.weatherChip.addEventListener("click", () => {
    const last = store.get("weather.last", null);
    if (last?.data) showWeather(last.data);
  });

  // Regen, Schnee, Blitze hinter dem Kreis, solange die Wetterkarte offen ist
  const ambient = {
    frame: 0, particles: [], kind: null, bolt: 0,
    start(sky) {
      this.stop(true);
      const kind = { rain: "rain", drizzle: "rain", thunder: "rain", snow: "snow" }[sky];
      if (!kind || settings.fx !== "voll" || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const canvas = els.ambient;
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = canvas.clientWidth * ratio;
      canvas.height = canvas.clientHeight * ratio;
      const ctx = canvas.getContext("2d");
      ctx.scale(ratio, ratio);
      const w = canvas.clientWidth, h = canvas.clientHeight;
      const count = kind === "snow" ? 90 : sky === "drizzle" ? 70 : 150;
      this.kind = kind;
      this.thunder = sky === "thunder";
      this.particles = Array.from({ length: count }, () => ({
        x: Math.random() * w, y: Math.random() * h, v: kind === "snow" ? 0.4 + Math.random() * 0.8 : 7 + Math.random() * 6,
        r: kind === "snow" ? 1 + Math.random() * 2.2 : 10 + Math.random() * 12, d: Math.random() * Math.PI * 2,
      }));
      canvas.classList.add("on");
      const tick = () => {
        ctx.clearRect(0, 0, w, h);
        if (this.thunder && Math.random() < 0.004) this.bolt = 1;
        if (this.bolt > 0.02) {  // Wetterleuchten
          ctx.fillStyle = `rgba(200, 215, 255, ${this.bolt * 0.18})`;
          ctx.fillRect(0, 0, w, h);
          this.bolt *= 0.85;
        }
        ctx.strokeStyle = "rgba(120, 180, 255, 0.35)";
        ctx.fillStyle = "rgba(235, 248, 255, 0.75)";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        for (const p of this.particles) {
          if (this.kind === "snow") {
            p.d += 0.01;
            p.x += Math.sin(p.d) * 0.4;
            p.y += p.v;
            ctx.moveTo(p.x + p.r, p.y);
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          } else {
            p.x -= p.v * 0.18;
            p.y += p.v;
            ctx.moveTo(p.x, p.y);
            ctx.lineTo(p.x + p.r * 0.18, p.y - p.r);
          }
          if (p.y > h + 20) { p.y = -20; p.x = Math.random() * (w + 60); }
        }
        if (this.kind === "snow") ctx.fill(); else ctx.stroke();
        this.frame = requestAnimationFrame(tick);
      };
      tick();
    },
    stop(instant = false) {
      cancelAnimationFrame(this.frame);
      this.frame = 0;
      els.ambient.classList.remove("on");
      if (instant) els.ambient.getContext("2d")?.clearRect(0, 0, els.ambient.width, els.ambient.height);
    },
  };

  // -- Nachgeschlagen: Wikipedia-Karte und Quellen unter der Antwort ----------------------------------------------
  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
  }

  function showKnowledge(article) {
    const card = el("section", `card knowledge${article.image ? "" : " noimg"}`);
    if (article.image?.startsWith("https://upload.wikimedia.org/")) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.src = article.image;
      img.addEventListener("error", () => { img.remove(); card.classList.add("noimg"); });
      card.append(img);
    }
    const text = el("div");
    text.append(el("p", "kicker", "Nachgeschlagen · Wikipedia"), el("h4", "", article.title ?? ""));
    if (article.description) text.append(el("p", "desc", article.description));
    if (/^https:\/\/[a-z-]+\.wikipedia\.org\//.test(article.url ?? "")) {
      const link = el("a", "", "Artikel öffnen ↗");
      link.href = article.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      text.append(link);
    }
    card.append(text);
    cards.show("knowledge", card, 120000);
  }

  function sourceLinks(results) {
    const box = el("div", "sources");
    const seen = new Set();
    for (const hit of results) {
      const host = hostOf(hit.url);
      if (!host || seen.has(host) || !/^https?:\/\//.test(hit.url)) continue;
      seen.add(host);
      const link = el("a", "", host);
      link.href = hit.url;
      link.title = hit.title ?? host;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      box.append(link);
      if (seen.size >= 4) break;
    }
    return seen.size ? box : null;
  }

  // -- E-Mail-Assistent: der Entwurf füllt sich Schritt für Schritt ---------------------------------------------
  const MAIL_STEPS = ["to", "subject", "body", "done"];
  const MAIL_HINTS = {
    to: "Kontakt nennen oder diktieren: „max punkt mustermann at gmx punkt de“ · „abbrechen“ verwirft",
    subject: "„Nein“ = Adresse korrigieren · „ohne Betreff“ · „fertig“ öffnet sofort",
    body: "„Der Betreff ist falsch“ korrigiert · „fertig“ öffnet ohne Text",
    done: "Bitte im Mailprogramm prüfen und selbst absenden – JARVIS verschickt nie in Ihrem Namen.",
    failed: "Der Entwurf ließ sich nicht öffnen – ist die PC-Steuerung verbunden?",
  };
  let mailShown = null;

  function showMail(data) {
    if (data.step === "cancel") { mailShown = null; cards.remove("mail"); return; }
    const existing = cards.items.get("mail")?.card;
    const card = existing ?? el("section", "card mail");
    card.dataset.step = data.step;
    const index = MAIL_STEPS.indexOf(data.step === "failed" ? "done" : data.step);
    const kicker = data.step === "done" ? "E-Mail-Entwurf · geöffnet ✓" : data.step === "failed" ? "E-Mail-Entwurf · nicht geöffnet"
      : `E-Mail-Entwurf · Schritt ${index + 1} von 3`;
    const inner = el("div", "inner");
    inner.append(el("p", "kicker", kicker));
    const rows = el("dl", "rows");
    [["to", "An"], ["subject", "Betreff"], ["body", "Text"]].forEach(([field, label], i) => {
      const value = data[field] ?? "";
      const current = i === index;
      const dt = el("dt", current ? "current" : "", label);
      const dd = el("dd", "", value || (current ? "" : i < index ? (field === "to" ? "im Entwurf eintragen" : "ohne") : "…"));
      if (current) dd.classList.add("current");
      else if (!value) dd.classList.add(i < index ? "empty" : "todo");
      if (value && mailShown && value !== mailShown[field]) dd.classList.add("fresh");  // eben verstanden
      rows.append(dt, dd);
    });
    inner.append(rows);
    const progress = el("div", "progress");
    for (let i = 0; i < 3; i += 1) progress.append(el("i", i < index || data.step === "done" ? "on" : i === index ? "now" : ""));
    inner.append(progress, el("p", "hint", MAIL_HINTS[data.step] ?? ""));
    card.querySelector(".inner")?.remove();
    card.append(inner);
    const lifetime = data.step === "done" ? 60000 : data.step === "failed" ? 30000 : 190000;  // Server vergisst nach 3 min
    if (existing) cards.touch("mail", lifetime); else cards.show("mail", card, lifetime);
    mailShown = data.step === "done" || data.step === "failed" ? null : { ...data };
  }

  // -- Sehen: ein Einzelbild auf Zuruf, Kamera sofort wieder aus, Bild bleibt auf dem JARVIS-Rechner -----------------
  const vision = {
    busy: false,
    async capture(question) {
      if (this.busy) return;
      this.busy = true;
      let stream = null;
      try {
        let video = presence.video;  // läuft die Anwesenheitserkennung, ihr Bild mitnutzen (Kamera bleibt an)
        if (!video) {
          if (!navigator.mediaDevices?.getUserMedia) throw new Error("Dieser Browser gibt keine Kamera frei.");
          stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 960 }, facingMode: "user" } });
          video = document.createElement("video");
          video.muted = true;
          video.playsInline = true;
          video.srcObject = stream;
          await video.play();
          await new Promise((resolve) => setTimeout(resolve, 700));  // Belichtung einregeln lassen
        }
        const width = Math.min(960, video.videoWidth || 640);
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = Math.round(width * (video.videoHeight || 480) / (video.videoWidth || 640));
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = canvas.toDataURL("image/jpeg", 0.82);
        stream?.getTracks().forEach((t) => t.stop());  // eigene Kamera sofort aus
        stream = null;
        const card = el("section", "card vision");
        const img = el("img");
        img.alt = "Aufgenommenes Kamerabild";
        img.src = image;
        const text = el("div");
        text.append(el("p", "kicker", "Gesehen · lokales Bildmodell"), el("p", "answer", "Ich sehe mir das Bild an …"));
        card.append(img, text);
        cards.show("vision", card, 120000);
        document.body.dataset.phase = "research";
        if (!tts.busy) setState("thinking", "Ich sehe mir das Bild an …");
        const { text: answer } = await api("POST", "/v1/vision", { image, question });
        card.querySelector(".answer").textContent = answer;
        addMessage("jarvis", answer, `JARVIS · ${timeNow()} · Sehen`);
        tts.speak(answer);
        flash("found");
      } catch (err) {
        const message = err.name === "NotAllowedError"
          ? "Die Kamera ist blockiert. Erlauben Sie sie links in der Adressleiste über das Schloss-Symbol."
          : err.name === "NotFoundError" ? "Ich finde keine Kamera an diesem Gerät." : err.message;
        addSystem(message, true);
        tts.speak(message);
        flash("error");
      } finally {
        stream?.getTracks().forEach((t) => t.stop());
        this.busy = false;
        delete document.body.dataset.phase;
        if (state === "thinking" && !turn) setState("idle");
      }
    },
  };

  // -- Anwesenheit: Bewegung im Kamerabild (64×48 Graustufen, einmal pro Sekunde) – Bilder verlassen den Browser nie
  function motionScore(previous, current) {
    let changed = 0;
    let sum = 0;
    for (let i = 0; i < current.length; i += 1) {
      sum += current[i];
      if (Math.abs(current[i] - previous[i]) > 24) changed += 1;
    }
    return { motion: changed / current.length, brightness: sum / current.length };
  }
  window.jarvisMotion = motionScore;  // für Tests

  const presence = {
    stream: null, video: null, canvas: null, timer: 0, previous: null, lastMotion: 0, away: false, noise: 0.004,
    awayMs: null,  // Tests: Abwesenheit in Millisekunden statt Minuten
    async start() {
      if (this.stream) return;
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error("Dieser Browser gibt keine Kamera frei.");
        this.stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 5, max: 10 } }, audio: false });
        this.video = document.createElement("video");
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.srcObject = this.stream;
        await this.video.play();
        this.canvas = document.createElement("canvas");
        this.canvas.width = 64;
        this.canvas.height = 48;
        this.previous = null;
        this.lastMotion = Date.now();
        this.away = false;
        this.timer = setInterval(() => this.tick(), 1000);
        els.camChip.hidden = false;
      } catch (err) {
        this.stop();
        settings.presence = false;
        store.set("presence", false);
        els.optPresence.checked = false;
        addSystem(err.name === "NotAllowedError"
          ? "Die Kamera ist blockiert – die Anwesenheitserkennung bleibt aus. Erlauben Sie die Kamera über das Schloss-Symbol in der Adressleiste."
          : err.name === "NotFoundError" ? "Keine Kamera gefunden – die Anwesenheitserkennung bleibt aus." : `Kamera: ${err.message}`, true);
      }
    },
    stop() {
      clearInterval(this.timer);
      this.timer = 0;
      this.stream?.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.video = null;
      this.previous = null;
      els.camChip.hidden = true;
    },
    limit() { return this.awayMs ?? settings.presenceMinutes * 60000; },
    tick() {
      if (!this.video || this.video.readyState < 2) return;
      const ctx = this.canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(this.video, 0, 0, 64, 48);
      const pixels = ctx.getImageData(0, 0, 64, 48).data;
      const gray = new Uint8Array(64 * 48);
      for (let i = 0; i < gray.length; i += 1) {
        gray[i] = (pixels[i * 4] * 77 + pixels[i * 4 + 1] * 150 + pixels[i * 4 + 2] * 29) >> 8;
      }
      const previous = this.previous;
      this.previous = gray;
      if (!previous) return;
      const { motion, brightness } = motionScore(previous, gray);
      const now = Date.now();
      if (brightness < 12) return;  // dunkel oder abgedeckt: nichts schließen
      if (motion > Math.max(0.01, this.noise * 3)) {
        this.moved(now);
      } else {
        this.noise = 0.95 * this.noise + 0.05 * motion;  // Rauschen (Licht, Kamera) laufend nachführen
        if (!this.away && now - this.lastMotion > this.limit()) {
          this.away = true;
          this.report("left", 0);
        }
      }
    },
    async moved(now) {
      if (this.away) {
        this.away = false;
        const minutes = Math.round((now - this.lastMotion) / 60000);
        const reply = await this.report("arrived", minutes);
        if (reply?.text && !turn) {
          addMessage("jarvis", reply.text, `JARVIS · ${timeNow()} · Anwesenheit`);
          tts.speak(reply.text);
          flash("found");
        }
      }
      this.lastMotion = now;
    },
    async report(state, awayMinutes) {
      try {
        return await api("POST", "/v1/presence", { state, away_minutes: awayMinutes });
      } catch {
        return null;  // Server kurz weg: beim nächsten Wechsel wieder
      }
    },
  };
  window.jarvisPresence = presence;  // für Tests
  els.camChip.addEventListener("click", () => openSettings("general"));
  els.optPresence.addEventListener("change", () => {
    settings.presence = els.optPresence.checked;
    store.set("presence", settings.presence);
    if (settings.presence) presence.start(); else presence.stop();
  });
  els.optPresenceMin.addEventListener("change", () => {
    settings.presenceMinutes = Number(els.optPresenceMin.value) || 20;
    store.set("presence.minutes", settings.presenceMinutes);
  });

  // -- Systemmonitor: Balken für Prozessor, Speicher, Laufwerke, Grafikkarte ----------------------------------------
  function showSysmon(data) {
    const card = el("section", "card sysmon");
    card.append(el("p", "kicker", `Systemmonitor · ${data.host || (data.source === "pc" ? "PC" : "JARVIS-Rechner")}`));
    const rows = el("div", "meters");
    const meter = (label, percent, detail) => {
      const row = el("div", "meter");
      const value = Math.max(0, Math.min(100, Math.round(percent)));
      row.dataset.level = value >= 90 ? "high" : value >= 70 ? "mid" : "low";
      const bar = el("div", "bar");
      const fill = el("span");
      fill.style.width = "0%";
      requestAnimationFrame(() => { fill.style.width = `${value}%`; });
      bar.append(fill);
      row.append(el("span", "label", label), bar, el("span", "value", detail ?? `${value} %`));
      rows.append(row);
    };
    const gb = (v) => Number(v || 0).toLocaleString("de-DE", { maximumFractionDigits: 1 });
    meter("Prozessor", data.cpu_percent ?? 0);
    if (data.memory_total_gb) {
      meter("Speicher", (100 * data.memory_used_gb) / data.memory_total_gb, `${gb(data.memory_used_gb)} / ${gb(data.memory_total_gb)} GB`);
    }
    for (const disk of (data.disks ?? []).slice(0, 3)) {
      meter(`Laufwerk ${String(disk.name).replace(/:$/, "")}`, 100 * (1 - disk.free_gb / (disk.total_gb || 1)), `${gb(disk.free_gb)} GB frei`);
    }
    for (const gpu of (data.gpus ?? []).slice(0, 2)) {
      meter("Grafikkarte", gpu.percent, `${gpu.percent} % · ${gpu.celsius} °C`);
    }
    card.append(rows);
    const facts = [];
    for (const t of (data.temperatures ?? []).slice(0, 3)) facts.push(`${t.label}: ${t.celsius} °C`);
    if (data.battery) facts.push(`Akku ${data.battery.percent} %${data.battery.plugged ? " ⚡" : ""}`);
    if (data.net_down_mbit !== undefined) facts.push(`Netz ↓ ${gb(data.net_down_mbit)} · ↑ ${gb(data.net_up_mbit)} Mbit/s`);
    if (data.uptime_hours !== undefined) facts.push(`läuft seit ${gb(data.uptime_hours)} h`);
    if (facts.length) card.append(el("p", "facts", facts.join(" · ")));
    cards.show("sysmon", card, 90000);
  }

  // -- Timer mit Countdown-Ring --------------------------------------------------------------------------------
  const RING = 2 * Math.PI * 22;
  const timers = {
    tick: 0,
    add(result) {
      // „due“ kommt minutengenau – bei bekannter Dauer zählt die Zeit ab Empfang (auf die Sekunde)
      const due = result.duration_s ? Date.now() + result.duration_s * 1000 : Date.parse(result.due);
      if (!Number.isFinite(due)) return;
      const card = el("section", "card timer");
      card.dataset.due = String(due);
      card.dataset.total = String((result.duration_s || Math.max(1, (due - Date.now()) / 1000)) * 1000);
      card.dataset.label = result.label ?? "";
      card.insertAdjacentHTML("afterbegin", `<svg viewBox="0 0 54 54" aria-hidden="true"><circle class="ring-bg" cx="27" cy="27" r="22"/>`
        + `<circle class="ring-fg" cx="27" cy="27" r="22" stroke-dasharray="${RING.toFixed(1)}" stroke-dashoffset="0"/></svg>`);
      const text = el("div");
      text.append(el("div", "left", "--:--"), el("div", "label", result.label ? `Timer „${result.label}“` : "Timer"));
      card.append(text);
      cards.show(`timer:${result.id}`, card);
      this.update();
      if (!this.tick) this.tick = setInterval(() => this.update(), 1000);
    },
    update() {
      const running = [...cards.items].filter(([key]) => key.startsWith("timer:"));
      if (!running.length) { clearInterval(this.tick); this.tick = 0; return; }
      for (const [, { card }] of running) {
        if (card.dataset.done) continue;  // „Abgelaufen“ bleibt stehen
        const left = Math.max(0, Number(card.dataset.due) - Date.now());
        const s = Math.ceil(left / 1000);
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        card.querySelector(".left").textContent = h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
          : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
        card.querySelector(".ring-fg").setAttribute("stroke-dashoffset", (RING * (1 - left / Number(card.dataset.total))).toFixed(1));
        if (!left) card.classList.add("due");
      }
    },
    done(id) {  // Meldung vom Server: abgelaufen
      const item = cards.items.get(`timer:${id}`);
      if (!item) return;
      item.card.classList.add("due");
      item.card.dataset.done = "1";
      item.card.querySelector(".left").textContent = "Abgelaufen";
      setTimeout(() => cards.remove(`timer:${id}`), 8000);
    },
    cancel(labels) {
      for (const [key, { card }] of [...cards.items]) {
        if (key.startsWith("timer:") && (!labels.length || labels.includes(card.dataset.label))) cards.remove(key);
      }
    },
  };

  // Aus den Aktionen einer Antwort die passenden Karten zeigen
  function showResults(result, item) {
    const actions = result.actions ?? [];
    for (const action of actions) {
      if (action.status !== "succeeded" || !action.result || typeof action.result !== "object") continue;
      const data = action.result;
      if (action.capability === "info.weather" && data.current) showWeather(data);
      else if (action.capability === "assistant.day_plan" && data.weather?.current) showWeather(data.weather);
      else if (action.capability === "info.wikipedia" && data.found) showKnowledge(data);
      else if (action.capability === "timer.start" && data.id) timers.add(data);
      else if (action.capability === "system.monitor") showSysmon(data);
      else if (action.capability === "timer.cancel") timers.cancel((data.cancelled ?? []).map((t) => t.label ?? ""));
    }
    if (result.card?.type === "mail") showMail(result.card);
    else if (result.card?.type === "vision") vision.capture(result.card.question);
    else if (mailShown) { mailShown = null; cards.remove("mail"); }  // Entwurf abgebrochen: anderer Wunsch
    const researched = result.route === "research" || result.route?.endsWith(":research");
    const web = actions.find((a) => a.capability === "web.search" && a.status === "succeeded");
    if (researched && web?.result?.results?.length && item) {
      const links = sourceLinks(web.result.results);
      if (links) item.append(links);
    }
  }

  // -- Uhr in der Kopfzeile ----------------------------------------------------------------------------------------
  const DATE = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "numeric", month: "short" });
  function tickClock() {
    const now = new Date();
    els.clockTime.textContent = now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    els.clockDate.textContent = DATE.format(now).replace(/\./g, "");
    setTimeout(tickClock, 60000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 50);
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
      // E-Mail-Adressen so vorlesen, wie man sie diktiert: „max punkt mustermann at gmx punkt de“
      .replace(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, (address) => address.replace(/@/g, " at ").replace(/\./g, " Punkt ")
        .replace(/_/g, " Unterstrich ").replace(/-/g, " Minus "))
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
    showResults(result, current.item);  // Wetter, Nachgeschlagenes, Timer als Karten
    flash(outcomeOf(result, finalText));
    setAlert(Boolean(document.querySelector(".confirm:not(.done)")));
    expectReply = Boolean(result.awaiting_reply) && current.spoken && canListen;
    scrollLog();
    if (!tts.busy) onSpeechDone();
  }

  function failTurn(message) {
    if (!turn) return;
    const current = endTurn();
    current.item.classList.add("error");
    current.body.textContent = current.streamed ? `${current.streamed}\n\n${message}` : message;
    flash("error");
    tts.stop();
    tts.speak(message);
    if (!tts.busy) onSpeechDone();
  }

  function endTurn() {
    const current = turn;
    turn = null;
    clearTimeout(current.slowTimer);
    current.item.classList.remove("pending");
    delete document.body.dataset.phase;
    lastTurnSpoken = current.spoken;
    updateComposer();
    return current;
  }

  function onSpeechDone() {
    if (turn) { setState("thinking"); return; }        // Antwort läuft noch, nächster Satz kommt
    if (!ws || ws.readyState !== WebSocket.OPEN) { setState("offline"); return; }
    if (((lastTurnSpoken && settings.convo) || expectReply) && (canListen || localVoice.enabled) && state !== "listening") {
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
    if (localVoice.enabled) {
      if (turn) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) { addSystem("Noch keine Verbindung zum Server.", true); return; }
      tts.stop();
      setState("listening", listenHint ?? "Ich höre … (lokal)");
      listenHint = undefined;
      localVoice.setMode("listen");
      return;
    }
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

  function stopListening() {
    if (localVoice.mode === "listen") {
      localVoice.setMode("idle");
      setState("idle");
    }
    recognition?.stop();
  }

  // Sprechpausen: Chrome meldet schon nach kurzem Zögern ein „fertiges“ Teilstück. Abgeschickt wird erst nach dieser
  // Stille – länger, wenn der Satz hörbar weitergeht („Such mir nach …“, „… auf“).
  const SETTLE_MS = 1300;
  const SETTLE_OPEN_MS = 2800;
  const OPEN_END = /(?:^|\s)(?:nach|auf|zu|zum|zur|von|vom|für|mit|und|oder|bei|in|im|mir|mal|den|die|das|der|dem|des|ein|eine|einen|einem|über|an|am|namens|such|suche|öffne|spiel|zeig|schreib|tippe?|schließe?)$/i;
  const settleDelay = (text) => (OPEN_END.test(text.trim()) ? SETTLE_OPEN_MS : SETTLE_MS);
  let listenHint;

  function onHudActivate() {
    if (recognition || localVoice.mode === "listen") { stopListening(); return; }
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
    const personal = learned.sounds();
    for (let i = 0; i < words.length; i += 1) {
      const single = words[i];
      const pair = words[i + 1] && `${single[0]}${words[i + 1][0]}`;
      for (const [candidate, end] of [[single[0], single.index + single[0].length],
                                      [pair, pair && words[i + 1].index + words[i + 1][0].length]]) {
        if (!candidate) continue;
        const sound = phonetic(candidate);
        if (sound.length >= 4 && sound.length <= 8 && sound[0] === "j" && distance(sound, "jarvis") <= 1) return end;
        if (personal.some((known) => known === sound || (known.length >= 5 && distance(sound, known) <= 1))) return end;
      }
    }
    return -1;
  }

  // Persönliche Schreibweisen: was die Spracherkennung bei diesem Nutzer aus „Jarvis“ macht („Service“, „Davis“).
  // Nur im Browser gespeichert; gewöhnliche Wörter („ja“, „das“) lassen sich nicht lernen.
  const WAKE_STOP = new Set(["ja", "sir", "der", "die", "das", "und", "ist", "es", "ich", "du", "er", "sie", "hallo",
    "hey", "okay", "ok", "nein", "bitte", "danke", "so", "na", "oh", "ah", "äh", "hm", "mal", "jetzt", "hier", "was",
    "wie", "wo", "gut", "an", "aus", "auf", "in", "im", "mit", "noch", "doch", "schon", "also"]);
  const learned = {
    words: store.get("wakeWords", []),
    sounds() { return this.words.map((word) => phonetic(word.replace(/\s+/g, ""))); },
    add(text) {  // -> "learned" | "known" | "rejected"
      const word = text.toLowerCase().replace(/[^\p{L}\s]/gu, " ").replace(/\s+/g, " ").trim();
      const parts = word.split(" ");
      if (!word || parts.length > 2 || parts.some((part) => WAKE_STOP.has(part)) || word.replace(/\s/g, "").length < 4) {
        return "rejected";
      }
      if (findWake(word) >= 0) return "known";
      this.words = [...this.words, word].slice(-12);
      store.set("wakeWords", this.words);
      return "learned";
    },
    reset() { this.words = []; store.set("wakeWords", []); },
  };

  // Chrome ab 2025: „Jarvis“ als erwartetes Wort vorgeben (Contextual Biasing) – wo nicht unterstützt, ohne weiter
  let phraseBoost = typeof window.SpeechRecognitionPhrase === "function";
  function boost(rec) {
    if (!phraseBoost) return;
    try { rec.phrases = [new window.SpeechRecognitionPhrase("Jarvis", 5)]; } catch { phraseBoost = false; }
  }

  // Was die Dauer-Erkennung gerade gehört hat, wenn kein „Jarvis“ darin war – mit „Das war „Jarvis““ lernbar
  const heard = {
    text: "", timer: 0,
    show(text) {
      if (!hasWords(text) || text.split(/\s+/).length > 2) return;
      this.text = text;
      els.heardText.textContent = `Gehört: „${text}“`;
      els.heardLearn.hidden = false;
      els.heard.hidden = false;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.hide(), 8000);
    },
    note(message) {
      els.heardText.textContent = message;
      els.heardLearn.hidden = true;
      els.heard.hidden = false;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.hide(), 5000);
    },
    hide() { els.heard.hidden = true; this.text = ""; },
  };
  window.jarvisFindWake = findWake;  // für Tests
  window.jarvisLearnWake = (text) => learned.add(text);

  const wake = {
    rec: null, timer: 0, settle: 0, failures: 0, collecting: false, buffer: "", interim: null, interimTimer: 0,
    wanted() {
      return settings.wake && (canListen || localVoice.enabled) && state === "idle" && !turn && !recognition && !tts.busy
        && !trainer.running && ws?.readyState === WebSocket.OPEN;
    },
    schedule(delay = 300) {
      clearTimeout(this.timer);
      if (settings.wake) this.timer = setTimeout(() => this.start(), delay);
    },
    start() {
      if (localVoice.enabled) {  // lokal: der Server hört auf „Jarvis“ (Whisper/openWakeWord)
        if (this.wanted()) localVoice.setMode("wake");
        return;
      }
      if (this.rec || !this.wanted()) return;
      const rec = new SpeechRecognition();
      rec.lang = "de-DE";
      rec.continuous = true;
      rec.interimResults = true;
      rec.maxAlternatives = 5;  // „Jarvis“ steht oft nur in einer der Alternativen
      boost(rec);
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
          if (!hit) {
            if (!result.isFinal) continue;
            const text = result[0].transcript.trim();
            if (this.interim && Date.now() - this.interim.at < 2500 && hasWords(text)) {
              // Das Zwischenergebnis enthielt „Jarvis“, die Endfassung nicht mehr („Davis, such …“): ihm glauben
              const rest = text.split(/\s+/).slice(this.interim.words).join(" ");
              this.interim = null;
              clearTimeout(this.interimTimer);
              if (hasWords(rest)) { this.collecting = true; this.collect(rest, true); continue; }
              this.acknowledge();
              return;
            }
            heard.show(text);
            continue;
          }
          this.failures = 0;
          if (!result.isFinal) {
            document.body.classList.add("wake-heard");
            this.interim = { at: Date.now(), words: hit.text.slice(0, hit.end).trim().split(/\s+/).length };
            // Kurze Einzelwörter verschluckt Chrome manchmal ganz: Kommt keine Endfassung, trotzdem antworten
            clearTimeout(this.interimTimer);
            this.interimTimer = setTimeout(() => { if (this.interim && !this.collecting) this.acknowledge(); }, 2000);
            continue;
          }
          this.interim = null;
          clearTimeout(this.interimTimer);
          heard.hide();
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
        if (failure === "phrases-not-supported") {  // Wortvorgabe hier nicht möglich: ohne sie neu starten
          phraseBoost = false;
          this.schedule(100);
          return;
        }
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
      this.interim = null;
      clearTimeout(this.interimTimer);
      clearTimeout(this.settle);
      document.body.classList.remove("wake-heard");
    },
    stop() {
      clearTimeout(this.timer);
      this.reset();
      if (localVoice.mode === "wake") localVoice.setMode("idle");
      const rec = this.rec;
      this.rec = null;
      if (rec) {
        rec.onresult = null;
        rec.onend = null;
        rec.abort();
      }
    },
  };

  // ---------------------------------------------------------------- Lokale Spracherkennung
  // Mikrofon -> 16 kHz/16 Bit -> WS /v1/audio. Der Server schneidet Äußerungen, erkennt „Jarvis“ und transkribiert
  // mit Whisper. Gesendet wird nur, solange JARVIS zuhört oder auf „Jarvis“ wartet – nie, während er spricht.
  const STT_STATE = {
    "local:ready": "Lokal bereit – Audio verlässt den JARVIS-Rechner nicht.",
    "local:loading": "Das Whisper-Modell wird geladen (beim ersten Mal ein Download von einigen hundert MB) …",
    "local:idle": "Das Whisper-Modell wird beim ersten Zuhören geladen.",
    "local:error": "Das Whisper-Modell ließ sich nicht laden – JARVIS nutzt die Browser-Erkennung. Details im Server-Protokoll.",
    browser: "Lokale Erkennung ist nicht installiert (Windows: „JARVIS installieren.cmd“ erneut ausführen).",
  };
  const localVoice = {
    socket: null, context: null, stream: null, node: null, mode: "idle", opening: null, retry: 0,
    get enabled() {
      // „auto“ (Standard): lokal, sobald der Server Whisper bereit hat – sonst die Browser-Erkennung
      return settings.stt !== "browser" && sttLocal === "local:ready";
    },
    update(status) {
      const before = this.enabled;
      sttLocal = status || "browser";
      canListen = Boolean(SpeechRecognition) || this.enabled;
      els.sttState.textContent = settings.stt !== "browser" ? (STT_STATE[sttLocal] ?? "") : "";
      if (before && !this.enabled) this.close();
      if (!before && this.enabled && settings.wake && state === "idle") wake.schedule(200);
    },
    async ensure() {
      if (this.socket && this.socket.readyState <= WebSocket.OPEN && this.context) return;
      if (this.opening) return this.opening;
      this.opening = (async () => {
        try {
          if (!this.stream) {
            this.stream = await navigator.mediaDevices.getUserMedia({
              audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
          }
          if (!this.context) {
            this.context = new AudioContext();
            const source = this.context.createMediaStreamSource(this.stream);
            this.node = this.context.createScriptProcessor(4096, 1, 1);
            this.node.onaudioprocess = (event) => this.pump(event.inputBuffer);
            source.connect(this.node);
            this.node.connect(this.context.destination);  // ohne Ausgang ruft Chrome onaudioprocess nicht auf
          }
          if (!this.socket || this.socket.readyState > WebSocket.OPEN) this.connect();
        } catch (err) {
          addSystem(`Mikrofon nicht verfügbar: ${err.message}`, true);
          setWake(false);
        } finally {
          this.opening = null;
        }
      })();
      return this.opening;
    },
    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${location.host}/v1/audio?token=${encodeURIComponent(token)}`);
      socket.binaryType = "arraybuffer";
      socket.onopen = () => { this.retry = 0; socket.send(JSON.stringify({ type: "mode", mode: this.mode })); };
      socket.onmessage = (event) => { try { this.handle(JSON.parse(event.data)); } catch { /* ungültig */ } };
      socket.onclose = () => {
        if (this.socket !== socket) return;
        this.socket = null;
        if (this.mode !== "idle" && this.enabled) {  // Server neu gestartet: wieder verbinden
          this.retry += 1;
          setTimeout(() => { if (this.mode !== "idle") this.connect(); }, Math.min(10000, 500 * 2 ** this.retry));
        }
      };
      this.socket = socket;
    },
    pump(buffer) {
      if (this.mode === "idle" || this.socket?.readyState !== WebSocket.OPEN) return;
      const input = buffer.getChannelData(0);
      const ratio = buffer.sampleRate / 16000;
      const out = new Int16Array(Math.floor(input.length / ratio));
      let sum = 0;
      for (let i = 0; i < out.length; i += 1) {  // Mittelwert über das Fenster = einfacher Tiefpass
        const from = Math.floor(i * ratio), to = Math.min(input.length, Math.floor((i + 1) * ratio));
        let acc = 0;
        for (let k = from; k < to; k += 1) acc += input[k];
        const value = Math.max(-1, Math.min(1, acc / Math.max(1, to - from)));
        sum += value * value;
        out[i] = value * 32767;
      }
      if (this.mode === "listen") setLevel(Math.min(1, Math.sqrt(sum / out.length) * 6));
      this.socket.send(out.buffer);
    },
    setMode(mode) {
      this.mode = mode;
      if (mode !== "idle") this.ensure();
      if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "mode", mode }));
      if (mode !== "listen") setLevel(0);
    },
    handle(event) {
      switch (event.type) {
        case "wake":
          this.setMode("idle");  // „Ja, Sir?“ nicht selbst hören
          acknowledgement.say(() => {
            if (turn) return;
            setState("listening", "Ja? Ich höre …");
            this.setMode("listen");
          });
          break;
        case "speech":
          if (state !== "listening") setState("listening");
          break;
        case "transcript":
          this.setMode("idle");
          els.caption.textContent = "";
          sendText(event.text, true);
          break;
        case "heard":
          heard.show(event.text);
          break;
        case "nothing":
          this.mode = "idle";
          setLevel(0);
          if (state === "listening") setState("idle");
          els.status.textContent = "Ich habe nichts gehört.";
          break;
        case "error":
          addSystem(event.message, true);
          break;
        default:
          break;
      }
    },
    close() {
      this.mode = "idle";
      const socket = this.socket;
      this.socket = null;
      socket?.close();
      this.node?.disconnect();
      this.node = null;
      this.context?.close().catch(() => {});
      this.context = null;
      this.stream?.getTracks().forEach((t) => t.stop());
      this.stream = null;
    },
  };
  window.jarvisLocalVoice = localVoice;  // für Tests
  els.optStt.addEventListener("change", () => {
    settings.stt = els.optStt.value;
    store.set("stt", settings.stt);
    wake.stop();
    localVoice.close();
    localVoice.update(sttLocal);
    if (settings.wake && state === "idle") wake.schedule(200);
  });

  // „Jarvis“ einlernen: viermal sagen, JARVIS merkt sich die Schreibweisen der Spracherkennung
  function listenOnce() {
    return new Promise((resolve) => {
      const rec = new SpeechRecognition();
      rec.lang = "de-DE";
      rec.continuous = false;
      rec.interimResults = false;
      rec.maxAlternatives = 5;
      boost(rec);
      const found = [];
      let failure = null;
      const limit = setTimeout(() => rec.stop(), 5000);
      rec.onresult = (event) => {
        for (const result of event.results) {
          for (let k = 0; k < result.length; k += 1) found.push(result[k].transcript.trim());
        }
      };
      rec.onerror = (event) => { failure = event.error; };
      rec.onend = () => {
        clearTimeout(limit);
        if (failure === "phrases-not-supported" && phraseBoost) { phraseBoost = false; listenOnce().then(resolve); return; }
        resolve({ found, failure });
      };
      try { rec.start(); } catch { clearTimeout(limit); resolve({ found, failure: "busy" }); }
    });
  }

  const trainer = {
    running: false,
    async start() {
      if (this.running || !canListen) return;
      this.running = true;
      wake.stop();
      if (recognition) recognition.abort();
      tts.stop();
      els.wakeTrain.disabled = true;
      const counts = new Map();
      let rounds = 0;
      let problem = null;
      for (let round = 1; round <= 4; round += 1) {
        els.wakeTrainStatus.textContent = `Sagen Sie jetzt „Jarvis“ (${round} von 4) …`;
        const { found, failure } = await listenOnce();
        if (failure && RECOGNITION_ERRORS[failure]) { problem = RECOGNITION_ERRORS[failure]; break; }
        if (found.length) rounds += 1;
        for (const text of new Set(found.map((t) => t.toLowerCase()))) counts.set(text, (counts.get(text) || 0) + 1);
        await new Promise((r) => setTimeout(r, 350));
      }
      const newWords = [];
      let known = 0;
      for (const text of counts.keys()) {
        const outcome = learned.add(text);
        if (outcome === "learned") newWords.push(text);
        if (outcome === "known") known += 1;
      }
      this.running = false;
      els.wakeTrain.disabled = false;
      if (problem) els.wakeTrainStatus.textContent = problem;
      else if (!rounds) els.wakeTrainStatus.textContent = "Ich habe nichts gehört. Ist das richtige Mikrofon ausgewählt?";
      else if (newWords.length) {
        els.wakeTrainStatus.textContent = `Gelernt: ${newWords.map((w) => `„${w}“`).join(", ")} gilt jetzt als „Jarvis“.`;
      } else if (known) els.wakeTrainStatus.textContent = "„Jarvis“ wird bereits zuverlässig erkannt.";
      else els.wakeTrainStatus.textContent = "Nichts Brauchbares gehört – bitte noch einmal deutlich „Jarvis“ sagen.";
      showLearned();
      wake.schedule(500);
    },
  };

  function showLearned() {
    els.wakeForget.hidden = !learned.words.length;
    if (!els.wakeTrainStatus.textContent && learned.words.length) {
      els.wakeTrainStatus.textContent = `Gelernte Schreibweisen: ${learned.words.map((w) => `„${w}“`).join(", ")}`;
    }
  }

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
      setHomeChip(health.smart_home);
      localVoice.update(health.stt);
      setModelChip(health.llm);
      acknowledgement.prepare();
    } catch {
      llmStatus = "unknown";
    }
    if (state === "idle") setState("idle");
    if (ws) healthTimer = setTimeout(checkHealth, LLM_STATUS[llmStatus] ? 3000 : 30000);
  }

  const HOME_CHIP = {
    connected: ["online", "Haus", "Smart Home verbunden – Licht, Heizung, Rollläden per Sprache"],
    disconnected: ["offline", "Haus", "Home Assistant ist gerade nicht erreichbar"],
    found: ["connecting", "Haus verbinden", "Home Assistant gefunden – hier klicken und mit einem Klick verbinden"],
  };
  function setHomeChip(value) {
    const entry = HOME_CHIP[value];
    els.homeChip.hidden = !entry;
    if (!entry) return;
    [els.homeChip.dataset.conn, els.homeChipText.textContent, els.homeChip.title] = entry;
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
      case "status": onStatus(message); break;
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
    flash("alert");
    if (message.kind === "timer" && message.id) timers.done(message.id);
    if (settings.speak && !turn?.muted) {
      if (!tts.busy && !turn) wake.stop();
      chime();
      setTimeout(() => tts.speak(text), 350);
    }
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      try { new Notification("JARVIS", { body: text, icon: "favicon.svg" }); } catch { /* ohne Desktop-Hinweis */ }
    }
  }
  const NOTICE_LABELS = { timer: "Timer", reminder: "Erinnerung", event: "Termin", suggestion: "Vorschlag" };

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

  function openSettings(tab) {
    els.optSpeak.checked = settings.speak;
    els.optConvo.checked = settings.convo;
    els.optConvo.disabled = !canListen;
    els.optLocation.value = settings.location;
    els.optFx.value = settings.fx;
    els.optPresence.checked = settings.presence;
    els.optPresenceMin.value = String(settings.presenceMinutes);
    els.optStt.value = settings.stt === "browser" ? "browser" : "local";
    localVoice.update(sttLocal);
    els.wakeTrainStatus.textContent = "";
    showLearned();
    fillVoices();
    selectTab(tab ?? currentTab);
    if (!els.settings.open) els.settings.showModal();
  }
  els.settingsBtn.addEventListener("click", () => openSettings());
  els.modelChip.addEventListener("click", () => openSettings("ai"));
  els.settings.addEventListener("close", () => { clearTimeout(ai.timer); clearTimeout(home.timer); });

  // Reiter: Allgemein · Stimme & Hören · KI-Modell (Pfeiltasten wechseln wie bei Reitern üblich)
  let currentTab = store.get("settings.tab", "general");
  function selectTab(name) {
    if (!els.tabs.some((tab) => tab.id === `tab-${name}`)) name = "general";
    currentTab = name;
    store.set("settings.tab", name);
    for (const tab of els.tabs) {
      const on = tab.id === `tab-${name}`;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !on;
    }
    if (name === "ai") ai.load();
    else clearTimeout(ai.timer);
    if (name === "home") home.load();
    else clearTimeout(home.timer);
  }
  els.tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectTab(tab.id.slice(4)));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const next = els.tabs[(i + (event.key === "ArrowRight" ? 1 : els.tabs.length - 1)) % els.tabs.length];
      selectTab(next.id.slice(4));
      next.focus();
    });
  });

  els.optFx.addEventListener("change", () => {
    settings.fx = els.optFx.value;
    store.set("fx", settings.fx);
    document.body.dataset.fx = settings.fx;
    if (settings.fx !== "voll") ambient.stop(true);
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

  // ---------------------------------------------------------------- KI-Modell (Einstellungen)
  async function api(method, path, body) {
    const response = await fetch(path, {
      method, cache: "no-store", body: body ? JSON.stringify(body) : undefined,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? "Die Eingabe ist ungültig." : data.detail;
      const error = new Error(data.user_message || detail || data.title || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  const PULL_STEPS = [[/manifest/, "Vorbereitung"], [/verifying|digest/, "Prüfe Download"], [/writing|removing/, "Speichere"],
    [/success|fertig/, "Fertig"], [/pulling|download/, "Lade herunter"]];
  const LOCAL_STATE = { ready: "bereit", loading: "lädt …", missing_model: "fehlt", unavailable: "nicht erreichbar", unknown: "" };
  const gb = (bytes) => (bytes / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 1 });
  const onLocalhost = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);

  const ai = {
    data: null, timer: 0,
    async load() {
      clearTimeout(this.timer);
      try {
        this.data = await api("GET", "/v1/settings/llm");
      } catch (err) {
        els.aiNow.textContent = err.status === 403 ? "Nur Erwachsene des Haushalts können das KI-Modell ändern."
          : `Die Modellwahl ist gerade nicht erreichbar: ${err.message}`;
        return;
      }
      this.render();
      const pull = this.data.local.pull;
      const busy = (pull && !pull.done) || this.data.local.status === "loading";
      if (els.settings.open && currentTab === "ai") this.timer = setTimeout(() => this.load(), busy ? 1000 : 5000);
    },
    render() {
      const { local, cloud, mode } = this.data;
      const pull = local.pull;
      const pulling = Boolean(pull && !pull.done);
      const cloudName = cloud.models.find((m) => m.id === cloud.model)?.name ?? cloud.model;
      const who = !cloud.configured || mode === "local" ? `Es antwortet nur das lokale Modell ${local.model}.`
        : mode === "cloud" ? `Es antwortet ${cloudName} (Sensibles bleibt lokal bei ${local.model}).`
          : `${local.model} antwortet, schwierige Fragen gehen an ${cloudName}.`;
      els.aiNow.textContent = local.reachable ? `${who} Lokales Modell: ${LOCAL_STATE[local.status] ?? local.status}.`
        : "Ollama ist nicht erreichbar – läuft JARVIS über ./deploy/start.sh?";

      // lokale Modelle: Vorschläge und alles, was sonst installiert ist
      const installed = new Map(local.installed.map((m) => [m.name, m]));
      const rows = local.suggested.map((m) => ({ ...m, size: m.installed && installed.get(m.name) ? `${gb(installed.get(m.name).size_gb * 1e9)} GB` : m.size }));
      for (const m of local.installed) {
        if (!rows.some((r) => r.name === m.name || `${r.name}:latest` === m.name)) {
          rows.push({ name: m.name, size: `${gb(m.size_gb * 1e9)} GB`, note: "installiert", installed: true });
        }
      }
      els.localModels.replaceChildren(...rows.map((m) => {
        const li = el("li");
        const active = m.name === local.model || `${m.name}:latest` === local.model;
        li.classList.toggle("active", active);
        li.append(el("span", "name", m.name), el("span", "note", `${m.size} · ${m.note}`));
        const side = el("span", "side");
        if (active) side.append(el("span", `badge ${local.status === "ready" ? "ok" : "busy"}`, `aktiv · ${LOCAL_STATE[local.status] || "…"}`));
        else if (m.installed) {
          side.append(el("span", "badge", "installiert"));
          const use = button("Verwenden", "btn small", () => this.change({ local_model: m.name }));
          use.disabled = pulling;
          side.append(use);
        } else {
          const get = button("Herunterladen", "btn small", () => this.pull(m.name));
          get.disabled = pulling;
          side.append(get);
        }
        li.append(side);
        return li;
      }));
      els.customPull.disabled = pulling;

      // Download-Fortschritt
      els.pullProgress.hidden = !pull;
      if (pull) {
        const pct = pull.total ? Math.round((pull.completed / pull.total) * 100) : 0;
        els.pullBar.style.width = `${pull.done && !pull.error ? 100 : pct}%`;
        const step = PULL_STEPS.find(([re]) => re.test(pull.status))?.[1] ?? pull.status;
        els.pullText.textContent = pull.error ? `${pull.model}: ${pull.error}`
          : pull.done ? `${pull.model} ist geladen${pull.activate ? " und aktiv" : ""}.`
            : `${pull.model}: ${step}${pull.total ? ` – ${gb(pull.completed)} von ${gb(pull.total)} GB (${pct} %)` : " …"}`;
        els.pullText.classList.toggle("warn", Boolean(pull.error));
      }

      // Claude
      els.claudeState.textContent = cloud.configured
        ? `Verbunden – Schlüssel ${cloud.key_hint} (${cloud.source === "ui" ? "hier gespeichert" : "aus deploy/.env"})`
          + (cloud.state === "open" ? ". Claude ist gerade gestört; JARVIS antwortet vorübergehend lokal." : ".")
        : "Kein Schlüssel hinterlegt – JARVIS antwortet nur lokal.";
      els.claudeRemove.hidden = cloud.source !== "ui";
      els.claudeHttp.hidden = onLocalhost;
      els.claudeModel.replaceChildren(...cloud.models.map((m) => new Option(
        `${m.name} – ${m.note} (${m.price_in} $ / ${m.price_out} $ je Mio. Tokens)`, m.id, false, m.id === cloud.model)));
      for (const radio of els.llmModes.querySelectorAll("input")) {
        radio.checked = radio.value === mode;
        radio.disabled = !cloud.configured && radio.value !== "local";
      }
    },
    async change(body) {
      els.aiError.hidden = true;
      try {
        this.data = await api("PUT", "/v1/settings/llm", body);
        this.render();
        checkHealth();
      } catch (err) {
        els.aiError.textContent = err.message;
        els.aiError.hidden = false;
        this.load();
      }
    },
    async pull(model) {
      els.aiError.hidden = true;
      try {
        this.data = await api("POST", "/v1/settings/llm/pull", { model, activate: true });
        this.render();
        this.timer = setTimeout(() => this.load(), 800);
      } catch (err) {
        els.aiError.textContent = err.message;
        els.aiError.hidden = false;
      }
    },
  };

  els.customPull.addEventListener("click", () => {
    const model = els.customModel.value.trim();
    if (model) ai.pull(model);
  });
  els.claudeSave.addEventListener("click", async () => {
    const key = els.claudeKey.value.trim();
    els.claudeError.hidden = true;
    if (!key) { els.claudeKey.focus(); return; }
    els.claudeSave.disabled = true;
    els.claudeSave.textContent = "Prüfe …";
    try {
      const data = await api("PUT", "/v1/settings/llm/claude-key", { api_key: key });
      els.claudeKey.value = "";
      ai.data = data;
      ai.render();
      els.claudeState.textContent = `✓ Verbunden mit ${data.checked}. ${els.claudeState.textContent}`;
      flash("done");
      checkHealth();
    } catch (err) {
      els.claudeError.textContent = err.message;
      els.claudeError.hidden = false;
    } finally {
      els.claudeSave.disabled = false;
      els.claudeSave.textContent = "Prüfen & speichern";
    }
  });
  els.claudeKey.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); els.claudeSave.click(); }
  });
  els.claudeRemove.addEventListener("click", async () => {
    try {
      ai.data = await api("DELETE", "/v1/settings/llm/claude-key");
      ai.render();
      checkHealth();
    } catch (err) {
      els.claudeError.textContent = err.message;
      els.claudeError.hidden = false;
    }
  });
  els.claudeModel.addEventListener("change", () => ai.change({ claude_model: els.claudeModel.value }));
  els.llmModes.addEventListener("change", (event) => {
    if (event.target.name === "llm-mode") ai.change({ mode: event.target.value });
  });

  // ---------------------------------------------------------------- Smart Home (Einstellungen)
  const DOMAIN_LABEL = { light: "Licht", switch: "Schalter", cover: "Rollladen", climate: "Heizung", scene: "Szene",
    lock: "Schloss", media_player: "Medien", sensor: "Sensor", binary_sensor: "Sensor" };
  const home = {
    data: null, timer: 0, busy: false,
    async load() {
      clearTimeout(this.timer);
      try {
        this.data = await api("GET", "/v1/settings/home");
      } catch (err) {
        els.homeNow.textContent = err.status === 403 ? "Nur Erwachsene des Haushalts können das Smart Home verbinden."
          : `Smart Home ist gerade nicht erreichbar: ${err.message}`;
        return;
      }
      this.render();
      this.loadRoutines();
      const d = this.data;
      const waiting = d.scanning || (d.configured && (!d.connected || !d.devices));
      if (els.settings.open && currentTab === "home") this.timer = setTimeout(() => this.load(), waiting ? 1500 : 10000);
    },
    render() {
      const d = this.data;
      const name = d.name ? `„${d.name}“` : "Home Assistant";
      els.homeNow.textContent = d.connected
        ? `Verbunden mit ${name} (${d.url}) – ${d.devices} Geräte in ${d.rooms.filter((r) => r.name !== "Ohne Raum").length} Räumen.`
        : d.configured ? `Verbinde mit ${d.url} …${d.error ? ` (${d.error})` : ""}`
          : d.scanning ? "Suche Home Assistant im Heimnetz …"
            : d.found.length ? "Home Assistant gefunden – ein Klick genügt." : "Noch kein Smart Home verbunden.";
      els.homeSetup.hidden = d.configured && d.connected;
      els.homeConnected.hidden = !d.configured;
      els.homeSearch.disabled = d.scanning || this.busy;
      els.homeSearch.textContent = d.scanning ? "Suche …" : "Suchen";
      if (!els.homeUrl.value && d.found.length) els.homeUrl.value = d.found[0];
      els.homeFound.replaceChildren(...d.found.map((url) => {
        const li = el("li", url === els.homeUrl.value ? "active" : "");
        li.append(el("span", "name", "Home Assistant gefunden"), el("span", "note", url));
        const side = el("div", "side");
        side.append(button("Verbinden", "btn primary small", () => { els.homeUrl.value = url; this.login(); }));
        li.append(side);
        return li;
      }));
      els.homeRooms.replaceChildren(...d.rooms.map((room) => {
        const li = el("li");
        li.append(el("b", "", room.name));
        const counts = {};
        room.devices.forEach((device) => { counts[device.domain] = (counts[device.domain] ?? 0) + 1; });
        li.append(el("span", "note", Object.entries(counts).map(([k, n]) => `${n} × ${DOMAIN_LABEL[k] ?? k}`).join(" · ")));
        li.title = room.devices.map((device) => device.name).join(", ");
        return li;
      }));
      setHomeChip(d.configured ? (d.connected ? "connected" : "disconnected") : d.found.length ? "found" : "off");
    },
    async loadRoutines() {
      let list = [];
      try { ({ suggestions: list } = await api("GET", "/v1/automations/suggestions")); } catch { return; }
      els.homeRoutinesEmpty.hidden = list.length > 0;
      els.homeRoutines.replaceChildren(...list.map((s) => {
        const li = el("li", s.status === "accepted" ? "active" : "");
        li.append(el("span", "name", s.status === "accepted" ? "Aktiv" : "Vorschlag"), el("span", "note", s.text));
        const side = el("div", "side");
        const decide = (accept) => this.run(async () => {
          await api("POST", `/v1/automations/suggestions/${encodeURIComponent(s.id)}`, { accept });
          await this.loadRoutines();
        });
        if (s.status === "accepted") side.append(button("Beenden", "btn small", () => decide(false)));
        else side.append(button("Übernehmen", "btn primary small", () => decide(true)), button("Nein", "btn small", () => decide(false)));
        li.append(side);
        return li;
      }));
    },
    fail(err) {
      els.homeError.textContent = err.message;
      els.homeError.hidden = false;
    },
    async run(fn) {
      els.homeError.hidden = true;
      this.busy = true;
      try { await fn(); } catch (err) { this.fail(err); } finally { this.busy = false; }
      if (this.data) this.render();
    },
    search() {
      return this.run(async () => {
        els.homeSearch.disabled = true;
        els.homeSearch.textContent = "Suche …";
        this.data = await api("POST", "/v1/settings/home/discover");
        if (!this.data.found.length) throw new Error("Kein Home Assistant gefunden. Ist er eingeschaltet und im selben Netz? Sonst die Adresse eintragen.");
      });
    },
    login() {
      return this.run(async () => {
        const url = els.homeUrl.value.trim() || this.data?.found?.[0];
        if (!url) throw new Error("Bitte erst suchen oder die Adresse von Home Assistant eintragen.");
        const { authorize_url: target } = await api("POST", "/v1/settings/home/oauth", { url });
        location.href = target;  // Anmeldung bei Home Assistant, danach zurück zu JARVIS (#home=ok)
      });
    },
    saveToken() {
      return this.run(async () => {
        const url = els.homeUrl.value.trim() || this.data?.found?.[0];
        if (!url) throw new Error("Bitte die Adresse von Home Assistant eintragen.");
        this.data = await api("PUT", "/v1/settings/home/token", { url, token: els.homeToken.value.trim() });
        els.homeToken.value = "";
        flash("found");
        this.load();
      });
    },
    remove() {
      return this.run(async () => { this.data = await api("DELETE", "/v1/settings/home"); });
    },
  };
  els.homeSearch.addEventListener("click", () => home.search());
  els.homeLogin.addEventListener("click", () => home.login());
  els.homeTokenSave.addEventListener("click", () => home.saveToken());
  els.homeToken.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); home.saveToken(); }
  });
  els.homeUrl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); home.login(); }
  });
  els.homeRemove.addEventListener("click", () => home.remove());
  els.homeChip.addEventListener("click", () => openSettings("home"));

  // Modell-Anzeige in der Kopfzeile
  const CLAUDE_SHORT = { "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-fable-5-1": "Fable 5.1" };
  function setModelChip(llm) {
    els.modelChip.hidden = !llm;
    if (!llm) return;
    const local = (llm.local_model || "lokal").replace(/-instruct$/, "").replace(":", " ");
    const cloud = llm.cloud_model ? `Claude ${CLAUDE_SHORT[llm.cloud_model] ?? ""}`.trim() : null;
    let text = local, kind = "local";
    if (cloud && llm.mode === "cloud") { text = cloud; kind = "cloud"; }
    else if (cloud && llm.mode !== "local") { text = `${local} + Claude`; kind = "cloud"; }
    if (LLM_STATUS[llmStatus]) kind = "loading";
    els.modelText.textContent = text;
    els.modelChip.dataset.conn = kind;
    els.modelChip.title = `KI-Modell: ${llm.local_model}${cloud ? ` · ${cloud} (${llm.mode === "cloud" ? "immer" : llm.mode === "local" ? "aus" : "für schwierige Fragen"})` : ""} – ändern im Zahnrad-Menü`;
  }
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
  els.heardLearn.addEventListener("click", () => {
    const text = heard.text;
    const outcome = learned.add(text);
    heard.note(outcome === "learned" ? `Gelernt: „${text}“ weckt JARVIS jetzt.`
      : outcome === "known" ? "Das erkennt JARVIS bereits."
        : `„${text}“ ist zu kurz oder zu gewöhnlich für ein Aktivierungswort.`);
  });
  els.wakeTrain.hidden = !canListen;
  els.wakeTrain.addEventListener("click", () => trainer.start());
  els.wakeForget.addEventListener("click", () => {
    learned.reset();
    els.wakeTrainStatus.textContent = "Gelernte Schreibweisen gelöscht.";
    showLearned();
  });
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
  if (settings.presence && token) presence.start();
  if (homeReturn && token) {  // zurück von der Home-Assistant-Anmeldung: Ergebnis gleich zeigen
    openSettings("home");
    if (homeReturn === "ok") flash("found");
    else home.fail(new Error("Die Anmeldung bei Home Assistant hat nicht geklappt. Bitte noch einmal versuchen."));
  }
  tickClock();
  weatherChip.restore();
  setWake(settings.wake);
  setState("offline", "Verbinde …");
  connect();
})();
