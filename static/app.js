// Personal Tavern client (§12). Vanilla JS + Alpine, no build step.
//
// Two streams feed the UI:
//   POST /api/chats/{id}/send  — the turn itself (deltas, reply, errors)
//   GET  /api/chats/{id}/events — the ambient bus, where background passes
//                                 land after the turn has already closed
// Keeping them separate is what lets a background pass update its panel
// without being tied to the request that started it (§1, §4.5).

const MASK_DISPLAY = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  },
  async patch(path, body) {
    const r = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  },
};

// Reads an SSE body from fetch. EventSource cannot POST, and the turn needs a
// request body, so the framing is parsed by hand.
async function* sseStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          yield JSON.parse(line.slice(5).trim());
        } catch (_) {
          /* a truncated frame is not worth killing the turn over */
        }
      }
    }
  }
}

function tavern() {
  return {
    // ---- state ----
    characters: [],
    chats: [],
    characterId: "",
    chatId: "",
    character: null,
    messages: [],
    bands: [],
    stateProvisional: false,
    scene: { place: "", weather: "", time: "" },
    summary: { text: "", covered_turn: 0 },
    toggles: [],
    toggleStates: {},
    expression: "neutral",
    background: "",

    draft: "",
    editing: null,
    editText: "",
    drawer: false,
    hud: false,
    error: "",

    streaming: false,
    composing: false,
    composingKind: "typing",
    composingLabel: "typing…",
    turn: 0,
    hudRuns: [],
    ambient: [],
    refreshing: { scene: false, expression: false, background: false },
    cost: { per_pass: [], per_turn: [], totals: {} },
    totals: { tokens_in: 0, tokens_out: 0 },

    events: null,

    // ---- lifecycle ----

    async boot() {
      window.Markup = window.Markup || {};
      try {
        this.characters = await api.get("/api/characters");
        if (!this.characters.length) {
          this.error = "No characters. Drop a card in data/characters/ and restart.";
          return;
        }
        this.characterId = this.characters[0].id;
        this.chats = await api.get("/api/chats");
        const last = localStorage.getItem("tavern:chat");
        if (last && this.chats.some((c) => c.id === last)) await this.openChat(last);
        else if (this.chats.length) await this.openChat(this.chats[0].id);
        else await this.newChat();
      } catch (e) {
        this.error = String(e.message || e);
      }
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
    },

    get portrait() {
      if (!this.character) return "";
      const set = this.character.pfp_set || {};
      const file = set[this.expression] || set.neutral || "";
      return file ? `/static/characters/${file}` : "";
    },

    async newChat() {
      const chat = await api.post("/api/chats", { character_id: this.characterId });
      this.chats = await api.get("/api/chats");
      await this.openChat(chat.id);
      this.drawer = false;
    },

    async openChat(id) {
      const data = await api.get(`/api/chats/${id}`);
      this.chatId = id;
      this.character = data.character;
      this.characterId = data.chat.character_id;
      this.messages = data.messages;
      this.bands = data.state.bands || [];
      this.summary = data.summary;
      this.toggleStates = data.toggles || {};
      this.turn = this.messages.length ? this.messages[this.messages.length - 1].turn : 0;
      this.applyColours(this.character);

      const sceneSlice = (data.slices || {})["state.scene"];
      if (sceneSlice) this.scene = { ...this.scene, ...sceneSlice.value };
      const expr = (data.slices || {})["state.expression"];
      if (expr && expr.value.emotion) this.expression = expr.value.emotion;

      const toggleData = await api.get(`/api/toggles?character_id=${this.characterId}&chat_id=${id}`);
      this.toggles = toggleData.toggles;
      this.toggleStates = toggleData.states;

      localStorage.setItem("tavern:chat", id);
      this.connectEvents();
      this.loadCost();
      this.$nextTick(() => this.scrollDown());
      this.drawer = false;
    },

    // Per-character colour overrides on top of the global palette (§18.4).
    applyColours(character) {
      const root = document.documentElement;
      for (const key of ["dialogue", "action", "strong", "default"]) {
        root.style.removeProperty(`--c-${key}`);
      }
      for (const [key, value] of Object.entries((character && character.colours) || {})) {
        root.style.setProperty(key.startsWith("--") ? key : `--${key}`, value);
      }
    },

    // ---- ambient stream ----

    connectEvents() {
      if (this.events) this.events.close();
      this.events = new EventSource(`/api/chats/${this.chatId}/events`);
      this.events.onmessage = (e) => {
        let event;
        try { event = JSON.parse(e.data); } catch (_) { return; }
        this.handleEvent(event, false);
      };
      this.events.onerror = () => { /* EventSource reconnects on its own */ };
    },

    handleEvent(event, fromTurn) {
      switch (event.type) {
        case "pass_status": {
          this.mergeRun(event.run, event.turn);
          const running = event.run.status === "running" || event.run.status === "pending";
          if (event.run.pass_id === "basic") {
            this.composingKind = event.run.animation;
            this.composingLabel = event.run.animation === "typing" ? "typing…" : event.run.label;
          } else if (event.run.tier !== "blocking") {
            // ambient: a subtle indicator, never a character-thinking cue
            this.ambient = running
              ? [...new Set([...this.ambient, event.run.label])]
              : this.ambient.filter((a) => a !== event.run.label);
            if (event.run.pass_id === "scene") this.refreshing.scene = running;
          }
          break;
        }
        case "panel":
          if (event.panel === "scene") {
            this.scene = { ...this.scene, ...event.value };
            this.refreshing.scene = false;
          } else if (event.panel === "expression" && event.value.emotion) {
            this.expression = event.value.emotion;
          } else if (event.panel === "background" && event.value.background) {
            this.background = event.value.background;
            this.applyBackground(event.value.background);
          }
          break;
        case "state":
          this.bands = event.state.bands || [];
          this.stateProvisional = !!event.state.provisional;
          break;
        case "summary":
          this.summary = { text: event.text, covered_turn: event.covered_turn };
          break;
        case "error":
          if (!fromTurn) this.error = event.error;
          break;
      }
    },

    applyBackground(id) {
      const found = ((this.character || {}).backgrounds || []).find(
        (b) => (b.id || b.img) === id
      );
      document.body.style.backgroundImage = found
        ? `linear-gradient(rgba(15,17,21,.86), rgba(15,17,21,.94)), url(/static/backgrounds/${found.img})`
        : "";
      document.body.style.backgroundSize = "cover";
    },

    mergeRun(run, turn) {
      if (turn !== undefined && turn !== this.turn) {
        if (turn > this.turn) { this.turn = turn; this.hudRuns = []; }
      }
      const index = this.hudRuns.findIndex((r) => r.id === run.id);
      if (index === -1) this.hudRuns.push(run);
      else this.hudRuns[index] = run;
      this.totals = {
        tokens_in: this.hudRuns.reduce((a, r) => a + (r.tokens_in || 0), 0),
        tokens_out: this.hudRuns.reduce((a, r) => a + (r.tokens_out || 0), 0),
      };
    },

    // ---- turn ----

    async send() {
      const text = this.draft.trim();
      if (!text || this.streaming) return;
      this.draft = "";
      this.error = "";
      if (this.$refs.input) this.$refs.input.style.height = "auto";
      await this.runStream(`/api/chats/${this.chatId}/send`, { text });
    },

    async swipe(message) {
      if (this.streaming) return;
      this.error = "";
      await this.runStream(`/api/messages/${message.id}/swipe`, {}, message.id);
    },

    async runStream(url, body, swipeMessageId) {
      this.streaming = true;
      this.composing = true;
      this.composingKind = "typing";
      this.composingLabel = "typing…";
      this.hudRuns = [];

      let target = null;
      let buffer = "";
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);

        for await (const event of sseStream(response)) {
          switch (event.type) {
            case "turn_start":
              this.turn = event.turn;
              this.messages.push(event.message);
              this.scrollDown();
              break;

            case "delta":
              buffer += event.text;
              if (!target) {
                // Stream into a placeholder so the reply renders live, with
                // markup colouring applied on every frame (§8).
                if (swipeMessageId) {
                  target = this.messages.find((m) => m.id === swipeMessageId);
                  if (target) target.text = "";
                } else {
                  target = {
                    id: "streaming",
                    role: "assistant",
                    turn: this.turn,
                    text: "",
                    variant_count: 1,
                    variant_index: 0,
                    edited: false,
                  };
                  this.messages.push(target);
                }
                this.composing = false;
              }
              if (target) target.text = buffer;
              this.scrollDown();
              break;

            case "reply": {
              const index = this.messages.findIndex((m) => m.id === "streaming");
              const message = { ...event.message, text: event.message.text };
              if (index === -1) this.messages.push(message);
              else this.messages[index] = message;
              this.bands = event.state.bands || [];
              this.stateProvisional = !!event.state.provisional;
              break;
            }

            case "variant": {
              const message = this.messages.find((m) => m.id === event.message_id);
              if (message) {
                message.text = event.variant.text;
                message.variant_index = event.variant.idx;
                message.variant_count = event.variant.idx + 1;
                message.edited = false;
              }
              this.bands = event.state.bands || [];
              this.stateProvisional = !!event.state.provisional;
              break;
            }

            case "error":
              this.error = event.error;
              break;

            default:
              this.handleEvent(event, true);
          }
        }
      } catch (e) {
        this.error = String(e.message || e);
        this.messages = this.messages.filter((m) => m.id !== "streaming");
      } finally {
        this.streaming = false;
        this.composing = false;
        this.scrollDown();
        this.loadCost();
      }
    },

    // ---- editing & variants ----

    startEdit(message) {
      this.editing = message.id;
      this.editText = message.text;
    },

    async saveEdit(message, reaudit) {
      const updated = await api.patch(`/api/messages/${message.id}`, {
        text: this.editText,
        reaudit,
      });
      message.text = updated.text;
      message.edited = true;
      this.editing = null;
    },

    async cycleVariant(message, direction) {
      const variants = await api.get(`/api/messages/${message.id}/variants`);
      if (variants.length < 2) return;
      const current = variants.findIndex((v) => v.text === message.text);
      const next = (((current === -1 ? 0 : current) + direction) % variants.length + variants.length) % variants.length;
      const updated = await api.post(`/api/messages/${message.id}/variants/${variants[next].id}`);
      message.text = updated.text;
      message.variant_index = updated.variant_index;
      message.variant_count = updated.variant_count;
    },

    async setToggle(id, enabled) {
      this.toggleStates[id] = enabled;
      await api.post(`/api/toggles/${id}`, { enabled, scope: "per_chat", scope_id: this.chatId });
    },

    // ---- settings (§13) ----
    //
    // The server sends "***" for any key it holds and never the real value.
    // Sending "***" back means "keep it", so an untouched key survives a save
    // without the browser ever having seen it.

    settings: { backends: [], tiers: {}, tier_names: [], kinds: [], templates: [], path: "" },
    settingsOpen: false,
    saving: false,
    saveMsg: "",
    saveError: "",
    testing: "",
    tests: {},

    async openSettings() {
      this.saveMsg = "";
      this.saveError = "";
      this.tests = {};
      try {
        const loaded = await api.get("/api/settings");
        // Show a dot placeholder rather than the literal mask, so it reads as
        // "a key is set" instead of looking like a corrupted value.
        loaded.backends.forEach((b) => { if (b.api_key === "***") b.api_key = MASK_DISPLAY; });
        this.settings = loaded;
        this.settingsOpen = true;
      } catch (e) {
        this.error = `could not load settings: ${e.message || e}`;
      }
    },

    // Turn the edited form back into what the API expects.
    settingsPayload() {
      return {
        ...this.settings,
        backends: this.settings.backends.map((b) => ({
          ...b,
          api_key: b.api_key === MASK_DISPLAY ? "***" : b.api_key,
        })),
      };
    },

    addBackend() {
      this.settings.backends.push({
        name: `backend-${this.settings.backends.length + 1}`,
        kind: "ollama", model: "", base_url: "", api_key: "",
        template: "auto", timeout: 120, models: [],
      });
    },

    removeBackend(index) {
      const [gone] = this.settings.backends.splice(index, 1);
      // A tier left pointing at a deleted backend would fail validation on
      // save; repoint it rather than making the user work out why.
      const fallback = (this.settings.backends[0] || {}).name;
      for (const tier of this.settings.tier_names) {
        if (this.settings.tiers[tier] === gone.name) this.settings.tiers[tier] = fallback;
      }
    },

    async saveSettings() {
      this.saving = true;
      this.saveMsg = "";
      this.saveError = "";
      try {
        const body = await fetch("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.settingsPayload()),
        });
        const result = await body.json();
        if (!body.ok) throw new Error(result.detail || result.error || body.statusText);
        result.settings.backends.forEach((b) => { if (b.api_key === "***") b.api_key = MASK_DISPLAY; });
        this.settings = { ...this.settings, ...result.settings };
        this.saveMsg = "saved";
      } catch (e) {
        this.saveError = String(e.message || e);
      } finally {
        this.saving = false;
      }
    },

    async testBackend(backend) {
      this.testing = backend.name;
      try {
        const payload = { ...backend, api_key: backend.api_key === MASK_DISPLAY ? "***" : backend.api_key };
        const response = await fetch("/api/settings/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        this.tests = { ...this.tests, [backend.name]: response.ok ? result : { ok: false, error: result.detail } };
      } catch (e) {
        this.tests = { ...this.tests, [backend.name]: { ok: false, error: String(e.message || e) } };
      } finally {
        this.testing = "";
      }
    },

    testLabel(name) {
      const t = this.tests[name];
      if (!t) return "";
      return t.ok ? `ok — ${t.model} in ${t.latency_ms}ms` : `failed — ${t.error}`;
    },

    // ---- cost dashboard (§14) ----

    async loadCost() {
      if (!this.chatId) return;
      try {
        this.cost = await api.get(`/api/chats/${this.chatId}/cost`);
      } catch (_) { /* the dashboard is never worth an error banner */ }
    },

    costWidth(row) {
      const max = Math.max(
        1,
        ...this.cost.per_pass.map((r) => (r.tokens_in || 0) + (r.tokens_out || 0))
      );
      return Math.round((((row.tokens_in || 0) + (row.tokens_out || 0)) / max) * 100);
    },

    // ---- misc ----

    autosize(el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, window.innerHeight * 0.3) + "px";
    },

    scrollDown() {
      const el = this.$refs.chat;
      if (!el) return;
      // Only follow the stream when the user is already at the bottom.
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      if (atBottom) this.$nextTick(() => { el.scrollTop = el.scrollHeight; });
    },
  };
}

window.tavern = tavern;
