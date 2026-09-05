// Personal Tavern client (§12). Vanilla JS + Alpine, no build step.
//
// Two streams feed the UI:
//   POST /api/chats/{id}/send  — the turn itself (deltas, reply, errors)
//   GET  /api/chats/{id}/events — the ambient bus, where background passes
//                                 land after the turn has already closed
// Keeping them separate is what lets a background pass update its panel
// without being tied to the request that started it (§1, §4.5).

// ------------------------------------------------------- freeze/crash log
//
// The half of a debug export (\u00a7 downloadDebugLog) the server cannot see:
// this tab's own JS errors, and how long the page itself ever stopped
// responding. Recorded from the moment this file loads \u2014 before Alpine
// exists, deliberately, since a freeze can happen before boot() ever runs \u2014
// into localStorage rather than a plain variable, so a crash that takes the
// tab down with it (a real one, not just a stall) still leaves the record
// behind for the next reload to export. Kept as its own small module-level
// state rather than folded into the Alpine data object: it has to work
// whether or not that object has finished constructing.
const DEBUG_LOG_KEY = "tavern_debug_log";
const DEBUG_LOG_MAX = 300;
// Longer than any ordinary GC pause or a big reactive re-render, short
// enough to still catch a freeze before someone gives up and force-closes
// the app over it. The heartbeat ticks every 1000ms, so this is "the tab
// went quiet for two and a half ticks in a row."
const STALL_THRESHOLD_MS = 2500;

function pushDebugEvent(kind, detail) {
  try {
    const raw = localStorage.getItem(DEBUG_LOG_KEY);
    const log = raw ? JSON.parse(raw) : [];
    log.push({ t: Date.now(), kind, detail: String(detail).slice(0, 2000) });
    while (log.length > DEBUG_LOG_MAX) log.shift();
    localStorage.setItem(DEBUG_LOG_KEY, JSON.stringify(log));
  } catch (_) {
    // Storage full, private browsing, whatever \u2014 a logger must never itself
    // be the thing that throws.
  }
}

window.addEventListener("error", (e) => {
  pushDebugEvent("error", `${e.message} @ ${e.filename}:${e.lineno}:${e.colno}`);
});
window.addEventListener("unhandledrejection", (e) => {
  const reason = e.reason;
  pushDebugEvent("unhandledrejection", (reason && (reason.stack || reason.message)) || reason);
});

(() => {
  let last = performance.now();
  setInterval(() => {
    const now = performance.now();
    const gap = now - last;
    if (gap > STALL_THRESHOLD_MS) {
      pushDebugEvent("stall", `unresponsive for about ${Math.round(gap)}ms`);
    }
    last = now;
  }, 1000);
})();

// The recorded half of a debug export, formatted \u2014 read fresh each time
// rather than cached, since the whole point is picking up whatever landed
// since the tab last loaded, including from a session that has since
// reloaded or crashed and come back.
function debugLogText() {
  let log = [];
  try {
    log = JSON.parse(localStorage.getItem(DEBUG_LOG_KEY) || "[]");
  } catch (_) {
    log = [];
  }
  const head = `-- this browser tab (${navigator.userAgent}) --`;
  if (!log.length) return `${head}\n(nothing recorded \u2014 no errors, no stalls)`;
  const body = log
    .map((e) => `  ${new Date(e.t).toISOString()}  ${e.kind}: ${e.detail}`)
    .join("\n");
  return `${head}\n${body}`;
}

const MASK_DISPLAY = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";

// Every saved credential comes back from the server as `***`. Show a dot
// placeholder instead, so a set key reads as "a key is set" rather than as a
// corrupted value \u2014 and do it in one place, because the settings arrive by two
// routes (boot, and opening the panel) and the one that skipped this step
// showed the literal asterisks after every reload.
function softenMasks(settings) {
  (settings.backends || []).forEach((b) => {
    if (b.api_key === "***") b.api_key = MASK_DISPLAY;
  });
  if (settings.search_key === "***") settings.search_key = MASK_DISPLAY;
  if (settings.avatar_key === "***") settings.avatar_key = MASK_DISPLAY;
  return settings;
}

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

// Transition timings the sequences here have to wait on. Read from the
// stylesheet rather than restated, because the restated version drifted: this
// block used to hold its own numbers under a comment saying they "must stay in
// step with styles.css", and `MESSAGE_SEND_MS` had become 460 against an
// animation that ran 420. Two numbers that agree by hand eventually do not.
//
// Read once and cached: these are custom properties on :root, so they cannot
// change without a stylesheet edit, and getComputedStyle is not something to
// do inside an animation sequence.
// Haptics, in one place so the rule lives with the code rather than in six
// call sites. Only for state changes you were *not* looking at — a reply
// starting, a delete arming, a variant landing under your thumb. Never for an
// ordinary tap: the screen already answered that, and a phone that buzzes on
// every touch is a phone people turn the feature off on.
//
// Android honours this; iOS ignores it, which is fine — the deploy target is
// Termux. Silent when the page is not in front of you, because a buzz from an
// app you are not looking at is a notification, and this is not one.
function buzz(ms) {
  if (!navigator.vibrate || document.visibilityState !== "visible") return;
  if (document.documentElement.classList.contains("motion-off")) return;
  navigator.vibrate(ms);
}

const durations = new Map();
function dur(name, fallback = 0) {
  if (!durations.has(name)) {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(`--dur-${name}`).trim();
    // Tokens are authored in ms; seconds are accepted so a later edit to `.3s`
    // does not silently become 0.3ms.
    const value = raw.endsWith("s") && !raw.endsWith("ms")
      ? parseFloat(raw) * 1000
      : parseFloat(raw);
    durations.set(name, Number.isFinite(value) ? value : fallback);
  }
  return durations.get(name);
}

// The easing tokens, for the few animations driven from JavaScript. Same rule
// as the durations: read the token, never write a bezier inline.
const easings = new Map();
function ease(name) {
  if (!easings.has(name)) {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(`--ease-${name}`).trim();
    easings.set(name, raw || "ease-out");
  }
  return easings.get(name);
}

const TEXT_FADE_MS = () => dur("text-fade", 170);
const BUBBLE_RESIZE_MS = () => dur("bubble-resize", 300);
const PANEL_LEAVE_MS = () => dur("panel-leave", 260);
const MESSAGE_SEND_MS = () => dur("message-send", 420);
const MESSAGE_LEAVE_MS = () => dur("message-leave", 220);

// Size the bubble draws in to while the cue is showing, so a long reply does
// not leave a screenful of empty card around three dots.
const REGEN_PILL_WIDTH = 84;
const REGEN_PILL_HEIGHT = 46;
// How long an armed delete stays armed before giving up on the second tap.
const CONFIRM_MS = 3000;
// The fixed set someone can react to a reply with (§ app/models.py's own
// copy, MESSAGE_REACTIONS — kept in sync by hand, six emoji neither side
// has a reason to change often).
const MESSAGE_REACTIONS = ["❤️", "😂", "😢", "😮", "😡", "👍"];
// The five canned notes "Suggest edit" offers instead of typing one out —
// the instruction text is what actually reaches the model (§
// run_suggest_edit, scheduler.py), so wording these well matters as much as
// wording a hand-typed one would. "Shorten" spells out *cut real length*
// because the vaguer first wording of it ("noticeably shorter") reliably
// came back the same length with a clause trimmed here and there — a model
// asked to be brief without being told what to actually drop tends to
// polish rather than cut.
const SUGGEST_EDIT_PRESETS = [
  {
    id: "shorten",
    label: "Shorten",
    instruction: "Make the reply meaningfully shorter — actually cut real length, don't just tighten the "
      + "wording. Remove superfluous content: repeated beats, filler description, hedging, anything that "
      + "doesn't move the scene forward. Keep every action and plot detail that actually matters.",
  },
  {
    id: "lengthen",
    label: "Lengthen",
    instruction: "Make the reply longer with real added material — more sensory detail, more of what the "
      + "character notices or does — not padding or repetition. Keep everything that already happens; add to it.",
  },
  {
    id: "describe_actions",
    label: "Describe more actions",
    instruction: "Add more description of what's happening — physical actions, gestures, body language, small "
      + "environmental detail — around the existing dialogue. Don't change what is said or the outcome, just "
      + "show more of what's going on while it's said.",
  },
  {
    id: "describe_less",
    label: "Describe less",
    instruction: "Cut back the description of physical actions, gestures and environmental detail around the "
      + "dialogue — trim it down to what actually matters, don't just skim over it. Don't change what is said "
      + "or the outcome, and don't cut the dialogue itself.",
  },
  {
    id: "fix_pov",
    label: "Fix grammar & perspective",
    instruction: "Proofread the reply for grammar mistakes and point-of-view errors — especially the character's "
      + "own actions written as \"you\", or the user's actions written as the character's — and fix them without "
      + "changing anything else.",
  },
];
// Deleting a character takes its chats with it — the one action in the app
// that cannot be undone by re-importing, so it gets a third gate the other
// armed deletes don't: a held press, timed rather than tapped.
const KILL_HOLD_MS = 7000;
// How long a one-line confirmation ("Copied") stays on screen.
const HINT_MS = 1900;
// A character's own line, on the other hand, is a sentence to actually read —
// longer than a two-word toast earns.
const REACTION_BUBBLE_MS = 3400;
// The three lines models.CharacterReactions holds, in the order the editor
// shows them. Kept as one list rather than repeated at each call site (§
// missingReactions, saveReactionField, regenerateAllReactions).
const REACTION_KEYS = ["starred", "unstarred", "killed"];
// Quiet time after the last keystroke before the template preview re-renders.
const PREVIEW_DEBOUNCE_MS = 200;
// Character budgets for the header's own row — see pillFitsInline. The
// fields can now wrap onto a second line inside the pill rather than
// forcing the whole thing out to float over the chat, so their budget is
// generous; the name never wraps (it ellipsizes on its own single line
// instead), so a name past this length is the one thing that still sends
// the pill out to float — there is no line left to give it more room on.
const WORLDBAR_NAME_BUDGET = 24;
const WORLDBAR_FIELDS_BUDGET = 64;
// How long a prompt section takes to slide past its neighbour when reordered.
const SECTION_MOVE_MS = 260;
// A rough average prose paragraph, for turning a paragraph count into the
// word-count range the craft:length block's text quotes alongside it — see
// setLengthRange. Rounded to the nearest 50 words so the number reads like
// someone chose it, not like a formula did. Kept in sync by hand with the
// shipped default in app/prompt_layout.py, which is this formula's output at
// 1-2 paragraphs.
const WORDS_PER_PARAGRAPH = 90;
// The four samplers every pass ships with a tuned value for. "Turn the extras
// off" leaves these alone: a pass's own temperature is a decision, not leftover
// tinkering, and resetting it would be a trap under that label.
const SAMPLING_DEFAULTS = { temp: 0.8, top_p: 0.95, top_k: 40, rep_penalty: 1.1 };

// ---- AI Horde quick-setup presets (§ applyHordePreset) ----
//
// Three different numbers, easy to blur into one "the budget" and each
// wrong to conflate with either of the others:
//
//   backend.context   → HordeProvider's `max_context_length`, told to Horde's
//                        queue so it knows which workers may even pick the
//                        job up (§ HordeProvider._probe_context). A worker-
//                        eligibility floor, not something the app spends —
//                        asking for less does not make the prompt smaller, it
//                        only shrinks which workers qualify. Held at Horde's
//                        own ceiling for every tier, so no preset ever asks
//                        for a narrower pool than Horde itself allows.
//   settings.token_budget → the ceiling `assembly.py` trims the *whole*
//                        assembled prompt against — prefix, lorebook,
//                        memories, summary and conversation together
//                        (§7.1/§7.2), not the card/craft prefix alone. Held
//                        at the same ceiling as backend.context for every
//                        tier, for the same reason: capping this is capping
//                        the conversation and everything else in the middle
//                        band right along with the prefix, which is not what
//                        a "smaller prompt" tier is supposed to buy. What
//                        actually limits a real turn is PassScheduler._fitted
//                        tightening this down to whatever the *selected
//                        model* genuinely holds (§ HordeProvider._probe_
//                        context) — a real ceiling, discovered per model,
//                        rather than a guess typed in here per tier.
//   prompt_budget (below) → not a setting at all, a *target* this file holds
//                        itself to when choosing which optional prefix
//                        content (the writing library, example dialogue —
//                        §HORDE_WRITING_*/HORDE_STRUCTURAL_* below) each tier
//                        turns on. This is where "Mini keeps its own prompt
//                        under 1.5k" actually happens: by asking for less
//                        prefix content, not by capping the request. A card
//                        whose own persona/scenario is already bigger than
//                        the target is a limit those selections cannot
//                        reach around — see the roster's own size warning
//                        (§ assembly.mandatory_cost) for that case.
//
// A tier's speed comes from what it asks a worker to *do*, not from who is
// allowed to pick the job up or how much of the window is spent on things
// that are not the card: less prefix to read is a faster generation once
// any worker takes it, and Mini also runs no foreground/background passes at
// all, so it is the only tier making a single Horde request per turn instead
// of several queued one after another.
const HORDE_CONTEXT_CEILING = 32000;  // Horde's own max_context_length ceiling (LIMITS)
const HORDE_REPLY_CEILING = 512;      // Horde's own max_length ceiling (LIMITS)
//
// Which of the eleven "writing" library blocks each tier turns on, and
// whether example dialogue runs — four blocks a preset never touches at all:
// craft:format (the markup convention the renderer depends on) and
// craft:length (what the reply-length stepper edits) are not a matter of
// tier, they are always-on regardless of backend; craft:combat and
// craft:adult are opt-in content choices, not a budget question, so a preset
// has no opinion on them either way. Each tier builds on the one below it
// rather than being listed from scratch, so the progression is visible here
// as a list, not something you have to diff three separate arrays to see.
//
// The selection itself is deliberately front-loaded towards *correctness*
// rather than *polish* now that the budgets are this tight: autonomy (never
// speak or act for {{user}}) and knowledge (no mind-reading, no knowing what
// was not witnessed) are the two rules whose absence reads as broken rather
// than merely plain, so Mini keeps only those. Voice, agency and simulation
// craft join at Standard once there is room to spend on them; the two most
// expensive blocks (first-look description, prose discipline) and the
// smaller polish ones wait for Max. This is the same "fewer, higher-leverage
// instructions beat many overlapping ones on a small model" finding
// KNOWN-ISSUES.md already recorded about the library generally — it matters
// more here, not less, because Horde workers skew towards smaller/quantised
// models and the context left for them to hold onto is an order of
// magnitude tighter than a local Ollama profile ever is.
const HORDE_WRITING_MINI = ["craft:autonomy", "craft:knowledge"];
const HORDE_WRITING_STANDARD = [
  ...HORDE_WRITING_MINI,
  "craft:sim", "craft:pov", "craft:voice", "craft:bold",
];
const HORDE_WRITING_MAX = [
  ...HORDE_WRITING_STANDARD,
  "craft:banned", "craft:hours", "craft:first_look", "craft:prose", "craft:drives",
];
// The full set a preset is allowed to have an opinion on — used to clear
// anything *not* in a preset's own list back off, so applying Mini after Max
// actually turns blocks back off instead of only ever adding more.
const HORDE_WRITING_SCALING = new Set(HORDE_WRITING_MAX);

// Example dialogue (§ STRUCTURAL "examples") is the single most expensive
// *optional* prefix section on most cards — often bigger than every writing
// block combined — and, unlike the craft library, is not something the app
// ships text for: it is exactly as long as the card's own `mes_example`.
// Worth its cost once Standard's context leaves room to spare; not at Mini,
// where every token of it is a token the actual conversation cannot have.
const HORDE_STRUCTURAL_MINI = [];
const HORDE_STRUCTURAL_STANDARD = [];
const HORDE_STRUCTURAL_MAX = ["examples"];
const HORDE_STRUCTURAL_SCALING = new Set(HORDE_STRUCTURAL_MAX);

const HORDE_PRESETS = [
  {
    id: "mini", label: "AI Horde — Mini",
    tagline: "Lightest and fastest: the smallest card-and-writing-rules "
      + "prefix of the three tiers, so the conversation itself — history, "
      + "memories, summary — gets the rest of whatever your selected model "
      + "actually holds, rather than sharing a small slice with the prefix. "
      + "Only the two rules that prevent broken replies (never speaking for "
      + "you, never knowing what it hasn't witnessed) run, and it's the "
      + "only tier making one Horde request a turn instead of several. A "
      + "long, detailed character card can still eat this whole prefix "
      + "target on its own description before the conversation gets a "
      + "single token — trim the card or move up a tier if replies seem to "
      + "have no memory of what was just said.",
    prompt_budget: 1536,
    foreground: false, background: false, auditsState: false,
    writing: HORDE_WRITING_MINI,
    structural: HORDE_STRUCTURAL_MINI,
  },
  {
    id: "standard", label: "AI Horde — Standard",
    tagline: "A balanced middle ground: place, weather and memories stay on "
      + "and more writing rules apply, at a moderate card-and-writing-rules "
      + "prefix. Slower than Mini — the secondary-info pass is its own "
      + "queued Horde request — but more consistent.",
    prompt_budget: 2560,
    foreground: false, background: true, auditsState: false,
    writing: HORDE_WRITING_STANDARD,
    structural: HORDE_STRUCTURAL_STANDARD,
  },
  {
    id: "max", label: "AI Horde — Max",
    tagline: "Everything on: post_process copy-edits each reply before it's "
      + "shown — worth knowing, since the reply stays hidden a beat longer "
      + "while that runs — every writing rule and the card's own example "
      + "dialogue apply, at the largest card-and-writing-rules prefix of the "
      + "three tiers. Slowest and heaviest — every turn is now three "
      + "separate queued Horde requests — pick this only when you want the "
      + "best Horde can give and don't mind the wait.",
    prompt_budget: 4608,
    foreground: true, background: true, auditsState: true,
    writing: HORDE_WRITING_MAX,
    structural: HORDE_STRUCTURAL_MAX,
  },
];

// state_auditor and expression moved onto the background tier when
// post_process took over foreground (§ KNOWN-ISSUES.md) — which used to be
// the one thing that made Max distinct from Standard ("reads the reply back
// and corrects it" vs. not) is now just two more passes background already
// runs. Background on its own can no longer tell the two apart, so a
// preset's own auditsState flag does what the tier split used to: which of
// these two passes it wants, independent of whether the rest of background
// (scene/summary/memory/backdrop/events) is on. Applied by id rather than
// folded into HORDE_WRITING_SCALING/HORDE_STRUCTURAL_SCALING above — those
// two sets are prompt-prefix content on the blocking tier; this is which
// background *passes* run at all, a different axis entirely.
const HORDE_AUDIT_PASSES = ["state_auditor", "expression"];

// Hold-to-open action wheel.
const HOLD_MS = 380;          // press this long and the wheel opens
const HOLD_SLOP = 10;         // finger movement that cancels the hold instead
const WHEEL_RADIUS = 78;      // how far the options sit from the press point
// Half the widest option box, plus a little. Keeps the whole circle on screen
// when it opens against an edge — see the clamp in openWheel.
const WHEEL_OPTION_REACH = 52;
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

// The actually-visible height, keyboard included. `window.innerHeight` is
// the layout viewport, which most mobile browsers do *not* shrink when the
// on-screen keyboard opens — it stays the full screen height while the
// keyboard covers the bottom of it. `visualViewport.height` is the one that
// tracks the keyboard, where the API exists (every browser this app targets;
// the fallback is only for a window with no visualViewport at all, such as
// a non-browser test harness). Anything sized as a fraction of "the screen"
// while a text field can be focused — the edit box chief among them — needs
// this rather than innerHeight, or the fraction is of a screen the keyboard
// has already eaten part of.
function viewportHeight() {
  return (window.visualViewport && window.visualViewport.height) || window.innerHeight;
}

// A card's picture, wherever it came from. An imported card names a file that
// shipped with it; one uploaded here is stored with the persona avatars and
// arrives as a path. Anything starting with a slash is already a URL.
function pfpUrl(file) {
  const name = String(file || "").trim();
  if (!name) return "";
  return name.startsWith("/") ? name : `/static/characters/${name}`;
}

// The CSS filter a character's chosen picture effect draws as, wherever the
// picture is (§models.PfpEffect) — the roster, the chat, the enlarged view,
// same as pfpUrl above and portraitShape below cover the file and the frame.
//
// Applied to a `.pfp-glow` sibling of the picture, never to the picture
// itself: a hue-rotated element hue-rotates everything it renders, so
// putting this on the same element as the photo would tint the photo along
// with the ring around it, which is exactly the mistake this was pulled back
// out of. `.pfp-glow`'s own base colour is a plain saturated red — hue-rotate
// turns that into whichever colour was actually chosen.
//
// Empty for an untouched character rather than six no-op filter functions,
// so most `.pfp-glow` spans carry no filter attribute at all — and, paired
// with pfpEffectOn below, are not even in the DOM.
function pfpEffectStyle(effect) {
  const e = effect || {};
  const parts = [];
  if (e.hue) parts.push(`hue-rotate(${e.hue}deg)`);
  if (e.saturate !== undefined && e.saturate !== 1) parts.push(`saturate(${e.saturate})`);
  if (e.brightness !== undefined && e.brightness !== 1) parts.push(`brightness(${e.brightness})`);
  if (e.contrast !== undefined && e.contrast !== 1) parts.push(`contrast(${e.contrast})`);
  if (e.sepia) parts.push(`sepia(${e.sepia})`);
  if (e.grayscale) parts.push(`grayscale(${e.grayscale})`);
  return parts.length ? `filter: ${parts.join(" ")}` : "";
}

// Whether there is a glow to show at all — an untouched character's effect
// is every field at its neutral default, and the ring should not render,
// let alone at zero-strength, when there is nothing to draw.
function pfpEffectOn(effect) {
  const e = effect || {};
  return !!(
    e.hue || (e.saturate ?? 1) !== 1 || (e.brightness ?? 1) !== 1 ||
    (e.contrast ?? 1) !== 1 || e.sepia || e.grayscale
  );
}

// How much slower the text is shown than the model produced it. A local model
// on a good card arrives faster than anyone reads, and a reply that lands in a
// lump is one you scroll back through rather than watch.
// The widest a stored portrait is written at. It is drawn at 148px at most and
// on a 3x screen that is 444, so this is generous; a 4MB card sent whole would
// be 4MB down the wire on every load, on the phone doing the loading.
const PORTRAIT_MAX_PX = 640;

const STREAM_PACE = 0.6;
// Once the model has finished, whatever is still queued is cleared inside this
// — a paced tail is pleasant, a paced tail four seconds after the backend went
// quiet is the app looking broken.
const STREAM_TAIL_MS = 1500;
const PACE_MIN_CPS = 14;
const PACE_MAX_CPS = 900;

// Reveals text at a fraction of the rate it arrives at.
//
// The model writes in bursts — a phone-hosted one especially — so the naive
// version (show everything the moment it lands) alternates between a wall of
// text and nothing at all. This keeps one buffer and hands it out smoothly,
// measuring how fast the source is actually going and staying that far behind
// it. `done()` waits for the backlog, so nothing downstream can replace the
// text while there is still some of it to show.
function makePacer(apply) {
  let full = "";
  let shown = 0;
  let started = 0;
  let last = 0;
  let closed = false;
  let frame = 0;
  let waiting = [];

  const settle = () => {
    for (const resolve of waiting) resolve();
    waiting = [];
  };

  const tick = (now) => {
    frame = 0;
    if (!started) started = now;
    if (!last) last = now;
    // Clamped: a backgrounded tab hands back one enormous delta, and without
    // this the reply would jump a paragraph the moment it came forward.
    const dt = Math.min(0.25, (now - last) / 1000);
    last = now;

    const elapsed = Math.max(0.2, (now - started) / 1000);
    const arrival = full.length / elapsed;
    let rate = Math.min(PACE_MAX_CPS, Math.max(PACE_MIN_CPS, arrival * STREAM_PACE));
    if (closed) {
      const backlog = full.length - shown;
      rate = Math.max(rate, backlog / (STREAM_TAIL_MS / 1000));
    }

    shown = Math.min(full.length, shown + rate * dt);
    apply(full.slice(0, Math.floor(shown)));
    if (Math.floor(shown) < full.length) schedule();
    else if (closed) settle();
  };

  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(tick);
  };

  return {
    push(text) {
      full = text;
      if (Math.floor(shown) < full.length) schedule();
    },
    // Back to nothing. The stream turned out to have been the model's
    // reasoning, so what is on screen is not the start of the reply and the
    // pacing so far says nothing about how fast the reply arrives.
    reset() {
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      full = "";
      shown = 0;
      started = 0;
      last = 0;
      closed = false;
      apply("");
      settle();
    },
    // Everything, now — used when the turn is abandoned rather than finished.
    flush() {
      closed = true;
      shown = full.length;
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      apply(full);
      settle();
    },
    done() {
      closed = true;
      if (Math.floor(shown) >= full.length) return Promise.resolve();
      schedule();
      return new Promise((resolve) => waiting.push(resolve));
    },
  };
}

// "Realistic chat speed" (Settings, on by default) — see runStream.
// Every range below is rolled once per turn, not re-rolled per word: the
// point is that a turn's own pace varies from the next one's, not that it
// wobbles within itself. Silence first — a person reads what was said and
// starts composing an answer before anything of theirs shows up at all —
// sized to how much there was to read; then the typing cue itself holds for
// at least a floor of its own, however fast the reply actually arrives.
//
// The per-word cost is deliberately close to a real silent-reading pace
// (~230-400 words/minute, i.e. 150-260ms/word) rather than a token-budget
// guess — the previous 40-80ms/word implied reading at over 700 words a
// minute, which is why a short question got answered before a person could
// plausibly have finished it. The base and cap moved up to match: a short
// message still lands quickly, but a longer one now visibly earns its
// pause instead of being swallowed by a floor tuned for one-liners.
const REALISTIC_SILENCE_BASE_MS = [1200, 2400];
const REALISTIC_SILENCE_PER_WORD_MS = [150, 260];
// A very long message still gets an answer in a bounded time — the point is
// pacing, not making someone wait minutes for a paragraph.
const REALISTIC_SILENCE_CAP_MS = 8000;
const REALISTIC_TYPING_MIN_MS = [1400, 2600];

function randRange([min, max]) {
  return min + Math.random() * (max - min);
}

function wordCount(text) {
  const m = String(text || "").trim().match(/\S+/g);
  return m ? m.length : 0;
}

// { silenceMs, typingMinMs } for one turn of "Realistic chat speed" — see
// runStream, which is the only thing that reads this. Based on the *sent*
// message's length alone: what the model does with reasoning tokens on the
// way to a reply is its own business, not a measure of how long a person
// would sit reading a message before they started answering it.
function realisticPacing(text) {
  const words = wordCount(text);
  const silenceMs = Math.min(
    REALISTIC_SILENCE_CAP_MS,
    randRange(REALISTIC_SILENCE_BASE_MS) + randRange(REALISTIC_SILENCE_PER_WORD_MS) * words,
  );
  return { silenceMs, typingMinMs: randRange(REALISTIC_TYPING_MIN_MS) };
}

// "/" lines in the composer are forced actions, not something to send — see
// send() and runSlashCommand(). A table rather than an if/else chain in
// send() itself, so a new command is one more entry here, not a new branch
// there. `passId` is run through the same on-demand endpoint the world-pill's
// own refresh button uses (§ refreshWorld) — a hand-forced run behaves
// exactly like a scheduled one, same event stream, same write rules — and
// `flag` is the matching key in `refreshing` that marks it running.
// `describe(vm)` reads whatever "current value" the outcome toast compares
// before and after the run, `label(vm, value)` turns that raw value into
// the word shown, and `outcome(vm, run, before)` builds the toast's actual
// text — all three live here rather than generic in resolveSlashRun, since
// what changed (and what "no change" even means) is different for every
// pass this table might someday cover.
const SLASH_COMMANDS = {
  background: {
    passId: "background_swap",
    flag: "background",
    hint: "Checking the background…",
    describe: (vm) => vm.backgroundFile(),
    label: (vm, file) => (file ? vm.bgLabel(file) : "no backdrop"),
    outcome(vm, run, before) {
      if (run.status === "failed") return `Error: ${run.error || "the pass failed"}`;
      const after = this.describe(vm);
      if (after !== before) {
        return `Background changed from ${this.label(vm, before)} to ${this.label(vm, after)}`;
      }
      // Same picture either way — but "unchanged" alone says nothing about
      // why, and that why is exactly what someone reaching for this command
      // wants to know (§ ISSUES-TRIAGE.md-style feedback: a status this app
      // already tracks, just not shown). "skipped" means the pass never
      // even had a handler to run (§ _build_pass_input, scheduler.py) —
      // worth telling apart from "stale", where it ran and answered but the
      // answer didn't stick (an invalid pick, or — per its own prompt — the
      // model choosing to keep the current one on purpose).
      if (run.status === "skipped") {
        if (!vm.backdrops.length) return "Background: nothing uploaded yet (Theme → Backdrop)";
        const eligible = vm.backdrops.some((b) => vm.bgMeta(b.name).auto !== false);
        if (!eligible) return "Background: every image is excluded from auto-pick (Theme → Backdrop)";
        return "Background unchanged";
      }
      if (run.status === "stale") return "Background unchanged — nothing else fit";
      return "Background unchanged — already showing that one";
    },
  },
  emotion: {
    passId: "expression",
    flag: "expression",
    hint: "Checking expression…",
    describe: (vm) => vm.expression || "",
    label: (vm, key) => (key ? key.charAt(0).toUpperCase() + key.slice(1) : "none"),
    outcome(vm, run, before) {
      if (run.status === "failed") return `Error: ${run.error || "the pass failed"}`;
      const after = this.describe(vm);
      if (after !== before) {
        return `Expression changed from ${this.label(vm, before)} to ${this.label(vm, after)}`;
      }
      // Unlike the backdrop library, a character always has at least a
      // generic six-word vocabulary to fall back on (§ _build_pass_input's
      // expression branch, scheduler.py) even with zero pictures of their
      // own, so "skipped" here only ever means every one of their real
      // slots is excluded — there is no "nothing uploaded yet" case to
      // tell apart from it the way backgrounds has.
      if (run.status === "skipped") {
        return "Expression: every portrait is excluded from auto-pick (character editor)";
      }
      if (run.status === "stale") return "Expression unchanged — nothing else fit";
      return "Expression unchanged — already this one";
    },
  },
};

// Only a whole "/word" line counts — "/" mid-sentence is just punctuation,
// and a command with trailing words ("/background please") is not one of
// the fixed few names above, so it falls through to being sent as text
// rather than silently doing the wrong thing.
function parseSlashCommand(text) {
  const match = /^\/([a-z]+)$/i.exec(text);
  return match ? SLASH_COMMANDS[match[1].toLowerCase()] || null : null;
}

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
// 2.5x what it was. At 96px an ordinary flick at the end of the chat armed it,
// which meant a gesture aimed at the last message opened the composer instead.
const PULL_DISTANCE = 240;     // travel past the bottom that fully reveals it
// A flick completes the pull without going the full distance. 520px/s is about
// where a deliberate throw separates from a drag that happened to end while
// moving; the reveal floor stops a fast scroll at the bottom of the chat from
// counting as one, since that gesture never committed to anything.
const FLICK_SPEED = 520;
const FLICK_MIN_REVEAL = 0.35;
const PULL_SETTLE_MS = 220;    // a wheel has no "let go"; a pause stands in

// Swipe thresholds, in CSS pixels.
const SWIPE_CLAIM = 12;   // movement before a drag counts as horizontal at all
const SWIPE_COMMIT = 64;  // release past this and the variant changes
const SWIPE_MAX = 130;    // furthest the bubble travels

// FastAPI puts a rejection's reason in `detail`; our own handlers use `error`.
// Without this a 400 that says exactly what is wrong — "a character needs a
// name" — reaches the user as "Bad Request".
// What a failed request says when the server did not say anything itself.
// "404 Not Found" is true and useless: it names the protocol's problem rather
// than the reader's. Anything the server does explain travels through
// untouched — those messages were written on purpose and are better than these.
const STATUS_TEXT = {
  404: "That is not there any more — it may have been deleted in another tab.",
  409: "Something else changed that first. Reopen it and try again.",
  413: "That file is too big.",
  422: "The server would not accept that. Check the fields and try again.",
  500: "The server hit an error. The log on the phone will say what.",
  502: "No answer from the model backend.",
  503: "The server is busy. Try that again in a moment.",
  504: "The model backend took too long to answer.",
};

// What went wrong, in a sentence rather than in the browser's words.
//
// `apiError` covers a request the server *answered* badly. This covers the one
// it never answered at all, which on a phone is the commonest failure there
// is: Android reaps the server, or Termux is swiped away, and every fetch
// rejects with a bare TypeError whose message is "Failed to fetch" — six words
// that name nothing and suggest nothing. Everywhere else this app says what
// happened and what to do about it; this was the one place it did not.
function errorText(e) {
  if (e && (e.name === "TypeError" || e.name === "NetworkError")) {
    return "The tavern's server is not answering. It runs on this phone — "
      + "check Termux is still open, then try again.";
  }
  return String((e && e.message) || e);
}

async function apiError(response) {
  const body = await response.json().catch(() => ({}));
  const said = body.detail || body.error;
  if (said) return new Error(said);
  return new Error(
    STATUS_TEXT[response.status] ||
    (response.status >= 500
      ? "The server could not do that."
      : "That request was refused.")
  );
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
    // { [characterId]: {mandatory_cost, headroom, too_big} } — the roster's
    // "this card may be too big" warning (§ loadCardBudget). A separate
    // fetch from `characters` itself, and best-effort: it needs a live probe
    // of the Messages backend (§ /api/characters/budget), which the roster
    // must still draw as a plain list while unable to answer.
    cardBudget: {},
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
    // Music controls (ROADMAP #39). Mirrors state.music server-side:
    // status "none"|"proposed"|"playing", track a filename or null,
    // character who proposed/is playing it or null (null for the user's
    // own manual pick). "proposed" is a pending action_card (§ musicRespond)
    // shown in the message flow; "playing" drives the <audio> element.
    music: { status: "none", track: null, character: null },
    musicLibrary: [],
    // The one talking-avatar clip currently live, if any (AVATAR-VIDEO-
    // CONTRACT.md) — { messageId, url } for the single message it was
    // rendered for, or null. Never more than one at a time (§ liveVideoFor).
    liveAvatarVideo: null,

    draft: "",
    editing: null,
    editText: "",
    editHeight: 0,
    editingEl: null,
    regenId: null,
    regenPrevious: "",
    fadingId: null,
    // Following the newest message. Cleared when the user scrolls up to read,
    // restored when they come back down or ask for the newest message. Also
    // what the scroll-to-bottom button's own visibility reads directly
    // (`!stick`, § ISSUES-TRIAGE.md #4) rather than a second flag: a chat too
    // short to scroll is trivially "at bottom" already, so nothing extra is
    // needed to keep the button off a conversation that doesn't need it.
    stick: true,
    scrollPort: null,
    // The header menu, and which of its destinations is open. One panel at a
    // time — "" means the conversation is unobstructed.
    menu: false,
    // Whether the world-info pill (§ world-pill-inline/world-pill-float,
    // index.html) has its refresh button open. Only meaningful once there is
    // a scene to read — the empty pill has nothing else in it, so its
    // refresh/"generate this" button stays visible outright rather than
    // needing this to reveal it. Shared by both pills rather than one flag
    // each: only one of the two ever renders at a time, so there is nothing
    // to desync.
    pillOpen: false,
    // Two fields rather than one: `panel` says which body to render and
    // `panelOpen` says whether the sheet is on screen. Clearing the name at
    // the moment of closing would unmount the body through its own x-if, and
    // the sheet would slide away empty.
    panel: "",
    panelOpen: false,
    // The confirm sheet for leaving a dirty panel (§ runOrGuard), and the
    // navigation it is holding open pending an answer. The three snapshots
    // below start at "" rather than matching whatever loads first by
    // accident: a real settings/character/persona object JSON.stringifies to
    // something that can never equal "", so panelDirty() reads as dirty
    // until the panel that owns each one has actually loaded once and taken
    // its own snapshot — never the wrong way around, where a coincidental
    // match waves through a real unsaved edit.
    confirmDiscardOpen: false,
    _pendingPanelAction: null,
    _settingsSnapshot: "",
    _characterSnapshot: "",
    _personaSnapshot: "",
    // Which of Brain's five categories is showing. Persists across closing
    // and reopening the panel — the same way openBackend/openTier already do
    // for what is folded open within one — rather than resetting to the
    // first tab every time.
    brainTab: "backends",
    historyFor: "",
    // Raised while a different chat's transcript is being fetched.
    loadingChat: false,
    // Variables whose band changed on the last update.
    bandsMoved: [],
    confirmChar: "",
    confirmChat: "",
    confirmMsg: "",
    // Which message's emoji picker is open, "" when none (§ message
    // reactions, openReactionPicker).
    reactingTo: "",
    // Exposed as a data property, not just the module-level const above —
    // Alpine's template expressions (x-for etc.) evaluate against the
    // component's own scope, not this script's outer closure, the same
    // reason every other fixed list a template iterates (settings.*,
    // this.chats, ...) lives on `this` rather than as a bare top-level name.
    messageReactions: MESSAGE_REACTIONS,
    // Which message's "Suggest edit" note box is open, "" when none (§
    // openSuggestEdit), and the free-text note being typed into it. The
    // presets live on `this` for the same reason messageReactions does.
    suggestingFor: "",
    suggestText: "",
    suggestEditPresets: SUGGEST_EDIT_PRESETS,
    // Which message is armed for select-to-copy, "" when none (§
    // startSelectCopy) — the bubble's own swipe/hold pointer handling steps
    // aside for this one message while it is set.
    selectingText: "",
    // The hold-to-delete modal for a character, null when closed. `state` is
    // "idle" (modal up, nothing pressed), "holding" (timing a press) or
    // "deleting" (the hold finished; the request is in flight).
    killHold: null,
    // A character's own line, shown as a speech bubble over its own row when
    // starring/unstarring it. `reactionBubble` is the content ({ id, text })
    // and outlives the visible window on purpose — reactionBubbleOpen alone
    // gates x-show, so the leave transition fades the actual sentence rather
    // than the text blanking out a frame before the fade even starts.
    reactionBubble: null,
    reactionBubbleOpen: false,
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
    revealSettling: false,
    composerMenu: false,
    // A press on the + button that has not been released yet: where it
    // started, which pointer it is, and whether the menu was already open
    // when the press began (that decides what a release over nothing does —
    // see onPlusUp). Which item the finger is currently over lives in
    // composerActive, kept separate so it can drive :class bindings on its
    // own without the whole hold object being reactive.
    plusHold: null,
    composerActive: -1,
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
    samplerBook: {},
    advancedFor: "",
    // Which passes' "More samplers" fold has been opened at least once this
    // visit to the Passes tab — see the x-if beside .fold in index.html.
    advancedSeen: {},
    rulesOpen: false,
    staged: [],
    cast: [],
    policies: [],
    policy: "natural",
    nextSpeaker: "",
    eventChance: 0,
    chatQuery: "",
    chatHits: [],
    searching: false,
    renamingChat: "",
    // Which chat's row-actions fold is open, if any — rename/export/delete
    // tucked behind the one glyph a chat-history row needs by default (§
    // index.html "Recent chats"), so the row itself is just a name and a
    // time instead of four tap targets fighting for a narrow phone screen.
    chatMenuFor: "",
    importingChat: false,
    armedRule: "",
    ruleSample: "The cat sat on the mat... twice.",
    ruleTests: {},
    sent: null,
    sentError: "",
    openPart: "",
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
    uploadingPfp: false,
    uploadingAvatarIdle: false,
    // Right at its default until it is not, same as the Advanced fold below.
    pfpEffectOpen: false,
    // Starts false on every open, flips true one frame later so the preview
    // has a "from" size already painted to grow out of — see openPfpEffect.
    pfpEffectGrown: false,
    // The picture-effect sheet's own sub-view: the six sliders that used to
    // sit under the swatch grid for everyone now only appear here, while
    // building a hue to add to the grid (§ openHueEditor). draftHue is that
    // in-progress value set; customHues is what's been saved from it,
    // loaded from and written back to localStorage since a hue someone
    // mixed themselves belongs to this browser, not to any one character.
    hueEditorOpen: false,
    draftHue: {},
    customHues: [],
    // The "Other expressions" editor — a fold that grew too long to sit
    // inline once a character had several, so it's a full-screen sheet
    // like Picture effect above rather than a wide.length grid of textareas
    // pushing the rest of the character form down.
    expressionsOpen: false,
    armedHue: "",
    armedHueTimer: 0,
    // The system prompt / stop strings / final instruction — technical
    // fields most edits never touch, folded away by default same as
    // showAdvancedBrain below.
    advancedCharOpen: false,
    // Reactions are generated text a player is not meant to read ahead of
    // triggering them — folded behind an explicit open instead of sitting in
    // the editor by default, which showed them to anyone who so much as
    // opened the character to change its name.
    reactionsOpen: false,
    // Set while a fill (background backfill or the explicit "Regenerate
    // reactions" button) is in flight, so the button can show it is doing
    // something instead of sitting there looking clickable a second time.
    regeneratingReactions: false,
    reactionsError: "",
    // What this character's memory pass has extracted so far — loaded
    // alongside the rest of the draft (§ editCharacter) so the "Edit
    // memories" button can show a live count without a second trip once the
    // modal actually opens.
    memoriesOpen: false,
    characterMemories: [],

    // Character vault (§ app/vault.py, main.py's /api/vault/*) — one PIN
    // keypad modal reused across setup/confirm/unlock/change/remove
    // (§ vaultModalMode/vaultModalTitle), plus the small menu the header's
    // gear opens while the vault is unlocked. `settings.vault_configured`/
    // `vault_unlocked` are the source of truth (§ loadSettings) — nothing
    // vault-shaped is tracked twice here.
    vaultModalOpen: false,
    vaultSettingsOpen: false,
    vaultModalMode: "unlock",
    vaultPinDigits: "",
    vaultPinFirst: "",
    vaultChangeCurrentPin: "",
    vaultChangeNewPin: "",
    vaultError: "",
    vaultShake: false,
    vaultBusy: false,

    // §main.py's /api/characters/{id}/compress — a preview only, same
    // "generate, review, then the ordinary Save path" shape as Reactions
    // above. Nothing here writes to the character until compressPanelApply
    // copies the reviewed text into draftCharacter, and even then only the
    // real Save button (§ saveCharacter) commits it.
    compressionOpen: false,
    compressing: false,
    compressionError: "",
    compressionResult: null,
    // Matches app/card_compression.py's FIELDS labels — the compression
    // sheet's own copy since the preview response only carries field ids.
    fieldLabels: {
      persona: "Persona", scenario: "Scenario",
      example_dialogue: "Example dialogue", system_prompt: "System prompt",
    },
    loadingMemories: false,
    memoryError: "",
    newMemoryText: "",
    armedMemory: "",
    armedMemoryTimer: 0,
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
    uploadingMusic: false,
    musicMsg: "",
    confirmMusic: "",
    importing: false,
    importMsg: "",
    importError: "",
    hud: false,
    debugLogBusy: false,
    // § brokenPfp/markPfpBroken below — which portrait URLs have 404'd.
    brokenPfps: {},
    error: "",

    streaming: false,
    composing: false,
    composingKind: "typing",
    composingLabel: "Typing…",
    // Who is answering, kept apart from the label so the same name can be put
    // in front of "is typing" and "is thinking" without parsing one back out
    // of the other.
    composingSpeaker: "",
    // Which message's portrait is currently blown up, and the picture filling
    // the screen if one is. Two states rather than one: enlarged is still part
    // of the conversation, full screen is not.
    bigPfp: "",
    // Choosing which part of a picture becomes the portrait. The box is held
    // in the image's own pixels, not the screen's: the stage resizes with the
    // sheet and with rotation, and a box stored in display pixels would move
    // every time it did.
    //
    // `slot` is which `pfp_set` entry this crop writes to on confirm —
    // "neutral" by default, or one of the character's other emotion sprites
    // (§ emotionPfpEntries, KNOWN-ISSUES.md "Emotion sprites don't go
    // through the cropper"). The shape toggle only applies to "neutral":
    // every other sprite is cropped to whatever shape neutral already
    // settled on, not free to disagree with it (§ cropShapeLocked).
    crop: {
      open: false, src: "", file: null, shape: "portrait", slot: "neutral", busy: false,
      nat: { w: 0, h: 0 },
      box: { x: 0, y: 0, w: 0, h: 0 },
      drag: null,
      // The frame is positioned from the *measured* image, and Alpine only
      // recomputes a binding when something reactive changes — so a layout
      // change with no state change left it where it was. Rotating the phone
      // put it 127px off the picture it was supposed to be framing. The
      // observer below bumps this, and the style reads it.
      tick: 0,
      watcher: null,
    },
    pfpFull: { src: "", shape: "portrait" },
    pfpFullLeaving: false,
    // How much reasoning has arrived this turn. Only ever a count: the
    // reasoning itself is not for the message stream (§5.6).
    thinkChars: 0,
    turn: 0,
    hudRuns: [],
    ambient: [],
    // music_select running (§ handleEvent's pass_status case) — shown as an
    // in-character line rather than an ambient chip, since "the character
    // is looking for a song" reads as something happening in the room, not
    // as ambient bookkeeping the way scene/expression/background refreshing
    // does.
    musicSearching: false,
    refreshing: { scene: false, expression: false, background: false },
    // "/" runs still resolving (§ runSlashCommand, resolveSlashRun), keyed
    // by the pass_runs id the server handed back when each was launched.
    pendingSlashRuns: {},
    // True once an automatic background_swap change has landed while a
    // settings-family panel (theme/brain/settings — one shared editing
    // session, § panelDirty) was open, and not yet acknowledged (§
    // settingsLocked, discardBackgroundChange). An ordinary background
    // edit made by hand in Theme already reads as dirty through
    // panelDirty() on its own; this is only for a change nobody in the
    // panel actually asked for.
    backgroundAutoChanged: false,
    cost: { per_pass: [], per_turn: [], totals: {} },
    totals: { tokens_in: 0, tokens_out: 0 },

    events: null,

    // ---- lifecycle ----

    async boot() {
      window.Markup = window.Markup || {};
      this.guardSliders();
      this.loadCustomHues();
      // Load settings first: the saved palette should be on screen before the
      // first paint of any message, not applied a beat later.
      try {
        this.settings = softenMasks(await api.get("/api/settings"));
        this.applyTheme();
      } catch (_) { /* defaults are already in the stylesheet */ }
      try {
        await this.loadCharacters();
        if (!this.characters.length) {
          // Not an error — a new install is supposed to look like this. It
          // used to raise a red banner telling the user to put a file in a
          // directory and restart, which was both alarming and wrong: the two
          // buttons that actually work are in the app, and neither needs a
          // restart. The empty state offers them instead.
          return;
        }
        this.characterId = this.characters[0].id;
        this.chats = await api.get("/api/chats");
        const last = localStorage.getItem("tavern:chat");
        if (last && this.chats.some((c) => c.id === last)) await this.openChat(last);
        else if (this.chats.length) await this.openChat(this.chats[0].id);
        else await this.newChat();
      } catch (e) {
        this.error = errorText(e);
      }
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
    },

    // ---- sliders that do not answer a scroll ----
    //
    // A range input sets its value the moment a finger lands on it, and every
    // panel here is a tall scroller full of them, so a scroll that began with
    // a thumb over one changed a setting on the way past.
    //
    // The first attempt at this called `preventDefault` on pointerdown, which
    // works for a mouse and does nothing at all on Android: the control is
    // driven by the touch sequence directly, not by the compatibility mouse
    // events that flag suppresses. So the input does not receive pointers at
    // all now (`pointer-events: none` in the stylesheet) and this drives it —
    // which means it cannot move unless the code below moves it.
    //
    // The claim rule is the message swipe's: vertical belongs to the scroller,
    // horizontal to the slider, and until the drag has committed to one it is
    // neither. A mouse skips the rule, having no scroll to conflict with.
    guardSliders() {
      const SLOP = 8;      // travel before the direction counts as decided
      const TAP_MS = 350;  // a still, brief press is a tap, not a held scroll

      const sliderUnder = (event) => {
        const field = event.target?.closest?.(".num-field, .num-box");
        const el = field?.querySelector('input[type="range"]');
        if (!el) return null;
        // Only when the press is actually on the slider's band. The field is
        // taller than the track: it holds the label and the note as well, and
        // a press on those is not a press on this.
        const rect = el.getBoundingClientRect();
        const within = event.clientY >= rect.top && event.clientY <= rect.bottom;
        return within ? el : null;
      };

      const setFromX = (el, clientX) => {
        const rect = el.getBoundingClientRect();
        if (!rect.width) return;
        const min = parseFloat(el.min || "0");
        const max = parseFloat(el.max === "" ? "100" : el.max);
        const step = parseFloat(el.step || "1") || 1;
        const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
        const raw = min + ratio * (max - min);
        // Snapped to the step and rounded, or a 0.05 step arrives as
        // 0.30000000000000004 and the number beside it says so.
        const value = parseFloat((Math.round(raw / step) * step).toFixed(4));
        if (String(value) === el.value) return;
        el.value = String(value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      };

      document.addEventListener("pointerdown", (event) => {
        if (!event.isPrimary) return;
        const el = sliderUnder(event);
        if (!el) return;
        const mouse = event.pointerType === "mouse";
        this._slide = {
          el, id: event.pointerId, x: event.clientX, y: event.clientY,
          at: performance.now(), claimed: mouse,
        };
        // A mouse press is unambiguous, so it acts immediately — click to
        // position, drag to adjust, exactly as the native control behaves.
        if (mouse) setFromX(el, event.clientX);
      });

      document.addEventListener("pointermove", (event) => {
        const slide = this._slide;
        if (!slide || event.pointerId !== slide.id) return;
        if (!slide.claimed) {
          const dx = event.clientX - slide.x;
          const dy = event.clientY - slide.y;
          // Vertical wins outright: that is a scroll, and this press is over.
          // The scroller is already handling it — `touch-action: pan-y` on the
          // field never let it stop.
          if (Math.abs(dy) > Math.abs(dx)) { this._slide = null; return; }
          if (Math.abs(dx) < SLOP) return;
          slide.claimed = true;
          // From here the panel must not scroll under the drag, which is what
          // the class switches off.
          slide.el.closest(".num-field")?.classList.add("sliding");
        }
        setFromX(slide.el, event.clientX);
        event.preventDefault();
      }, { passive: false });

      const release = (event) => {
        const slide = this._slide;
        if (!slide || (event.pointerId != null && event.pointerId !== slide.id)) return;
        this._slide = null;
        slide.el.closest(".num-field")?.classList.remove("sliding");
        if (slide.claimed && event.clientX != null) return setFromX(slide.el, event.clientX);
        // Never moved and let go quickly: a tap on a slider is a deliberate
        // way to set it, and a scroll is not still.
        const still = Math.hypot(event.clientX - slide.x, event.clientY - slide.y) < SLOP;
        if (still && performance.now() - slide.at < TAP_MS) setFromX(slide.el, event.clientX);
      };
      document.addEventListener("pointerup", release);
      document.addEventListener("pointercancel", release);
    },

    // Whether anything can add to Story > State on its own right now — both
    // halves have to be true (§ app/config.py post_process_tracks_state):
    // the foreground tier itself running, and the sub-toggle within it. Read
    // by the State section to show itself as inactive rather than just
    // silently empty when either is off.
    stateTrackingActive() {
      return !(this.settings.tiers_off || []).includes("foreground")
        && !!this.settings.post_process_tracks_state;
    },

    // Which variables changed on the last update, so the rows that moved can
    // say so. Trust and mood shift every turn and the panel used to just
    // re-render with different words — the most distinctive thing the engine
    // does, and invisible unless you were already reading carefully.
    //
    // `quiet` is for opening a chat: everything is new then, and flashing
    // every row would say nothing.
    setBands(next, { quiet = false } = {}) {
      const before = new Map(this.bands.map((b) => [b.variable, b.band]));
      this.bandsMoved = quiet || !before.size
        ? []
        : next.filter((b) => before.has(b.variable) && before.get(b.variable) !== b.band)
              .map((b) => b.variable);
      this.bands = next;
      if (!this.bandsMoved.length) return;
      buzz(8);
      clearTimeout(this._bandsTimer);
      // Cleared so a later re-render of the same rows does not replay it.
      this._bandsTimer = setTimeout(() => { this.bandsMoved = []; },
                                    dur("slow", 340) * 2 + 80);
    },

    // The shape the skeleton draws. Fixed rather than random: a placeholder
    // that reshuffles every time it appears draws attention to itself, and the
    // one thing it must not do is look like content arriving.
    skeletonRows: [
      { id: 1, side: "assistant", lines: [92, 78, 54] },
      { id: 2, side: "user", lines: [64] },
      { id: 3, side: "assistant", lines: [88, 71] },
      { id: 4, side: "user", lines: [46] },
    ],

    // A fresh install, before there is anyone to talk to. Derived rather than
    // stored so it cannot disagree with the list after a create, an import or
    // a delete — every one of those already refreshes `characters`.
    get nobodyYet() {
      return !this.characters.length;
    },

    // The transcript ends on something nobody answered — the reply failed, or
    // was stopped before a single token arrived. Not while a reply is on its
    // way, or the offer to retry would appear under every message as it is
    // being sent.
    get unanswered() {
      if (this.streaming || this.composing || !this.messages.length) return false;
      return this.messages[this.messages.length - 1].role === "user";
    },

    // Whether the scene pass has produced anything yet. Before it has, the
    // header shows the name alone rather than a row of placeholder dashes.
    get hasScene() {
      return !!(this.scene.place || this.scene.weather || this.scene.time);
    },

    // Whether the world-info fields belong in the header (§ .world-pill-inline
    // in index.html, wrapping onto a second line there if one line isn't
    // enough) rather than floating over the chat instead (§ .world-pill, the
    // Dynamic Island — now the fallback of last resort, not the everyday
    // case). A character count, not a measured pixel width: the header holds
    // a fixed 44px menu button and stretches over whatever width the phone
    // actually has, so there is no one pixel budget to measure against
    // without a ResizeObserver re-running on every keystroke of a streamed
    // scene update — and the two texts are set in the same font at adjacent
    // sizes, so length tracks width closely enough to draw the line. Both
    // budgets are tuned for a 360-412px phone, which is what this app is
    // built for.
    pillFitsInline() {
      if (!this.character) return false;
      const fields = [this.scene.place, this.scene.weather, this.scene.time].filter(Boolean);
      if (!fields.length) return false;
      if (this.character.name.length > WORLDBAR_NAME_BUDGET) return false;
      const fieldsCost = fields.join("").length + fields.length;
      return fieldsCost <= WORLDBAR_FIELDS_BUDGET;
    },

    get portrait() {
      if (!this.character) return "";
      const set = this.character.pfp_set || {};
      return pfpUrl(set[this.expression] || set.neutral || "");
    },

    // How this speaker's picture is framed. Per character, because it is a
    // property of the drawing rather than of the app (§models.pfp_shape).
    portraitShape(message) {
      if (message && message.speaker_id && this.cast.length > 1) {
        const who = this.cast.find((m) => m.character_id === message.speaker_id);
        if (who && who.pfp_shape) return who.pfp_shape;
      }
      return (this.character && this.character.pfp_shape) || "portrait";
    },

    // Same resolution as portraitShape, for the colour treatment instead of
    // the frame — it belongs to whoever is speaking, not to the chat. Returns
    // the raw effect object, not a style string: the caller draws it onto a
    // `.pfp-glow` sibling of the picture, never the picture itself (see the
    // note on pfpEffectStyle), and needs the object for pfpEffectOn too.
    portraitEffect(message) {
      if (message && message.speaker_id && this.cast.length > 1) {
        const who = this.cast.find((m) => m.character_id === message.speaker_id);
        if (who && who.pfp_effect) return who.pfp_effect;
      }
      return this.character && this.character.pfp_effect;
    },

    // Tap to look at it, tap again to put it back. The bubble beside it gives
    // up the width, which is the point: at 34px a portrait is punctuation, and
    // this is for when you actually want to see who you are talking to.
    togglePfp(message) {
      if (!message || !this.portraitFor(message)) return;
      this.bigPfp = this.bigPfp === message.id ? "" : message.id;
      buzz(4);
    },

    openPfpFull(src, shape, effect) {
      if (!src) return;
      this.pfpFullLeaving = false;
      this.pfpFull = { src, shape: shape || "portrait", effect: effect || null };
      buzz(6);
    },

    // Kept mounted until the shrink has finished — an overlay that vanishes on
    // the frame you release it never appears to have gone anywhere.
    closePfpFull() {
      if (!this.pfpFull.src || this.pfpFullLeaving) return;
      this.pfpFullLeaving = true;
      setTimeout(() => {
        this.pfpFull = { src: "", shape: "portrait", effect: null };
        this.pfpFullLeaving = false;
      }, PANEL_LEAVE_MS());
    },

    // A portrait `<img>` that has 404'd, keyed by its own src (§ brokenPfps
    // in the data object). The fix for a real crash: every portrait `<img>`
    // is the sole child of an x-if, and used to answer its own onerror with
    // `$el.remove()` — reaching past Alpine to yank out the exact element
    // its x-if was tracking. The next time that x-if's condition changed,
    // Alpine tried to reconcile against a node that no longer existed and
    // threw ("Cannot read properties of undefined (reading 'after')",
    // caught by a debug export — ISSUES-TRIAGE.md, freeze/crash follow-up).
    // Routing the failure through this reactive flag instead lets the x-if
    // itself go false through Alpine's own machinery, which is what removes
    // the element correctly. A URL stays marked broken for the rest of this
    // page load — right for a 404 off this app's own server, which was
    // never going to start working without a reload anyway.
    brokenPfp(url) {
      return !!url && !!this.brokenPfps[url];
    },
    markPfpBroken(url) {
      if (url) this.brokenPfps[url] = true;
    },

    // The face for one message. In a solo chat that is the character, with
    // whatever expression the last pass chose; in a group it is whoever spoke,
    // at rest, because the expression slice belongs to the chat and not to
    // each member of it.
    portraitFor(message) {
      if (!message || message.role !== "assistant") return "";
      if (message.speaker_id && this.cast.length > 1) {
        const who = this.cast.find((m) => m.character_id === message.speaker_id);
        if (who) return pfpUrl(who.pfp || "");
      }
      return this.portrait;
    },

    // The talking-video clip for one message, only while it is the one
    // currently live (AVATAR-VIDEO-CONTRACT.md) — every other row, and
    // every row in a group chat, always shows the static portrait above.
    // Group chats are out of scope for now: the contract renders against
    // one character's own idle loop, and a group's per-speaker version of
    // that isn't built.
    liveVideoFor(message) {
      if (!message || message.role !== "assistant") return "";
      if (this.cast.length > 1) return "";
      if (!this.liveAvatarVideo || this.liveAvatarVideo.messageId !== message.id) return "";
      if (!this.character || !(this.character.avatar_video || {}).enabled) return "";
      return this.liveAvatarVideo.url;
    },

    // The video plays once; once it has, the row falls back to the ordinary
    // static portrait with nothing left to track — no replay on scrollback.
    endAvatarVideo() {
      this.liveAvatarVideo = null;
    },

    async newChat(characterId) {
      const id = characterId || this.characterId;
      this.error = "";
      try {
        const chat = await api.post("/api/chats", { character_id: id });
        this.chats = await api.get("/api/chats");
        await this.openChat(chat.id);
        this.closePanel();
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // Tapping a character row opens the conversation it was last in — the
    // ordinary "open a contact" gesture — rather than doing nothing, which is
    // what the row did before this existed. `chatsFor` is already sorted by
    // `updated_at DESC` (touched on every message, not just on creation), so
    // the first entry is genuinely the most recently active one, not just the
    // most recently created.
    async openLastChat(character) {
      const chats = this.chatsFor(character.id);
      if (chats.length) {
        await this.openChat(chats[0].id);
        this.closePanel();
      } else {
        await this.newChat(character.id);
      }
    },

    async openChat(id) {
      // A portrait left enlarged in one chat has nothing to do with the next.
      this.bigPfp = "";
      // The transcript comes off a SQLite database on a phone, so this is a
      // real wait rather than a hypothetical one. It used to cut: the old
      // conversation vanished, nothing stood in for it, and the new one
      // appeared whole. The skeleton is only raised when the chat is actually
      // changing — reopening the one already on screen should not blank it.
      const switching = this.chatId !== id;
      if (switching) {
        this.messages = [];
        this.loadingChat = true;
      }
      let data;
      try {
        data = await api.get(`/api/chats/${id}`);
      } finally {
        this.loadingChat = false;
      }
      this.chatId = id;
      this.character = data.character;
      this.characterId = data.chat.character_id;
      this.pinCurrentCharacter();
      this.messages = data.messages;
      this.setBands(data.state.bands || [], { quiet: true });
      this.summary = data.summary;
      this.toggleStates = data.toggles || {};
      this.persona = data.persona || null;
      this.turn = this.messages.length ? this.messages[this.messages.length - 1].turn : 0;
      this.nextSpeaker = "";
      // No backdrop reset here on purpose: the backdrop is global (§
      // Settings.background, applyBackground below), so opening a different
      // chat keeps showing whatever it already was rather than reverting to
      // something else until background_swap fires again for this one.
      this.applyTheme();
      // Not awaited: the transcript should be on screen before the room's
      // membership is known, and a speaker label appearing a beat later is
      // better than a blank chat while one request finishes.
      this.loadCast();
      // Same reasoning — the library only matters once someone opens the
      // picker, which is never before the chat itself is on screen.
      this.loadMusicLibrary();

      // Cleared before being refilled. Merging onto whatever the last chat
      // left behind meant a brand-new conversation opened showing the previous
      // one's weather — the slice is per chat, so its absence is information.
      this.scene = { place: "", weather: "", time: "" };
      this.expression = "neutral";
      const sceneSlice = (data.slices || {})["state.scene"];
      if (sceneSlice) this.scene = { ...this.scene, ...sceneSlice.value };
      const expr = (data.slices || {})["state.expression"];
      if (expr && expr.value.emotion) this.expression = expr.value.emotion;
      // Same reasoning as scene/expression above: a chat that was never told
      // to play anything must not show whatever the last one left playing.
      this.music = { status: "none", track: null, character: null };
      const musicSlice = (data.slices || {})["state.music"];
      if (musicSlice) this.music = { ...this.music, ...musicSlice.value };

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
      await this.runOrGuard(() => this._openPanelNow(name));
    },

    async _openPanelNow(name) {
      this.panel = name;
      this.panelOpen = true;
      this.saveMsg = "";
      this.saveError = "";
      try {
        // Every load below is independent — none reads a value another one
        // sets — so they run as one round trip's worth of *wall time*
        // instead of stacking their latencies in a chain. Sequential
        // `await`s here used to be exactly why opening Brain in particular
        // (five separate fetches) read as the sheet stalling partway
        // through its own opening animation on a slow connection: the sheet
        // itself opens the instant `panelOpen` above is set, independent of
        // any of this, but a phone's single thread still has to get through
        // all this JSON before it can spend a frame on paint, so five fetches
        // in a row costs five fetches' worth of stall even though the sheet
        // was technically already "open".
        if (name === "brain") {
          this.passMsg = "";
          await Promise.all([
            this.loadSettings(),
            // Served rather than duplicated here: a slider for a parameter
            // no backend is sent would be worse than no slider.
            api.get("/api/samplers").then((s) => { this.samplerBook = s; }),
            api.get("/api/passes").then((p) => { this.passes = p; }),
          ]);
        } else if (name === "story") {
          // A top-level destination of its own now, not a tab riding along
          // inside Brain (§12) — loads only what it needs rather than
          // everything Brain's own branch above fetches. `settings` isn't
          // among them: boot() already loaded it once for the theme, and
          // Story's own read of it (the web_search toggle's visibility)
          // only ever needs whatever is already current, the same way
          // Brain's own tabs share one copy rather than each keeping its own.
          await Promise.all([
            this.loadNote(),
            api.get("/api/passes").then((p) => { this.passes = p; }),
          ]);
          // Folded in here rather than calling the loadEventChance this
          // replaced: that method fetched /api/passes a second time for a
          // list the line above already has.
          const randomEvent = this.passes.find((p) => p.id === "random_event");
          this.eventChance = randomEvent ? (randomEvent.trigger.probability || 0) : 0;
        } else if (name === "theme") {
          this.bgMsg = "";
          await Promise.all([this.loadSettings(), this.loadBackdrops()]);
        } else if (name === "music") {
          this.musicMsg = "";
          await Promise.all([this.loadSettings(), this.loadMusicLibrary()]);
        } else if (name === "chats") {
          await Promise.all([
            this.loadCharacters(),
            api.get("/api/chats").then((c) => { this.chats = c; }),
            this.loadPersonas(),
          ]);
          // Every history starts closed: a roster of characters is the thing
          // being looked at, and one of them unrolled pushes the rest down.
          this.historyFor = "";
          this.chatMenuFor = "";
        } else if (name === "settings") {
          await Promise.all([
            this.loadSettings(),
            // So the "Auto-rename chats" checkbox below has the chat_rename
            // pass to read and flip — the same object Brain's own pass-line
            // switch uses (§ chatRenamePass, togglePass), which is what
            // keeps the two connected: they are one boolean, not two.
            api.get("/api/passes").then((p) => { this.passes = p; }),
          ]);
        }
      } catch (e) {
        this.error = errorText(e);
      }
    },

    closePanel() {
      this.runOrGuard(() => this._closePanelNow());
    },

    _closePanelNow() {
      this.panelOpen = false;
      this.confirmChar = "";
      this.confirmChat = "";
      // Drop the body only once the sheet has finished leaving, and only if
      // nothing has been opened in the meantime.
      setTimeout(() => { if (!this.panelOpen) this.panel = ""; }, PANEL_LEAVE_MS());
    },

    // Character and persona editors have their own back arrow straight to
    // the roster, separate from the sheet's own ✕ — same guard either way,
    // since both leave whichever editor is open.
    backToRoster() {
      this.runOrGuard(() => { this.panel = "chats"; });
    },

    // ---- unsaved-changes guard (§ "ask if someone exits without saving") --
    //
    // One deep-equality snapshot per editable destination, taken the moment
    // its data is freshly loaded or created and refreshed the moment it
    // actually saves (§ snapshotSettings, snapshotCharacter, snapshotPersona
    // and their call sites). Simpler than threading a dirty flag through
    // every field across three different editors, and exact: JSON.stringify
    // has nothing to get wrong on plain data with no functions or Dates in
    // it, which is all any of these three ever hold.
    panelDirty() {
      if (this.panel === "brain" || this.panel === "theme" || this.panel === "settings") {
        return JSON.stringify(this.settings) !== this._settingsSnapshot;
      }
      if (this.panel === "character") {
        return JSON.stringify([this.draftCharacter, this.altGreetings, this.stopStrings])
          !== this._characterSnapshot;
      }
      if (this.panel === "persona") {
        return JSON.stringify(this.draftPersona) !== this._personaSnapshot;
      }
      return false;
    },

    // Whether a settings-family panel should hold off on further edits: an
    // automatic background change either still running or sitting
    // unacknowledged (§ backgroundAutoChanged) — either way, nothing in the
    // panel body should be touched until it is dealt with, since the next
    // Save would otherwise fold the AI's own pick in alongside whatever the
    // person actually meant to change.
    get settingsLocked() {
      return (this.refreshing.background || this.backgroundAutoChanged)
        && (this.panel === "brain" || this.panel === "theme" || this.panel === "settings");
    },

    snapshotSettings() {
      this._settingsSnapshot = JSON.stringify(this.settings);
      // A fresh baseline absorbs whatever background value is current —
      // nothing left to discard against once the panel has (re)started
      // from here, whether that's a normal Save or a freshly opened panel.
      this.backgroundAutoChanged = false;
    },

    // "Unlock changes and discard current background change": puts
    // settings.background back to what it was when this editing session's
    // own snapshot was last taken, so the automatic pick that landed
    // mid-edit does not count as something to save — the same "preview
    // only, Save keeps it" rule every other field on this panel already
    // follows, just reached from the opposite direction. Purely local
    // until the panel's own Save is pressed, same as any other field here:
    // the server keeps whatever background_swap actually persisted unless
    // and until that happens.
    discardBackgroundChange() {
      const before = JSON.parse(this._settingsSnapshot || "{}").background;
      if (before !== undefined) this.settings.background = before;
      this.applyBackground();
      this.snapshotSettings();
    },

    snapshotCharacter() {
      this._characterSnapshot = JSON.stringify([this.draftCharacter, this.altGreetings, this.stopStrings]);
    },

    snapshotPersona() {
      this._personaSnapshot = JSON.stringify(this.draftPersona);
    },

    // The one place all three things that try to move away from a dirty
    // panel actually go through — closing it (closePanel), switching to a
    // sibling settings tab that would otherwise silently re-fetch out from
    // under an unsaved edit (openPanel), and the character/persona editor's
    // own back arrow (backToRoster). Runs `action` outright when there is
    // nothing to lose; otherwise holds it until the confirm sheet answers.
    async runOrGuard(action) {
      if (this.panelOpen && this.panelDirty()) {
        this._pendingPanelAction = action;
        this.confirmDiscardOpen = true;
        return;
      }
      await action();
    },

    async discardAndProceed() {
      this.confirmDiscardOpen = false;
      const action = this._pendingPanelAction;
      this._pendingPanelAction = null;
      if (action) await action();
    },

    cancelDiscard() {
      this.confirmDiscardOpen = false;
      this._pendingPanelAction = null;
    },

    // Drives the one Save bar pinned to the bottom of the sheet (§ index.html,
    // ".sheet-actions-pinned"), so every panel that can be dirty gets an
    // always-visible Save without five copies of the same markup. Null means
    // "no bar here" — either nothing in this panel is saved this way (Story
    // saves per field as you type) or the panel itself has nothing to save
    // (browsing chats, "What was sent", ...).
    activeSaveAction() {
      if (this.panel === "story") return null;
      if (this.panel === "brain" || this.panel === "theme" || this.panel === "settings") {
        return { save: () => this.saveSettings(), saving: this.saving, msg: this.saveMsg, error: this.saveError };
      }
      if (this.panel === "character") {
        return { save: () => this.saveCharacter(), saving: this.savingCharacter, msg: this.charMsg, error: this.charError };
      }
      if (this.panel === "persona") {
        return { save: () => this.savePersona(), saving: this.savingPersona, msg: this.personaMsg, error: this.personaError };
      }
      return null;
    },

    panelTitle() {
      return {
        brain: "Model & engine",
        theme: "Appearance",
        chats: "Characters & chats",
        settings: "Settings",
        character: "Edit character",
        persona: "Edit persona",
        story: "Story state",
        sent: "What was sent",
        thought: "What it thought",
        music: "Music library",
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
        this.error = errorText(e);
      }
    },

    // Runs a "/" command (§ SLASH_COMMANDS, send()) — the same on-demand
    // pass endpoint refreshWorld() above uses, just for whichever pass the
    // command names. `pass_status` events (§ handleEvent) are what turn
    // `refreshing[flag]` back off *and* report the outcome (§
    // resolveSlashRun below) once the run actually finishes; nothing here
    // waits on either, since run_pass_now launches the pass and returns
    // immediately rather than awaiting it. The "before" value is read now,
    // not when the run resolves — by then it may already be the "after".
    async runSlashCommand(command) {
      if (!this.chatId || this.refreshing[command.flag]) return;
      this.refreshing[command.flag] = true;
      this.flashHint(command.hint);
      try {
        const result = await api.post(`/api/chats/${this.chatId}/passes/${command.passId}/run`, {});
        this.pendingSlashRuns[result.run_id] = { command, before: command.describe(this) };
      } catch (e) {
        this.refreshing[command.flag] = false;
        this.flashHint(errorText(e));
      }
    },

    // The other half of runSlashCommand: called from handleEvent for every
    // pass_status that lands, cheap to no-op on the ones that were never a
    // "/" run (almost all of them) since the lookup is by that run's own id.
    // A "/" run's own pass_status is otherwise indistinguishable from a
    // scheduled one — same event, same shape — which is the point (§
    // run_pass_now's own docstring, scheduler.py): a forced run behaves
    // exactly like one the engine decided to make on its own.
    resolveSlashRun(run) {
      const pending = this.pendingSlashRuns[run.id];
      if (!pending) return;
      delete this.pendingSlashRuns[run.id];
      // A panel event for this same run, if the pick was valid, arrives over
      // the same SSE connection strictly before this terminal status does —
      // so by the time command.outcome calls describe() again, it already
      // reads whatever changed, with nothing here needing to read the panel
      // event itself.
      this.flashHint(pending.command.outcome(this, run, pending.before));
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
        this.previewText = errorText(e);
        this.previewStop = "";
      }
    },

    // ---- what it thought (§5.6) ----

    async showThinking(message) {
      this.thought = null;
      this.thoughtError = "";
      this.openPanel("thought");
      try {
        const body = await api.get(`/api/messages/${message.id}/thinking`);
        if (body.ok) this.thought = body;
        else this.thoughtError = body.reason || "Nothing was kept for this one.";
      } catch (e) {
        this.thoughtError = errorText(e);
      }
    },

    // ---- what was sent (§15) ----

    async showPrompt(message) {
      this.sent = null;
      this.sentError = "";
      this.openPart = "";
      this.openPanel("sent");
      try {
        const body = await api.get(`/api/messages/${message.id}/prompt`);
        if (body.ok) this.sent = body;
        else this.sentError = body.reason || "No record of this one.";
      } catch (e) {
        this.sentError = errorText(e);
      }
    },

    // Rows are grouped under their band, in the order they were sent. `note`
    // rides along from settings.prompt_bands even though the per-part rows
    // below don't show it — the context meter's legend does, and pulling it
    // from here rather than a second lookup keeps the two views unable to
    // name a band differently.
    sentBands() {
      if (!this.sent) return [];
      const known = this.settings.prompt_bands || [];
      const out = [];
      for (const part of this.sent.parts) {
        const last = out[out.length - 1];
        if (last && last.id === part.band) last.parts.push(part);
        else {
          const band = known.find((b) => b.id === part.band);
          out.push({
            id: part.band,
            label: band ? band.label : part.band,
            note: band ? band.note : "",
            parts: [part],
          });
        }
      }
      return out;
    },

    // ---- context meter (§15) ----
    //
    // The summary bar at the top of "What was sent": how much of the fitted
    // budget this turn actually used, broken down by the same three bands
    // the rows below are already sorted into. Only drawn when the record
    // carries a budget — an older record (saved before this existed, or one
    // whose backend never got fitted at all) has nothing to measure against
    // and is still shown in full below, just without the meter.

    // A token count as the eye reads it rather than as the estimator
    // produced it: bare below a thousand (812), one decimal up to ten
    // thousand (7.8k), a round number above that (78k). Also used by
    // hordePresetSummary() for its own target-size tagline, so the two
    // never draw a token count in two different shapes.
    fmtTokens(n) {
      n = Math.round(n || 0);
      if (n < 1000) return String(n);
      return n < 10000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k`;
    },

    ctxBandColors: { prefix: "var(--band-prefix)", middle: "var(--band-middle)", volatile: "var(--band-volatile)" },

    ctxBandColor(id) {
      return this.ctxBandColors[id] || "var(--muted)";
    },

    // Same grouping sentBands() already computed, collapsed to one token
    // count per band — so the meter and the accordion can never disagree
    // about which parts a band counted.
    ctxMeterBands() {
      return this.sentBands().map((band) => ({
        ...band,
        tokens: band.parts.reduce((sum, p) => sum + p.tokens, 0),
      }));
    },

    ctxMeterPct() {
      const budget = this.sent && this.sent.budget;
      if (!budget) return 0;
      return Math.round(((this.sent.total_tokens || 0) / budget) * 100);
    },

    // A band's own share of the *budget*, not of the total used — so each
    // segment's width means the same thing turn to turn, and the bar's
    // overall filled width already equals how full the window actually is,
    // rather than always summing to a fixed bar regardless of usage.
    ctxMeterSegWidth(band) {
      const budget = this.sent && this.sent.budget;
      if (!budget) return "0%";
      return `${Math.max(0, (band.tokens / budget) * 100)}%`;
    },

    // A band's share of what was actually sent, for the legend row — same
    // denominator and rounding as partShare() below, just grouped.
    ctxBandShare(band) {
      const total = (this.sent && this.sent.total_tokens) || 1;
      return `${Math.round((band.tokens / total) * 100)}%`;
    },

    // A share of the whole prompt, for the bar next to each row. Against the
    // largest section rather than the total: at a hundred sections every bar
    // would otherwise be a slit, and the question being asked is which of
    // these is the expensive one.
    partWidth(part) {
      const most = Math.max(...this.sent.parts.map((p) => p.tokens), 1);
      return `${Math.max(3, Math.round((part.tokens / most) * 100))}%`;
    },

    partShare(part) {
      const total = this.sent.total_tokens || 1;
      return `${Math.round((part.tokens / total) * 100)}%`;
    },

    togglePart(part) {
      if (!part.text) return this.flashHint("These are the messages on screen");
      this.openPart = this.openPart === part.id ? "" : part.id;
    },

    // ---- find and replace (§16) ----

    scopeNote(rule) {
      const scopes = (this.settings.regex_meta || {}).scopes || [];
      return (scopes.find((s) => s.id === rule.scope) || {}).note || "";
    },

    addRule() {
      const max = ((this.settings.regex_meta || {}).max_rules) || 40;
      const rules = this.settings.regex_rules || (this.settings.regex_rules = []);
      if (rules.length >= max) return this.flashHint(`${max} rules is the limit`);
      rules.push({
        id: Math.random().toString(36).slice(2, 10),
        // Display, always: it is the only scope that can be undone, so it is
        // the only honest place for a rule nobody has tested yet to start.
        label: "New rule", find: "", replace: "", scope: "display", role: "both",
        enabled: true, ignore_case: true, multiline: false, dot_all: false,
      });
    },

    removeRule(rule) {
      if (this.armedRule !== rule.id) {
        this.armedRule = rule.id;
        clearTimeout(this._armedRuleTimer);
        this._armedRuleTimer = setTimeout(() => { this.armedRule = ""; }, CONFIRM_MS);
        return;
      }
      this.armedRule = "";
      const rules = this.settings.regex_rules;
      this.flipRules(() => rules.splice(rules.indexOf(rule), 1));
    },

    moveRule(rule, direction) {
      const rules = this.settings.regex_rules;
      const at = rules.indexOf(rule);
      const to = at + direction;
      if (to < 0 || to >= rules.length) {
        return this.flashHint(direction < 0 ? "Already first" : "Already last");
      }
      this.flipRules(() => {
        rules.splice(at, 1);
        rules.splice(to, 0, rule);
      });
    },

    // Same FLIP as the prompt sections: a rule travels past its neighbour
    // rather than the two of them swapping in one frame. Order is the whole of
    // how two rules interact, so the move has to be legible.
    flipRules(mutate) {
      const before = new Map(
        [...document.querySelectorAll(".rule")].map((r) => [
          r.dataset.rid, r.getBoundingClientRect().top,
        ]),
      );
      mutate();
      this.$nextTick(() => {
        for (const row of document.querySelectorAll(".rule")) {
          const was = before.get(row.dataset.rid);
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

    // The pattern is run on the server, by the same code that will run it for
    // real — including the check that refuses one slow enough to hang the app.
    // A second regex engine in the browser would disagree about escapes,
    // groups and Unicode, which is the whole reason this is a round trip.
    queueRuleTest(rule) {
      clearTimeout(this._ruleTestTimer);
      this._ruleTestTimer = setTimeout(() => this.testRule(rule), PREVIEW_DEBOUNCE_MS);
    },

    async testRule(rule) {
      if (!rule.find) {
        this.ruleTests = { ...this.ruleTests, [rule.id]: null };
        return;
      }
      try {
        const body = await api.post("/api/regex/test", { rule, sample: this.ruleSample });
        this.ruleTests = { ...this.ruleTests, [rule.id]: body };
      } catch (e) {
        this.ruleTests = {
          ...this.ruleTests,
          [rule.id]: { ok: false, error: errorText(e) },
        };
      }
    },

    // ---- prompt manager ----

    sectionsIn(band) {
      return (this.settings.prompt_sections || []).filter((s) => s.band === band);
    },

    // The two numbers a backend has rather than a pass: how much it may write
    // at once, and how much it can hold. Ranges are the useful span — the box
    // beside each still takes anything the server allows.
    backendNumbers() {
      return [
        { key: "max_tokens", label: "Max output", min: 128, max: 16384, step: 64,
          unit: " tok", zero: "",
          note: "The most this backend writes in one answer. Every pass asks "
              + "for what it needs and is capped here; reasoning is paid for "
              + "on top." },
        { key: "context", label: "Context window", min: 0, max: 250000, step: 1024,
          unit: " tok", zero: "asked",
          note: "Prompt and answer together. Left at 0 the backend is asked "
              + "what it is serving, which is the right answer whenever it can "
              + "give one — set a number only to override it." },
      ];
    },

    backendNumber(backend, field) {
      const value = parseInt(backend[field.key], 10);
      return Number.isFinite(value) ? value : field.min;
    },

    setBackendNumber(backend, field, raw) {
      const value = parseInt(raw, 10);
      if (Number.isNaN(value)) return;
      backend[field.key] = Math.max(field.min, Math.min(field.max, value));
    },

    backendNumberLabel(backend, field) {
      const value = this.backendNumber(backend, field);
      return !value && field.zero ? field.zero : `${value}${field.unit || ""}`;
    },

    // Sections that carry their own words rather than filling a slot: your
    // blocks, and the writing blocks that ship with the app. Both open an
    // editor when their name is tapped; everything else toggles.
    hasText(section) {
      return !!(section.custom || section.shipped);
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

    // The craft:length block's number-box editor (§ index.html): a friendlier
    // way to write its text than typing prose, for the one block whose whole
    // job is a number. The range lives in the text itself rather than a
    // separate setting — reading it back out with the same shape
    // setLengthRange writes keeps the boxes in step with whatever is actually
    // there, including a shipped default nobody has touched yet. Returns null
    // for wording that doesn't start that way (hand-edited past recognition),
    // and the boxes fall back to the shipped 1-2 for display until touched.
    lengthRange(section) {
      const m = /^(\d+)(?:\s*to\s*(\d+))?\s*paragraphs?\b/i.exec((section.text || "").trim());
      if (!m) return null;
      const min = parseInt(m[1], 10);
      return { min, max: m[2] ? parseInt(m[2], 10) : min };
    },

    // Regenerates the block's whole text from a paragraph range — touching
    // either box always produces boxes-driven text, even over hand-written
    // wording, rather than leaving an ambiguous "which one wins" state.
    // Word counts are derived, not typed: nobody can keep "6 to 10 paragraphs,
    // roughly 400 to 600 words" internally consistent by hand as the
    // paragraph count changes, so the words follow the paragraphs instead.
    setLengthRange(section, minRaw, maxRaw) {
      const min = Math.max(1, Math.min(60, parseInt(minRaw, 10) || 1));
      const max = Math.max(min, Math.min(60, parseInt(maxRaw, 10) || min));
      const words = (p) => Math.round((p * WORDS_PER_PARAGRAPH) / 50) * 50;
      const span = min === max ? `${min} paragraph${min === 1 ? "" : "s"}` : `${min} to ${max} paragraphs`;
      const wordsMin = words(min), wordsMax = words(max);
      const wordSpan = wordsMin === wordsMax ? `roughly ${wordsMin} words` : `roughly ${wordsMin} to ${wordsMax} words`;
      section.text = `${span}, ${wordSpan}. The upper end is a hard ceiling — `
        + "stop there even if the scene is not resolved, and leave the rest for the next "
        + "reply, rather than adding one more paragraph to tie things up. Never stop "
        + "mid-sentence: finish the clause you are in, then stop. The lower end is not a "
        + "floor to fill: shorter is always fine.\n\n"
        + "Do not take the length of earlier messages in the conversation as the target.\n";
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
        this.error = errorText(e);
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
        this.error = errorText(e);
      }
    },

    // ---- personas ----

    async loadPersonas() {
      try {
        this.personas = (await api.get("/api/personas")).personas;
      } catch (e) {
        this.error = errorText(e);
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
        this.error = errorText(e);
      }
    },

    async makeDefaultPersona(persona) {
      try {
        await api.put(`/api/personas/${persona.id}`, { is_default: true });
        await this.loadPersonas();
        this.flashHint(`New chats will use ${persona.name}`);
      } catch (e) {
        this.error = errorText(e);
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
      this.snapshotPersona();
    },

    editPersona(persona) {
      this.draftPersona = { ...persona, is_default: !!persona.is_default };
      this.personaMsg = "";
      this.personaError = "";
      this.panel = "persona";
      this.snapshotPersona();
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
        this.snapshotPersona();
        await this.loadPersonas();
        // The one in use may be the one just renamed.
        if (this.chatId) await this.refreshPersona();
        this.personaMsg = "Saved";
      } catch (e) {
        this.personaError = errorText(e);
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
        this.error = errorText(e);
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
        this.personaError = errorText(e);
      } finally {
        this.uploadingAvatar = false;
        event.target.value = "";
      }
    },

    // A picture for the character being edited. The same endpoint the persona
    // avatars use — one upload path, one size limit, one set of allowed types
    // — and the URL it returns is stored rather than a bare filename, so it
    // can be told apart from the file an imported card brought with it.
    // The file is not uploaded here any more: it opens the cropper, and what
    // gets uploaded is the frame chosen there. A whole card squeezed into a
    // 34px box by object-fit is a picture of somebody's midriff.
    //
    // `slot` is which `pfp_set` entry this replaces — "neutral" from the
    // main picture control, or one of the emotion sprites listed below it
    // (§ emotionPfpEntries). Either way the shape is fixed to whatever neutral
    // already uses (§ crop.slot, above) rather than read back out of
    // whatever this one image happens to be shaped like: a card's `happy`
    // sprite arriving square while `neutral` is a portrait is exactly the
    // mismatch this exists to fix, not a second shape to introduce.
    uploadCharacterPfp(event, slot = "neutral") {
      const file = (event.target.files || [])[0];
      event.target.value = "";
      if (!file) return;
      this.charError = "";
      if (this.crop.src) URL.revokeObjectURL(this.crop.src);
      this.crop = {
        ...this.crop,
        open: true,
        busy: false,
        file,
        slot,
        src: URL.createObjectURL(file),
        shape: this.draftCharacter.pfp_shape || "portrait",
        nat: { w: 0, h: 0 },
        box: { x: 0, y: 0, w: 0, h: 0 },
        drag: null,
      };
    },

    // ---- the cropper ----

    // Widest box of the wanted shape that fits, centred. Recomputed rather than
    // scaled when the shape changes, so switching back and forth cannot walk
    // the frame off the edge one rounding error at a time.
    cropFit(shape) {
      const { w, h } = this.crop.nat;
      const ratio = shape === "square" ? 1 : 2 / 3;   // width ÷ height
      let width = Math.min(w, h * ratio);
      let height = width / ratio;
      if (height > h) { height = h; width = height * ratio; }
      return { x: (w - width) / 2, y: (h - height) / 2, w: width, h: height };
    },

    cropLoaded(event) {
      const img = event.target;
      this.crop.nat = { w: img.naturalWidth || 1, h: img.naturalHeight || 1 };
      this.crop.box = this.cropFit(this.crop.shape);
      this.crop.watcher?.disconnect();
      if (typeof ResizeObserver === "function") {
        this.crop.watcher = new ResizeObserver(() => { this.crop.tick += 1; });
        this.crop.watcher.observe(img);
      }
    },

    setCropShape(shape) {
      if (this.crop.shape === shape) return;
      this.crop.shape = shape;
      if (this.crop.nat.w) this.crop.box = this.cropFit(shape);
      buzz(4);
    },

    // Image pixels per screen pixel. The stage letterboxes the picture, so the
    // rendered box is measured rather than assumed.
    cropScale() {
      const img = this.$refs.cropImage;
      if (!img || !img.clientWidth || !this.crop.nat.w) return 1;
      return this.crop.nat.w / img.clientWidth;
    },

    cropBoxStyle() {
      void this.crop.tick;   // recompute when the picture is re-laid-out
      const img = this.$refs.cropImage;
      const stage = this.$refs.cropStage;
      if (!img || !stage || !this.crop.nat.w) return "display: none";
      const scale = this.cropScale();
      // The picture is centred in the stage; the frame is drawn against the
      // stage, so its offset has to come back.
      const left = (stage.clientWidth - img.clientWidth) / 2;
      const top = (stage.clientHeight - img.clientHeight) / 2;
      const b = this.crop.box;
      return `left: ${left + b.x / scale}px; top: ${top + b.y / scale}px;`
        + ` width: ${b.w / scale}px; height: ${b.h / scale}px`;
    },

    cropDown(event, mode) {
      event.target.setPointerCapture?.(event.pointerId);
      this.crop.drag = {
        mode,
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        box: { ...this.crop.box },
      };
      const move = (e) => this.cropMove(e);
      const up = (e) => {
        this.crop.drag = null;
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    },

    cropMove(event) {
      const drag = this.crop.drag;
      if (!drag || event.pointerId !== drag.id) return;
      const scale = this.cropScale();
      const dx = (event.clientX - drag.x) * scale;
      const dy = (event.clientY - drag.y) * scale;
      const { w: nw, h: nh } = this.crop.nat;
      const ratio = this.crop.shape === "square" ? 1 : 2 / 3;

      if (drag.mode === "move") {
        this.crop.box = {
          ...drag.box,
          x: Math.min(Math.max(0, drag.box.x + dx), nw - drag.box.w),
          y: Math.min(Math.max(0, drag.box.y + dy), nh - drag.box.h),
        };
        return;
      }
      // Resize from the far corner, keeping the shape. The larger of the two
      // movements wins, so a diagonal drag does what it looks like it does.
      const wanted = Math.max(drag.box.w + dx, (drag.box.h + dy) * ratio);
      const limit = Math.min(nw - drag.box.x, (nh - drag.box.y) * ratio);
      const width = Math.min(Math.max(28, wanted), Math.max(28, limit));
      this.crop.box = { ...drag.box, w: width, h: width / ratio };
    },

    cancelCrop() {
      if (this.crop.src) URL.revokeObjectURL(this.crop.src);
      this.crop.watcher?.disconnect();
      this.crop = {
        ...this.crop, open: false, src: "", file: null, drag: null, watcher: null,
      };
    },

    // Drawn to a canvas at the size it will be shown at rather than uploaded
    // whole: a 4MB card behind a 34px frame is 4MB down the wire on every
    // load, and the phone is the thing loading it.
    async confirmCrop() {
      if (!this.crop.file || !this.crop.nat.w) return this.cancelCrop();
      this.crop.busy = true;
      try {
        const b = this.crop.box;
        const width = Math.min(PORTRAIT_MAX_PX, Math.round(b.w));
        const height = Math.round(width * (b.h / b.w));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const image = this.$refs.cropImage;
        canvas.getContext("2d").drawImage(
          image, b.x, b.y, b.w, b.h, 0, 0, width, height,
        );
        const blob = await new Promise((resolve) =>
          canvas.toBlob(resolve, "image/png"));
        if (!blob) throw new Error("could not read that image");

        this.uploadingPfp = true;
        const name = (this.crop.file.name || "portrait").replace(/\.[^.]+$/, "") + ".png";
        const response = await fetch(
          `/api/avatars?filename=${encodeURIComponent(name)}`,
          { method: "POST", body: blob },
        );
        if (!response.ok) throw await apiError(response);
        const saved = await response.json();
        this.draftCharacter.pfp_set = {
          ...(this.draftCharacter.pfp_set || {}), [this.crop.slot]: saved.url,
        };
        // Only neutral's own crop sets the character's shape — every other
        // slot was cropped *to* that shape (§ cropShapeLocked), not free to
        // set a different one now that it has its own picture again.
        if (this.crop.slot === "neutral") this.draftCharacter.pfp_shape = this.crop.shape;
        this.cancelCrop();
      } catch (e) {
        this.charError = errorText(e);
      } finally {
        this.uploadingPfp = false;
        this.crop.busy = false;
      }
    },

    // True once the cropper is open for anything but the main picture — the
    // shape toggle is hidden then (§ index.html), so this is also what locks
    // the crop box to neutral's own ratio rather than reading `crop.shape`
    // back out of whatever the image being replaced happened to be.
    cropShapeLocked() {
      return this.crop.slot !== "neutral";
    },

    clearCharacterPfp(slot = "neutral") {
      const set = { ...(this.draftCharacter.pfp_set || {}) };
      delete set[slot];
      this.draftCharacter.pfp_set = set;
      if (slot in (this.draftCharacter.expression_meta || {})) {
        const meta = { ...this.draftCharacter.expression_meta };
        delete meta[slot];
        this.draftCharacter.expression_meta = meta;
      }
    },

    // Per-emotion description/auto-pick metadata (§ Character.
    // expression_meta) — what the expression pass reads alongside the
    // slot's own name (scheduler.py). Live-edited here like every other
    // field on this panel (immutable replace, not a deep mutation, same
    // as setBgMeta in the Theme panel) and only persisted on Save.
    exprMeta(key) {
      return (this.draftCharacter.expression_meta || {})[key] || {};
    },
    setExprMeta(key, field, value) {
      const all = { ...(this.draftCharacter.expression_meta || {}) };
      all[key] = { ...(all[key] || {}), [field]: value };
      this.draftCharacter.expression_meta = all;
    },

    // Renaming a slot means rebuilding pfp_set with a different key
    // holding the same image — the dict key *is* the id, both for the
    // pass that picks by it (scheduler.py) and for whatever matches
    // `expression` against it to choose a portrait (§ get portrait), so
    // there is no separate label field the way a background has one.
    // Refused silently rather than erroring: a blank name reverts to
    // what it was, and a name colliding with an existing slot — "neutral"
    // included, the hero picture's own reserved key — would otherwise
    // quietly overwrite it instead of renaming anything.
    renameExpression(oldKey, rawNewKey) {
      const newKey = String(rawNewKey || "").trim().slice(0, 60);
      if (!newKey || newKey === oldKey) return;
      const set = { ...(this.draftCharacter.pfp_set || {}) };
      if (!(oldKey in set) || newKey in set) return;
      set[newKey] = set[oldKey];
      delete set[oldKey];
      this.draftCharacter.pfp_set = set;

      const meta = { ...(this.draftCharacter.expression_meta || {}) };
      if (oldKey in meta) {
        meta[newKey] = meta[oldKey];
        delete meta[oldKey];
        this.draftCharacter.expression_meta = meta;
      }
    },

    // A blank slot to crop a picture into (§ uploadCharacterPfp), same
    // upload path "Replace" on an existing slot already uses — the only
    // difference is the key is new rather than one already in pfp_set.
    // Left for the person to rename afterward (§ renameExpression), same
    // as a freshly uploaded background defaults to its filename.
    addCharacterExpression(event) {
      const set = this.draftCharacter.pfp_set || {};
      let n = 1;
      while (`expression-${n}` in set) n += 1;
      this.uploadCharacterPfp(event, `expression-${n}`);
    },

    draftPortrait() {
      const set = this.draftCharacter.pfp_set || {};
      return pfpUrl(set.neutral || Object.values(set)[0] || "");
    },

    // Every sprite besides neutral — a card's happy/sad/angry/… entries,
    // listed so each can be individually replaced or removed through the
    // same cropper neutral already goes through (§KNOWN-ISSUES.md, "Emotion
    // sprites don't go through the cropper"). Sorted for a stable order:
    // object key order follows insertion, which for an imported card is
    // whatever order its JSON happened to list them in.
    emotionPfpEntries() {
      const set = this.draftCharacter.pfp_set || {};
      return Object.keys(set)
        .filter((key) => key !== "neutral")
        .sort()
        .map((key) => ({ key, url: pfpUrl(set[key]) }));
    },

    // ---- picture effect (§models.PfpEffect) ----

    // The ring is just a colour, so the sheet is just a grid of colours —
    // a fresh, wider wheel replacing the old warm/cool/vivid/sepia/noir/dream
    // mix of six-knob looks (§KNOWN-ISSUES.md history), none of which read as
    // a "hue" at a glance. 360 rather than 0 for Red: hue-rotate(0) and
    // hue-rotate(360) draw identically, but 0 is also "untouched" (see
    // pfpEffectOn), and Red picked would otherwise vanish back into None.
    pfpEffectPresets() {
      const wheel = [
        ["Red", 360], ["Orange", 30], ["Yellow", 60], ["Lime", 90],
        ["Green", 120], ["Teal", 150], ["Cyan", 180], ["Sky", 210],
        ["Blue", 240], ["Violet", 270], ["Purple", 300], ["Pink", 330],
      ];
      return [
        // Its own id, not "" — "" is what a freehand-tuned custom hue clears
        // `preset` to (see addCustomHue), and reusing it here would make the
        // "None" swatch light up for values that are anything but.
        { id: "none", label: "None", values: { hue: 0, saturate: 1, brightness: 1, contrast: 1, sepia: 0, grayscale: 0 } },
        ...wheel.map(([label, hue]) => ({
          id: label.toLowerCase(),
          label,
          values: { hue, saturate: 1, brightness: 1, contrast: 1, sepia: 0, grayscale: 0 },
        })),
      ];
    },

    // What "New hue" below hands back — the built-ins plus whatever this
    // browser has mixed and kept. Appended, not merged in by hue order: a
    // hue someone made stays findable at the end rather than shuffling
    // around as more get added next to it.
    allHues() {
      return [...this.pfpEffectPresets(), ...this.customHues];
    },

    customHuesKey: "tavern:customHues",

    loadCustomHues() {
      try {
        const raw = localStorage.getItem(this.customHuesKey);
        const parsed = raw ? JSON.parse(raw) : [];
        this.customHues = Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        this.customHues = [];
      }
    },

    saveCustomHues() {
      try {
        localStorage.setItem(this.customHuesKey, JSON.stringify(this.customHues));
      } catch (_) { /* private browsing, full storage — a lost custom hue isn't worth erroring over */ }
    },

    // The colour a swatch actually draws — the same filter the ring itself
    // uses (§pfpEffectStyle), laid over the same base red .pfp-glow paints,
    // so picking a hue here is picking the true colour, not an approximation
    // of it. "None" has no filter to show, so it gets its own dashed, empty
    // look instead of defaulting to a plain red circle no one chose.
    hueSwatchStyle(values) {
      if (!pfpEffectOn(values)) return "";
      const filter = pfpEffectStyle(values);
      return `background: hsl(0 90% 55%); ${filter}`;
    },

    // The six sliders that used to sit under the swatch grid for every
    // character now only exist here, mixing a hue rather than tuning one
    // character's ring — see openHueEditor. One list rather than six
    // near-identical num-field blocks in the template.
    pfpEffectFields() {
      return [
        { key: "hue", label: "Hue", min: -180, max: 180, step: 1, default: 0 },
        { key: "saturate", label: "Saturation", min: 0, max: 3, step: 0.05, default: 1 },
        { key: "brightness", label: "Brightness", min: 0.5, max: 1.5, step: 0.02, default: 1 },
        { key: "contrast", label: "Contrast", min: 0.5, max: 1.5, step: 0.02, default: 1 },
        { key: "sepia", label: "Sepia", min: 0, max: 1, step: 0.02, default: 0 },
        { key: "grayscale", label: "Grayscale", min: 0, max: 1, step: 0.02, default: 0 },
      ];
    },

    applyPfpPreset(hue) {
      this.draftCharacter.pfp_effect = { preset: hue.id, ...hue.values };
    },

    pfpEffectActive() {
      return pfpEffectOn(this.draftCharacter.pfp_effect);
    },

    // A full-screen preview to choose a ring colour at, rather than the fold
    // it used to be — a treatment that shows as a few pixels around a 34px
    // face has to be judged bigger than that to be judged at all. Opens
    // small and grows into place (§ pfpEffectGrown, .pfp-effect-preview-slot
    // in the stylesheet) so the picture reads as the same one from the
    // identity block above, not a second one appearing from nowhere.
    openPfpEffect() {
      this.pfpEffectGrown = false;
      this.pfpEffectOpen = true;
      this.hueEditorOpen = false;
      this.$nextTick(() => requestAnimationFrame(() => { this.pfpEffectGrown = true; }));
    },

    closePfpEffect() {
      this.pfpEffectOpen = false;
      this.pfpEffectGrown = false;
      this.hueEditorOpen = false;
    },

    openExpressions() {
      this.expressionsOpen = true;
    },

    closeExpressions() {
      this.expressionsOpen = false;
    },

    // What the big preview at the top of the sheet actually shows — the
    // character's chosen ring normally, or the hue being mixed while the
    // editor below is open, so the same sliders that used to live right
    // next to that preview still drive it live.
    previewEffect() {
      return this.hueEditorOpen ? this.draftHue : this.draftCharacter.pfp_effect;
    },

    previewEffectActive() {
      return pfpEffectOn(this.previewEffect());
    },

    // The sliders' new home: starts from "no effect" every time rather than
    // wherever the character's own ring happens to be, since the point is to
    // mix a new hue, not nudge the current one — nudging it is what tapping
    // a swatch and then None-ing back out is for.
    openHueEditor() {
      this.draftHue = { hue: 0, saturate: 1, brightness: 1, contrast: 1, sepia: 0, grayscale: 0 };
      this.hueEditorOpen = true;
    },

    closeHueEditor() {
      this.hueEditorOpen = false;
    },

    tuneDraftHue(key, value) {
      this.draftHue = { ...this.draftHue, [key]: +value };
    },

    // toFixed rather than the raw bound value: a range input's own step math
    // can hand back 1.1500000000000001 for a perfectly ordinary 0.05 step,
    // same reasoning as m.talkativeness.toFixed(1) elsewhere in this file.
    draftHueValue(field) {
      const raw = this.draftHue[field.key] ?? field.default;
      return field.key === "hue" ? `${Math.round(raw)}°` : Number(raw).toFixed(2);
    },

    // Saves the mix as a new swatch — appended to customHues, picked for
    // this character immediately (mixing one is also choosing it), and
    // written to localStorage so it's still there next time this browser
    // opens any character's picture effect, not just this one's save.
    addCustomHue() {
      const hue = {
        id: `custom-${Date.now()}`,
        label: "Custom",
        values: { ...this.draftHue },
        custom: true,
      };
      this.customHues = [...this.customHues, hue];
      this.saveCustomHues();
      this.applyPfpPreset(hue);
      this.hueEditorOpen = false;
    },

    // Same armed-then-confirm shape as every other delete in this app (§
    // removeBlock, deletePersona, ...) — a second tap within CONFIRM_MS,
    // rather than a bare click, on a grid where the first tap already means
    // something (choosing the hue).
    removeCustomHue(hue) {
      if (this.armedHue !== hue.id) {
        this.armedHue = hue.id;
        clearTimeout(this.armedHueTimer);
        this.armedHueTimer = setTimeout(() => { this.armedHue = ""; }, CONFIRM_MS);
        return;
      }
      this.armedHue = "";
      this.customHues = this.customHues.filter((h) => h.id !== hue.id);
      this.saveCustomHues();
      // The character had this exact hue picked — falls back to None rather
      // than leaving `preset` pointing at a hue that no longer exists.
      if ((this.draftCharacter.pfp_effect || {}).preset === hue.id) {
        this.applyPfpPreset(this.pfpEffectPresets()[0]);
      }
    },

    // ---- reactions (§models.CharacterReactions) ----

    missingReactions() {
      const r = this.draftCharacter.reactions || {};
      return REACTION_KEYS.filter((k) => !(r[k] || "").trim());
    },

    // A field saves the moment it is changed rather than waiting on the
    // character editor's own pinned Save bar — same idiom as a memory's
    // textarea (§ saveMemoryEdit below), and for the same reason a step
    // further: clearing this field is the trigger for getting the line back
    // (see the call to fillMissingReactions at the bottom), and that only
    // works against what is actually in the database, not a draft still
    // sitting unsaved in the browser. Keeps `_characterSnapshot` in step too,
    // so having only touched Reactions does not leave the editor claiming
    // unsaved changes on the way out.
    async saveReactionField(key, value) {
      const text = value.trim();
      this.draftCharacter.reactions[key] = text;
      if (!this.draftCharacter.id) return; // a new, unsaved character — nothing to persist yet
      try {
        await api.put(`/api/characters/${this.draftCharacter.id}`, {
          reactions: { [key]: text },
        });
      } catch (e) {
        this.reactionsError = errorText(e);
        return;
      }
      this.reactionsError = "";
      this.syncCharacterReactions();
      this.snapshotCharacter();
      if (!text) this.fillMissingReactions();
    },

    // Whatever is left blank comes back on its own — no button to press,
    // just the same fill a stalled import or a first reply already gets
    // (§ app/character_reactions.py). Quiet on failure: this runs after
    // every field save, including ones nobody is watching for a result, so
    // an unreachable backend here should not read as this save having
    // failed — the field it just typed is not blank, only the generated one
    // still is.
    async fillMissingReactions() {
      if (!this.draftCharacter.id || this.regeneratingReactions) return;
      this.regeneratingReactions = true;
      try {
        const data = await api.post(
          `/api/characters/${this.draftCharacter.id}/reactions/regenerate`, {},
        );
        Object.assign(this.draftCharacter.reactions, data.reactions);
        this.syncCharacterReactions();
        this.snapshotCharacter();
      } catch (e) {
        // silent — see comment above
      } finally {
        this.regeneratingReactions = false;
      }
    },

    // The explicit do-over: all three, regardless of what is already there
    // — the one call that overwrites a line someone is happy with, so it
    // only ever runs off a tap, never on its own.
    async regenerateAllReactions() {
      if (!this.draftCharacter.id || this.regeneratingReactions) return;
      this.regeneratingReactions = true;
      this.reactionsError = "";
      try {
        const data = await api.post(
          `/api/characters/${this.draftCharacter.id}/reactions/regenerate`,
          { keys: [...REACTION_KEYS] },
        );
        Object.assign(this.draftCharacter.reactions, data.reactions);
        this.syncCharacterReactions();
        this.snapshotCharacter();
      } catch (e) {
        this.reactionsError = errorText(e);
      } finally {
        this.regeneratingReactions = false;
      }
    },

    // Runs the preview and opens the sheet — or, when the card already fits
    // the current budget, says so instead of opening an empty modal. Reads
    // from the *saved* character (§ main.py's compress route), so a new,
    // never-saved draft has nothing to compress yet.
    async runCompression() {
      if (!this.draftCharacter.id || this.compressing) return;
      this.compressing = true;
      this.compressionError = "";
      try {
        const result = await api.post(`/api/characters/${this.draftCharacter.id}/compress`, {});
        if (!result.needed) {
          this.flashHint("This card already fits your current prompt budget.");
          return;
        }
        this.compressionResult = result;
        this.compressionOpen = true;
      } catch (e) {
        this.compressionError = errorText(e);
        this.compressionOpen = true;
      } finally {
        this.compressing = false;
      }
    },

    // Fields worth showing at all — a field compression left untouched
    // (nothing to gain, or the backend's attempt came back no shorter, see
    // card_compression.compress_field) is not something to ask anyone to
    // review.
    compressedFields() {
      if (!this.compressionResult) return [];
      return Object.entries(this.compressionResult.fields)
        .filter(([, field]) => field.changed)
        .map(([id, field]) => ({ id, ...field }));
    },

    // Copies every changed field's compressed text into the draft — not
    // saved yet. The character editor's own pinned Save bar is what commits
    // it, same as typing the words in by hand would be; closing this sheet
    // without saving afterward discards it exactly like any other edit.
    applyCompression() {
      for (const field of this.compressedFields()) {
        this.draftCharacter[field.id] = field.after;
      }
      this.closeCompression();
      this.flashHint("Compressed text applied — review and Save");
    },

    closeCompression() {
      this.compressionOpen = false;
      this.compressionResult = null;
      this.compressionError = "";
    },

    // draftCharacter, the character behind the open chat, and this
    // character's own row in the roster are three separate objects — a
    // regenerated line has to reach all three it appears in, or a bubble
    // shown from whichever one this did not reach (star/unstar reads the
    // roster row; the header reads the open chat's copy) would still be
    // showing the stale line. saveCharacter()'s own flow gets this for free
    // by reloading the whole roster after every save; a reactions edit
    // saves far more often than that (every field, on blur) and reloading
    // the roster on each of those would be a lot of network for a change
    // this small, so it patches the one row that could actually be stale.
    syncCharacterReactions() {
      if (this.character && this.character.id === this.draftCharacter.id) {
        this.character.reactions = { ...this.draftCharacter.reactions };
      }
      const row = this.characters.find((c) => c.id === this.draftCharacter.id);
      if (row) row.reactions = { ...this.draftCharacter.reactions };
    },

    closeReactions() {
      this.reactionsOpen = false;
    },

    // ---- memories (§ app/memory.py) ----
    //
    // The extraction pass, the dedupe, and the keyword retrieval all live
    // server-side and stay untouched by this — this is only the one place a
    // person can see what got remembered, fix a wrong one, add a fact by
    // hand, or turn the whole thing off for this character. `keys` (what
    // retrieval actually matches on) is never shown or edited here: a memory
    // is a sentence to a person, and re-deriving the keys from whatever text
    // ends up saved is simpler and less to get wrong than asking anyone to
    // maintain a parallel list of keywords by hand.

    async loadCharacterMemories() {
      if (!this.draftCharacter.id) { this.characterMemories = []; return; }
      this.loadingMemories = true;
      try {
        this.characterMemories = await api.get(`/api/characters/${this.draftCharacter.id}/memories`);
      } catch (e) {
        this.memoryError = errorText(e);
      } finally {
        this.loadingMemories = false;
      }
    },

    openMemories() {
      this.memoryError = "";
      this.newMemoryText = "";
      this.memoriesOpen = true;
      // The list loaded on editCharacter() may be stale by the time this is
      // actually opened — cheap enough to just ask again.
      this.loadCharacterMemories();
    },

    closeMemories() {
      this.memoriesOpen = false;
    },

    async addMemory() {
      const text = this.newMemoryText.trim();
      if (!text) return;
      this.memoryError = "";
      try {
        this.characterMemories = await api.post(
          `/api/characters/${this.draftCharacter.id}/memories`, { text },
        );
        this.newMemoryText = "";
      } catch (e) {
        this.memoryError = errorText(e);
      }
    },

    // Saves on blur/change rather than behind a per-row button — the same
    // "edit in place, no separate save step" idiom as Story's author's note
    // and every colour field in Theme.
    async saveMemoryEdit(memory, rawText) {
      const text = rawText.trim();
      if (!text || text === memory.text) return;
      this.memoryError = "";
      try {
        const updated = await api.put(`/api/memories/${memory.id}`, { text });
        memory.text = updated.text;
      } catch (e) {
        this.memoryError = errorText(e);
      }
    },

    // Same armed-then-confirm shape as every other delete in this app.
    async removeMemory(memory) {
      if (this.armedMemory !== memory.id) {
        this.armedMemory = memory.id;
        clearTimeout(this.armedMemoryTimer);
        this.armedMemoryTimer = setTimeout(() => { this.armedMemory = ""; }, CONFIRM_MS);
        return;
      }
      this.armedMemory = "";
      try {
        await api.del(`/api/memories/${memory.id}`);
        this.characterMemories = this.characterMemories.filter((m) => m.id !== memory.id);
      } catch (e) {
        this.memoryError = errorText(e);
      }
    },

    // ---- talking avatar (AVATAR-VIDEO-CONTRACT.md) ----

    async uploadAvatarIdle(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.uploadingAvatarIdle = true;
      this.charError = "";
      try {
        const response = await fetch(
          `/api/characters/${this.draftCharacter.id}/avatar-idle?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        if (!response.ok) throw await apiError(response);
        // Merged onto the draft rather than replacing it: the server hands
        // back the character as it is actually stored, and this is the one
        // field that just changed under it — a whole-object overwrite would
        // also discard whatever the rest of the form has unsaved.
        this.draftCharacter.avatar_video = (await response.json()).avatar_video;
      } catch (e) {
        this.charError = errorText(e);
      } finally {
        this.uploadingAvatarIdle = false;
        event.target.value = "";
      }
    },

    // The prep step runs in the background and can take a while; this is a
    // deliberate re-check rather than a poll loop, so opening the editor
    // does not start a timer that outlives it.
    async refreshAvatarPrepStatus() {
      try {
        const updated = await api.get(`/api/characters/${this.draftCharacter.id}`);
        this.draftCharacter.avatar_video = updated.avatar_video;
      } catch (_) { /* the character may have been deleted meanwhile */ }
    },

    avatarPrepLabel() {
      const status = (this.draftCharacter.avatar_video || {}).prep_status;
      return {
        pending: "Preparing…", ready: "Ready", failed: "Preparation failed",
      }[status] || "";
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
          id: "attach", label: "Attach a file", icon: "#i-attach",
          note: "An image, or a text file to read",
          run: () => this.pickAttachment(),
        },
        {
          id: "music", label: "Music", icon: "#i-music",
          note: this.music.status === "playing" ? "Playing now — change or add a track" : "Play something from the library",
          disabled: !this.chatId,
          run: () => this.openPanel("music"),
        },
      ];
    },

    runComposerAction(action) {
      this.composerMenu = false;
      if (action.soon) return this.flashHint(`${action.label} is not built yet`);
      if (action.run) action.run();
    },

    // ---- composer's + button: tap, or press-hold-drag-release ----
    //
    // A tap and a hold start exactly the same way — there is no delay to
    // distinguish them, unlike the message wheel's HOLD_MS — so the press
    // opens the menu immediately on pointerdown, same as the old plain
    // @click did. What pointerdown can't know yet is what the release means:
    // dragged onto an item, it picks that item; released over nothing, it
    // should reproduce whatever @click used to do, which depended on whether
    // the menu was already open. `wasOpen` remembers that so onPlusUp can
    // tell "just opened by this press, leave it up" from "already open,
    // this press is the close tap" apart.
    //
    // Pointer capture means move/up keep arriving even once the finger has
    // left the small + button and is over the sheet — but it also means
    // event.target stops reporting what is really underneath the finger, so
    // composerItemAt hit-tests by rectangle instead, the same way the wheel's
    // wheelIndexAt does for its own gesture.
    onPlusDown(event) {
      if (!event.isPrimary || this.nobodyYet) return;
      this.plusHold = {
        pointerId: event.pointerId,
        wasOpen: this.composerMenu,
      };
      try { event.currentTarget.setPointerCapture(event.pointerId); } catch (_) { /* mouse */ }
      this.composerMenu = true;
      this.composerActive = -1;
      // See dismissComposerSheet: the backdrop it just opened now sits under
      // this same finger, and this timestamp is what stops that from
      // reading as a tap on it.
      this._composerOpenedAt = performance.now();
    },

    onPlusMove(event) {
      const hold = this.plusHold;
      if (!hold || event.pointerId !== hold.pointerId) return;
      this.composerActive = this.composerItemAt(event.clientX, event.clientY);
    },

    onPlusUp(event) {
      const hold = this.plusHold;
      this.plusHold = null;
      if (!hold || event.pointerId !== hold.pointerId) return;
      const index = this.composerItemAt(event.clientX, event.clientY);
      this.composerActive = -1;
      if (index >= 0) {
        return this.runComposerAction(this.composerActions()[index]);
      }
      // Released over nothing: a plain tap that opened the menu leaves it
      // open for a separate tap on an item, same as before this gesture
      // existed; a tap that found the menu already open is the "tap again to
      // close" one, and closing it is the whole reason @click could do this
      // in one handler where this needs two.
      if (hold.wasOpen) this.composerMenu = false;
    },

    onPlusCancel() {
      this.plusHold = null;
      this.composerActive = -1;
    },

    // The backdrop's own "tap outside closes it" handler — not a bare
    // `composerMenu = false` on the element, because the same press that
    // opens the sheet (onPlusDown, above) ends with the finger sitting on
    // top of that now-visible backdrop. Some engines dispatch the
    // compatibility `click` that follows a pointerup to whatever is under
    // the pointer rather than to the button that actually captured the
    // gesture, which read as the menu opening and immediately closing again
    // on the very tap meant to open it. A tap can't physically land here
    // less than a frame or two after the press that raised it, so anything
    // this soon after _composerOpenedAt is that stray click, not a real
    // "dismiss" tap, and is ignored rather than undoing the press it rode in on.
    dismissComposerSheet() {
      if (performance.now() - (this._composerOpenedAt || 0) < 250) return;
      this.composerMenu = false;
    },

    // Which composer-item the given point is over, skipping disabled ones —
    // a drag that ends on a disabled row picks nothing, same as a tap on one
    // already does via the button's own :disabled.
    composerItemAt(x, y) {
      const items = document.querySelectorAll(".composer-item");
      for (let i = 0; i < items.length; i += 1) {
        const el = items[i];
        if (el.disabled) continue;
        const r = el.getBoundingClientRect();
        if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return i;
      }
      return -1;
    },

    // ---- group chats (roadmap 8) ----

    async loadCast() {
      if (!this.chatId) return;
      try {
        const body = await api.get(`/api/chats/${this.chatId}/members`);
        this.cast = body.members;
        this.policies = body.policies;
        this.policy = body.policy;
        // A choice made for a room that has since changed is not a choice.
        if (!this.cast.some((m) => m.character_id === this.nextSpeaker)) {
          this.nextSpeaker = "";
        }
      } catch (e) {
        this.error = errorText(e);
      }
    },

    speakerName(message) {
      const who = this.cast.find((m) => m.character_id === message.speaker_id);
      return who ? who.name : "";
    },

    charactersNotHere() {
      const here = new Set(this.cast.map((m) => m.character_id));
      return this.characters.filter((c) => !here.has(c.id));
    },

    policyNote() {
      return (this.policies.find((p) => p.id === this.policy) || {}).note || "";
    },

    async addMember(characterId) {
      if (!characterId) return;
      try {
        const body = await api.post(`/api/chats/${this.chatId}/members`,
                                    { character_id: characterId });
        this.cast = body.members;
        const added = this.cast.find((m) => m.character_id === characterId);
        this.flashHint(added ? `${added.name} joined` : "Joined");
      } catch (e) {
        this.flashHint(errorText(e));
      }
    },

    async dropMember(member) {
      try {
        const body = await api.del(`/api/chats/${this.chatId}/members/${member.character_id}`);
        this.cast = body.members;
        this.flashHint(`${member.name} left`);
      } catch (e) {
        // The server refuses to empty a chat, and its reason is the useful
        // one to show — "someone has to be here, mute them instead".
        this.flashHint(errorText(e));
      }
    },

    async toggleMuted(member) {
      member.muted = !member.muted;
      if (member.muted && this.nextSpeaker === member.character_id) this.nextSpeaker = "";
      await this.patchMember(member, { muted: member.muted });
      this.flashHint(member.muted ? `${member.name} is silent` : `${member.name} can speak`);
    },

    setTalkativeness(member, raw) {
      const value = parseFloat(raw);
      if (Number.isNaN(value)) return;
      member.talkativeness = value;
      clearTimeout(this._memberTimer);
      this._memberTimer = setTimeout(
        () => this.patchMember(member, { talkativeness: value }), PREVIEW_DEBOUNCE_MS,
      );
    },

    async patchMember(member, body) {
      try {
        await api.patch(`/api/chats/${this.chatId}/members/${member.character_id}`, body);
      } catch (e) {
        this.error = errorText(e);
      }
    },

    async setPolicy(policy) {
      const previous = this.policy;
      this.policy = policy;
      try {
        await api.put(`/api/chats/${this.chatId}/policy`, { policy });
      } catch (e) {
        this.policy = previous;
        this.flashHint(errorText(e));
      }
    },

    // How often the world intrudes. It lives on the pass's own trigger rather
    // than in settings, so there is one number rather than a setting and a
    // trigger that have to agree. Read straight from this.passes wherever
    // it's set from (openPanel('brain') is the only caller today) rather
    // than a dedicated loader — the list is one fetch either way, and this
    // used to be a second, redundant one.

    setEventChance(raw) {
      const value = parseFloat(raw);
      if (Number.isNaN(value)) return;
      this.eventChance = value;
      clearTimeout(this._eventTimer);
      this._eventTimer = setTimeout(async () => {
        try {
          const passes = await api.get("/api/passes");
          const found = passes.find((p) => p.id === "random_event");
          if (!found) return;
          found.trigger = { ...found.trigger, type: "chance", probability: value };
          await api.put(`/api/passes/${found.id}`, found);
        } catch (e) {
          this.error = errorText(e);
        }
      }, PREVIEW_DEBOUNCE_MS);
    },

    // ---- attachments (§19) ----

    pickAttachment() {
      // The hidden input lives in the composer; clicking it is the only way to
      // open a file picker from script.
      const input = document.querySelector("#attach-input");
      if (input) input.click();
    },

    async attachFiles(event) {
      const files = [...(event.target.files || [])];
      event.target.value = "";
      for (const file of files) {
        // One at a time: the failure of a second file should not discard the
        // first, and the chips are meant to appear as they land.
        await this.stageOne(file);
      }
    },

    async stageOne(file) {
      const pending = {
        id: `pending-${Math.random().toString(36).slice(2, 9)}`,
        name: file.name, kind: file.type.startsWith("image/") ? "image" : "text",
        uploading: true,
      };
      this.staged.push(pending);
      try {
        const response = await fetch(
          `/api/attachments?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        if (!response.ok) throw await apiError(response);
        const stored = await response.json();
        // Replace in place so the chip does not jump.
        this.staged.splice(this.staged.indexOf(pending), 1, stored);
      } catch (e) {
        this.staged.splice(this.staged.indexOf(pending), 1);
        this.flashHint(errorText(e));
      }
    },

    async unstage(item) {
      this.staged.splice(this.staged.indexOf(item), 1);
      // Not just dropped from the list: the file is already on disk, and a
      // chip removed without this would leave it there until the hour is up.
      if (!item.uploading) {
        try {
          await fetch(`/api/attachments/${item.id}`, { method: "DELETE" });
        } catch { /* the sweeper will get it */ }
      }
    },

    stagedIds() {
      return this.staged.filter((s) => !s.uploading).map((s) => s.id);
    },

    attachmentUrl(item) {
      return `/api/attachments/${item.id}/file`;
    },

    sizeLabel(bytes) {
      if (!bytes) return "";
      // Bytes below a kilobyte, because rounding a small file to "0 KB" reads
      // as the upload having failed.
      if (bytes < 1024) return `${bytes} B`;
      return bytes < 1024 * 1024
        ? `${Math.round(bytes / 1024)} KB`
        : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
        this.error = errorText(e);
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
        this.bgMsg = errorText(e);
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
        this.bgMsg = errorText(e);
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
        // The server already drops its own copy of this image's meta on
        // delete (§ remove_background, main.py); this is just the same
        // cleanup on the in-memory settings a Save would otherwise send
        // straight back — harmless either way since the server re-filters
        // by what still exists, but no reason to carry it around.
        const meta = { ...(this.settings.background_meta || {}) };
        delete meta[backdrop.name];
        this.settings.background_meta = meta;
        // The server resets the setting if the deleted image was in use; take
        // its word for what the backdrop is now rather than assuming.
        this.setBackground(result.background);
        this.bgMsg = `Removed ${backdrop.name}`;
      } catch (e) {
        this.bgMsg = errorText(e);
      }
    },

    // Per-image metadata for the shared backdrop library (§ Settings.
    // background_meta, config.py) — what background_swap reads to pick one
    // automatically. Live-edited here like every other Theme-panel setting
    // (immutable replace, not a deep mutation, same as setTheme below) and
    // only actually persisted on the panel's own Save.
    bgMeta(name) {
      return (this.settings.background_meta || {})[name] || {};
    },
    bgLabel(name) {
      return this.bgMeta(name).label || name.replace(/\.[^.]+$/, "");
    },
    setBgMeta(name, key, value) {
      const all = { ...(this.settings.background_meta || {}) };
      all[name] = { ...(all[name] || {}), [key]: value };
      this.settings.background_meta = all;
    },

    // ---- music (ROADMAP #39) ----
    //
    // Same shape as the backdrop trio above: load/upload/delete against the
    // shared library, plus a live meta editor. What's different is the
    // per-chat playback state (this.music) and the three ways it changes —
    // the person's own pick, answering a proposed card, and the <audio>
    // element itself reporting a track ended — which all write-then-listen
    // for the "panel"/"music" SSE echo (§ handleEvent) rather than mutating
    // this.music optimistically, the same pattern saveChatName/deleteChat
    // already use elsewhere.

    async loadMusicLibrary() {
      try {
        this.musicLibrary = (await api.get("/api/music")).tracks;
      } catch (e) {
        this.musicMsg = errorText(e);
      }
    },

    // Posted as a raw body, same reasoning as uploadBackdrop above.
    async uploadMusicTrack(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.uploadingMusic = true;
      this.musicMsg = "";
      try {
        const response = await fetch(
          `/api/music?filename=${encodeURIComponent(file.name)}`,
          { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file },
        );
        if (!response.ok) throw await apiError(response);
        const added = await response.json();
        await this.loadMusicLibrary();
        this.musicMsg = `Added ${added.name}`;
      } catch (e) {
        this.musicMsg = errorText(e);
      } finally {
        this.uploadingMusic = false;
        event.target.value = "";   // so the same file can be picked again
      }
    },

    async deleteMusicTrack(track) {
      if (this.confirmMusic !== track.name) {
        this.confirmMusic = track.name;
        clearTimeout(this._musicTimer);
        this._musicTimer = setTimeout(() => { this.confirmMusic = ""; }, CONFIRM_MS);
        return;
      }
      this.confirmMusic = "";
      try {
        await api.del(`/api/music/${encodeURIComponent(track.name)}`);
        await this.loadMusicLibrary();
        // The server already drops its own copy of this track's meta on
        // delete (§ remove_music, main.py) — same harmless local mirror as
        // deleteBackdrop's own meta cleanup above.
        const meta = { ...(this.settings.music_meta || {}) };
        delete meta[track.name];
        this.settings.music_meta = meta;
        this.musicMsg = `Removed ${track.name}`;
      } catch (e) {
        this.musicMsg = errorText(e);
      }
    },

    musicMeta(name) {
      return (this.settings.music_meta || {})[name] || {};
    },
    setMusicMeta(name, key, value) {
      const all = { ...(this.settings.music_meta || {}) };
      all[name] = { ...(all[name] || {}), [key]: value };
      this.settings.music_meta = all;
    },

    // What the player/card shows for a track — its description if one was
    // written, the bare filename otherwise. Same fallback as pending_music
    // on the server (§ assembly.py), so the two never disagree.
    musicLabel(name) {
      if (!name) return "";
      // The title, not the description (§ Settings.music_meta, config.py) —
      // same split as bgLabel/bgMeta above. Extension stripped in the
      // fallback for the same reason bgLabel strips it: nobody asking
      // permission to play something needs to read its file format.
      return this.musicMeta(name).label || name.replace(/\.[^.]+$/, "");
    },

    // The person's own pick — no card, no permission needed.
    async pickMusic(name) {
      if (!this.chatId) return;
      try {
        await api.post(`/api/chats/${this.chatId}/music`, { track: name });
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // Answers a pending action_card (§ music_select, registry.py).
    async musicRespond(choice) {
      if (!this.chatId) return;
      try {
        await api.post(`/api/chats/${this.chatId}/music/respond`, { choice });
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // The <audio> element's own `ended` event.
    async reportMusicEnded() {
      if (!this.chatId) return;
      try {
        await api.post(`/api/chats/${this.chatId}/music/ended`, {});
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // ---- theme presets ----

    // Whole palettes rather than one colour at a time — and *whole* means the
    // markup colours as well. They were left out of everything but Night, so
    // the amber preset drew its dialogue in the rose palette's pink: the three
    // colours a reader actually looks at were the three the preset did not
    // touch. Every palette states all of them now.
    themePresets() {
      return [
        {
          id: "rose", label: "Rose",
          swatch: "background: linear-gradient(135deg,#fdf7f9,#c2617f)",
          theme: {},   // the stylesheet's own defaults, which are the full set
        },
        {
          id: "slate", label: "Slate",
          swatch: "background: linear-gradient(135deg,#f4f6f8,#4c6b8a)",
          theme: {
            "--bg": "#f5f7f9", "--panel": "#ffffff", "--panel-2": "#eceff3",
            "--line": "#dbe1e8", "--text": "#2b3138", "--muted": "#78828d",
            "--accent": "#4c6b8a", "--user-bubble": "#e9eef4", "--ai-bubble": "#ffffff",
            "--c-default": "#2f353c", "--c-dialogue": "#2f5f8a", "--c-action": "#2f6f6a",
            "--c-strong": "#7a5a2a",
          },
        },
        {
          id: "amber", label: "Amber",
          swatch: "background: linear-gradient(135deg,#fdf8f0,#b1762a)",
          theme: {
            "--bg": "#fdf8f0", "--panel": "#fffdf9", "--panel-2": "#f6ecdc",
            "--line": "#ecdcc2", "--text": "#38301f", "--muted": "#8a7c64",
            "--accent": "#b1762a", "--user-bubble": "#f7ecd9", "--ai-bubble": "#fffdf9",
            "--c-default": "#3a3222", "--c-dialogue": "#96541c", "--c-action": "#5d6630",
            "--c-strong": "#9c3d24",
          },
        },
        {
          id: "moss", label: "Moss",
          swatch: "background: linear-gradient(135deg,#f5f8f3,#4a7a55)",
          theme: {
            "--bg": "#f4f8f3", "--panel": "#ffffff", "--panel-2": "#e9f0e8",
            "--line": "#d5e2d3", "--text": "#26302a", "--muted": "#6f7d72",
            "--accent": "#4a7a55", "--user-bubble": "#e6f0e6", "--ai-bubble": "#ffffff",
            "--c-default": "#2a352d", "--c-dialogue": "#2f6b4f", "--c-action": "#5e5a8a",
            "--c-strong": "#8a6520",
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
            "--band-prefix": "#c2547d", "--band-middle": "#8877d9", "--band-volatile": "#bd8129",
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
        await this.loadCharacters();
        this.importMsg = `Imported ${added.name}`;
      } catch (e) {
        this.importError = errorText(e);
      } finally {
        this.importing = false;
        event.target.value = "";
      }
    },

    // The star flips right where it is — the row does not jump to the top
    // (or back down) mid-browse, which used to reorder the list out from
    // under whatever else you were about to tap. Starred still sorts to the
    // top, same as ever; it just takes effect the next time the roster is
    // loaded (§ loadCharacters), the same way a rename or a new chat count
    // does not resort the list you are currently looking at either.
    async toggleFavourite(character) {
      const wanted = !character.favourite;
      try {
        await api.post(`/api/characters/${character.id}/favourite`, { favourite: wanted });
      } catch (e) {
        this.error = errorText(e);
        return;
      }
      character.favourite = wanted;
      // The character's own reaction where there is one and the feature is
      // on; the plain toast otherwise — either there isn't a line yet
      // (§ character_reactions.py — a card can still be waiting on its
      // first generation), or reactions are switched off in Settings, which
      // hides this the same way it stops one from ever being written.
      const line = this.settings.feature_character_reactions
        && (character.reactions || {})[wanted ? "starred" : "unstarred"];
      if (line) this.showReactionBubble(character.id, line);
      else this.flashHint(wanted ? `${character.name} starred` : `${character.name} unstarred`);
    },

    // A speech bubble over the character's own row, in their own words,
    // rather than the generic toast every other action here uses. Replaces
    // whatever bubble was already showing instead of queuing behind it —
    // the same "only the latest matters" reasoning as arm() below.
    showReactionBubble(characterId, text) {
      this.reactionBubble = { id: characterId, text };
      this.reactionBubbleOpen = true;
      clearTimeout(this._reactionBubbleTimer);
      this._reactionBubbleTimer = setTimeout(() => {
        this.reactionBubbleOpen = false;
      }, REACTION_BUBBLE_MS);
    },

    // One card in or out of the vault. Reachable on a still-visible row
    // either way (§ index.html's x-show="settings.vault_configured" on the
    // button) — vaulting a card that's showing needs nothing from the
    // server beyond the flag flip, un-vaulting one only ever happens on a
    // row that's showing because the vault is already open. The one case
    // that needs local bookkeeping: vaulting a card while the vault
    // happens to be *closed* hides it immediately, and the server won't
    // send it back on the next roster load — so it's dropped from
    // `characters` right here instead of waiting for one.
    async toggleVaulted(character) {
      const wanted = !character.vaulted;
      try {
        await api.post(`/api/characters/${character.id}/vault`, { vaulted: wanted });
      } catch (e) {
        this.error = errorText(e);
        return;
      }
      const nowHidden = wanted && this.settings.vault_configured && !this.settings.vault_unlocked;
      if (nowHidden) {
        this.characters = this.characters.filter((c) => c.id !== character.id);
      } else {
        character.vaulted = wanted;
      }
      this.flashHint(wanted ? `${character.name} locked` : `${character.name} unlocked`);
    },

    // The header's single vault button: what tapping it does depends on
    // which of the three states the vault is already in, same idea as
    // toggleFavourite reading the current flag before deciding. Locking
    // needs no PIN — closing a safe never does — so that branch acts
    // immediately instead of opening the keypad.
    openVaultHeaderAction() {
      if (!this.settings.vault_configured) return this.openVaultModal("setup");
      if (!this.settings.vault_unlocked) return this.openVaultModal("unlock");
      this.lockVault();
    },

    openVaultSettings() {
      this.vaultSettingsOpen = true;
    },

    openVaultModal(mode) {
      this.vaultModalMode = mode;
      this.vaultPinDigits = "";
      this.vaultPinFirst = "";
      this.vaultChangeCurrentPin = "";
      this.vaultChangeNewPin = "";
      this.vaultError = "";
      this.vaultModalOpen = true;
    },

    closeVaultModal() {
      this.vaultModalOpen = false;
      this.vaultPinDigits = "";
      this.vaultPinFirst = "";
      this.vaultChangeCurrentPin = "";
      this.vaultChangeNewPin = "";
      this.vaultError = "";
    },

    vaultModalTitle() {
      return {
        setup: "Set a lock PIN",
        "setup-confirm": "Confirm the PIN",
        unlock: "Enter the lock PIN",
        "change-current": "Enter the current PIN",
        "change-new": "Choose a new PIN",
        "change-confirm": "Confirm the new PIN",
        remove: "Enter the PIN to turn off Locked",
      }[this.vaultModalMode] || "";
    },

    vaultKeyTap(digit) {
      if (this.vaultBusy || this.vaultPinDigits.length >= 6) return;
      this.vaultError = "";
      this.vaultPinDigits += String(digit);
      if (this.vaultPinDigits.length === 6) this.vaultPinComplete();
    },

    vaultBackspace() {
      this.vaultPinDigits = this.vaultPinDigits.slice(0, -1);
    },

    vaultMismatch(backTo) {
      this.vaultError = "Didn't match — try again";
      this.vaultPinDigits = "";
      this.vaultPinFirst = "";
      this.triggerVaultShake();
      this.vaultModalMode = backTo;
    },

    triggerVaultShake() {
      this.vaultShake = true;
      setTimeout(() => { this.vaultShake = false; }, 420);
    },

    // The one handler for all six modes (§ vaultModalMode) — each mode either
    // moves on to the next (first entry of a pair that needs confirming, or
    // the current-PIN step of change/remove) or calls the server and closes.
    async vaultPinComplete() {
      const pin = this.vaultPinDigits;
      const mode = this.vaultModalMode;
      if (mode === "setup") {
        this.vaultPinFirst = pin;
        this.vaultPinDigits = "";
        this.vaultModalMode = "setup-confirm";
        return;
      }
      if (mode === "setup-confirm") {
        if (pin !== this.vaultPinFirst) return this.vaultMismatch("setup");
        return this.vaultSubmit("/api/vault/setup", { pin }, (r) => {
          this.settings.vault_configured = r.vault_configured;
          this.settings.vault_unlocked = r.vault_unlocked;
        }, true);
      }
      if (mode === "unlock") {
        return this.vaultSubmit("/api/vault/unlock", { pin }, (r) => {
          this.settings.vault_unlocked = r.vault_unlocked;
        }, true);
      }
      if (mode === "change-current") {
        this.vaultChangeCurrentPin = pin;
        this.vaultPinDigits = "";
        this.vaultModalMode = "change-new";
        return;
      }
      if (mode === "change-new") {
        this.vaultChangeNewPin = pin;
        this.vaultPinDigits = "";
        this.vaultModalMode = "change-confirm";
        return;
      }
      if (mode === "change-confirm") {
        if (pin !== this.vaultChangeNewPin) return this.vaultMismatch("change-new");
        return this.vaultSubmit("/api/vault/change", {
          current_pin: this.vaultChangeCurrentPin, new_pin: this.vaultChangeNewPin,
        }, () => this.flashHint("PIN changed"));
      }
      if (mode === "remove") {
        return this.vaultSubmit("/api/vault/remove", { current_pin: pin }, () => {
          this.settings.vault_configured = false;
          this.settings.vault_unlocked = false;
          this.flashHint("Locked turned off");
        }, true);
      }
    },

    async vaultSubmit(path, body, onOk, refreshRoster) {
      this.vaultBusy = true;
      try {
        const r = await api.post(path, body);
        onOk(r);
        this.closeVaultModal();
        // The roster and the chat list are both filtered server-side by the
        // same lock (§ _vault_locked, app/main.py) — refreshing only the
        // roster left `this.chats` holding whatever it had at the last
        // fetch, so a chat belonging to a card unlocked just now still read
        // as "No chats yet" until something else happened to reload it.
        if (refreshRoster) {
          await Promise.all([this.loadCharacters(), this.reloadChats()]);
        }
      } catch (e) {
        this.vaultError = errorText(e);
        this.vaultPinDigits = "";
        this.triggerVaultShake();
      } finally {
        this.vaultBusy = false;
      }
    },

    async lockVault() {
      try {
        await api.post("/api/vault/lock", {});
      } catch (e) {
        this.error = errorText(e);
        return;
      }
      this.settings.vault_unlocked = false;
      await Promise.all([this.loadCharacters(), this.reloadChats()]);
    },

    // The roster's one true order: whoever is open right now first — see
    // pinCurrentCharacter and the note on .char.current in styles.css —
    // then starred, then alphabetical. Every place that touches the order
    // of `characters` (a fresh fetch, a star toggle, switching chats) reads
    // this same comparator, so they can never disagree about where a row
    // belongs.
    compareCharacters(a, b) {
      const aCurrent = a.id === this.characterId, bCurrent = b.id === this.characterId;
      if (aCurrent !== bCurrent) return aCurrent ? -1 : 1;
      if (!!a.favourite !== !!b.favourite) return a.favourite ? -1 : 1;
      return a.name.localeCompare(b.name);
    },

    // The one place `characters` is fetched from the server. The backend's
    // own ORDER BY (favourite DESC, name) does not know which character is
    // open — that is client state — so this re-sorts with compareCharacters
    // on every load rather than trusting the wire order.
    async loadCharacters() {
      this.characters = (await api.get("/api/characters")).sort((a, b) => this.compareCharacters(a, b));
      this.loadCardBudget();
    },

    async reloadChats() {
      this.chats = await api.get("/api/chats");
    },

    // Fire-and-forget, deliberately not awaited by loadCharacters: it needs
    // a live round trip to whichever backend the Messages tier points at
    // (§ /api/characters/budget), and the roster itself has nothing to do
    // with that answer beyond drawing a badge once it lands. A failure here
    // — no backend configured, one that cannot be reached — just means no
    // badges this load, not a roster that refuses to appear.
    async loadCardBudget() {
      try {
        this.cardBudget = await api.get("/api/characters/budget");
      } catch (_) {
        this.cardBudget = {};
      }
    },

    // Whether c's own card content leaves too little room for real
    // conversation under the Messages backend as currently configured
    // (§ assembly.card_too_big). Unknown (no answer yet, or the check
    // failed) reads as false — a badge that cannot back up its own claim is
    // worse than one that is occasionally a turn late to appear.
    cardTooBig(c) {
      return !!(this.cardBudget[c.id] && this.cardBudget[c.id].too_big);
    },

    cardBudgetTitle(c) {
      const info = this.cardBudget[c.id];
      if (!info) return "";
      const room = Math.max(0, info.headroom);
      return `This card's own description, scenario and writing rules use most of the current `
        + `prompt budget — as little as ~${room} tokens may be left for the conversation itself `
        + `on your current backend. Move to a bigger tier, or compress the card, from its editor.`;
    },

    // Whether a lorebook entry on c looks like it may describe someone other
    // than this character while still writing {{char}} for itself
    // (§ lorebook.possible_misattributions, ISSUES-TRIAGE.md #6). A
    // heuristic warning, not a correction — the fix, if there is one, is
    // editing the card's own lorebook, which this app does not do.
    cardMisattributed(c) {
      const info = this.cardBudget[c.id];
      return !!(info && info.misattributions && info.misattributions.length);
    },

    cardMisattributionTitle(c) {
      const keys = ((this.cardBudget[c.id] || {}).misattributions || [])
        .map((m) => m.keys.join("/")).join(", ");
      if (!keys) return "";
      return `A lorebook entry keyed on "${keys}" writes {{char}} throughout without ever `
        + `naming them — worth checking it's actually about ${c.name} and not someone else `
        + `the card describes the same way.`;
    },

    // Moves whichever character is now open to the top of an already-loaded
    // roster, the same animated way toggleFavourite moves a newly-starred
    // row (see flipCharacters) — called from openChat, where characterId
    // changes without characters itself being refetched.
    pinCurrentCharacter() {
      if (!this.characters.length) return;
      this.flipCharacters(() => {
        this.characters = [...this.characters].sort((a, b) => this.compareCharacters(a, b));
      });
    },

    flipCharacters(mutate) {
      const before = new Map(
        [...document.querySelectorAll(".char")].map((r) => [
          r.dataset.cid, r.getBoundingClientRect().top,
        ]),
      );
      let done = false;
      const applyFlip = () => {
        if (done) return;
        done = true;
        for (const row of document.querySelectorAll(".char")) {
          const was = before.get(row.dataset.cid);
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
      };
      // Not $nextTick: this vendored Alpine resolves it through a bare
      // setTimeout, which yields to the browser's paint first — so by the
      // time the callback ran, the row had already been rendered sitting at
      // its new spot with no compensating transform on it yet, one or two
      // real frames before this code got a chance to jump it back. That
      // painted frame is exactly the row disappearing from its old spot and
      // reappearing, already settled, at the new one. A MutationObserver's
      // callback is a microtask, which the spec guarantees runs before the
      // next paint no matter when Alpine actually moves the rows, so it
      // catches the reorder in the same tick it happens in, every time.
      const roster = document.querySelector(".char-roster");
      if (roster) {
        const observer = new MutationObserver(() => {
          observer.disconnect();
          applyFlip();
        });
        observer.observe(roster, { childList: true });
        // A re-sort that leaves everyone's rank exactly where it was moves
        // nothing, so the observer has nothing to catch — fall back once
        // Alpine's own tick confirms there was nothing more coming.
        this.$nextTick(() => { observer.disconnect(); applyFlip(); });
      }
      mutate();
    },

    // ---- chat management (§10) ----

    async importChat(event) {
      const file = (event.target.files || [])[0];
      if (!file) return;
      this.importingChat = true;
      this.importMsg = "";
      this.importError = "";
      try {
        const response = await fetch("/api/chats/import", { method: "POST", body: file });
        if (!response.ok) throw await apiError(response);
        const body = await response.json();
        this.chats = await api.get("/api/chats");
        await this.loadCharacters();
        // Open the history of whoever it belongs to, so the imported chat is
        // visible rather than merely reported.
        this.historyFor = body.chat.character_id;
        this.importMsg = `Imported "${body.chat.title || "untitled"}"`;
      } catch (e) {
        this.importError = errorText(e);
      } finally {
        this.importingChat = false;
        event.target.value = "";
      }
    },

    startRenameChat(chat, button) {
      const row = button.closest("li");
      this.renamingChat = chat.id;
      // $nextTick alone is not enough: Alpine has set renamingChat by then, but
      // x-show has not finished taking display:none off the input, and focusing
      // a hidden element is a silent no-op. One frame later it is really there.
      this.$nextTick(() => requestAnimationFrame(() => {
        const field = row.querySelector(".chat-rename");
        if (field) { field.focus(); field.select(); }
      }));
    },

    async saveChatName(chat, title) {
      if (this.renamingChat !== chat.id) return;  // blur after enter
      this.renamingChat = "";
      const wanted = String(title || "").trim();
      if (wanted === (chat.title || "")) return;
      chat.title = wanted;
      try {
        await api.patch(`/api/chats/${chat.id}`, { title: wanted });
        this.flashHint(wanted ? `Renamed to "${wanted}"` : "Name cleared");
      } catch (e) {
        this.error = errorText(e);
      }
    },

    queueChatSearch() {
      clearTimeout(this._chatSearchTimer);
      const query = this.chatQuery.trim();
      if (!query) {
        this.chatHits = [];
        this.searching = false;
        return;
      }
      this.searching = true;
      this._chatSearchTimer = setTimeout(() => this.runChatSearch(), PREVIEW_DEBOUNCE_MS);
    },

    async runChatSearch() {
      const query = this.chatQuery.trim();
      if (!query) return;
      try {
        const hits = await api.get(`/api/chats/search?q=${encodeURIComponent(query)}`);
        // Only if the box still says what this search was for: replies can
        // land out of order, and the older one arriving last would leave the
        // list showing results for a prefix of what was typed.
        if (this.chatQuery.trim() === query) this.chatHits = hits;
      } catch (e) {
        this.error = errorText(e);
      } finally {
        if (this.chatQuery.trim() === query) this.searching = false;
      }
    },

    // The roster's export link (§ index.html) — PNG when there is more
    // than a neutral portrait to carry (§ has_expressions, repo.py), since
    // only a PNG export actually bundles the extra portraits' own bytes
    // (§ export_character_png, main.py); plain JSON otherwise, same as
    // always for a character with nothing more to lose.
    characterExportUrl(c) {
      return c.has_expressions
        ? `/api/characters/${c.id}/export.png?download=true`
        : `/api/characters/${c.id}/export?download=true`;
    },
    characterExportName(c) {
      return `${c.name}.card.${c.has_expressions ? "png" : "json"}`;
    },

    async newCharacter() {
      try {
        const created = await api.post("/api/characters", { name: "New character" });
        await this.loadCharacters();
        await this.editCharacter(created.id);
      } catch (e) {
        this.error = errorText(e);
      }
    },

    async editCharacter(characterId) {
      this.charMsg = "";
      this.charError = "";
      try {
        this.draftCharacter = await api.get(`/api/characters/${characterId}`);
        this.altGreetings = (this.draftCharacter.alternate_greetings || []).join("\n\n");
        this.stopStrings = (this.draftCharacter.stop_strings || []).join("\n");
        this.pfpEffectOpen = false;
        this.pfpEffectGrown = false;
        this.hueEditorOpen = false;
        this.advancedCharOpen = false;
        this.reactionsOpen = false;
        this.reactionsError = "";
        this.memoriesOpen = false;
        this.newMemoryText = "";
        this.memoryError = "";
        this.panel = "character";
        // Usually reached from the chats panel, where the sheet is already up
        // — but not from the first-run empty state, which is on the chat screen
        // with nothing open. Setting the panel without opening it left that
        // path creating a character and then appearing to do nothing.
        this.panelOpen = true;
        this.snapshotCharacter();
        // Not awaited: the editor should paint immediately, and a moment's
        // delay on the "Edit memories" button's own count is a fair trade
        // for that — same reasoning as the samplers/passes fetches Brain
        // kicks off without blocking its own open.
        this.loadCharacterMemories();
      } catch (e) {
        this.error = errorText(e);
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
          pfp_set: draft.pfp_set || {},
          expression_meta: draft.expression_meta || {},
          pfp_shape: draft.pfp_shape || "portrait",
          pfp_effect: draft.pfp_effect || {},
          reactions: draft.reactions || {},
          memory_enabled: draft.memory_enabled !== false,
          avatar_video: {
            enabled: !!(draft.avatar_video || {}).enabled,
            voice: (draft.avatar_video || {}).voice || "",
          },
        });
        await this.loadCharacters();
        // The open chat holds its own copy of the card, and the header reads
        // the name off that — without this the title keeps the old name until
        // the chat is reopened.
        if (this.character && this.character.id === saved.id) this.character = saved;
        this.snapshotCharacter();
        this.charMsg = "saved";
      } catch (e) {
        this.charError = errorText(e);
      } finally {
        this.savingCharacter = false;
      }
    },

    // First tap arms, same as every other delete in the app. The second tap
    // used to delete outright; deleting a character takes its chats with it,
    // which is more than any other armed action here does, so the second tap
    // now opens the hold-to-confirm modal instead — see openKillModal below.
    //
    // It still disarms after CONFIRM_MS if the second tap never comes, like
    // every other armed row.
    deleteCharacter(character) {
      if (this.confirmChar !== character.id) {
        this.arm("confirmChar", character.id);
        return;
      }
      this.confirmChar = "";
      this.openKillModal(character);
    },

    // ---- character deletion: hold to confirm ----
    //
    // A modal over a sheet is its own problem on a phone (see deleteChat
    // below, which stays two-tap for exactly that reason) — but a character
    // takes every one of its chats with it, unrecoverably, and
    // that is worth the one exception. A timed hold rather than a third tap:
    // a tap is one instant, indistinguishable from the two that armed it, and
    // the whole point is a gesture that cannot happen by accident.
    openKillModal(character) {
      this.killHold = { character, state: "idle", previewShown: false };
      buzz(14);
    },

    closeKillModal() {
      if (!this.killHold) return;
      cancelAnimationFrame(this._killRaf);
      this._killRaf = null;
      this.killHold = null;
    },

    onKillDown(event) {
      const hold = this.killHold;
      if (!event.isPrimary || !hold || hold.state === "deleting") return;
      try { event.currentTarget.setPointerCapture(event.pointerId); } catch (_) { /* mouse */ }
      hold.state = "holding";
      buzz(8);
      const startedAt = performance.now();
      cancelAnimationFrame(this._killRaf);
      // The fill is written straight to the element every frame, the same as
      // the message wheel's magnetise() — seven seconds of Alpine re-render on
      // every frame is exactly the kind of work that costs frames on a phone,
      // and nothing here needs to be reactive on every frame except the
      // three state names. previewShown is the one exception: a single
      // reactive flip partway through, not a per-frame write, so it costs
      // nothing like the fill would.
      const tick = (now) => {
        if (!this.killHold || this.killHold.state !== "holding") return;
        const progress = Math.min(1, (now - startedAt) / KILL_HOLD_MS);
        if (this.$refs.killFill) this.$refs.killFill.style.transform = `scaleX(${progress})`;
        // The character's own goodbye — the same line star/unstar shows,
        // from §models.CharacterReactions — surfacing partway through the
        // hold rather than only after it finishes: by the time the fill
        // bar is this close to done, the character has as good as noticed.
        // Held past 60% rather than the moment the hold starts, so a stray
        // brush of the button doesn't raise it for a press going nowhere.
        if (!this.killHold.previewShown && progress >= 0.6) this.killHold.previewShown = true;
        if (progress >= 1) { this.finishKill(); return; }
        this._killRaf = requestAnimationFrame(tick);
      };
      this._killRaf = requestAnimationFrame(tick);
    },

    onKillUp() { this.releaseKillHold(); },
    onKillCancel() { this.releaseKillHold(); },

    // Letting go before seven seconds resets the fill rather than closing the
    // modal — the modal itself stays up so a second attempt is another hold,
    // not two more taps on the row behind it.
    releaseKillHold() {
      const hold = this.killHold;
      if (!hold || hold.state !== "holding") return;
      cancelAnimationFrame(this._killRaf);
      this._killRaf = null;
      hold.state = "idle";
      hold.previewShown = false;
      if (this.$refs.killFill) this.$refs.killFill.style.transform = "scaleX(0)";
    },

    async finishKill() {
      const hold = this.killHold;
      if (!hold) return;
      cancelAnimationFrame(this._killRaf);
      this._killRaf = null;
      hold.state = "deleting";
      buzz(25);
      const character = hold.character;
      try {
        await api.del(`/api/characters/${character.id}`);
        await this.loadCharacters();
        this.chats = await api.get("/api/chats");
        // Deleting the character behind the open chat takes the chat with it,
        // so the app has to land somewhere real rather than on a dead id.
        if (character.id === this.characterId) await this.fallbackChat();
      } catch (e) {
        this.error = errorText(e);
      }
      this.killHold = null;
    },

    // Two taps rather than a confirm dialog: a modal over a sheet on a phone
    // is its own problem, and the second tap is the same finger in the same
    // place. One chat, not every chat with a character — deleteCharacter is
    // the exception that gets a third gate, not this one.
    async deleteChat(chat) {
      if (this.confirmChat !== chat.id) {
        this.arm("confirmChat", chat.id);
        return;
      }
      this.confirmChat = "";
      try {
        await api.del(`/api/chats/${chat.id}`);
        this.chats = await api.get("/api/chats");
        await this.loadCharacters();
        if (chat.id === this.chatId) await this.fallbackChat();
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // Arm one destructive row, disarming the other and setting the same
    // timeout every other armed action in the app uses. Two rows rather than
    // one shared field because a character row and a chat row can be on screen
    // together, and arming one must visibly cancel the other rather than
    // leaving two buttons that both look ready.
    arm(field, id) {
      buzz(14);
      this.confirmChar = "";
      this.confirmChat = "";
      this[field] = id;
      clearTimeout(this._armTimer);
      this._armTimer = setTimeout(() => {
        this.confirmChar = "";
        this.confirmChat = "";
      }, CONFIRM_MS);
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
      this.applyMotion();
      // Not animated here: applyTheme runs on boot and on every settings
      // reload, and easing into glass each time would flash the interface
      // solid for a frame before frosting it. Only the switch and the slider
      // animate, because those are the moments someone is looking at it.
      this.applyGlass({ animate: false });
    },

    // Frosted glass, as a layer over whatever palette is in force. One slider
    // drives both halves: at nothing it is a barely-there film, at everything
    // the room is clearly visible through the interface. They move together
    // because transparency without blur reads as a rendering fault rather than
    // as glass.
    applyGlass({ animate = true } = {}) {
      const root = document.documentElement;
      const on = !!this.settings.glass;

      // Recomputed inside the closure rather than captured: the "on" path
      // defers this by two frames, and the slider can move in between. The
      // captured version wrote the value the switch was thrown at, undoing
      // whatever the slider had since asked for.
      const write = () => {
        const live = !!this.settings.glass;
        const a = Math.max(0, Math.min(100, Number.isFinite(this.settings.glass_amount)
          ? this.settings.glass_amount : 60)) / 100;
        // The slider runs **frosted to clear**, and the blur runs *down* as
        // the pane opens up. That is the whole shape of the thing: frosted
        // glass is opaque and heavily diffused, clear glass is transparent and
        // sharp. Raising both together — which is what this did at first —
        // gives you a thin sheet of fog, which is neither.
        //
        // Solid is a unitless number, not a percentage: a percentage cannot be
        // subtracted from 1, and doing it anyway silently voids every rule
        // derived from it.
        root.style.setProperty("--glass-solid", live ? (0.78 - a * 0.70).toFixed(3) : "1");
        root.style.setProperty("--glass-blur", live ? `${(26 - a * 25).toFixed(1)}px` : "0px");
        this.applyBackground();
      };

      clearTimeout(this._glassTimer);
      if (!animate) {
        root.classList.toggle("glass", on);
        write();
        return;
      }

      if (on) {
        // The class has to be on the element *before* the values move, or the
        // rules that read them are not applied yet and there is nothing to
        // transition from. One frame of solid-and-unblurred, then it eases in.
        if (!root.classList.contains("glass")) {
          root.classList.add("glass");
          root.style.setProperty("--glass-solid", "1");
          root.style.setProperty("--glass-blur", "0px");
          requestAnimationFrame(() => requestAnimationFrame(write));
        } else {
          write();
        }
      } else {
        // Going the other way, the class has to stay *until* the values have
        // finished moving — dropping it first would take backdrop-filter from
        // a value to none in a single frame, which is the snap this exists to
        // avoid. Same keep-it-mounted-until-it-finishes rule as everywhere.
        write();
        this._glassTimer = setTimeout(
          () => root.classList.remove("glass"), dur("slow", 340) + 40
        );
      }
    },

    setGlass(on) {
      this.settings.glass = !!on;
      this.applyGlass();
    },

    // Dragging the slider must not animate: the value is already tracking the
    // finger, and easing towards a target that moves every frame lags behind
    // it. Only the switch eases.
    setGlassAmount(value) {
      this.settings.glass_amount = parseInt(value, 10);
      this.applyGlass({ animate: false });
    },

    // Named along the axis the slider actually travels, which is not "how
    // much glass" but "what kind" — from a bathroom window to a windowpane.
    get glassLabel() {
      const v = Number.isFinite(this.settings.glass_amount) ? this.settings.glass_amount : 60;
      if (v <= 12) return "frosted";
      if (v <= 35) return "misted";
      if (v <= 60) return "hazy";
      if (v <= 82) return "almost clear";
      return "clear";
    },

    // The motion dial, as a multiplier every duration is written against.
    // `prefers-reduced-motion` is a switch; this is the dial between it and
    // full — someone who finds the interface busy but does not want it dead
    // has nowhere to go otherwise. Durations are cached from the tokens on
    // first read, so the cache is dropped whenever the scale changes.
    applyMotion() {
      const raw = Number.isFinite(this.settings.motion) ? this.settings.motion : 100;
      const scale = Math.max(0, Math.min(100, raw)) / 100;
      document.documentElement.style.setProperty("--motion", String(scale));
      document.documentElement.classList.toggle("motion-off", scale === 0);
      durations.clear();
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

    // Which pairs actually have to be legible. Not every combination — most of
    // these colours never touch — so the check names the ones that do and says
    // nothing about the rest.
    CONTRAST_PAIRS: [
      ["--text", "--bg", "Text on the background"],
      ["--text", "--panel", "Text on the bars"],
      ["--muted", "--bg", "Muted text on the background"],
      ["--c-default", "--panel", "Narration in a bubble"],
      ["--c-dialogue", "--panel", "Dialogue in a bubble"],
      ["--c-action", "--panel", "Action in a bubble"],
    ],

    // Live, against what is in the form rather than what is saved, so it warns
    // while the colour is being chosen rather than after it is committed. Only
    // a warning: this is a personal theme on a personal phone, and a palette
    // that fails a standard but reads fine to the person who made it is their
    // call to make. What it will not do is let the app go unreadable silently.
    get contrastWarnings() {
      const value = (name) => {
        const token = (this.settings.theme_tokens || []).find((t) => t.var === name);
        return (this.settings.theme || {})[name] || (token && token.default) || "";
      };
      const out = [];
      for (const [fg, bg, label] of this.CONTRAST_PAIRS) {
        const a = value(fg);
        const b = value(bg);
        if (!a || !b) continue;
        const light = Math.max(luminance(a), luminance(b));
        const dark = Math.min(luminance(a), luminance(b));
        const ratio = (light + 0.05) / (dark + 0.05);
        // 4.5:1 is the WCAG AA threshold for body text.
        if (ratio < 4.5) out.push({ label, ratio: ratio.toFixed(1) });
      }
      return out;
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
      this.applyBackground();
    },

    // Named rather than a bare percentage: "40%" says nothing about what it
    // will feel like, and the ends of the range are the two anyone actually
    // reaches for.
    get motionLabel() {
      const v = Number.isFinite(this.settings.motion) ? this.settings.motion : 100;
      if (v === 0) return "nothing moves";
      if (v <= 40) return "brisk";
      if (v <= 80) return "calm";
      if (v < 100) return "gentle";
      return "full";
    },

    // Applied live and persisted by the panel's Save, exactly like the
    // backdrop fade below it — you have to be able to feel the setting while
    // you are choosing it.
    setMotion(value) {
      this.settings.motion = parseInt(value, 10);
      this.applyMotion();
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
            // Not while it is thinking: the reply pass reports itself as
            // running the moment it starts, which is *before* the model has
            // said anything, so letting it through here would drop the cue
            // back to the dots on the first status after the thought began.
            if (!(this.composingKind === "thinking" && running)) {
              this.composingKind = event.run.animation;
              this.composingLabel =
                event.run.animation === "typing" ? this.cueLabel("typing") : event.run.label;
            }
          } else if (event.run.pass_id === "music_select") {
            // A line in the flow, not an ambient chip (§ musicSearching,
            // the cue row in index.html) — "the character is looking for a
            // song" is something happening in the room, unlike the other
            // background passes below.
            this.musicSearching = running;
          } else if (event.run.tier !== "blocking") {
            // ambient: a subtle indicator, never a character-thinking cue
            this.ambient = running
              ? [...new Set([...this.ambient, event.run.label])]
              : this.ambient.filter((a) => a !== event.run.label);
            if (event.run.pass_id === "scene") this.refreshing.scene = running;
            if (event.run.pass_id === "background_swap") this.refreshing.background = running;
            if (event.run.pass_id === "expression") this.refreshing.expression = running;
          }
          // A "/" run reaching done/stale/failed/skipped — reported as a
          // toast (§ resolveSlashRun) independent of the branches above,
          // which only cover the ambient cue for a *scheduled* run.
          if (!running) this.resolveSlashRun(event.run);
          break;
        }
        case "panel":
          if (event.panel === "scene") {
            this.scene = { ...this.scene, ...event.value };
            this.refreshing.scene = false;
          } else if (event.panel === "expression" && event.value.emotion) {
            this.expression = event.value.emotion;
          } else if (event.panel === "background" && event.value.background) {
            // Global now, not per-chat (§ background_swap's handler,
            // scheduler.py) — the same field the Theme panel's own manual
            // picker writes, so this just mirrors what the server already
            // persisted rather than tracking a separate chat-local value.
            this.settings.background = event.value.background;
            // Flagged only while someone is actually looking at a settings
            // panel to reconcile it against (§ settingsLocked,
            // discardBackgroundChange) — no one there, nothing to flag.
            if (this.panelOpen && (this.panel === "brain" || this.panel === "theme" || this.panel === "settings")) {
              this.backgroundAutoChanged = true;
            }
            this.applyBackground();
          } else if (event.panel === "music") {
            // A full replace, not a merge (§ pending_music, assembly.py) —
            // the slice is small and always sent whole, whoever changed it:
            // music_select proposing, or a manual pick/respond/ended call.
            this.music = { ...event.value };
          } else if (event.panel === "reaction") {
            // Someone (maybe another tab on this same chat) set or cleared
            // a reaction — mirror it onto the message so both read the
            // same mark.
            const reacted = this.messages.find((m) => m.id === event.value.message_id);
            if (reacted) reacted.user_reaction = event.value.user_reaction;
          }
          break;
        case "message_reaction":
          // message_reaction (§ scheduler.py) answered — persisted server-
          // side already (repo.set_reaction_ack), so merging it onto the
          // message here is just catching this tab up, not the only place
          // it's remembered.
          {
            const target = this.messages.find((m) => m.id === event.message_id);
            if (target) target.reaction_ack = event.ack;
          }
          break;
        case "chat_renamed": {
          // Fires on whichever chat just crossed a message-count milestone
          // (§ scheduler.py _maybe_rename_chat) — reflect it in the sidebar
          // list if this listener happens to be attached to that chat.
          const renamed = (this.chats || []).find((c) => c.id === event.chat_id);
          if (renamed) renamed.title = event.title;
          break;
        }
        // The search runs before the reply pass starts, so the cue would
        // otherwise say "Typing…" for however long the engine takes. Both
        // events always arrive as a pair, and the reply's own pass_status
        // overwrites the label a moment later anyway.
        case "search_start":
          this.composingLabel = "Looking it up…";
          break;
        case "search_done":
          this.composingLabel = event.count ? "Reading…" : this.cueLabel("typing");
          break;
        case "state":
          this.setBands(event.state.bands || []);
          this.stateProvisional = !!event.state.provisional;
          break;
        case "summary":
          this.summary = { text: event.text, covered_turn: event.covered_turn };
          break;
        // A render can land well after the turn's own request has closed
        // (§ app/avatar_video.py), so — like every other background-pass
        // result — this only ever arrives over the ambient stream.
        case "avatar_video":
          if (event.message_id && event.video_url) {
            this.liveAvatarVideo = { messageId: event.message_id, url: event.video_url };
          }
          break;
        case "error":
          if (!fromTurn) this.error = event.error;
          break;
      }
    },

    // No argument: the backdrop is entirely `settings.background` now (§
    // background_swap's handler, scheduler.py, and the "panel" case in
    // handleEvent above, both of which set that directly rather than
    // passing a value through here) — nothing left for this to resolve or
    // fall back from.
    applyBackground() {
      const file = this.backgroundFile();
      let dim = Number.isFinite(this.settings.background_dim)
        ? this.settings.background_dim : 70;

      // Glass lets the room back in. The wash exists to keep text readable
      // over the image; with glass on, that job is done by the translucent
      // blurred surface the text actually sits on, and leaving the wash at
      // full strength starves the glass of anything to show through — a
      // frosted pane over a blank wall is just a pale rectangle.
      if (this.settings.glass) {
        const a = Math.max(0, Math.min(100, Number.isFinite(this.settings.glass_amount)
          ? this.settings.glass_amount : 60)) / 100;
        // At full glass the wash is almost gone: the room is the point, and
        // the pane's own rim and sheen are carrying the readability now.
        dim = Math.round(dim - (dim - 8) * a);
      }

      // The wash derives from --bg rather than being a fixed dark overlay, so
      // it works on a light palette as well as a dark one — and it is what
      // keeps text readable over an image at all.
      const image = file
        ? `linear-gradient(color-mix(in srgb, var(--bg) ${dim}%, transparent),` +
          ` color-mix(in srgb, var(--bg) ${Math.min(100, dim + 7)}%, transparent)),` +
          ` url("/backgrounds/${encodeURIComponent(file)}")`
        : "";

      const layers = document.querySelectorAll(".backdrop-layer");
      if (layers.length < 2) return; // not mounted yet
      const shown = this._backdropShown || 0;
      if (file === this._backdropFile) {
        // Same picture, only the wash changed (the fade slider, glass) —
        // update whichever layer is actually showing in place. Crossfading
        // a picture that never left would just be a pointless flicker.
        layers[shown].style.backgroundImage = image;
        return;
      }
      const first = this._backdropFile === undefined;
      this._backdropFile = file;
      if (first) {
        // boot()'s own first call, before either layer has ever shown
        // anything — the real starting picture, not a "change" to dissolve
        // into. Painted straight onto the layer already marked shown (§
        // index.html) so the very first paint of the app is not a
        // multi-second fade-in of its own backdrop.
        layers[shown].style.backgroundImage = image;
        return;
      }
      // A real change: paint it onto the *other* layer and raise that one
      // over the one currently showing, fading it in. The showing layer
      // needs nothing done to it — being covered by a rising twin is the
      // whole crossfade (§ .backdrop-layer, styles.css).
      const next = shown ? 0 : 1;
      layers[next].style.backgroundImage = image;
      layers[next].classList.add("shown");
      layers[shown].classList.remove("shown");
      this._backdropShown = next;
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

    // null when the setting is off, otherwise this turn's { silenceMs,
    // typingMinMs } (§ realisticPacing) — the one thing runStream needs to
    // know to run "Realistic chat speed" for a send or a retry. `!== false`
    // rather than `=== true`: settings loads before any chat can open (§
    // boot), so the only gap this covers is that fetch having failed
    // outright, and the setting defaults on.
    realisticPacingFor(text) {
      return this.settings.realistic_chat_speed !== false ? realisticPacing(text) : null;
    },

    async send() {
      const text = this.draft.trim();
      // A recognised "/" line is a forced action (§ SLASH_COMMANDS above),
      // not a message — it never reaches the transcript at all, same as
      // hitting the world-pill's own refresh button by hand.
      const command = parseSlashCommand(text);
      if (command) {
        this.draft = "";
        if (this.$refs.input) this.$refs.input.style.height = "auto";
        return this.runSlashCommand(command);
      }
      const files = this.stagedIds();
      // "Look at this" with a picture and no words is a real message; only one
      // with neither is empty.
      if ((!text && !files.length) || this.streaming) return;
      // A file still uploading is not ready to be claimed, and sending without
      // it would silently drop the thing they were waiting for.
      if (this.staged.some((s) => s.uploading)) {
        return this.flashHint("Still uploading…");
      }
      // The server would refuse this too, but after the message was stored —
      // asking here keeps the turn from half-happening.
      if (this.policy === "manual" && this.cast.length > 1 && !this.nextSpeaker) {
        return this.flashHint("Pick who answers first");
      }
      // The menu row stays open once opened (§ its own comment in
      // index.html) for browsing between Brain/Theme/Characters/Settings,
      // but sending is leaving that browsing behind for the conversation —
      // so this is the one moment it closes on its own rather than waiting
      // to be told to.
      this.menu = false;
      this.draft = "";
      this.staged = [];
      this.error = "";
      if (this.$refs.input) this.$refs.input.style.height = "auto";
      const speaker = this.nextSpeaker;
      this.nextSpeaker = "";
      const sent = await this.runStream(`/api/chats/${this.chatId}/send`, {
        text, attachments: files, speaker_id: speaker,
      }, undefined, this.realisticPacingFor(text));
      // It never reached the server, so there is no stored message to retry
      // and nothing on screen — the words would simply have been gone. Put
      // them back where they were typed. A request that *did* land leaves the
      // message in the transcript and the retry affordance answers it.
      if (!sent) {
        this.draft = text;
        this.nextSpeaker = speaker;
      }
    },

    // Answer a message whose reply never came. Deliberately not "send it
    // again": that would put the same words in the transcript twice, and the
    // message is already stored — only the reply is missing.
    async retryTurn() {
      if (this.streaming || !this.chatId) return;
      this.error = "";
      this.scrollDown();
      // The message being answered, for the pacing's word count — it is
      // always the last one (§ the `unanswered` getter this button answers).
      const last = this.messages[this.messages.length - 1];
      const realistic = this.realisticPacingFor(last && last.role === "user" ? last.text : "");
      await this.runStream(`/api/chats/${this.chatId}/retry`, {}, undefined, realistic);
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
      await sleep(TEXT_FADE_MS());

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
      this.$nextTick(() => requestAnimationFrame(() => this.followGrowth(message, bubble)));
    },

    // Keeps a growing bubble's pinned size chasing the text as it streams in,
    // each step covered by the same eased transition (§ .bubble) that the
    // shrink-to-pill uses — rather than pinning once to the first chunk and
    // releasing outright, which is what this replaced. Releasing that early
    // left the rest of the growth to raw, unanimated reflow, and since the
    // pacer (§ makePacer) reveals text at a roughly constant rate, that read
    // as the bubble growing at one constant, linear speed for however long
    // the reply took, not easing into its resting size the way the shrink
    // does. Re-measured on an interval the length of the transition itself
    // rather than every frame — retriggering the transition that often never
    // lets a step land, which reads as a stutter, not motion; a step that
    // does land simply keeps easing towards wherever the text has grown to
    // by the time the next one fires. Stops once the text stops changing for
    // two ticks running — the pacer has caught up and there is nothing left
    // to chase — and releases the pin outright so nothing is left pinned to
    // a size that will go stale the moment more text is edited in.
    followGrowth(message, bubble) {
      // A chase step's target moves again next tick, unlike the one-shot
      // collapse/snap-back .bubble is otherwise built for (§ .bubble.chasing
      // in styles.css) — swapped in for exactly the ticks this drives.
      bubble.classList.add("chasing");
      let lastLen = -1;
      let stableTicks = 0;
      const step = () => {
        if (!bubble.isConnected) { clearInterval(timer); return; }
        const len = message.text.length;
        stableTicks = len === lastLen ? stableTicks + 1 : 0;
        lastLen = len;
        const natural = this.measureNatural(bubble);
        this.pinTo(bubble, `${natural.width}px`, `${natural.height}px`);
        if (stableTicks >= 2) {
          clearInterval(timer);
          setTimeout(() => {
            this.setPin(bubble, "", "");
            bubble.classList.remove("clipping", "chasing");
          }, BUBBLE_RESIZE_MS());
        }
      };
      const timer = setInterval(step, BUBBLE_RESIZE_MS());
      step();
    },

    // A brand-new reply has no bubble of its own to shrink into first the
    // way a regeneration does (§ endRegen) — only the cue's, snapshotted in
    // reveal() the instant before it disappeared. Pins the new bubble to
    // that same footprint the moment it exists, then grows it out to
    // whatever the reply needs (§ followGrowth), so the cue and the reply
    // read as one shape changing continuously rather than two elements
    // trading places in a single frame — the same difference §5 draws
    // between a sent message that pops into place and one that flies up
    // from the composer.
    growFromCue(messageId, size) {
      this.$nextTick(() => {
        const bubble = this.bubbleFor(messageId);
        const message = this.messages.find((m) => m.id === messageId);
        if (!bubble || !message) return;
        bubble.classList.add("clipping");
        this.pinInstant(bubble, `${size.width}px`, `${size.height}px`);
        requestAnimationFrame(() => this.followGrowth(message, bubble));
      });
    },

    isRegenerating(message) {
      return this.regenId === message.id && !message.text;
    },

    // ---- the composing cue ----

    // What the cue says. Two states, because they are two different things:
    // the dots mean the backend has not answered yet, and the thinking cue
    // means it has and the model is reasoning its way towards a reply. Both
    // carry the speaker's name in a group, where "who is thinking" is a real
    // question.
    cueLabel(kind) {
      const who = this.composingSpeaker;
      if (kind === "thinking") return who ? `${who} is thinking…` : "Thinking…";
      return who ? `${who} is typing…` : "Typing…";
    },

    // How deep into the thought, 0 → 1, saturating. Nothing knows how long a
    // thought will run — there is no total to divide by — so this can only say
    // "a while" and must never look like a progress bar about to finish. It
    // rises fastest over the first few hundred characters, which is where the
    // question is still "is it working at all", and then flattens.
    depthFor(chars) {
      return chars ? 1 - Math.exp(-chars / 900) : 0;
    },

    get thinkDepth() {
      return this.depthFor(this.thinkChars);
    },

    // Stops whatever is generating. The fetch is aborted, which drops the
    // connection; the server sees the reader hang up, keeps the text that had
    // already arrived and records the run as stopped.
    stopGenerating() {
      if (this.streamAbort) this.streamAbort.abort();
    },

    // Returns whether the turn actually started — a caller that never reached
    // the server has a message on its hands that nothing else will account for.
    //
    // `realistic`, when given (§ realisticPacing, "Realistic chat speed" in
    // settings), holds { silenceMs, typingMinMs } for this one turn — see
    // send() and retryTurn(), the only two callers that ever pass it; swipe()
    // never does, on purpose (§ AskUserQuestion this was scoped by).
    // Everything below it gates on `gateOpen`, which starts closed only when
    // there is a plan to open it: every call with no `realistic` behaves
    // exactly as this function always has, gate open from the first line.
    async runStream(url, body, swipeMessageId, realistic) {
      let reached = false;
      this.streaming = true;
      this.streamAbort = new AbortController();

      let gateOpen = !realistic;
      // What the cue should say once it is actually shown — tracked even
      // while nothing is visible, so a reasoning event that arrives during
      // the silence does not get lost and have the cue open on the wrong
      // word the moment it appears.
      let pendingKind = "typing";
      // A reply/variant event that finished before the gate opened — held
      // rather than applied immediately, so a server that answers inside the
      // silence still waits out the same pacing as a slow one (§ below).
      let pendingFinal = null;
      let realisticTimer = null;

      this.composing = !swipeMessageId && gateOpen;
      this.composingKind = "typing";
      this.composingSpeaker = "";
      this.thinkChars = 0;
      this.composingLabel = this.cueLabel("typing");
      this.hudRuns = [];

      let target = null;
      let buffer = "";
      let firstToken = false;
      // Shows the reply at a fraction of the speed it arrives at. `target` is
      // assigned on the first delta, so the pacer reads it rather than closing
      // over it.
      const pacer = makePacer((text) => {
        if (target) target.text = text;
        this.markFlowing();
      });

      // Creates the placeholder bubble (or finds the one being swiped) and
      // hands whatever has accumulated in `buffer` to the pacer. Called from
      // the delta case once the gate is already open, and from the gate's own
      // timer to catch up on whatever arrived while it was still closed —
      // both need the exact same thing to happen, so this is the one place
      // that does it.
      const reveal = () => {
        if (!target) {
          let cueSize = null;
          if (swipeMessageId) {
            target = this.messages.find((m) => m.id === swipeMessageId);
          } else {
            // The cue's own on-screen footprint, snapshotted the instant
            // before it disappears — a regeneration has an existing bubble
            // to shrink into and grow back out of (§ endRegen); a brand-new
            // reply has nothing but the cue's, so that is what this one
            // opens from instead of popping straight to whatever the first
            // chunk of text needs.
            if (this.composing) {
              const cue = document.querySelector(".bubble.composing");
              if (cue) cueSize = { width: cue.offsetWidth, height: cue.offsetHeight };
            }
            // Read it back rather than keeping the object that went in.
            // Alpine stores a *proxy* of what you push, and only writes
            // through that proxy are seen: holding the raw object meant
            // every `target.text = buffer` below landed on something
            // nothing was watching. The reply then arrived in one frame
            // at the end of the stream — the bubble sat at the size of
            // the first token for the whole generation and then snapped
            // open, and the per-token fade (§2.1) never ran at all. The
            // swipe path never had this because `find()` hands back the
            // proxy already.
            this.messages.push({
              id: "streaming",
              role: "assistant",
              turn: this.turn,
              text: "",
              variant_count: 1,
              variant_index: 0,
              edited: false,
            });
            target = this.messages[this.messages.length - 1];
          }
          this.composing = false;

          if (target && this.regenId === target.id) {
            // First token of a regeneration: grow out of the typing cue
            // instead of snapping open. The callback writes the buffer as
            // it stands when the animation measures, so it cannot undo a
            // later delta the way a captured copy would.
            this.endRegen(target, () => { target.text = buffer; });
            return;
          }

          if (cueSize) this.growFromCue(target.id, cueSize);
        }
        // Trailing whitespace is trimmed from what is *shown*, never
        // from the buffer. `pre-wrap` renders a trailing newline as a
        // real empty line, so a reply that streams one mid-paragraph
        // stood a blank line tall until the server's cleaned copy
        // arrived and took it away — the bubble lost a line of height at
        // the moment the reply settled, which is the one moment nothing
        // should move.
        pacer.push(buffer.replace(/\s+$/, ""));
        if (!firstToken) { firstToken = true; buzz(6); }
        // `content-visibility: auto` on a message row skips style and
        // layout for its whole subtree, so the per-token fade ran as a
        // perfectly healthy animation that never moved a pixel. The
        // `:has(.mk-new)` rule cannot fix it — the animation starts in
        // the frame the subtree is still being skipped in. Marking the
        // row for the length of the stream can, and there is exactly one
        // row streaming at a time.
        this.markStreamingRow(target.id);
      };

      // Applies a reply/variant event exactly as the switch below always
      // has — pulled out so a pending one (§ pendingFinal) can be run from
      // the gate's own timer once it opens, not only from the loop.
      const applyFinal = async (final) => {
        await pacer.done();
        if (final.type === "reply") {
          const index = this.messages.findIndex((m) => m.id === "streaming");
          const message = { ...final.event.message, text: final.event.message.text };
          if (index === -1) this.messages.push(message);
          else this.messages[index] = message;
        } else {
          const message = this.messages.find((m) => m.id === final.event.message_id);
          if (message) {
            message.text = final.event.variant.text;
            message.variant_index = final.event.variant.idx;
            message.variant_count = final.event.variant.idx + 1;
            message.edited = false;
            message.has_thinking = !!final.event.variant.has_thinking;
          }
        }
        this.setBands(final.event.state.bands || []);
        this.stateProvisional = !!final.event.state.provisional;
      };

      // Resolved once the gate's own timers actually open it — see the
      // await right after the loop below. A fast server finishes the SSE
      // stream long before silenceMs + typingMinMs have elapsed, and the
      // `for await` loop exits the moment the connection closes; without
      // this, `finally` would run on its heels, clearTimeout the still-
      // pending gate and tear the turn down with the reply held in
      // `pendingFinal` and never shown. Left null when there is no
      // `realistic` to wait for.
      let gateOpenResolve = null;
      const gateOpenSettled = realistic
        ? new Promise((resolve) => { gateOpenResolve = resolve; })
        : null;

      if (realistic) {
        realisticTimer = setTimeout(() => {
          // The silence is over. Show the cue — in whichever kind a
          // reasoning event during the silence said it should open on —
          // and give it its own floor before the gate can open under it.
          this.composing = true;
          this.composingKind = pendingKind;
          this.composingLabel = this.cueLabel(pendingKind);
          realisticTimer = setTimeout(async () => {
            gateOpen = true;
            if (buffer) reveal();
            if (pendingFinal) await applyFinal(pendingFinal);
            gateOpenResolve();
          }, realistic.typingMinMs);
        }, realistic.silenceMs);
      }

      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: this.streamAbort.signal,
        });
        // Through apiError like every other request. This used to format its
        // own "404 Not Found", which is the one path where a bare status code
        // still reached the screen after the rest were given sentences.
        if (!response.ok) throw await apiError(response);
        // The server answered. Whatever happens now, the message is its
        // problem: it is stored, and the retry affordance can answer it.
        reached = true;

        for await (const event of sseStream(response)) {
          switch (event.type) {
            case "turn_start":
              this.turn = event.turn;
              // Who is answering, so the cue carries their name — typing or
              // thinking — rather than the chat's nominal character.
              if (event.speaker && this.cast.length > 1) {
                this.composingSpeaker = event.speaker.name;
                this.composingLabel = this.cueLabel(this.composingKind);
              }
              this.messages.push(event.message);
              // Fly it up from the composer rather than having it appear in
              // the column. Cleared once the keyframes are done so a later
              // re-render of the same message does not replay them.
              this.sendingId = event.message.id;
              setTimeout(() => {
                if (this.sendingId === event.message.id) this.sendingId = "";
              }, MESSAGE_SEND_MS());
              this.scrollDown();
              break;

            // A retry. The user message is already on screen and already in
            // the transcript, so this carries the same payload as turn_start
            // without appending a second copy of it.
            case "turn_resume":
              this.turn = event.turn;
              if (event.speaker && this.cast.length > 1) {
                this.composingSpeaker = event.speaker.name;
                this.composingLabel = this.cueLabel(this.composingKind);
              }
              this.scrollDown();
              break;

            case "delta":
              buffer += event.text;
              // Still silent, or still holding the typing cue's own floor
              // (§ realistic above) — accumulate and wait. The gate's timer
              // calls reveal() itself once it opens, to catch up on exactly
              // this backlog; there is nothing more to do with this one.
              if (!gateOpen) break;
              reveal();
              // No scroll call here on purpose: the observer follows the text
              // as it grows, and forcing it per delta would drag the user back
              // down every token if they had scrolled up to read.
              break;

            // The model is reasoning. Nothing visible arrives while it does
            // — that is the whole reason this event exists — so the cue has
            // to say so itself, or a minute of thinking looks like a backend
            // that never answered.
            case "reasoning": {
              const chars = event.chars || 0;
              // One of these arrives per token. Writing the count every time
              // would restyle the cue on each one for a ring that stops moving
              // measurably after the first few hundred characters, so it is
              // only written when it would actually show.
              if (!this.thinkChars || this.depthFor(chars) - this.thinkDepth >= 0.005) {
                this.thinkChars = chars;
              }
              // Remembered regardless of whether the cue is on screen yet, so
              // the gate's own timer (§ realistic above) opens it already
              // reading "thinking" instead of defaulting to "typing" for a
              // frame. Only actually repainted here when there is a cue up to
              // repaint — during the silence there is nothing to update.
              pendingKind = "thinking";
              if (this.composing) {
                this.composingKind = "thinking";
                this.composingLabel = this.cueLabel("thinking");
              }
              break;
            }

            // Everything streamed so far was the model thinking out loud: it
            // emitted a closing </think> for an opening tag its own chat
            // template had written, so the reply had not started at all. What
            // is on screen has to go back.
            case "reply_reset":
              buffer = "";
              pacer.reset();
              pendingFinal = null;
              if (swipeMessageId) {
                if (target) target.text = "";
              } else {
                this.messages = this.messages.filter((m) => m.id !== "streaming");
                target = null;
                pendingKind = "thinking";
                // Only during the typing-cue phase does this belong on
                // screen; during the silence there is still nothing to show,
                // and forcing composing on here would open the cue early,
                // ahead of the gate's own timer.
                if (this.composing) {
                  this.composingKind = "thinking";
                  this.composingLabel = this.cueLabel("thinking");
                }
              }
              break;

            case "reply":
              // Not before the paced text has caught up: swapping in the
              // finished reply while the bubble is still filling would skip
              // the last of it into place. While the gate is still closed
              // there is no bubble to catch up yet — held instead, and
              // applied by the gate's own timer once it opens (§ realistic
              // above), so a server that answers inside the silence or the
              // typing cue's floor still waits out the same pacing as a slow
              // one rather than snapping the reply in early.
              if (!gateOpen) { pendingFinal = { type: "reply", event }; break; }
              await applyFinal({ type: "reply", event });
              break;

            case "variant":
              if (!gateOpen) { pendingFinal = { type: "variant", event }; break; }
              await applyFinal({ type: "variant", event });
              break;

            case "error":
              this.error = event.error;
              break;

            default:
              this.handleEvent(event, true);
          }
        }
        // The stream itself just finished — often before the gate does, on
        // a fast server. Wait for the gate's own timers to run their course
        // and actually open it (§ gateOpenSettled above), so a reply held
        // in `pendingFinal` gets shown rather than torn down unseen the
        // moment `finally` clears the timer that was still going to reveal
        // it. Only reached on a clean finish, never on error/abort below —
        // there is nothing worth pacing out once the turn has failed.
        if (realistic && !gateOpen) await gateOpenSettled;
      } catch (e) {
        pacer.flush();
        if (e.name === "AbortError") {
          // Stopping is something the user did on purpose, so it is not an
          // error. The text that arrived stays where it is; the placeholder is
          // reconciled with what the server actually kept.
          this.flashHint("Stopped");
          await this.reloadMessages();
        } else {
          this.error = errorText(e);
          this.messages = this.messages.filter((m) => m.id !== "streaming");
        }
        // A failed regeneration must not leave the message blank.
        if (swipeMessageId && this.regenPrevious) {
          const original = this.messages.find((m) => m.id === swipeMessageId);
          if (original && !original.text) original.text = this.regenPrevious;
        }
      } finally {
        // Whichever of the two (§ realistic above) is still pending: the
        // stream is done, one way or another, and a timer that fired after
        // this would reveal or finalize against state this has already torn
        // down or reloaded.
        clearTimeout(realisticTimer);
        await pacer.done();
        this.streaming = false;
        this.composing = false;
        this.streamAbort = null;
        this.markStreamingRow(null);
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
      return reached;
    },

    // ---- editing & variants ----

    // Editing must not make the bubble jump: it keeps the width and height the
    // rendered text had, and only grows from there.
    startEdit(message, fromEl) {
      const bubble = fromEl && fromEl.closest(".bubble");
      // `:not(.regen)` matters: every bubble holds two `.body` elements and the
      // first is the hidden regeneration cue, which has no box. Measuring that
      // one gave every edit box a height of zero, so it fell back to its 2.5em
      // floor — two lines to edit six paragraphs in.
      const body = bubble && bubble.querySelector(".body:not(.regen)");
      if (bubble && body) {
        const rect = body.getBoundingClientRect();
        bubble.style.minWidth = `${Math.ceil(bubble.getBoundingClientRect().width)}px`;
        this.editHeight = Math.ceil(rect.height);
      }
      this.editingEl = bubble || null;
      this.editing = message.id;
      this.editText = message.text;
      // Fit the box to the text now rather than on the first keystroke: the
      // measurement above is the *rendered* height, and markup renders shorter
      // than the raw text it came from — asterisks and quotes are characters
      // in the box and formatting on the screen.
      this.$nextTick(() => {
        const box = this.editingEl?.querySelector(".edit-box");
        if (!box) return;
        box.focus();
        // Never taller than a little over half the screen, whatever the
        // rendered text measured: the bubble it came from can be the whole
        // screen, and a text box that tall has nowhere to put the keyboard.
        // viewportHeight(), not window.innerHeight — see that function's own
        // note. Without it this cap was computed against the keyboard-less
        // full screen, so a box already at the cap could still end up taller
        // than the room actually left once the keyboard focus() just
        // triggered finished opening underneath it.
        if (this.editHeight) {
          box.style.minHeight = `${Math.min(this.editHeight, viewportHeight() * 0.55)}px`;
        }
        // A frame after the tick, not in it: `x-model` writes the value during
        // the same flush, and a box measured before its text is in it reports
        // the height of an empty one — which is how a six-paragraph reply got
        // three lines to be edited in.
        requestAnimationFrame(() => this.autosize(box, 0.55));
        // The keyboard can still be mid-animation at this point — focus()
        // starts it, it does not wait for it — so the cap above may yet be
        // measured against a viewport that has not finished shrinking.
        // visualViewport fires its own resize as the keyboard settles (and
        // again if it is dismissed, or the phone rotates), so re-running the
        // same cap then is what actually keeps the box inside the screen
        // rather than just inside the screen at the instant editing opened.
        if (window.visualViewport) {
          this._editViewportResize = () => this.autosize(box, 0.55);
          window.visualViewport.addEventListener("resize", this._editViewportResize);
        }
      });
    },

    endEdit() {
      if (this.editingEl) {
        this.editingEl.style.minWidth = "";
        const box = this.editingEl.querySelector(".edit-box");
        if (box) { box.style.minHeight = ""; box.style.height = ""; }
      }
      if (this._editViewportResize && window.visualViewport) {
        window.visualViewport.removeEventListener("resize", this._editViewportResize);
      }
      this._editViewportResize = null;
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
        this.error = errorText(e);
        return;
      }
      if (el) {
        // Let it shrink out of the column rather than blinking away and
        // yanking everything below it up a bubble's height.
        el.classList.add("leaving");
        // The bubble fading was only half of it: the row it sits in kept its
        // height until the element left the DOM, so everything below still
        // snapped up in one frame at the end. Collapsing the row — and the
        // column gap with it, which height alone does not touch — is what
        // makes the list close rather than jump.
        const row = el.closest(".msg");
        if (row) {
          const height = row.getBoundingClientRect().height;
          row.style.height = `${height}px`;
          row.style.overflow = "hidden";
          void row.offsetHeight;          // commit the start value before moving
          row.classList.add("collapsing");
          row.style.height = "0px";
          row.style.marginBottom = `-${getComputedStyle(
            row.parentElement
          ).rowGap || "0px"}`;
        }
        await sleep(MESSAGE_LEAVE_MS());
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
        pacer.flush();
        if (e.name === "AbortError") {
          this.flashHint("Stopped");
          await this.reloadMessages();
        } else {
          this.error = errorText(e);
          message.text = start;
        }
      } finally {
        this.streaming = false;
        this.streamAbort = null;
      }
    },

    // Undoes a "Cut excess paragraphs" cut (§ Settings.cut_excess_paragraphs)
    // on this one message. The button only shows when has_full_text is true
    // (§ index.html), so there is always something on the server to put back.
    async restoreFullLength(message) {
      try {
        const updated = await api.post(`/api/messages/${message.id}/restore`);
        message.text = updated.text;
        message.has_full_text = false;
        this.flashHint("Restored to full length");
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // post_process's own undo (§ app/reply_polish.py) — the model's own
    // first draft, before the copy-edit. Independent of has_full_text
    // above: a reply post_process rewrote and the length backstop then also
    // cut carries both, and each button only ever undoes its own step.
    async restoreDraft(message) {
      try {
        const updated = await api.post(`/api/messages/${message.id}/restore-draft`);
        message.text = updated.text;
        message.has_draft_text = false;
        this.flashHint("Restored to the original draft");
      } catch (e) {
        this.error = errorText(e);
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
        this.error = errorText(e);
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
      // Slide in from the side you came from. The text used to cross-fade,
      // which says something changed but not which way you moved — and the
      // whole point of variants is that they sit in an order you are walking
      // along. Forward arrives from the right, back from the left.
      const row = this.bubbleFor(message.id)?.closest(".msg");
      message.text = updated.text;
      message.variant_index = updated.variant_index;
      message.variant_count = updated.variant_count;
      // The reasoning belongs to the variant, so walking to the next one walks
      // to its thoughts — or to none, if this one did not think.
      message.has_thinking = !!updated.has_thinking;
      // Driven from JS rather than a class, because a class here has to win a
      // cascade fight it keeps losing: `.body.regen` is still on the element
      // from the swipe that made this variant, and the row carries
      // `content-visibility: auto`, so a rule turning that off through
      // `:has()` does not land in the frame the animation would have started
      // in. An explicit animation has no cascade to lose and no subtree to be
      // skipped in.
      //
      // Still on the tokens: the easing and the duration are read from :root,
      // so the motion dial scales this like everything else and there is no
      // bezier written inline.
      this.slideVariant(this.bubbleFor(message.id), direction);
      buzz(8);
    },

    // The row tokens are currently arriving into. Kept as a single element
    // reference rather than a class on every render, so the common case costs
    // one identity comparison per frame.
    markStreamingRow(messageId) {
      const row = messageId
        ? this.bubbleFor(messageId)?.closest(".msg") || null
        : null;
      if (row === this._streamRow) return;
      this._streamRow?.classList.remove("animating", "streaming", "flowing");
      row?.classList.add("animating", "streaming");
      this._streamRow = row;
      if (!row) {
        clearTimeout(this._flowTimer);
        this._flowTimer = 0;
      }
    },

    // The cursor is solid while text is arriving and blinks once it stops, the
    // way a terminal's does. Anything longer than a held breath between chunks
    // counts as stopped — a model on a phone delivers unevenly enough that a
    // shorter window would have the cursor flickering between tokens, which
    // says "stalled" about a reply that is arriving perfectly well.
    markFlowing() {
      const row = this._streamRow;
      if (!row) return;
      row.classList.add("flowing");
      clearTimeout(this._flowTimer);
      this._flowTimer = setTimeout(() => row.classList.remove("flowing"), 420);
    },

    // Which way you moved. A cross-fade says the text changed; a slide says
    // you walked one step along the row of variants, which is what happened.
    // Small travel on purpose — a step, not a page turn, and text that travels
    // far is text you cannot read on the way.
    slideVariant(bubble, direction) {
      // `:not(.regen)` matters: every bubble holds two `.body` elements, and
      // the first is the hidden regeneration cue. Animating that one creates a
      // perfectly healthy animation on a zero-width box.
      const body = bubble && bubble.querySelector(".body:not(.regen)");
      if (!body || !body.animate) return;
      // The row carries `content-visibility: auto`, which does not merely skip
      // painting — it skips style and layout for the whole subtree, so an
      // animation on the text runs while nothing about it is ever recomputed.
      // The animation object exists, the pixels never move. Lifting it for the
      // duration is the same trick the send and delete animations already use.
      const row = bubble.closest(".msg");
      row?.classList.add("animating");
      const from = direction > 0 ? 14 : -14;
      const run = body.animate(
        [{ opacity: 0, transform: `translateX(${from}px)` },
         { opacity: 1, transform: "none" }],
        { duration: dur("base", 240), easing: ease("out"), fill: "both" }
      );
      run.finished.catch(() => {}).then(() => row?.classList.remove("animating"));
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
      // Selecting text is the one gesture on this element that has to reach
      // the browser untouched — claiming the pointer for a hold or a swipe
      // here would beat native long-press-to-select to the touch every time.
      if (this.selectingText === message.id) return;
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
      // A cancelled pointer used to leave the wheel open and deaf: the release
      // never comes, so neither path can choose. Falling back to tap-to-choose
      // means the worst case is one extra tap rather than a dead menu.
      if (this.wheel && !this.wheel.released) {
        this.wheel.released = true;
        this.wheel.active = -1;
        this.clearMagnet();
      }
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
        // Only on replies: a message you typed had no prompt behind it.
        ...(message && message.role === "assistant"
          ? [{ id: "prompt", label: "What was sent", icon: "#i-list" }]
          : []),
        // Only when this variant actually came with reasoning, which makes the
        // option itself the answer to "did it think?" — present means it did.
        ...(message && message.has_thinking
          ? [{ id: "thought", label: "What it thought", icon: "#i-brain" }]
          : []),
        { id: "delete", label: "Delete", icon: "#i-delete", danger: true },
        // Only on the literal last message in the chat (§ canSuggestEdit) —
        // an edit to an older reply would be revising something everything
        // said since has already answered.
        ...(this.canSuggestEdit(message)
          ? [{ id: "suggest", label: "Suggest edit", icon: "#i-suggest" }]
          : []),
        // Only on replies — reacting to your own message is not what this
        // is for, and the character-noticing-a-reaction pass (§
        // message_reaction, registry.py) only ever runs against one of its
        // own lines anyway.
        ...(message && message.role === "assistant"
          ? [{ id: "react", label: "React", icon: "#i-react" }]
          : []),
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
      // The clamp has to clear the option *box*, not just the radius: an
      // option sits WHEEL_RADIUS from the centre and then extends half its own
      // width past that. 42 was tuned against the short labels and let the
      // widest one ("What was sent", 88px) hang 3px off the left edge when the
      // wheel opened in a corner. Half the widest box, plus slack.
      const margin = WHEEL_RADIUS + WHEEL_OPTION_REACH;
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
      buzz(12);

      // The bubble is `touch-action: pan-y`, and that is read when the touch
      // *starts* — so the first vertical move after the wheel opens is a
      // scroll as far as the browser is concerned. It takes the gesture,
      // fires pointercancel, and the release that would have chosen an option
      // never arrives: the option under the finger is not the one that gets
      // picked, most of the time. Blocking touchmove from here on stops the
      // pan before it begins, which is possible only because the finger has
      // been still for HOLD_MS and no scroll has started yet.
      if (!this._wheelBlock) {
        this._wheelBlock = (e) => e.preventDefault();
      }
      document.addEventListener("touchmove", this._wheelBlock, { passive: false });

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
      if (this._wheelBlock) {
        document.removeEventListener("touchmove", this._wheelBlock, { passive: false });
      }
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
      if (option.id === "copy") return this.startSelectCopy(message);
      if (option.id === "continue") return this.continueReply(message);
      if (option.id === "hide") return this.toggleHidden(message);
      if (option.id === "prompt") return this.showPrompt(message);
      if (option.id === "thought") return this.showThinking(message);
      if (option.id === "react") return this.openReactionPicker(message);
      if (option.id === "suggest") return this.openSuggestEdit(message);
      if (option.id === "delete") {
        // Arm the bubble's own delete rather than deleting outright. A drag
        // that lands one option over should not be able to destroy a message.
        this.confirmMsg = message.id;
        clearTimeout(this._confirmTimer);
        this._confirmTimer = setTimeout(() => { this.confirmMsg = ""; }, CONFIRM_MS);
        this.flashHint("Tap the bin to confirm");
      }
    },

    // ---- select-to-copy ----
    //
    // Copying used to grab the whole message outright. Picking a line out of
    // six paragraphs meant copying all of it and cutting the rest somewhere
    // else, so "Copy" now arms selection instead: it steps the bubble's own
    // pointer handling aside (§ onMsgDown, the swipe/hold gesture that would
    // otherwise beat the browser's native long-press-to-select to the touch)
    // so a normal selection drag works, and the bar it shows is what turns
    // "done selecting" into an actual copy.

    startSelectCopy(message) {
      this.selectingText = message.id;
    },

    cancelSelectCopy() {
      this.selectingText = "";
      window.getSelection?.().removeAllRanges();
    },

    async confirmSelectCopy() {
      const text = (window.getSelection?.().toString() || "").trim();
      if (!text) {
        this.flashHint("Select some text first");
        return;
      }
      this.flashHint(await this.writeClipboard(text) ? "Copied" : "Could not copy");
      this.cancelSelectCopy();
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

    // The built-in `web_search` toggle, hidden along with everything else
    // Web search owns (§ Brain → Connect) when the feature itself is off —
    // a switch for a service nobody configured, sitting
    // beside toggles that actually do something, is exactly the clutter
    // that master switch exists to hide. A custom toggle a person made
    // themselves is never filtered here, whatever it happens to be called.
    visibleToggles() {
      if (this.settings.feature_web_search) return this.toggles;
      return this.toggles.filter((t) => t.id !== "web_search");
    },

    // ---- message reactions ----
    //
    // Reacting is instant and local (this.reactingTo just closes); the mark
    // and, once message_reaction answers, the ack line both arrive over the
    // chat's own SSE stream (§ handleEvent's "panel"/reaction and
    // "message_reaction" cases) rather than being set optimistically here —
    // the same write-then-listen-for-the-echo pattern the rest of this app
    // already uses for anything the server might also reject or race.

    openReactionPicker(message) {
      this.suggestingFor = "";
      this.reactingTo = this.reactingTo === message.id ? "" : message.id;
    },

    async setReaction(message, emoji) {
      this.reactingTo = "";
      try {
        await api.post(`/api/messages/${message.id}/react`, { emoji });
      } catch (e) {
        this.error = errorText(e);
      }
    },

    // ---- suggest edit ----
    //
    // A rewrite in place, not a branch: the same variant keeps its id and its
    // swipe position, only its text changes (§ run_suggest_edit, scheduler.py)
    // — the same thing a hand-typed edit does, just written by asking for a
    // change instead of typing the fix yourself. Only ever offered on the
    // literal last message in the chat: an edit to an older reply would be
    // revising something everything since has already answered.

    canSuggestEdit(message) {
      if (!message || message.role !== "assistant") return false;
      const last = this.messages[this.messages.length - 1];
      return !!last && last.id === message.id;
    },

    openSuggestEdit(message) {
      this.reactingTo = "";
      this.suggestingFor = this.suggestingFor === message.id ? "" : message.id;
      this.suggestText = "";
    },

    closeSuggestEdit() {
      this.suggestingFor = "";
      this.suggestText = "";
    },

    // Streams the revision straight into the bubble that is already on
    // screen, same shape as continueReply — there is nothing to choose
    // between afterwards, so this replaces the text in place rather than
    // creating a variant.
    async applySuggestedEdit(message, instruction) {
      const note = (instruction || this.suggestText || "").trim();
      if (!note || this.streaming) return;
      this.closeSuggestEdit();
      this.streaming = true;
      this.streamAbort = new AbortController();
      const start = message.text || "";
      let buffer = "";
      try {
        const response = await fetch(`/api/messages/${message.id}/suggest-edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ instruction: note }),
          signal: this.streamAbort.signal,
        });
        if (!response.ok) throw await apiError(response);
        for await (const event of sseStream(response)) {
          if (event.type === "delta") {
            buffer += event.text;
            message.text = buffer;
          } else if (event.type === "edited") {
            message.text = event.text;
            message.edited = true;
          } else if (event.type === "error") {
            this.error = event.error;
          }
        }
      } catch (e) {
        if (e.name === "AbortError") {
          this.flashHint("Stopped");
          await this.reloadMessages();
        } else {
          this.error = errorText(e);
          message.text = start;
        }
      } finally {
        this.streaming = false;
        this.streamAbort = null;
      }
    },

    // ---- settings (§13) ----
    //
    // The server sends "***" for any key it holds and never the real value.
    // Sending "***" back means "keep it", so an untouched key survives a save
    // without the browser ever having seen it.

    openTier: "",
    // By position, not by name. Keyed by name it closed itself on the first
    // keystroke of a rename — the key it was matching against had changed.
    openBackend: -1,
    settings: { backends: [], tiers: {}, tier_names: [], tier_groups: [], kinds: [],
                templates: [], think_modes: [], tiers_off: [], pass_every: {},
                kind_defaults: {}, theme_tokens: [], theme: {},
                backgrounds: [], background: "none", background_dim: 70, background_meta: {},
                path: "" },
    thought: null,
    thoughtError: "",
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
      this.settings = softenMasks(await api.get("/api/settings"));
      this.applyTheme();
      this.snapshotSettings();
    },

    // The numbers behind the reply. Two groups, split by what question they
    // answer rather than how often they get touched: `budgets` is every
    // number that decides how much reaches the model — token budget, memory,
    // lorebook, the rolling summary — grouped together in Brain → Prompt
    // regardless of which pass reads it, so "how much" is answered in one
    // place instead of two different hidden folds. `timeouts` is retry/wait
    // knobs, correct at their default until there is a specific reason they
    // are not — those stay in Brain → Advanced.
    //
    // Ranges are the useful span, not the legal one. A slider whose track
    // covers every value the field will accept spends most of its length in
    // territory nobody wants, and the interesting range ends up a few pixels
    // wide — so the ceiling here is the largest sensible setting and the box
    // beside it still takes anything the server allows.
    numberFields(group) {
      const fields = {
        budgets: [
          { key: "token_budget", label: "Context budget", min: 1024, max: 131072, step: 1024,
            unit: " tok",
            note: "Prompt the reply is built from. Match your model's window — past it, the far end is dropped anyway. 32k is what a local model on a PC usually has." },
          { key: "verbatim_window", label: "Messages kept in full", min: 4, max: 60,
            note: "The fewest recent messages sent word for word. Above this the context "
                + "budget decides, so a chat with room to spare is sent whole." },
          { key: "memory_max_injected", label: "Memories recalled", min: 0, max: 12,
            note: "Extracted facts added to a prompt at most. Zero turns recall off." },
          { key: "summary_budget", label: "Summary budget", min: 200, max: 2000, step: 50,
            unit: " tok",
            note: "How long the rolling summary may grow before it is re-summarised." },
          { key: "lorebook_scan_depth", label: "Lorebook scan depth", min: 0, max: 20,
            note: "How many recent messages are searched for lorebook keywords." },
          { key: "lorebook_total_budget", label: "Lorebook budget", min: 0, max: 2000, step: 50,
            unit: " tok",
            note: "Tokens of lorebook entries allowed into one prompt." },
        ],
        timeouts: [
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
    // The two everyone reaches for, plus the length cap. Their ranges come
    // from the catalogue rather than being written again here, so the basic
    // slider and the advanced one for the same parameter cannot disagree.
    samplingFields() {
      const book = this.samplerBook.samplers || [];
      const from = (key, label) => {
        const s = book.find((x) => x.key === key);
        return s ? { ...s, label } : { key, label, min: 0, max: 1, step: 0.01 };
      };
      return [
        from("temp", "Temperature"),
        from("top_p", "Top p"),
        // Not in the catalogue: it has no neutral value and nobody tunes it
        // for taste, so it is not one of the samplers that can be left unsent.
        // Length is not here any more: it belongs to the backend, which is
        // the thing that has a window. See the two sliders in the backend
        // editor. A pass still asks for what it needs and the backend caps it.
      ];
    },

    // ---- advanced samplers (§17) ----

    samplersIn(group) {
      return (this.samplerBook.samplers || []).filter(
        (s) => s.group === group && !["temp", "top_p"].includes(s.key),
      );
    },

    // Which backend this pass's tier actually resolves to. A sampler is a
    // property of the backend, not of the pass, so the same slider is real for
    // one pass and inert for another.
    backendFor(definition) {
      const name = (this.settings.tiers || {})[definition.model_tier];
      return (this.settings.backends || []).find((b) => b.name === name) || null;
    },

    samplerSupported(definition, sampler) {
      const backend = this.backendFor(definition);
      if (!backend) return false;
      return ((this.samplerBook.supported || {})[backend.kind] || []).includes(sampler.key);
    },

    // Moved off the value at which it does nothing — which is exactly the
    // condition under which it gets sent at all.
    samplerActive(definition, sampler) {
      const value = (definition.sampling || {})[sampler.key];
      if (value === undefined) return false;
      const gate = (this.samplerBook.gates || {})[sampler.group];
      if (gate && gate !== sampler.key) {
        const gateValue = (definition.sampling || {})[gate];
        const gateSpec = (this.samplerBook.samplers || []).find((s) => s.key === gate);
        return !!gateSpec && Number(gateValue) !== Number(gateSpec.neutral);
      }
      return Number(value) !== Number(sampler.neutral);
    },

    // Active *and* accepted here — the badge answers "what am I sending beyond
    // the two sliders above", so a knob that has been moved but goes nowhere
    // does not count, and neither do the basics shown separately.
    sentHere(definition, sampler) {
      return this.samplerActive(definition, sampler) && this.samplerSupported(definition, sampler);
    },

    changedCount(definition) {
      return (this.samplerBook.groups || [])
        .flatMap((g) => this.samplersIn(g.id))
        .filter((s) => this.sentHere(definition, s)).length;
    },

    samplerNote(definition, sampler) {
      if (this.samplerSupported(definition, sampler)) return sampler.note;
      const backend = this.backendFor(definition);
      return `${sampler.note} Not sent to ${backend ? backend.kind : "this backend"}.`;
    },

    // Only the extras. Temperature, top-p, top-k and the repetition penalty are
    // left exactly as they are: each pass ships with its own tuned values for
    // those, and a button labelled "turn the extras off" that quietly retuned
    // the reply pass would be a trap.
    resetSamplers(definition) {
      let turned = 0;
      for (const sampler of this.samplerBook.samplers || []) {
        if (sampler.key in SAMPLING_DEFAULTS) continue;
        if (Number(definition.sampling[sampler.key]) !== Number(sampler.neutral)) turned += 1;
        definition.sampling[sampler.key] = sampler.neutral;
      }
      if (!turned) return this.flashHint("Nothing extra was on");
      this.queuePassSave(definition);
      this.flashHint(turned === 1 ? "Turned one off" : `Turned ${turned} off`);
    },

    // Sampling is saved per pass as it is edited. It lives in the pass registry,
    // not in settings.json, so it does not ride along on the settings save —
    // and a number typed here that then needed a second, different-looking
    // button pressed to take effect would be its own bug report.
    async setSampling(definition, field, raw) {
      const key = typeof field === "string" ? field : field.key;
      const value = parseFloat(raw);
      if (Number.isNaN(value)) return;
      const integer = key === "max_tokens" || (typeof field === "object" && field.integer);
      definition.sampling[key] = integer ? Math.round(value) : value;
      this.queuePassSave(definition);
    },

    queuePassSave(definition) {
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
        // Untouched means "keep what is stored": the mask goes back as the
        // mask, so saving an unrelated setting cannot wipe a key you cannot
        // see to retype.
        search_key: this.settings.search_key === MASK_DISPLAY
          ? "***" : this.settings.search_key,
        avatar_key: this.settings.avatar_key === MASK_DISPLAY
          ? "***" : this.settings.avatar_key,
        tiers_off: [...(this.settings.tiers_off || [])],
        pass_every: { ...(this.settings.pass_every || {}) },
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
        template: "auto", timeout: 120, think: "auto",
        max_tokens: 5000, context: 0, models: [],
      };
      this.applyKindDefaults(backend);
      this.settings.backends.push(backend);
      // Open: you added it to fill it in.
      this.openBackend = this.settings.backends.length - 1;
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
      backend.think = defaults.think ?? "auto";
      backend.max_tokens = defaults.max_tokens ?? 5000;
      backend.context = defaults.context ?? 0;
      backend.models = [];
    },

    onKindChange(backend) {
      this.applyKindDefaults(backend);
      delete this.tests[backend.name];
      delete this.models[backend.name];
      delete this.modelErrors[backend.name];
    },

    // Open one backend, and ask it what it serves while it opens. The list is
    // the only way to choose a model now, so an empty one is a dead end — and
    // the answer takes a moment, which is a moment better spent while the fold
    // is still moving.
    toggleBackend(index, backend) {
      this.openBackend = this.openBackend === index ? -1 : index;
      if (this.openBackend !== index) return;
      if (backend.kind === "echo") return;
      if (this.models[backend.name] || this.loadingModels === backend.name) return;
      this.loadModels(backend);
    },

    // What the model list offers: whatever the backend reported, plus whatever
    // is configured — a model that has been pulled since, or one this backend
    // cannot enumerate, must not vanish from its own setting. Objects, not
    // bare names (§ Provider.list_models_detail) — Horde's carry an eta and
    // arrive pre-sorted fastest-first; a name synthesised here for a
    // still-configured-but-unlisted model carries none, which modelLabel
    // reads as "nothing more to say about this one" rather than "instant".
    modelOptions(backend) {
      const found = this.models[backend.name] || [];
      const current = (backend.model || "").trim();
      return current && !found.some((m) => m.name === current)
        ? [{ name: current }, ...found]
        : found;
    },

    // The option text for one model: its name alone, or with Horde's own
    // ETA alongside it once there is one to show.
    modelLabel(m) {
      return m.eta === undefined || m.eta === null ? m.name : `${m.name} · ~${this.etaText(m.eta)}`;
    },

    etaText(seconds) {
      if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
      return `${Math.round(seconds / 60)}m`;
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
        this.modelErrors[backend.name] = errorText(e);
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

    // ---- AI Horde quick-setup presets ----

    hordePresets() {
      return HORDE_PRESETS.map((p) => ({ ...p, summary: this.hordePresetSummary(p) }));
    },

    // One line of plain fact under each card's tagline, generated rather
    // than hand typed so it can never drift from what applyHordePreset()
    // actually does to your settings.
    hordePresetSummary(preset) {
      return [
        `Post-process ${preset.foreground ? "on" : "off"}`,
        `Secondary info ${preset.background ? "on" : "off"}`,
        `${preset.writing.length} writing rule${preset.writing.length === 1 ? "" : "s"}`,
        `examples ${preset.structural.includes("examples") ? "on" : "off"}`,
        // preset.prompt_budget is a target this file holds its own writing-
        // library choices to (§ the module comment above), not a setting —
        // backend.context and settings.token_budget are both held at
        // Horde's own ceiling on every tier now, so neither would read
        // differently between the three cards or tell you anything about
        // what actually differs. fmtTokens rather than /1024: the number as
        // anyone shopping for "a 32k model" already thinks of it, with one
        // decimal below 10k — these tiers sit at 1.5/2.5/4.5, and rounding
        // to a bare integer would show Mini's 1536 as a misleadingly-round
        // "2k".
        `~${this.fmtTokens(preset.prompt_budget)} card+rules prefix`,
      ].join(" · ");
    },

    // A local, one-shot bulk mutation — same shape as applyPreset() (§ theme
    // presets) below: it rides the existing Save bar rather than saving
    // itself, so tapping a card is exactly as reversible as hand-editing any
    // one field, and nothing here is a persisted "which preset is active"
    // flag that could go stale the moment something is tweaked afterward.
    async applyHordePreset(id) {
      const preset = HORDE_PRESETS.find((p) => p.id === id);
      if (!preset) return;

      // One horde backend, found or made — not one per preset. Re-picking a
      // different tier is meant to retune the same connection, not leave
      // three "AI Horde" entries behind for every tier someone tried.
      let backend = this.settings.backends.find((b) => b.kind === "horde");
      const isNew = !backend;
      if (isNew) {
        backend = { name: "AI Horde", kind: "horde", models: [] };
        this.settings.backends.push(backend);
      }
      const defaults = (this.settings.kind_defaults || {}).horde || {};
      backend.base_url = defaults.base_url ?? backend.base_url ?? "";
      backend.template = defaults.template ?? backend.template ?? "chatml";
      backend.timeout = defaults.timeout ?? backend.timeout ?? 300;
      backend.model = defaults.model ?? backend.model ?? "";
      backend.think = defaults.think ?? backend.think ?? "off";
      // Only written on a fresh backend: re-applying a preset over one a
      // person has already put their own key into must not quietly erase it.
      if (isNew) backend.api_key = defaults.api_key ?? "0000000000";
      // Horde's own ceilings, uniformly, on every tier — not this preset's
      // lever (§ the module comment above). What actually distinguishes the
      // tiers is which optional prefix content they turn on, set below.
      backend.context = HORDE_CONTEXT_CEILING;
      backend.max_tokens = HORDE_REPLY_CEILING;
      backend.models = backend.models || [];

      // Committed to Horde for every tier regardless of which are switched
      // on below — so flipping Post-process back on by hand later finds it
      // already pointed here instead of at whatever backend it last was.
      this.settings.tiers.blocking = backend.name;
      this.settings.tiers.foreground = backend.name;
      this.settings.tiers.background = backend.name;
      const off = [];
      if (!preset.foreground) off.push("foreground");
      if (!preset.background) off.push("background");
      this.settings.tiers_off = off;

      // state_auditor/expression, specifically — not a tier switch (§
      // HORDE_AUDIT_PASSES above). Requires this.passes to already be
      // loaded, which it is by the time the Presets tab can be reached at
      // all (§ openPanel("brain") loads both together). Saved immediately
      // rather than held for the pinned Save bar: these two live in the
      // separate pass_defs table, not in `settings`, so nothing else on
      // this screen is what would save them.
      for (const passId of HORDE_AUDIT_PASSES) {
        const pass = (this.passes || []).find((p) => p.id === passId);
        if (!pass || pass.enabled === preset.auditsState) continue;
        pass.enabled = preset.auditsState;
        try {
          await api.put(`/api/passes/${pass.id}`, pass);
        } catch (e) {
          this.error = errorText(e);
        }
      }

      const wanted = new Set(preset.writing);
      const wantedStructural = new Set(preset.structural);
      for (const section of this.settings.prompt_sections || []) {
        if (HORDE_WRITING_SCALING.has(section.id)) section.enabled = wanted.has(section.id);
        else if (HORDE_STRUCTURAL_SCALING.has(section.id)) section.enabled = wantedStructural.has(section.id);
      }

      // Held at the same ceiling as backend.context, on every tier — this is
      // *not* where Mini/Standard/Max differ (§ the module comment above).
      // settings.token_budget caps the whole assembled prompt — prefix,
      // lorebook, memories, summary and conversation together — so capping
      // it per tier was capping the conversation right along with the
      // prefix, leaving the *middle* band nothing to work with regardless
      // of how lean the writing-library selection below made the prefix
      // itself. A real, per-model ceiling still applies at generation time
      // (PassScheduler._fitted, tightened to what the selected model
      // actually holds) — this just stops a preset from asking for less
      // than that model already gives.
      this.settings.token_budget = HORDE_CONTEXT_CEILING;

      // Opened regardless of isNew, not only for a fresh backend: AI Horde
      // is the one kind that cannot be saved with no model chosen at all
      // (§ config.py's Save validation), so the field that makes this
      // savable needs to actually be on screen, not just true it will load
      // once someone happens to open the row by hand.
      this.openBackend = this.settings.backends.indexOf(backend);
      this.flashHint(`${preset.label} applied — review and Save`);

      // Fastest first is the whole point of ordering by ETA (§ modelLabel) —
      // picked automatically only when nothing is chosen yet, so re-applying
      // a preset over a model someone already picked never overrides it.
      if (!backend.model) {
        await this.loadModels(backend);
        const fastest = (this.models[backend.name] || [])[0];
        if (fastest && !backend.model) backend.model = fastest.name;
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
        this.snapshotSettings();
        this.saveMsg = "saved";
      } catch (e) {
        this.saveError = errorText(e);
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
        this.tests = { ...this.tests, [backend.name]: { ok: false, error: errorText(e) } };
      } finally {
        this.testing = "";
      }
    },

    // The stored values are what Ollama's API calls them; these are what they
    // mean to someone choosing between them.
    thinkLabel(mode) {
      return {
        on: "Always reason first. Costs part of the reply's token budget, and "
            + "what it thought is kept — hold a reply to read it.",
        auto: "Whatever the model does by default. Nothing is sent either way.",
        off: "Answer straight away. Ask a reasoning model not to reason.",
      }[mode] || mode;
    },

    testLabel(name) {
      const t = this.tests[name];
      if (!t) return "";
      return t.ok ? `ok — ${t.model} in ${t.latency_ms}ms` : `failed — ${t.error}`;
    },

    // ---- the three groups of passes (§3) ----
    //
    // The tiers are named for *when* they run, which is the wrong question for
    // someone opening the panel. Grouped and named for what they are for, each
    // one owns its backend, its switch and its own settings.

    tierOn(tier) {
      return !(this.settings.tiers_off || []).includes(tier);
    },

    toggleTier(group) {
      if (group.required) {
        return this.flashHint(`${group.label} is what makes it a conversation`);
      }
      const off = [...(this.settings.tiers_off || [])];
      const at = off.indexOf(group.tier);
      if (at >= 0) off.splice(at, 1);
      else off.push(group.tier);
      this.settings.tiers_off = off;
    },

    passesIn(tier) {
      return (this.passes || []).filter((p) => p.model_tier === tier);
    },

    // "3 passes · ollama", so a collapsed group still answers the two things
    // worth knowing about it.
    tierSummary(group) {
      const count = this.passesIn(group.tier).length;
      const backend = (this.settings.tiers || {})[group.tier] || "—";
      if (!this.tierOn(group.tier)) return "off";
      return `${count} · ${backend}`;
    },

    // Passes whose answer keeps between turns, and which are therefore worth
    // spacing out. A pass gated on a signal already runs only when something
    // happened; one gated on a count is being paid for on a timetable.
    spacedPasses(tier) {
      return this.passesIn(tier).filter((p) => p.output && p.output.type !== "reply");
    },

    passEvery(pass) {
      return (this.settings.pass_every || {})[pass.id] || 1;
    },

    setPassEvery(pass, raw) {
      const value = Math.max(1, Math.min(50, parseInt(raw, 10) || 1));
      const every = { ...(this.settings.pass_every || {}) };
      if (value <= 1) delete every[pass.id];
      else every[pass.id] = value;
      this.settings.pass_every = every;
    },

    everyLabel(pass) {
      return pass.label || pass.id;
    },

    // Four words for when a pass runs, for the row that is always visible.
    // The long version lives on the spacing control inside the fold.
    passWhen(pass) {
      if (!pass.enabled) return "off";
      const n = this.passEvery(pass);
      const type = (pass.trigger || {}).type;
      if (type === "over_budget") return "when the context fills";
      if (n > 1) return `every ${n} messages`;
      if (type === "chance") return `${Math.round((pass.trigger.probability || 0) * 100)}% of turns`;
      if (type === "every_n" && pass.trigger.n > 1) return `every ${pass.trigger.n} turns`;
      if (type === "on_signal") return "when something moves";
      return "every turn";
    },

    // The one pass Settings has its own checkbox for (§ index.html, "Chat
    // naming"), alongside its ordinary line under Brain → Passes → Secondary
    // info generator — same object, so flipping either one is flipping the
    // other (§ togglePass just below).
    chatRenamePass() {
      return (this.passes || []).find((p) => p.id === "chat_rename");
    },

    async togglePass(pass) {
      pass.enabled = !pass.enabled;
      try {
        await api.put(`/api/passes/${pass.id}`, pass);
      } catch (e) {
        pass.enabled = !pass.enabled;
        this.error = errorText(e);
      }
    },

    everyNote(pass) {
      if (!pass.enabled) return "Off. It will not run at all.";
      // A pass that waits for the context to fill is not on a timetable, and
      // saying "whenever it has something to do" of it is how the summary
      // ended up being read as free.
      const n = this.passEvery(pass);
      if (pass.trigger && pass.trigger.type === "over_budget") {
        return n <= 1
          ? "Only once the prompt has run out of room, over the messages that "
            + "have left it. Nothing is summarised while there is space to keep it."
          : `Only once the prompt is full, and at most once every ${n} messages.`;
      }
      if (n <= 1) return "Whenever it has something to do.";
      return `At most once every ${n} messages, however often it would fire.`;
    },

    backendSummary(backend) {
      const tiers = Object.entries(this.settings.tiers || {})
        .filter(([, name]) => name === backend.name).length;
      const model = backend.model || backend.kind;
      return tiers ? `${model} · ${tiers} in use` : model;
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

    // Everything worth handing someone else about a freeze or a crash, as
    // one plain-text file: the server's own log tail and any pass still
    // stuck mid-flight (§ /api/debug/export, app/debug_export.py) plus
    // whatever this browser tab itself recorded (§ debugLogText below) —
    // built and downloaded entirely client-side rather than through a
    // route, since combining "what the server has" with "what only this
    // tab saw" has nowhere to live but here.
    async downloadDebugLog() {
      this.debugLogBusy = true;
      let serverPart;
      try {
        serverPart = await (await fetch("/api/debug/export")).text();
      } catch (e) {
        serverPart = `(couldn't reach the server for its half: ${errorText(e)})`;
      }
      const blob = new Blob([serverPart, "\n\n", debugLogText()], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `tavern-debug-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
      a.click();
      URL.revokeObjectURL(url);
      this.debugLogBusy = false;
    },

    // ---- misc ----

    // Grows to fit, up to a limit, and scrolls past that. The limit is what
    // stops a long reply from filling the screen with a text box; scrolling is
    // what makes the rest of it reachable, which it was not — the box was
    // `overflow: hidden`, so anything past the cap was simply gone.
    autosize(el, cap = 0.3) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, viewportHeight() * cap) + "px";
    },

    // Keeps --worldbar-h in step with the header's own rendered height, so
    // the floating world-info pill (§ .world-pill in styles.css) can sit
    // flush under it without a hard-coded pixel guess. The header's height
    // isn't constant: the safe-area inset differs by phone, and the menu
    // row folding open adds a whole nav strip's worth of height while it's
    // open — the pill is hidden then (x-show="!menu"), but it re-measures
    // anyway so it's correct the instant the menu closes again.
    initWorldbarHeight(el) {
      const set = () => {
        document.documentElement.style.setProperty("--worldbar-h", `${el.offsetHeight}px`);
      };
      set();
      if (window.ResizeObserver) new ResizeObserver(set).observe(el);
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
      // The last few samples of the drag, so the release can ask how fast the
      // finger was still moving rather than only how far it got. A flick means
      // the same thing as a long slow pull and should do the same thing;
      // reading position alone makes a quick confident gesture fail while a
      // hesitant one succeeds, which is backwards.
      let track = [];
      const sample = (y) => {
        track.push({ y, t: performance.now() });
        if (track.length > 5) track.shift();
      };
      // Pixels per second, upward positive, over the tail of the gesture.
      const flickSpeed = () => {
        if (track.length < 2) return 0;
        const first = track[0];
        const last = track[track.length - 1];
        const dt = last.t - first.t;
        // Samples older than ~120ms are not part of a flick; a finger that
        // travelled fast and then held still has stopped, and releasing from
        // a hold should mean release, not throw.
        if (dt <= 0 || last.t - first.t > 160) return 0;
        return ((first.y - last.y) / dt) * 1000;
      };

      port.addEventListener("touchstart", (event) => {
        // Not while a message is being held or its wheel is open: that finger
        // is choosing an option, and reading it as a pull past the end of the
        // chat opened "write for me" behind the menu.
        if (this.wheel || this.hold) { pullFrom = null; return; }
        pullFrom = this.atVeryBottom() ? event.touches[0].clientY : null;
        track = [];
        if (pullFrom !== null) sample(pullFrom);
      }, { passive: true });
      port.addEventListener("touchmove", (event) => {
        if (this.wheel || this.hold) { pullFrom = null; this.setReveal(0); return; }
        if (pullFrom === null) return;
        if (!this.atVeryBottom()) { pullFrom = null; this.setReveal(0); return; }
        const y = event.touches[0].clientY;
        sample(y);
        // Finger moving up the screen means pulling the content up past its end.
        this.setReveal((pullFrom - y) / PULL_DISTANCE);
      }, { passive: true });
      const endPull = () => {
        // A flick counts, but only once the gesture has committed to being one
        // — a fast flick from a standing start at the bottom of the chat is
        // just a scroll that had nowhere to go.
        const flicked = this.reveal >= FLICK_MIN_REVEAL && flickSpeed() >= FLICK_SPEED;
        pullFrom = null;
        track = [];
        if (this.revealArmed || flicked) this.impersonate();
        else this.settleReveal();
      };
      port.addEventListener("touchend", endPull, { passive: true });
      port.addEventListener("touchcancel", endPull, { passive: true });

      port.addEventListener("wheel", (event) => {
        if (this.wheel || this.hold) { if (this.reveal) this.setReveal(0); return; }
        if (event.deltaY <= 0 || !this.atVeryBottom()) {
          if (this.reveal) this.setReveal(0);
          return;
        }
        this.setReveal(this.reveal + event.deltaY / PULL_DISTANCE);
        clearTimeout(this._pullTimer);
        // A wheel has no "let go", so a pause in scrolling is the release.
        this._pullTimer = setTimeout(() => {
          if (this.revealArmed) this.impersonate();
          else this.settleReveal();
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

    // Let go and it springs shut rather than vanishing. `settling` is the only
    // time the panel is allowed a transition — while the finger is down it has
    // to track the thumb exactly, and a control that lags the thumb is worse
    // than one that does not move at all.
    settleReveal() {
      this.revealSettling = true;
      clearTimeout(this._settleTimer);
      this._settleTimer = setTimeout(() => { this.revealSettling = false; },
                                     dur("slow", 340) + 60);
      this.setReveal(0);
    },

    setReveal(value) {
      const next = Math.max(0, Math.min(1, value));
      if (next === this.reveal) return;
      this.reveal = next;
      const armed = next >= 1;
      if (armed !== this.revealArmed) {
        this.revealArmed = armed;
        // The one moment worth a buzz: past here, letting go does something.
        if (armed) buzz(14);
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
