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

// Transition timings. Kept in JS as well as CSS because the sequence has to
// wait for each step; they must stay in step with styles.css.
const TEXT_FADE_MS = 170;
const BUBBLE_RESIZE_MS = 300;
// Size the bubble draws in to while the cue is showing, so a long reply does
// not leave a screenful of empty card around three dots.
const REGEN_PILL_WIDTH = 84;
const REGEN_PILL_HEIGHT = 46;
// Matches .sheet-leave in styles.css: how long a panel takes to slide away.
const PANEL_LEAVE_MS = 260;
// Matches .msg.sending and .bubble.leaving in styles.css.
const MESSAGE_SEND_MS = 460;
const MESSAGE_LEAVE_MS = 220;
// How long an armed delete stays armed before giving up on the second tap.
const CONFIRM_MS = 3000;
// How long a one-line confirmation ("Copied") stays on screen.
const HINT_MS = 1900;
// Quiet time after the last keystroke before the template preview re-renders.
const PREVIEW_DEBOUNCE_MS = 200;
// How long a prompt section takes to slide past its neighbour when reordered.
const SECTION_MOVE_MS = 260;

// Hold-to-open action wheel.
const HOLD_MS = 380;          // press this long and the wheel opens
const HOLD_SLOP = 10;         // finger movement that cancels the hold instead
const WHEEL_RADIUS = 78;      // how far the options sit from the press point
const WHEEL_PICK_MIN = 34;    // drag at least this far before a release picks
// Magnet. Within this distance an option starts leaning towards the finger,
// travelling at most WHEEL_MAGNET_MAX px from where it sits. It is what makes
// the wheel feel like it is reaching for the thumb rather than waiting to be
// hit exactly, and it is a much bigger help on a phone than the extra few
// pixels of hit area would be.
const WHEEL_MAGNET_RANGE = 96;
const WHEEL_MAGNET_MAX = 14;
// How long the fly-out runs before the options are left to the magnet. Matches
// the animation plus the last option's stagger in styles.css.
const WHEEL_SETTLE_MS = 340 + 34 * 5;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// How far from the bottom still counts as "following the conversation". Big
// enough to absorb sub-pixel scroll heights and the rubber-band at the end of
// a touch scroll, small enough that one deliberate flick upward detaches.
const BOTTOM_SLACK = 48;
// How long after a gesture on the scroller its scroll events still count as
// the user's doing. A drag fires move and scroll events together, so this only
// has to outlast one frame; kept short so a later layout shift is never
// mistaken for the tail of a gesture.
const GESTURE_WINDOW_MS = 250;

// Pull-up-past-the-end, which reveals the impersonate control.
const PULL_DISTANCE = 96;      // travel past the bottom that fully reveals it
const PULL_SETTLE_MS = 220;    // a wheel has no "let go"; a pause stands in

// Swipe thresholds, in CSS pixels.
const SWIPE_CLAIM = 12;   // movement before a drag counts as horizontal at all
const SWIPE_COMMIT = 64;  // release past this and the variant changes
const SWIPE_MAX = 130;    // furthest the bubble travels

// FastAPI puts a rejection's reason in `detail`; our own handlers use `error`.
// Without this a 400 that says exactly what is wrong — "a character needs a
// name" — reaches the user as "Bad Request".
async function apiError(response) {
  const body = await response.json().catch(() => ({}));
  return new Error(body.detail || body.error || response.statusText);
}

const jsonRequest = (method) => async function (path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw await apiError(r);
  return r.json();
};

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw await apiError(r);
    return r.json();
  },
  post: jsonRequest("POST"),
  put: jsonRequest("PUT"),
  patch: jsonRequest("PATCH"),
  async del(path) {
    const r = await fetch(path, { method: "DELETE" });
    if (!r.ok) throw await apiError(r);
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
    fadingId: null,
    // Following the newest message. Cleared when the user scrolls up to read,
    // restored when they come back down or ask for the newest message.
    stick: true,
    scrollPort: null,
    // The header menu, and which of its destinations is open. One panel at a
    // time — "" means the conversation is unobstructed.
    menu: false,
    // Two fields rather than one: `panel` says which body to render and
    // `panelOpen` says whether the sheet is on screen. Clearing the name at
    // the moment of closing would unmount the body through its own x-if, and
    // the sheet would slide away empty.
    panel: "",
    panelOpen: false,
    historyFor: "",
    confirmChar: "",
    confirmChat: "",
    confirmMsg: "",
    // Hold-to-open action wheel. `wheel` is null when closed; when open it
    // carries the message, where it was opened, and which option the finger
    // is currently over.
    wheel: null,
    wheelSettled: false,
    wheelHint: "",
    // How far the pull-up control is revealed, 0 to 1, and whether letting go
    // now would fire it.
    reveal: 0,
    revealArmed: false,
    composerMenu: false,
    impersonating: false,
    // The message currently playing the send animation. Held only long enough
    // for the keyframes to run; a class left on would replay on every re-render.
    sendingId: "",
    // Live while a reply is streaming, so it can be called off.
    streamAbort: null,
    draftCharacter: { id: "", name: "" },
    // The alternates are a list on the card and a paragraph-separated textarea
    // in the editor. Held separately so the textarea can be edited freely —
    // splitting on every keystroke would renumber the list under the cursor.
    altGreetings: "",
    stopStrings: "",
    previewFor: "",
    previewText: "",
    previewStop: "",
    previewTimer: 0,
    openBlock: "",
    armedBlock: "",
    armedBlockTimer: 0,
    promptOpen: false,
    personas: [],
    note: { text: "", depth: 0, frequency: 1 },
    noteFromChat: false,
    noteMsg: "",
    persona: null,
    draftPersona: { id: "", name: "", description: "", avatar: "", is_default: false },
    savingPersona: false,
    personaMsg: "",
    personaError: "",
    uploadingAvatar: false,
    confirmPersona: "",
    savingCharacter: false,
    charMsg: "",
    charError: "",
    passes: [],
    passMsg: "",
    // Detail that is right at its default until it is not, folded away.
    showAdvancedBrain: false,
    showAdvancedTheme: false,
    backdrops: [],
    uploadingBg: false,
    bgMsg: "",
    confirmBg: "",
    importing: false,
    importMsg: "",
    importError: "",
    hud: false,
    error: "",

    streaming: false,
    composing: false,
    composingKind: "typing",
    composingLabel: "Typing…",
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

    // Whether the scene pass has produced anything yet. Before it has, the
    // header shows the name alone rather than a row of placeholder dashes.
    get hasScene() {
      return !!(this.scene.place || this.scene.weather || this.scene.time);
    },

    get portrait() {
      if (!this.character) return "";
      const set = this.character.pfp_set || {};
      const file = set[this.expression] || set.neutral || "";
      return file ? `/static/characters/${file}` : "";
    },

    async newChat(characterId) {
      const id = characterId || this.characterId;
      const chat = await api.post("/api/chats", { character_id: id });
      this.chats = await api.get("/api/chats");
      await this.openChat(chat.id);
      this.closePanel();
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
      this.persona = data.persona || null;
      this.turn = this.messages.length ? this.messages[this.messages.length - 1].turn : 0;
      this.sceneBackgroundFile = "";
      this.applyTheme();

      // Cleared before being refilled. Merging onto whatever the last chat
      // left behind meant a brand-new conversation opened showing the previous
      // one's weather — the slice is per chat, so its absence is information.
      this.scene = { place: "", weather: "", time: "" };
      this.expression = "neutral";
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
      // Resume following before the messages render — the observer re-pins
      // once they do, so this does not need to wait for layout.
      this.scrollDown();
    },

    // ---- header menu & panels ----

    // Every destination loads what it needs on the way in, so nothing is
    // fetched for a panel the user never opens. Opening one closes the menu:
    // the icon row has done its job and the world line can come back up.
    async openPanel(name) {
      if (this.panelOpen && this.panel === name) return this.closePanel();
      this.panel = name;
      this.panelOpen = true;
      this.saveMsg = "";
      this.saveError = "";
      try {
        if (name === "brain") {
          this.passMsg = "";
          await this.loadSettings();
          this.passes = await api.get("/api/passes");
        } else if (name === "theme") {
          this.bgMsg = "";
          await this.loadSettings();
          await this.loadBackdrops();
        } else if (name === "story") {
          await this.loadNote();
        } else if (name === "chats") {
          this.characters = await api.get("/api/characters");
          this.chats = await api.get("/api/chats");
          await this.loadPersonas();
          // Every history starts closed: a roster of characters is the thing
          // being looked at, and one of them unrolled pushes the rest down.
          this.historyFor = "";
        }
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    closePanel() {
      this.panelOpen = false;
      this.confirmChar = "";
      this.confirmChat = "";
      // Drop the body only once the sheet has finished leaving, and only if
      // nothing has been opened in the meantime.
      setTimeout(() => { if (!this.panelOpen) this.panel = ""; }, PANEL_LEAVE_MS);
    },

    panelTitle() {
      return {
        brain: "Model & engine",
        theme: "Appearance",
        chats: "Characters & chats",
        character: "Edit character",
        persona: "Edit persona",
        story: "Story state",
      }[this.panel] || "";
    },

    // Refresh the world line by hand. The pass runs on the server and reports
    // over the same event stream as a scheduled run, so the indicator, the HUD
    // and the resulting write all behave identically — this only bypasses the
    // decision about *when*.
    async refreshWorld() {
      if (!this.chatId || this.refreshing.scene) return;
      this.refreshing.scene = true;
      try {
        await api.post(`/api/chats/${this.chatId}/passes/scene/run`, {});
      } catch (e) {
        this.refreshing.scene = false;
        this.error = String(e.message || e);
      }
    },

    // Opening the roster from the header is about *this* character, so theirs
    // is the history that unrolls — the general entry from the menu leaves
    // everything closed.
    async openCharacters() {
      const current = this.characterId;
      await this.openPanel("chats");
      this.historyFor = current;
    },

    // The server is the record of what was actually kept, which after a stop
    // is not what the placeholder holds.
    async reloadMessages() {
      if (!this.chatId) return;
      try {
        this.messages = await api.get(`/api/chats/${this.chatId}/messages`);
      } catch (_) { /* the chat may have gone */ }
    },

    // ---- instruct templates ----

    // A newline in a turn marker is the normal case, and a textbox that eats
    // it is unusable — so they are shown and typed as \n and converted at the
    // edges. Nothing else is escaped: these are literal strings otherwise.
    showEscapes(value) {
      return String(value || "").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
    },

    parseEscapes(value) {
      return String(value || "").replace(/\\n/g, "\n").replace(/\\t/g, "\t");
    },

    setTemplateField(backend, key, raw) {
      backend.template_spec = { ...(backend.template_spec || {}), [key]: this.parseEscapes(raw) };
      this.queuePreview(backend);
    },

    // One request per pause, not one per keystroke. A turn marker is typed a
    // character at a time and each one would otherwise be a round trip, with
    // the replies free to land out of order and leave the preview showing a
    // prefix of what is in the boxes.
    queuePreview(backend) {
      if (this.previewFor !== backend.name) return;
      clearTimeout(this.previewTimer);
      this.previewTimer = setTimeout(() => this.refreshPreview(backend), PREVIEW_DEBOUNCE_MS);
    },

    useTemplatePreset(backend, name) {
      const preset = (this.settings.template_presets || {})[name];
      if (!preset) return;
      // Replaced wholesale: half of one format and half of another is not a
      // format, and it fails in ways that read as a bad prompt.
      backend.template_spec = { ...preset };
      this.flashHint(`Filled in from ${name}`);
      // Not queued: a preset arrives all at once, so there is no pause to wait
      // for and the wait would just read as lag.
      if (this.previewFor === backend.name) this.refreshPreview(backend);
    },

    async previewTemplate(backend) {
      clearTimeout(this.previewTimer);
      if (this.previewFor === backend.name) {
        this.previewFor = "";
        return;
      }
      this.previewFor = backend.name;
      await this.refreshPreview(backend);
    },

    // Rendered by the server, by the same function that runs for real. A
    // preview drawn any other way is a second implementation that can
    // disagree with the first.
    async refreshPreview(backend) {
      try {
        const body = await api.post("/api/settings/template/preview", {
          template: backend.template,
          template_spec: backend.template_spec || {},
        });
        this.previewText = body.prompt;
        this.previewStop = body.stop.length
          ? `Stops at: ${body.stop.map((s) => this.showEscapes(s)).join("  ")}`
          : "No stop sequences from this template.";
      } catch (e) {
        this.previewText = String(e.message || e);
        this.previewStop = "";
      }
    },

    // ---- prompt manager ----

    sectionsIn(band) {
      return (this.settings.prompt_sections || []).filter((s) => s.band === band);
    },

    isFixed(section) {
      return (this.settings.prompt_fixed || []).includes(section.id);
    },

    toggleSection(section) {
      if (this.isFixed(section)) {
        this.flashHint(`${section.label} is what makes it a conversation`);
        return;
      }
      section.enabled = !section.enabled;
    },

    // A section only ever moves inside its own band. Bands do not move at all:
    // the last one changes every turn, and anything that ends up above a
    // changing section is recomputed along with it on every reply. Enforcing
    // that here rather than warning about it means there is no arrangement of
    // these controls that produces a slow prompt.
    moveSection(section, direction) {
      const all = this.settings.prompt_sections;
      const siblings = this.sectionsIn(section.band);
      const at = siblings.indexOf(section);
      const target = siblings[at + direction];
      if (!target) {
        this.flashHint(direction < 0 ? "Already first in its group" : "Already last in its group");
        return;
      }
      const from = all.indexOf(section);
      const to = all.indexOf(target);
      this.flipSections(() => {
        all.splice(from, 1);
        all.splice(to, 0, section);
      });
    },

    // Read every row's position, let Alpine reorder them, then put each one
    // back where it started and release it. The rows travel to their new
    // places instead of teleporting, which is the only way the swap reads as
    // one thing moving past another rather than as two rows blinking.
    flipSections(mutate) {
      const rows = [...document.querySelectorAll(".p-row")];
      const before = new Map(rows.map((r) => [r.dataset.sid, r.getBoundingClientRect().top]));
      mutate();
      this.$nextTick(() => {
        for (const row of document.querySelectorAll(".p-row")) {
          const was = before.get(row.dataset.sid);
          if (was === undefined) continue;
          const delta = was - row.getBoundingClientRect().top;
          if (!delta) continue;
          row.style.transition = "none";
          row.style.transform = `translateY(${delta}px)`;
          requestAnimationFrame(() => {
            row.style.transition = `transform ${SECTION_MOVE_MS}ms var(--ease-out)`;
            row.style.transform = "";
            setTimeout(() => { row.style.transition = ""; }, SECTION_MOVE_MS);
          });
        }
      });
    },

    addBlock(band) {
      const all = this.settings.prompt_sections;
      const siblings = this.sectionsIn(band);
      const last = siblings[siblings.length - 1];
      const block = {
        id: `custom:${Math.random().toString(36).slice(2, 10)}`,
        band, label: "New block", text: "", enabled: true, custom: true,
        note: "Your own text, expanded like the rest of the card.",
      };
      // At the end of its own band, which is where someone looking at the
      // group they just pressed expects it to appear.
      all.splice(last ? all.indexOf(last) + 1 : all.length, 0, block);
      this.openBlock = block.id;
    },

    removeBlock(section) {
      if (this.armedBlock !== section.id) {
        this.armedBlock = section.id;
        clearTimeout(this.armedBlockTimer);
        this.armedBlockTimer = setTimeout(() => { this.armedBlock = ""; }, CONFIRM_MS);
        return;
      }
      this.armedBlock = "";
      const all = this.settings.prompt_sections;
      this.flipSections(() => all.splice(all.indexOf(section), 1));
    },

    // How many sections in this band are actually on, for the collapsed
    // summary — the whole panel is long, and "4 of 6" is the thing you came
    // to check.
    bandSummary(band) {
      const rows = this.sectionsIn(band);
      const on = rows.filter((s) => s.enabled).length;
      return on === rows.length ? `All ${rows.length}` : `${on} of ${rows.length}`;
    },

    // ---- author's note ----

    async loadNote() {
      if (!this.chatId) return;
      this.noteMsg = "";
      try {
        const body = await api.get(`/api/chats/${this.chatId}/note`);
        this.note = body.note;
        this.noteFromChat = body.from_chat;
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    // Saved on change rather than behind a button: there is one field and a
    // couple of sliders, and a note you thought you had set is worse than a
    // save you did not notice.
    async saveNote() {
      if (!this.chatId) return;
      try {
        const body = await api.put(`/api/chats/${this.chatId}/note`, this.note);
        this.note = body.note;
        this.noteFromChat = body.from_chat;
        this.noteMsg = body.note.text.trim() ? "Saved" : "Cleared";
        clearTimeout(this._noteTimer);
        this._noteTimer = setTimeout(() => { this.noteMsg = ""; }, HINT_MS);
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    // ---- personas ----

    async loadPersonas() {
      try {
        this.personas = (await api.get("/api/personas")).personas;
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    // Tapping a persona is "be this person here", not "be this person always" —
    // the star is the one that changes what new chats get.
    async usePersona(persona) {
      if (!this.chatId) return this.makeDefaultPersona(persona);
      try {
        const result = await api.post(`/api/chats/${this.chatId}/persona`,
                                      { persona_id: persona.id });
        this.persona = result.persona;
        this.flashHint(`Playing as ${persona.name}`);
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    async makeDefaultPersona(persona) {
      try {
        await api.put(`/api/personas/${persona.id}`, { is_default: true });
        await this.loadPersonas();
        this.flashHint(`New chats will use ${persona.name}`);
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    newPersona() {
      this.draftPersona = {
        id: "", name: "", description: "", avatar: "",
        // The first one has to be the default; there is nothing else for
        // {{user}} to fall back to.
        is_default: !this.personas.length,
      };
      this.personaMsg = "";
      this.personaError = "";
      this.panel = "persona";
    },

    editPersona(persona) {
      this.draftPersona = { ...persona, is_default: !!persona.is_default };
      this.personaMsg = "";
      this.personaError = "";
      this.panel = "persona";
    },

    async savePersona() {
      this.savingPersona = true;
      this.personaMsg = "";
      this.personaError = "";
      const draft = this.draftPersona;
      try {
        const body = {
          name: draft.name,
          description: draft.description,
          avatar: draft.avatar,
          is_default: !!draft.is_default,
        };
        const saved = draft.id
          ? await api.put(`/api/personas/${draft.id}`, body)
          : await api.post("/api/personas", body);
        this.draftPersona = { ...saved, is_default: !!saved.is_default };
        await this.loadPersonas();
        // The one in use may be the one just renamed.
        if (this.chatId) await this.refreshPersona();
        this.personaMsg = "Saved";
      } catch (e) {
        this.personaError = String(e.message || e);
      } finally {
        this.savingPersona = false;
      }
    },

    async deletePersona(persona) {
      if (this.confirmPersona !== persona.id) {
        this.confirmPersona = persona.id;
        clearTimeout(this._personaTimer);
        this._personaTimer = setTimeout(() => { this.confirmPersona = ""; }, CONFIRM_MS);
        return;
      }
      this.confirmPersona = "";
      try {
        await api.del(`/api/personas/${persona.id}`);
        await this.loadPersonas();
        if (this.chatId) await this.refreshPersona();
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    // Ask the server rather than guessing: which persona is active depends on
    // the chat, the character and the global default, and that resolution
    // lives in one place on purpose.
    async refreshPersona() {
      try {
        this.persona = (await api.get(`/api/chats/${this.chatId}`)).persona;
      } catch (_) { /* the chat may have just been deleted */ }
    },

    async uploadAvatar(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.uploadingAvatar = true;
      this.personaError = "";
      try {
        const response = await fetch(
          `/api/avatars?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        if (!response.ok) throw await apiError(response);
        this.draftPersona.avatar = (await response.json()).name;
      } catch (e) {
        this.personaError = String(e.message || e);
      } finally {
        this.uploadingAvatar = false;
        event.target.value = "";
      }
    },

    // ---- composer actions ----

    composerActions() {
      const lastReply = [...this.messages].reverse()
        .find((m) => m.role === "assistant" && m.id !== "streaming");
      return [
        {
          id: "regenerate", label: "Regenerate", icon: "#i-refresh",
          note: "Another version of the last reply",
          disabled: this.streaming || !lastReply,
          run: () => this.goToVariant(lastReply, 1),
        },
        {
          id: "continue", label: "Continue", icon: "#i-continue",
          note: "Carry on from where the reply stopped",
          disabled: this.streaming || !lastReply,
          run: () => this.continueReply(lastReply),
        },
        {
          id: "impersonate", label: "Impersonate", icon: "#i-impersonate",
          note: "Write my next message for me",
          disabled: this.streaming || !this.chatId,
          run: () => this.impersonate(),
        },
        {
          id: "refresh", label: "Refresh world info", icon: "#i-place",
          note: "Re-read place, weather and time",
          disabled: this.refreshing.scene || !this.chatId,
          run: () => this.refreshWorld(),
        },
        {
          id: "newchat", label: "New chat", icon: "#i-plus",
          note: "Start again with this character",
          disabled: !this.characterId,
          run: () => this.newChat(this.characterId),
        },
        {
          id: "attach", label: "Send attachment", icon: "#i-attach",
          note: "Not built yet", soon: true,
        },
      ];
    },

    runComposerAction(action) {
      this.composerMenu = false;
      if (action.soon) return this.flashHint(`${action.label} is not built yet`);
      if (action.run) action.run();
    },

    // ---- impersonate ----

    // Streams into the draft rather than into the conversation: it is a
    // suggestion for the user's own message, so it lands where they can rewrite
    // it before it counts as having been said.
    async impersonate() {
      if (!this.chatId || this.streaming) return;
      this.reveal = 0;
      this.revealArmed = false;
      this.streaming = true;
      this.impersonating = true;
      this.draft = "";
      let buffer = "";
      try {
        const response = await fetch(`/api/chats/${this.chatId}/impersonate`, { method: "POST" });
        if (!response.ok) throw await apiError(response);
        for await (const event of sseStream(response)) {
          if (event.type === "delta") {
            buffer += event.text;
            this.draft = buffer;
          } else if (event.type === "impersonated") {
            this.draft = event.text;
          } else if (event.type === "error") {
            this.error = event.error;
          }
        }
      } catch (e) {
        this.error = String(e.message || e);
      } finally {
        this.streaming = false;
        this.impersonating = false;
        // The composer grew while the text arrived; let it settle to the size
        // the final draft actually needs.
        this.$nextTick(() => {
          if (this.$refs.input) {
            this.autosize(this.$refs.input);
            this.$refs.input.focus();
          }
        });
      }
    },

    // ---- backdrops ----

    async loadBackdrops() {
      try {
        this.backdrops = (await api.get("/api/backgrounds")).backgrounds;
      } catch (e) {
        this.bgMsg = String(e.message || e);
      }
    },

    // Posted as a raw body rather than multipart: the server wants the bytes
    // and the name, and a form encoding for two values is a lot of ceremony
    // for a phone to do to a 4 MB photo.
    async uploadBackdrop(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.uploadingBg = true;
      this.bgMsg = "";
      try {
        const response = await fetch(
          `/api/backgrounds?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file },
        );
        if (!response.ok) throw await apiError(response);
        const added = await response.json();
        await this.loadBackdrops();
        this.settings.backgrounds = this.backdrops.map((b) => b.name);
        this.setBackground(added.name);
        this.bgMsg = `Added ${added.name}`;
      } catch (e) {
        this.bgMsg = String(e.message || e);
      } finally {
        this.uploadingBg = false;
        event.target.value = "";   // so the same file can be picked again
      }
    },

    async deleteBackdrop(backdrop) {
      if (this.confirmBg !== backdrop.name) {
        this.confirmBg = backdrop.name;
        clearTimeout(this._bgTimer);
        this._bgTimer = setTimeout(() => { this.confirmBg = ""; }, CONFIRM_MS);
        return;
      }
      this.confirmBg = "";
      try {
        const result = await api.del(`/api/backgrounds/${encodeURIComponent(backdrop.name)}`);
        await this.loadBackdrops();
        this.settings.backgrounds = this.backdrops.map((b) => b.name);
        // The server resets the setting if the deleted image was in use; take
        // its word for what the backdrop is now rather than assuming.
        this.setBackground(result.background);
        this.bgMsg = `Removed ${backdrop.name}`;
      } catch (e) {
        this.bgMsg = String(e.message || e);
      }
    },

    // ---- theme presets ----

    // Whole palettes rather than one colour at a time. Each is the two or
    // three tokens that actually carry a look; the rest follow from them.
    themePresets() {
      return [
        {
          id: "rose", label: "Rose",
          swatch: "background: linear-gradient(135deg,#fdf7f9,#c2617f)",
          theme: {},   // the stylesheet's own defaults
        },
        {
          id: "slate", label: "Slate",
          swatch: "background: linear-gradient(135deg,#f4f6f8,#4c6b8a)",
          theme: {
            "--bg": "#f5f7f9", "--panel": "#ffffff", "--panel-2": "#eceff3",
            "--line": "#dbe1e8", "--text": "#2b3138", "--muted": "#78828d",
            "--accent": "#4c6b8a", "--user-bubble": "#e9eef4", "--ai-bubble": "#ffffff",
          },
        },
        {
          id: "amber", label: "Amber",
          swatch: "background: linear-gradient(135deg,#fdf8f0,#b1762a)",
          theme: {
            "--bg": "#fdf8f0", "--panel": "#fffdf9", "--panel-2": "#f6ecdc",
            "--line": "#ecdcc2", "--text": "#38301f", "--muted": "#8a7c64",
            "--accent": "#b1762a", "--user-bubble": "#f7ecd9", "--ai-bubble": "#fffdf9",
          },
        },
        {
          id: "night", label: "Night",
          swatch: "background: linear-gradient(135deg,#1a1b20,#8f7fd4)",
          theme: {
            "--bg": "#16171b", "--panel": "#1e2026", "--panel-2": "#262931",
            "--line": "#333743", "--text": "#e6e4ea", "--muted": "#9a97a5",
            "--accent": "#8f7fd4", "--user-bubble": "#2a2733", "--ai-bubble": "#1e2026",
            "--c-default": "#e6e4ea", "--c-dialogue": "#e0a3bd", "--c-action": "#a99ae0",
            "--c-strong": "#d8b06a",
          },
        },
      ];
    },

    applyPreset(preset) {
      // Replace rather than merge: a preset is a whole look, and leaving one
      // stray token from the last one is how palettes end up unreadable.
      this.settings.theme = { ...preset.theme };
      this.applyTheme();
    },

    accentToken() {
      return (this.settings.theme_tokens || []).find((t) => t.var === "--accent")
        || { var: "--accent", type: "color", default: "#c2617f", label: "Accent" };
    },

    // ---- characters ----

    chatsFor(characterId) {
      return this.chats.filter((c) => c.character_id === characterId);
    },

    whenLabel(chat) {
      const when = chat.updated_at || chat.created_at;
      if (!when) return "";
      const date = new Date(when * (when > 1e11 ? 1 : 1000));
      if (Number.isNaN(date.getTime())) return "";
      const days = Math.floor((Date.now() - date.getTime()) / 86400000);
      if (days <= 0) return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (days === 1) return "yesterday";
      if (days < 7) return `${days}d ago`;
      return date.toLocaleDateString([], { day: "numeric", month: "short" });
    },

    toggleHistory(characterId) {
      this.historyFor = this.historyFor === characterId ? "" : characterId;
    },

    // The file goes up as its own bytes: the server sniffs a PNG by signature
    // and parses JSON otherwise, so a card works whichever of the two forms it
    // arrived in without the browser having to tell them apart.
    async importCard(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.importing = true;
      this.importMsg = "";
      this.importError = "";
      try {
        const response = await fetch(
          `/api/characters/import?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        if (!response.ok) throw await apiError(response);
        const added = await response.json();
        this.characters = await api.get("/api/characters");
        this.importMsg = `Imported ${added.name}`;
      } catch (e) {
        this.importError = String(e.message || e);
      } finally {
        this.importing = false;
        event.target.value = "";
      }
    },

    async newCharacter() {
      try {
        const created = await api.post("/api/characters", { name: "New character" });
        this.characters = await api.get("/api/characters");
        await this.editCharacter(created.id);
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    async editCharacter(characterId) {
      this.charMsg = "";
      this.charError = "";
      try {
        this.draftCharacter = await api.get(`/api/characters/${characterId}`);
        this.altGreetings = (this.draftCharacter.alternate_greetings || []).join("\n\n");
        this.stopStrings = (this.draftCharacter.stop_strings || []).join("\n");
        this.panel = "character";
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    async saveCharacter() {
      this.savingCharacter = true;
      this.charMsg = "";
      this.charError = "";
      const draft = this.draftCharacter;
      try {
        const saved = await api.put(`/api/characters/${draft.id}`, {
          name: draft.name,
          persona: draft.persona,
          first_mes: draft.first_mes,
          example_dialogue: draft.example_dialogue,
          scenario: draft.scenario,
          system_prompt: draft.system_prompt,
          post_history_instructions: draft.post_history_instructions,
          alternate_greetings: this.altGreetings,
          stop_strings: this.stopStrings,
        });
        this.characters = await api.get("/api/characters");
        // The open chat holds its own copy of the card, and the header reads
        // the name off that — without this the title keeps the old name until
        // the chat is reopened.
        if (this.character && this.character.id === saved.id) this.character = saved;
        this.charMsg = "saved";
      } catch (e) {
        this.charError = String(e.message || e);
      } finally {
        this.savingCharacter = false;
      }
    },

    // Two taps rather than a confirm dialog: a modal over a sheet on a phone is
    // its own problem, and the second tap is the same finger in the same place.
    async deleteCharacter(character) {
      if (this.confirmChar !== character.id) {
        this.confirmChar = character.id;
        this.confirmChat = "";
        return;
      }
      this.confirmChar = "";
      try {
        await api.del(`/api/characters/${character.id}`);
        this.characters = await api.get("/api/characters");
        this.chats = await api.get("/api/chats");
        // Deleting the character behind the open chat takes the chat with it,
        // so the app has to land somewhere real rather than on a dead id.
        if (character.id === this.characterId) await this.fallbackChat();
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    async deleteChat(chat) {
      if (this.confirmChat !== chat.id) {
        this.confirmChat = chat.id;
        this.confirmChar = "";
        return;
      }
      this.confirmChat = "";
      try {
        await api.del(`/api/chats/${chat.id}`);
        this.chats = await api.get("/api/chats");
        this.characters = await api.get("/api/characters");
        if (chat.id === this.chatId) await this.fallbackChat();
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    // Land on something after deleting whatever was open.
    async fallbackChat() {
      if (this.chats.length) return this.openChat(this.chats[0].id);
      this.chatId = "";
      this.messages = [];
      this.character = null;
      localStorage.removeItem("tavern:chat");
      if (this.characters.length) {
        this.characterId = this.characters[0].id;
        await this.newChat(this.characterId);
      } else {
        this.characterId = "";
        this.error = "No characters left. Add one from Characters & chats.";
      }
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
            this.composingLabel = event.run.animation === "typing" ? "Typing…" : event.run.label;
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
          ` url("/backgrounds/${encodeURIComponent(file)}")`
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
      this.scrollDown();
      // Hand over to the typing cue gradually: fade the old text, shrink the
      // bubble to the cue's size, fade the cue in. Swapping the two outright
      // reads as a glitch — the text vanishes and the bubble collapses in the
      // same frame, then snaps back open when the first token lands.
      await this.beginRegen(message);
      await this.runStream(`/api/messages/${message.id}/swipe`, {}, message.id);
    },

    bubbleFor(id) {
      return document.querySelector(`.bubble[data-mid="${CSS.escape(id)}"]`);
    },

    // Hold the bubble at an exact size. Both bounds, always set together: a
    // maximum on its own cannot keep the box open once the text is cleared out
    // from under it — the bubble collapses onto the cue in a single frame and
    // the shrink never animates — and a minimum on its own stops binding the
    // moment the new reply arrives, so the grow snaps instead. Pinned to the
    // same number they pin the box outright, which is what animates.
    setPin(el, width, height) {
      el.style.minWidth = width;
      el.style.maxWidth = width;
      el.style.minHeight = height;
      el.style.maxHeight = height;
    },

    // Commit a size without animating to it. A transition always runs from the
    // element's *current* value, so setting a start value while the transition
    // is live animates towards it — and the intended animation then starts
    // from a value that has not moved yet and does nothing.
    pinInstant(el, width, height) {
      el.classList.add("no-anim");
      this.setPin(el, width, height);
      void el.offsetWidth;              // commit before re-enabling transitions
      el.classList.remove("no-anim");
    },

    pinTo(el, width, height) {
      this.setPin(el, width, height);
    },

    // Size the element would take unpinned. Every read happens while
    // transitions are off: re-arming them before measuring means the browser
    // has already started animating back towards the free size, and the
    // measurement catches a box mid-flight — a tall thin column that then gets
    // animated to.
    measureNatural(el) {
      const previous = {
        minWidth: el.style.minWidth, maxWidth: el.style.maxWidth,
        minHeight: el.style.minHeight, maxHeight: el.style.maxHeight,
      };
      el.classList.add("no-anim");
      this.setPin(el, "", "");
      void el.offsetWidth;
      const size = { width: el.offsetWidth, height: el.offsetHeight };
      Object.assign(el.style, previous);
      void el.offsetWidth;
      el.classList.remove("no-anim");
      return size;
    },

    async beginRegen(message) {
      const bubble = this.bubbleFor(message.id);
      this.regenPrevious = message.text;
      // Start from the footprint the message already had, so nothing jumps at
      // the moment the cue takes over. `clipping` hides text that no longer
      // fits while the box is contracting around it.
      if (bubble) {
        bubble.classList.add("clipping");
        this.pinInstant(bubble, `${bubble.offsetWidth}px`, `${bubble.offsetHeight}px`);
      }

      this.fadingId = message.id;
      await sleep(TEXT_FADE_MS);

      this.regenId = message.id;
      message.text = "";
      this.fadingId = null;

      // Then draw in to a compact pill, both ways. A full-width card holding
      // three dots looks like a rendering fault; contracting to something the
      // size of the cue reads as the message being re-thought.
      if (bubble) {
        requestAnimationFrame(() =>
          this.pinTo(bubble, `${REGEN_PILL_WIDTH}px`, `${REGEN_PILL_HEIGHT}px`));
      }
    },

    // First tokens: expand back out of the pill to whatever the new reply
    // needs, rather than popping to full width in one frame.
    endRegen(message, applyText) {
      const bubble = this.bubbleFor(message.id);
      applyText();
      this.regenId = null;
      if (!bubble) return;

      // $nextTick alone lands before the cue has actually been swapped out for
      // the text, so the measurement comes back as the pill's own size and the
      // pin becomes a no-op — leaving the release to do the resizing, uncapped
      // and unanimated. One frame further on the DOM holds the new reply.
      this.$nextTick(() => requestAnimationFrame(() => {
        const natural = this.measureNatural(bubble);
        requestAnimationFrame(() =>
          this.pinTo(bubble, `${natural.width}px`, `${natural.height}px`));
        // Release entirely so streaming text can keep growing the bubble.
        setTimeout(() => this.releaseRegenPin(message.id), BUBBLE_RESIZE_MS);
      }));
    },

    releaseRegenPin(messageId) {
      const bubble = this.bubbleFor(messageId);
      if (!bubble) return;
      this.setPin(bubble, "", "");
      bubble.classList.remove("clipping");
    },

    isRegenerating(message) {
      return this.regenId === message.id && !message.text;
    },

    // Stops whatever is generating. The fetch is aborted, which drops the
    // connection; the server sees the reader hang up, keeps the text that had
    // already arrived and records the run as stopped.
    stopGenerating() {
      if (this.streamAbort) this.streamAbort.abort();
    },

    async runStream(url, body, swipeMessageId) {
      this.streaming = true;
      this.streamAbort = new AbortController();
      this.composing = !swipeMessageId;
      this.composingKind = "typing";
      this.composingLabel = "Typing…";
      this.hudRuns = [];

      let target = null;
      let buffer = "";
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: this.streamAbort.signal,
        });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);

        for await (const event of sseStream(response)) {
          switch (event.type) {
            case "turn_start":
              this.turn = event.turn;
              this.messages.push(event.message);
              // Fly it up from the composer rather than having it appear in
              // the column. Cleared once the keyframes are done so a later
              // re-render of the same message does not replay them.
              this.sendingId = event.message.id;
              setTimeout(() => {
                if (this.sendingId === event.message.id) this.sendingId = "";
              }, MESSAGE_SEND_MS);
              this.scrollDown();
              break;

            case "delta":
              buffer += event.text;
              if (!target) {
                // Stream into a placeholder so the reply renders live, with
                // markup colouring applied on every frame (§8).
                if (swipeMessageId) {
                  target = this.messages.find((m) => m.id === swipeMessageId);
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

                if (target && this.regenId === target.id) {
                  // First token of a regeneration: grow out of the typing cue
                  // instead of snapping open. The callback writes the buffer as
                  // it stands when the animation measures, so it cannot undo a
                  // later delta the way a captured copy would.
                  this.endRegen(target, () => { target.text = buffer; });
                  break;
                }
              }
              if (target) target.text = buffer;
              // No scroll call here on purpose: the observer follows the text
              // as it grows, and forcing it per delta would drag the user back
              // down every token if they had scrolled up to read.
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
        if (e.name === "AbortError") {
          // Stopping is something the user did on purpose, so it is not an
          // error. The text that arrived stays where it is; the placeholder is
          // reconciled with what the server actually kept.
          this.flashHint("Stopped");
          await this.reloadMessages();
        } else {
          this.error = String(e.message || e);
          this.messages = this.messages.filter((m) => m.id !== "streaming");
        }
        // A failed regeneration must not leave the message blank.
        if (swipeMessageId && this.regenPrevious) {
          const original = this.messages.find((m) => m.id === swipeMessageId);
          if (original && !original.text) original.text = this.regenPrevious;
        }
      } finally {
        this.streaming = false;
        this.composing = false;
        this.streamAbort = null;
        // Only release here if no token ever arrived — a stream that failed or
        // returned nothing. When endRegen ran it already owns the release, and
        // clearing the caps underneath it mid-animation drops the bubble to
        // whatever its half-grown width implies: a brief, very tall column.
        if (swipeMessageId && this.regenId === swipeMessageId) {
          this.releaseRegenPin(swipeMessageId);
        }
        this.regenId = null;
        this.regenPrevious = "";
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

    // Same two-tap arming as the character and chat deletes: on a phone the
    // buttons sit under a thumb that is already moving.
    async deleteMessage(message) {
      if (this.confirmMsg !== message.id) {
        this.confirmMsg = message.id;
        clearTimeout(this._confirmTimer);
        // Unlike a list row, a message scrolls away. An armed button left
        // behind somewhere off-screen is a trap, so it disarms itself.
        this._confirmTimer = setTimeout(() => { this.confirmMsg = ""; }, CONFIRM_MS);
        return;
      }
      this.confirmMsg = "";
      clearTimeout(this._confirmTimer);
      const el = this.bubbleFor(message.id);
      try {
        await api.del(`/api/messages/${message.id}`);
      } catch (e) {
        this.error = String(e.message || e);
        return;
      }
      if (el) {
        // Let it shrink out of the column rather than blinking away and
        // yanking everything below it up a bubble's height.
        el.classList.add("leaving");
        await sleep(MESSAGE_LEAVE_MS);
      }
      this.messages = this.messages.filter((m) => m.id !== message.id);
    },

    // Extends the reply in place. Unlike a swipe there is nothing to choose
    // between afterwards, so it streams into the bubble that is already on
    // screen rather than creating a variant.
    async continueReply(message) {
      if (this.streaming || !message) return;
      this.streaming = true;
      this.streamAbort = new AbortController();
      const start = message.text || "";
      let buffer = "";
      try {
        const response = await fetch(`/api/messages/${message.id}/continue`, {
          method: "POST", signal: this.streamAbort.signal,
        });
        if (!response.ok) throw await apiError(response);
        for await (const event of sseStream(response)) {
          if (event.type === "delta") {
            buffer += event.text;
            message.text = `${start}${start && !/\s$/.test(start) ? " " : ""}${buffer}`;
          } else if (event.type === "continued") {
            message.text = event.text;
          } else if (event.type === "error") {
            this.error = event.error;
          }
        }
      } catch (e) {
        if (e.name === "AbortError") {
          this.flashHint("Stopped");
          await this.reloadMessages();
        } else {
          this.error = String(e.message || e);
          message.text = start;
        }
      } finally {
        this.streaming = false;
        this.streamAbort = null;
      }
    },

    // Hidden messages stay on screen and leave the prompt. Not a `stage`: the
    // eviction ladder owns that and would promote it back.
    async toggleHidden(message) {
      const next = !message.hidden;
      try {
        await api.post(`/api/messages/${message.id}/hidden`, { hidden: next });
        message.hidden = next;
        this.flashHint(next ? "Hidden from the prompt" : "Back in the prompt");
      } catch (e) {
        this.error = String(e.message || e);
      }
    },

    lastReply() {
      return [...this.messages].reverse()
        .find((m) => m.role === "assistant" && m.id !== "streaming");
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
    hold: null,

    // Only the newest reply can be re-rolled. Regenerating an older one would
    // rewrite history the conversation has already been built on: every reply
    // after it was written in response to the version being replaced, and the
    // state that came with it has already been folded in and decayed. The
    // engine has no way to unwind that, so the honest thing is not to offer it.
    isLastReply(message) {
      if (!message || message.role !== "assistant") return false;
      const last = this.lastReply();
      return !!last && last.id === message.id;
    },

    // The arrows and the swipe are the same act, so they agree on when.
    swipeable(message) {
      return this.isLastReply(message) && this.editing !== message.id && !this.streaming;
    },

    // One press does three different things depending on what happens next:
    // move sideways and it is a swipe, move at all soon after and it belongs to
    // the scroller, stay still and it opens the wheel. All three start here.
    onMsgDown(event, message) {
      if (!event.isPrimary) return;
      this.hold = { x: event.clientX, y: event.clientY, pointerId: event.pointerId, id: message.id };
      clearTimeout(this._holdTimer);
      this._holdTimer = setTimeout(() => this.openWheel(message), HOLD_MS);

      if (!this.swipeable(message)) return;
      this.dragStart = { x: event.clientX, y: event.clientY, id: message.id, claimed: false };
      this.dragDx = 0;
    },

    cancelHold() {
      clearTimeout(this._holdTimer);
      this.hold = null;
    },

    onMsgMove(event, message) {
      // While the wheel is up, the finger is choosing, not dragging.
      if (this.wheel && !this.wheel.released) return this.trackWheel(event);

      if (this.hold) {
        const moved = Math.hypot(event.clientX - this.hold.x, event.clientY - this.hold.y);
        if (moved > HOLD_SLOP) this.cancelHold();
      }

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

    onMsgUp(event, message) {
      this.cancelHold();
      if (this.wheel && !this.wheel.released) return this.releaseWheel(event);

      const start = this.dragStart;
      const dx = this.dragDx;
      this.dragStart = null;
      this.dragId = null;
      this.dragDx = 0;
      if (!start || !start.claimed || Math.abs(dx) < SWIPE_COMMIT) return;
      this.goToVariant(message, dx < 0 ? 1 : -1);
    },

    onMsgCancel() {
      this.cancelHold();
      this.dragStart = null;
      this.dragId = null;
      this.dragDx = 0;
    },

    // ---- hold-to-open action wheel ----

    // Everything a message can have done to it, in one place. Regenerate is
    // deliberately absent: the arrows and the swipe already cover it.
    wheelOptions(message) {
      const isReply = this.isLastReply(message);
      const hidden = !!(message && message.hidden);
      return [
        { id: "edit", label: "Edit", icon: "#i-edit" },
        {
          id: "hide",
          label: hidden ? "Unhide" : "Hide",
          icon: hidden ? "#i-eye" : "#i-eye-off",
        },
        ...(isReply ? [{ id: "continue", label: "Continue", icon: "#i-continue" }] : []),
        { id: "copy", label: "Copy", icon: "#i-copy" },
        { id: "delete", label: "Delete", icon: "#i-delete", danger: true },
        { id: "suggest", label: "Suggest edit", icon: "#i-suggest", soon: true },
        { id: "react", label: "React", icon: "#i-react", soon: true },
      ];
    },

    openWheel(message) {
      const hold = this.hold;
      if (!hold || this.streaming || this.editing === message.id) return;

      // The hold has won; whatever the swipe was accumulating is not a swipe.
      this.dragStart = null;
      this.dragId = null;
      this.dragDx = 0;

      // Keep receiving move and up events after the finger leaves the bubble —
      // the options sit outside it by design.
      const bubble = this.bubbleFor(message.id);
      if (bubble) {
        try { bubble.setPointerCapture(hold.pointerId); } catch (_) { /* mouse, already gone */ }
      }

      // Keep the whole circle on screen: opened against an edge it would put
      // half its options where no finger can reach them.
      const options = this.wheelOptions(message);
      const margin = WHEEL_RADIUS + 42;
      const step = 360 / options.length;
      this.wheel = {
        message,
        cx: Math.max(margin, Math.min(window.innerWidth - margin, hold.x)),
        cy: Math.max(margin, Math.min(window.innerHeight - margin, hold.y)),
        active: -1,
        released: false,
        options: options.map((option, i) => {
          const degrees = -90 + i * step;
          const radians = (degrees * Math.PI) / 180;
          return {
            ...option,
            angle: degrees,
            dx: Math.cos(radians) * WHEEL_RADIUS,
            dy: Math.sin(radians) * WHEEL_RADIUS,
          };
        }),
      };
      this.hold = null;
      if (navigator.vibrate) navigator.vibrate(12);

      // The fly-out is a keyframe animation holding its end state, which would
      // override any transform the magnet sets. Once it has finished the wheel
      // is marked settled and the same transform becomes a transition.
      clearTimeout(this._wheelSettleTimer);
      this.wheelSettled = false;
      this._wheelNodes = null;
      this._wheelSettleTimer = setTimeout(() => { this.wheelSettled = true; }, WHEEL_SETTLE_MS);
    },

    // Each option leans towards the finger in proportion to how close it is.
    // Written straight to the element rather than through Alpine: this runs on
    // every pointermove, and a reactive re-render per frame is exactly the kind
    // of work that costs frames on a phone.
    magnetise(x, y) {
      const wheel = this.wheel;
      if (!wheel) return;
      const nodes = this._wheelNodes || (this._wheelNodes = document.querySelectorAll(".wheel-opt"));
      wheel.options.forEach((option, i) => {
        const node = nodes[i];
        if (!node) return;
        const ox = wheel.cx + option.dx;
        const oy = wheel.cy + option.dy;
        const distance = Math.hypot(x - ox, y - oy);
        const pull = Math.max(0, 1 - distance / WHEEL_MAGNET_RANGE);
        // Eased so the lean builds as the finger closes rather than tracking
        // it linearly, which reads as the option being dragged.
        const eased = pull * pull;
        const scale = distance > 1 ? (WHEEL_MAGNET_MAX * eased) / distance : 0;
        node.style.setProperty("--mx", `${(x - ox) * scale}px`);
        node.style.setProperty("--my", `${(y - oy) * scale}px`);
        node.style.setProperty("--pull", eased.toFixed(3));
      });
    },

    clearMagnet() {
      for (const node of document.querySelectorAll(".wheel-opt")) {
        node.style.setProperty("--mx", "0px");
        node.style.setProperty("--my", "0px");
        node.style.setProperty("--pull", "0");
      }
    },

    // Which option the finger is over, or -1 for none. Near the centre is
    // deliberately nothing, so there is somewhere to retreat to.
    wheelIndexAt(x, y) {
      const wheel = this.wheel;
      if (!wheel) return -1;
      const dx = x - wheel.cx;
      const dy = y - wheel.cy;
      if (Math.hypot(dx, dy) < WHEEL_PICK_MIN) return -1;
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
      let best = -1;
      let bestGap = Infinity;
      wheel.options.forEach((option, i) => {
        const gap = Math.abs(((angle - option.angle + 540) % 360) - 180);
        if (gap < bestGap) { bestGap = gap; best = i; }
      });
      return best;
    },

    trackWheel(event) {
      this.wheel.active = this.wheelIndexAt(event.clientX, event.clientY);
      this.magnetise(event.clientX, event.clientY);
    },

    // Letting go is the first of the two ways to choose. If the finger moved
    // onto an option, that is the choice. If it did not, the wheel stays where
    // it is, puts names on everything, and waits to be tapped.
    releaseWheel(event) {
      const index = this.wheelIndexAt(event.clientX, event.clientY);
      if (index >= 0) return this.pickWheel(this.wheel.options[index]);
      this.wheel.released = true;
      this.wheel.active = -1;
      this.clearMagnet();
    },

    closeWheel() {
      clearTimeout(this._wheelSettleTimer);
      this.wheel = null;
      this.wheelSettled = false;
      this._wheelNodes = null;
    },

    flashHint(text) {
      this.wheelHint = text;
      clearTimeout(this._hintTimer);
      this._hintTimer = setTimeout(() => { this.wheelHint = ""; }, HINT_MS);
    },

    async pickWheel(option) {
      if (!this.wheel) return;
      const message = this.wheel.message;
      this.closeWheel();

      if (option.soon) return this.flashHint(`${option.label} is not built yet`);
      if (option.id === "edit") return this.startEdit(message, this.bubbleFor(message.id));
      if (option.id === "copy") return this.copyMessage(message);
      if (option.id === "continue") return this.continueReply(message);
      if (option.id === "hide") return this.toggleHidden(message);
      if (option.id === "delete") {
        // Arm the bubble's own delete rather than deleting outright. A drag
        // that lands one option over should not be able to destroy a message.
        this.confirmMsg = message.id;
        clearTimeout(this._confirmTimer);
        this._confirmTimer = setTimeout(() => { this.confirmMsg = ""; }, CONFIRM_MS);
        this.flashHint("Tap the bin to confirm");
      }
    },

    async copyMessage(message) {
      this.flashHint(await this.writeClipboard(message.text || "") ? "Copied" : "Could not copy");
    },

    // Two mechanisms, and the order matters. execCommand goes first because it
    // only works from inside the user gesture that started this, and awaiting
    // anything spends that gesture; it is also the one that works on plain
    // http://<phone-ip>:8787, which is how this app gets reached from another
    // device on the network. The async API is the fallback, for the browsers
    // that have dropped execCommand — it needs a secure context *and*
    // permission, and rejects rather than returning false when refused.
    async writeClipboard(text) {
      try {
        const box = document.createElement("textarea");
        box.value = text;
        box.setAttribute("readonly", "");
        box.style.cssText = "position:fixed;top:-1000px;opacity:0";
        document.body.appendChild(box);
        box.select();
        const copied = document.execCommand("copy");
        box.remove();
        if (copied) return true;
      } catch (_) { /* fall through */ }

      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (_) { /* refused */ }
      }
      return false;
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
    saving: false,
    saveMsg: "",
    saveError: "",
    testing: "",
    tests: {},
    models: {},
    modelErrors: {},
    loadingModels: "",

    async loadSettings() {
      this.tests = {};
      this.models = {};
      this.modelErrors = {};
      const loaded = await api.get("/api/settings");
      // Show a dot placeholder rather than the literal mask, so it reads as
      // "a key is set" instead of looking like a corrupted value.
      loaded.backends.forEach((b) => { if (b.api_key === "***") b.api_key = MASK_DISPLAY; });
      this.settings = loaded;
      this.applyTheme();
    },

    // The numbers behind the reply. Two groups on purpose: `basic` is what
    // someone actually reaches for, and `advanced` is everything that is
    // correct at its default until there is a specific reason it is not.
    //
    // Ranges are the useful span, not the legal one. A slider whose track
    // covers every value the field will accept spends most of its length in
    // territory nobody wants, and the interesting range ends up a few pixels
    // wide — so the ceiling here is the largest sensible setting and the box
    // beside it still takes anything the server allows.
    numberFields(group) {
      const fields = {
        basic: [
          { key: "token_budget", label: "Context budget", min: 1024, max: 32768, step: 512,
            unit: " tok",
            note: "Prompt the reply is built from. Match your model's window — past it, the far end is dropped anyway." },
          { key: "verbatim_window", label: "Messages kept in full", min: 4, max: 60,
            note: "Recent messages quoted word for word. Older ones survive as summary and memory." },
          { key: "memory_max_injected", label: "Memories recalled", min: 0, max: 12,
            note: "Extracted facts added to a prompt at most. Zero turns recall off." },
        ],
        advanced: [
          { key: "summary_budget", label: "Summary budget", min: 200, max: 2000, step: 50,
            unit: " tok",
            note: "How long the rolling summary may grow before it is re-summarised." },
          { key: "lorebook_scan_depth", label: "Lorebook scan depth", min: 0, max: 20,
            note: "How many recent messages are searched for lorebook keywords." },
          { key: "lorebook_total_budget", label: "Lorebook budget", min: 0, max: 2000, step: 50,
            unit: " tok",
            note: "Tokens of lorebook entries allowed into one prompt." },
          { key: "background_retries", label: "Retries", min: 0, max: 5,
            note: "Attempts a failed background pass gets before it gives up." },
          { key: "pass_timeout", label: "Pass timeout", min: 15, max: 600, step: 5, float: true,
            unit: " s",
            note: "A slow phone model can need minutes. Too low and long replies are cut off." },
          { key: "blocking_await_ms", label: "Blocking grace", min: 0, max: 8000, step: 100,
            unit: " ms",
            note: "How long a new turn waits for the last one's blocking pass before going on with provisional state." },
        ],
      };
      return fields[group] || [];
    },

    setNumber(field, raw) {
      const value = field.float ? parseFloat(raw) : parseInt(raw, 10);
      if (Number.isNaN(value)) return;
      // The slider cannot leave its track; the box beside it can, and a
      // deliberately typed number is not something to quietly overrule.
      const floor = field.key === "token_budget" ? 256 : 0;
      this.settings[field.key] = Math.max(floor, value);
    },

    numberLabel(field) {
      const value = this.settings[field.key];
      return `${value}${field.unit || ""}`;
    },

    // Ranges chosen for where the values are worth being, not where they are
    // legal. Temperature above ~1.4 is noise on every model worth using, and
    // top_p below 0.5 makes a roleplay model repeat itself.
    samplingFields() {
      return [
        { key: "temp", label: "Temperature", min: 0, max: 1.5, step: 0.05 },
        { key: "top_p", label: "Top p", min: 0.5, max: 1, step: 0.01 },
        { key: "max_tokens", label: "Max tokens", min: 32, max: 2048, step: 16 },
      ];
    },

    // Sampling is saved per pass as it is edited. It lives in the pass registry,
    // not in settings.json, so it does not ride along on the settings save —
    // and a number typed here that then needed a second, different-looking
    // button pressed to take effect would be its own bug report.
    async setSampling(definition, field, raw) {
      const key = typeof field === "string" ? field : field.key;
      const value = parseFloat(raw);
      if (Number.isNaN(value)) return;
      definition.sampling[key] = key === "max_tokens" ? Math.round(value) : value;
      clearTimeout(this._passSaveTimer);
      this._passSaveTimer = setTimeout(async () => {
        try {
          await api.put(`/api/passes/${definition.id}`, definition);
          this.passMsg = "sampling saved";
        } catch (e) {
          this.passMsg = `could not save ${definition.id}: ${e.message || e}`;
        }
      }, 500);
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

    // Renaming a backend has to drag everything that referred to it by name
    // along. A tier is stored as the backend's name, so without this the first
    // keystroke of a rename orphans the tier, the tier <select> falls off its
    // options, and saving is rejected with "tier blocking points at unknown
    // backend" — naming the value the user is in the middle of typing.
    renameBackend(backend, name) {
      const previous = backend.name;
      backend.name = name;
      if (previous === name) return;
      for (const tier of this.settings.tier_names) {
        if (this.settings.tiers[tier] === previous) this.settings.tiers[tier] = name;
      }
      for (const map of [this.models, this.tests, this.modelErrors]) {
        if (previous in map) {
          map[name] = map[previous];
          delete map[previous];
        }
      }
      if (this.loadingModels === previous) this.loadingModels = name;
      if (this.testing === previous) this.testing = name;
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

    // ---- stick to bottom ----
    //
    // Scrolling is observed, not announced. Calling scrollDown() from each
    // feature that adds content cannot work: it has to guess when the DOM has
    // caught up, and $nextTick fires before layout reflects the change, so the
    // scroll lands short of the message that triggered it — hence having to
    // nudge the list by hand after every reply. It also silently misses every
    // height change no feature reports: the regeneration bubble animating over
    // 300ms, a portrait or webfont arriving, the composer growing under a long
    // draft, the keyboard opening.
    //
    // Instead a ResizeObserver watches the content box and re-pins whenever it
    // changes height, whatever the cause. Anything added later is covered by
    // construction. The only thing that stops the following is the user
    // scrolling up, and the only thing that resumes it is them coming back
    // down — or an action of their own (opening a chat, sending, regenerating)
    // that is meant to take them to the newest message.
    initScroll(inner) {
      const port = inner.parentElement;
      this.scrollPort = port;

      if (window.ResizeObserver) {
        // Content growing or shrinking: follow it if we were following.
        new ResizeObserver(() => this.pinBottom()).observe(inner);
        // The port itself resizing is the keyboard opening or the composer
        // growing. The content did not move; the window onto it did.
        new ResizeObserver(() => this.pinBottom()).observe(port);
      }

      // Position alone cannot tell "the user scrolled up" from "the layout
      // moved underneath them", and the two happen constantly: a growing
      // composer, the keyboard, a bubble animating, and the pin's own scroll
      // event, which is delivered after layout and so reports a position the
      // content has already grown past. Every one of those looks like someone
      // scrolling away. So detaching requires an actual gesture on the
      // scroller, and only the moment right after one counts.
      let gestureAt = -Infinity;
      const gesture = () => { gestureAt = performance.now(); };
      for (const type of ["wheel", "touchstart", "touchmove", "keydown"]) {
        port.addEventListener(type, gesture, { passive: true });
      }

      port.addEventListener("scroll", () => {
        // Back at the bottom, however we got there: follow again.
        if (this.nearBottom()) { this.stick = true; return; }
        if (performance.now() - gestureAt < GESTURE_WINDOW_MS) this.stick = false;
      }, { passive: true });

      // Pull-up-past-the-end. The scroller has nowhere left to go, so the
      // over-drag is ours to read: how far past the bottom the finger has
      // travelled becomes how far the impersonate control is revealed. Touch
      // and wheel both feed it, so it works with a mouse as well as a thumb.
      let pullFrom = null;
      port.addEventListener("touchstart", (event) => {
        pullFrom = this.atVeryBottom() ? event.touches[0].clientY : null;
      }, { passive: true });
      port.addEventListener("touchmove", (event) => {
        if (pullFrom === null) return;
        if (!this.atVeryBottom()) { pullFrom = null; this.setReveal(0); return; }
        // Finger moving up the screen means pulling the content up past its end.
        this.setReveal((pullFrom - event.touches[0].clientY) / PULL_DISTANCE);
      }, { passive: true });
      const endPull = () => {
        pullFrom = null;
        if (this.revealArmed) this.impersonate();
        else this.setReveal(0);
      };
      port.addEventListener("touchend", endPull, { passive: true });
      port.addEventListener("touchcancel", endPull, { passive: true });

      port.addEventListener("wheel", (event) => {
        if (event.deltaY <= 0 || !this.atVeryBottom()) {
          if (this.reveal) this.setReveal(0);
          return;
        }
        this.setReveal(this.reveal + event.deltaY / PULL_DISTANCE);
        clearTimeout(this._pullTimer);
        // A wheel has no "let go", so a pause in scrolling is the release.
        this._pullTimer = setTimeout(() => {
          if (this.revealArmed) this.impersonate();
          else this.setReveal(0);
        }, PULL_SETTLE_MS);
      }, { passive: true });

      // A phone keyboard resizes the visual viewport without resizing any
      // element, so neither observer above sees it.
      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", () => this.pinBottom());
      }

      this.pinBottom();
    },

    nearBottom() {
      const el = this.scrollPort;
      if (!el) return true;
      return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK;
    },

    // Stricter than nearBottom: the pull gesture may only start when there is
    // genuinely nothing left to scroll, otherwise a flick near the end would
    // start revealing the control instead of finishing the scroll.
    atVeryBottom() {
      const el = this.scrollPort;
      if (!el) return false;
      return el.scrollHeight - el.scrollTop - el.clientHeight <= 2;
    },

    setReveal(value) {
      const next = Math.max(0, Math.min(1, value));
      if (next === this.reveal) return;
      this.reveal = next;
      const armed = next >= 1;
      if (armed !== this.revealArmed) {
        this.revealArmed = armed;
        // The one moment worth a buzz: past here, letting go does something.
        if (armed && navigator.vibrate) navigator.vibrate(14);
      }
    },

    // Follow the bottom, but only while the user has not scrolled away.
    //
    // Coalesced to one write per frame. The observer fires on every token, and
    // reading scrollHeight to write scrollTop forces a layout each time — a
    // hundred times a second during a fast stream, for a position that can
    // only be seen once per frame.
    pinBottom() {
      if (!this.scrollPort || !this.stick || this._pinQueued) return;
      this._pinQueued = true;
      requestAnimationFrame(() => {
        this._pinQueued = false;
        const el = this.scrollPort;
        if (el && this.stick) el.scrollTop = el.scrollHeight;
      });
    },

    // For the deliberate moves: opening a chat, sending, regenerating. These
    // resume following even if the user had scrolled up to read.
    scrollDown() {
      this.stick = true;
      this.pinBottom();
    },
  };
}

window.tavern = tavern;
