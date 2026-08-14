// Personal Tavern client (§12). Vanilla JS + Alpine, no build step.
//
// Two streams feed the UI:
//   POST /api/chats/{id}/send  — the turn itself (deltas, reply, errors)
//   GET  /api/chats/{id}/events — the ambient bus, where background passes
//                                 land after the turn has already closed
// Keeping them separate is what lets a background pass update its panel
// without being tied to the request that started it (§1, §4.5).

const MASK_DISPLAY = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";

// WCAG relative luminance of a #rgb / #rrggbb colour, 0 (black) to 1 (white).
function luminance(hex) {
  const m = String(hex).trim().replace("#", "");
  const full = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  if (!/^[0-9a-fA-F]{6}/.test(full)) return 1;
  const [r, g, b] = [0, 2, 4]
    .map((i) => parseInt(full.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

// Swipe thresholds, in CSS pixels.
const SWIPE_CLAIM = 12;   // movement before a drag counts as horizontal at all
const SWIPE_COMMIT = 64;  // release past this and the variant changes
const SWIPE_MAX = 130;    // furthest the bubble travels

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
    editHeight: 0,
    editingEl: null,
    regenId: null,
    regenPrevious: "",
    sceneBackgroundFile: "",
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
      // Load settings first: the saved palette should be on screen before the
      // first paint of any message, not applied a beat later.
      try {
        this.settings = await api.get("/api/settings");
        this.applyTheme();
      } catch (_) { /* defaults are already in the stylesheet */ }
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
      this.sceneBackgroundFile = "";
      this.applyTheme();

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

    // Two layers, applied in order: the saved theme is the global palette, and
    // a character card may override individual tokens on top of it (§18.4).
    applyTheme() {
      const root = document.documentElement;
      for (const token of this.settings.theme_tokens || []) {
        root.style.removeProperty(token.var);
      }
      for (const [name, value] of Object.entries(this.settings.theme || {})) {
        root.style.setProperty(name, value);
      }
      this.applyColours(this.character);
      this.updateColorScheme();
      this.applyBackground();
    },

    // Declare which scheme the palette actually is. Both "only light" and
    // "dark" opt the page out of forced dark repainting; announcing the wrong
    // one would give us light scrollbars and form controls on a dark palette,
    // or vice versa. Derived from the background rather than a stored flag, so
    // it stays right whatever the user picks.
    updateColorScheme() {
      const root = document.documentElement;
      const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
      const dark = luminance(bg) < 0.4;
      root.style.colorScheme = dark ? "dark" : "only light";
      // Keep the browser/PWA chrome on the same palette as the page.
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta && bg) meta.setAttribute("content", bg);
    },

    applyColours(character) {
      const root = document.documentElement;
      for (const [key, value] of Object.entries((character && character.colours) || {})) {
        root.style.setProperty(key.startsWith("--") ? key : `--${key}`, value);
      }
    },

    // ---- appearance editor ----

    themeGroups() {
      return [...new Set((this.settings.theme_tokens || []).map((t) => t.group))];
    },

    themeIn(group) {
      return (this.settings.theme_tokens || []).filter((t) => t.group === group);
    },

    themeValue(token) {
      return (this.settings.theme || {})[token.var] || token.default;
    },

    setTheme(token, value) {
      // Stored only when it differs from the default, so a reset is just an
      // empty map and the palette can be changed later without stale overrides.
      const theme = { ...(this.settings.theme || {}) };
      if (value === token.default) delete theme[token.var];
      else theme[token.var] = value;
      this.settings.theme = theme;
      this.applyTheme();
    },

    resetTheme() {
      this.settings.theme = {};
      this.applyTheme();
    },

    setBackground(value) {
      this.settings.background = value;
      // A settings change should show immediately, so drop any scene backdrop
      // that would otherwise hide the choice being made.
      this.sceneBackgroundFile = "";
      this.applyBackground();
    },

    setBackgroundDim(value) {
      this.settings.background_dim = parseInt(value, 10);
      this.applyBackground();
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

    // A scene set by the background_swap pass wins; otherwise the backdrop
    // chosen in settings. Kept in one place so the two cannot fight.
    applyBackground(id) {
      if (id) {
        const found = ((this.character || {}).backgrounds || []).find(
          (b) => (b.id || b.img) === id
        );
        this.sceneBackgroundFile = found ? found.img : "";
      }
      const file = this.sceneBackgroundFile || this.backgroundFile();
      const dim = Number.isFinite(this.settings.background_dim)
        ? this.settings.background_dim : 70;

      // The wash derives from --bg rather than being a fixed dark overlay, so
      // it works on a light palette as well as a dark one — and it is what
      // keeps text readable over an image at all.
      document.body.style.backgroundImage = file
        ? `linear-gradient(color-mix(in srgb, var(--bg) ${dim}%, transparent),` +
          ` color-mix(in srgb, var(--bg) ${Math.min(100, dim + 7)}%, transparent)),` +
          ` url("/static/backgrounds/${encodeURIComponent(file)}")`
        : "";
      document.body.style.backgroundSize = "cover";
      document.body.style.backgroundPosition = "center";
      document.body.style.backgroundAttachment = "fixed";
    },

    backgroundFile() {
      const chosen = this.settings.background;
      return !chosen || chosen === "none" ? "" : chosen;
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
      // Blank the message and mark it regenerating so the bubble itself shows
      // the typing cue. Keep the old text so a failure can put it back rather
      // than leaving an empty bubble.
      this.regenId = message.id;
      this.regenPrevious = message.text;
      message.text = "";
      await this.runStream(`/api/messages/${message.id}/swipe`, {}, message.id);
    },

    isRegenerating(message) {
      return this.regenId === message.id && !message.text;
    },

    async runStream(url, body, swipeMessageId) {
      this.streaming = true;
      this.composing = !swipeMessageId;
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
        // A failed regeneration must not leave the message blank.
        if (swipeMessageId && this.regenPrevious) {
          const original = this.messages.find((m) => m.id === swipeMessageId);
          if (original && !original.text) original.text = this.regenPrevious;
        }
      } finally {
        this.streaming = false;
        this.composing = false;
        this.regenId = null;
        this.regenPrevious = "";
        this.scrollDown();
        this.loadCost();
      }
    },

    // ---- editing & variants ----

    // Editing must not make the bubble jump: it keeps the width and height the
    // rendered text had, and only grows from there.
    startEdit(message, fromEl) {
      const bubble = fromEl && fromEl.closest(".bubble");
      const body = bubble && bubble.querySelector(".body");
      if (bubble && body) {
        const rect = body.getBoundingClientRect();
        bubble.style.minWidth = `${Math.ceil(bubble.getBoundingClientRect().width)}px`;
        this.editHeight = Math.ceil(rect.height);
      }
      this.editingEl = bubble || null;
      this.editing = message.id;
      this.editText = message.text;
    },

    endEdit() {
      if (this.editingEl) this.editingEl.style.minWidth = "";
      this.editingEl = null;
      this.editHeight = 0;
      this.editing = null;
    },

    cancelEdit() {
      this.endEdit();
    },

    async saveEdit(message, reaudit) {
      const updated = await api.patch(`/api/messages/${message.id}`, {
        text: this.editText,
        reaudit,
      });
      message.text = updated.text;
      message.edited = true;
      this.endEdit();
    },

    // direction: +1 forward (generate a new variant past the end), -1 back.
    async goToVariant(message, direction) {
      if (this.streaming) return;
      const next = (message.variant_index || 0) + direction;
      if (next < 0) return;

      const variants = await api.get(`/api/messages/${message.id}/variants`);
      if (next >= variants.length) {
        // Past the last variant means "give me another one", the same as the
        // swipe button.
        await this.swipe(message);
        return;
      }
      const updated = await api.post(
        `/api/messages/${message.id}/variants/${variants[next].id}`
      );
      message.text = updated.text;
      message.variant_index = updated.variant_index;
      message.variant_count = updated.variant_count;
    },

    // ---- swipe gestures (§9) ----
    //
    // Swipe the message itself: left goes forward (and generates a new variant
    // once you are past the last one), right goes back. Pointer events rather
    // than touch events so a mouse behaves identically on the desktop.
    //
    // The chat is a vertical scroller, so a horizontal gesture has to be
    // claimed carefully: nothing happens until the drag is clearly sideways,
    // and once it is, the scroller must not also act on it.

    dragId: null,
    dragDx: 0,
    dragStart: null,

    swipeable(message) {
      return message.role === "assistant" && this.editing !== message.id && !this.streaming;
    },

    onSwipeStart(event, message) {
      if (!this.swipeable(message) || !event.isPrimary) return;
      this.dragStart = { x: event.clientX, y: event.clientY, id: message.id, claimed: false };
      this.dragDx = 0;
    },

    onSwipeMove(event, message) {
      const start = this.dragStart;
      if (!start || start.id !== message.id) return;
      const dx = event.clientX - start.x;
      const dy = event.clientY - start.y;

      if (!start.claimed) {
        // Undecided: let small movements and anything vertical belong to the
        // scroller. Requiring horizontal dominance stops a slightly-diagonal
        // scroll from being read as a swipe.
        if (Math.abs(dy) > Math.abs(dx)) { this.dragStart = null; return; }
        if (Math.abs(dx) < SWIPE_CLAIM) return;
        start.claimed = true;
        this.dragId = message.id;
      }
      // Resist dragging right at the first variant — there is nothing there,
      // and the rubber-band says so without an error.
      const atStart = (message.variant_index || 0) === 0;
      this.dragDx = Math.max(-SWIPE_MAX, Math.min(SWIPE_MAX, atStart && dx > 0 ? dx / 3 : dx));
    },

    onSwipeEnd(event, message) {
      const start = this.dragStart;
      const dx = this.dragDx;
      this.dragStart = null;
      this.dragId = null;
      this.dragDx = 0;
      if (!start || !start.claimed || Math.abs(dx) < SWIPE_COMMIT) return;
      this.goToVariant(message, dx < 0 ? 1 : -1);
    },

    swipeHint(message) {
      if (this.dragId !== message.id || Math.abs(this.dragDx) < SWIPE_COMMIT) return "";
      if (this.dragDx < 0) {
        const last = (message.variant_index || 0) >= (message.variant_count || 1) - 1;
        return last ? "new reply" : "next";
      }
      return (message.variant_index || 0) > 0 ? "previous" : "";
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

    settings: { backends: [], tiers: {}, tier_names: [], kinds: [], templates: [],
                kind_defaults: {}, theme_tokens: [], theme: {},
                backgrounds: [], background: "none", background_dim: 70, path: "" },
    settingsOpen: false,
    saving: false,
    saveMsg: "",
    saveError: "",
    testing: "",
    tests: {},
    models: {},
    modelErrors: {},
    loadingModels: "",

    async openSettings() {
      this.saveMsg = "";
      this.saveError = "";
      this.tests = {};
      this.models = {};
      this.modelErrors = {};
      try {
        const loaded = await api.get("/api/settings");
        // Show a dot placeholder rather than the literal mask, so it reads as
        // "a key is set" instead of looking like a corrupted value.
        loaded.backends.forEach((b) => { if (b.api_key === "***") b.api_key = MASK_DISPLAY; });
        this.settings = loaded;
        this.applyTheme();
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
      const backend = {
        name: `backend-${this.settings.backends.length + 1}`,
        kind: "ollama", model: "", base_url: "", api_key: "",
        template: "auto", timeout: 120, models: [],
      };
      this.applyKindDefaults(backend);
      this.settings.backends.push(backend);
    },

    // Reset every connection field to what this kind needs. All of them are
    // kind-specific — an Ollama model name means nothing to Horde, and Horde's
    // anonymous key is not an OpenAI key — so carrying values across a kind
    // change produces a config that looks filled in and is quietly wrong.
    applyKindDefaults(backend) {
      const defaults = (this.settings.kind_defaults || {})[backend.kind];
      if (!defaults) return;
      backend.base_url = defaults.base_url ?? "";
      backend.template = defaults.template ?? "auto";
      backend.timeout = defaults.timeout ?? 120;
      backend.model = defaults.model ?? "";
      backend.api_key = defaults.api_key ?? "";
      backend.models = [];
    },

    onKindChange(backend) {
      this.applyKindDefaults(backend);
      delete this.tests[backend.name];
      delete this.models[backend.name];
      delete this.modelErrors[backend.name];
    },

    // Ask the backend what it can serve. Typing a model name from memory is
    // the easiest way to misconfigure a backend, and the mistake only surfaces
    // later as a confusing provider error.
    async loadModels(backend) {
      this.loadingModels = backend.name;
      delete this.modelErrors[backend.name];
      try {
        const payload = {
          ...backend,
          api_key: backend.api_key === MASK_DISPLAY ? "***" : backend.api_key,
        };
        const response = await fetch("/api/settings/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
          this.modelErrors[backend.name] = result.error || result.detail || response.statusText;
          this.models[backend.name] = [];
        } else {
          this.models[backend.name] = result.models;
          if (!result.models.length) {
            this.modelErrors[backend.name] = "this backend reported no models";
          }
        }
      } catch (e) {
        this.modelErrors[backend.name] = String(e.message || e);
      } finally {
        this.loadingModels = "";
      }
    },

    modelHint(name) {
      if (this.modelErrors[name]) return `couldn't list models — ${this.modelErrors[name]}`;
      const found = this.models[name];
      if (!found) return "";
      return `${found.length} model${found.length === 1 ? "" : "s"} available — tap the field to pick one`;
    },

    kindNote(kind) {
      return ((this.settings.kind_defaults || {})[kind] || {}).note || "";
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
        this.applyTheme();
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
