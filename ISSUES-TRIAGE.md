# Issues triage

A working list for deciding what to fix next, pulled together from
`KNOWN-ISSUES.md`, `ROADMAP.md`'s undecided/deferred sections, and a fresh
external read of the repo. This is a decision table, not a new audit —
everything already fixed in those two files is left out.

Columns:

- **Impact** — how much it actually costs *your* day-to-day use of the app,
  not a general-audience score.
- **Effort** — rough size of the fix.
- **Call** — my recommendation. **Fix now** / **Fix soon** / **Later** /
  **Skip**. You decide; this is a starting point, not a verdict.

| # | Issue | Type | Impact | Effort | Call |
|---|---|---|---|---|---|
| 1 | **No backup / restore.** Everything — chats, characters, settings — lives in one `data/tavern.db` on one phone. `DESIGN.md` §17 only promises presets/cards as portable; whole-app export is `ROADMAP.md` #30, still undecided. A wiped phone or storage clear loses everything. | Gap | **High** — total data loss risk on the only device this runs on | S–M — a "download everything as one .zip" endpoint (db file + `data/characters`, `data/backgrounds`, `data/avatars`) | **Fix now** |
| 2 | **No local test gate.** No CI, no pre-commit hook running `pytest`. A regression is only caught if you happen to run the suite before trusting a change. | Gap (process) | **Med** — you are the only QA; recent history (row 3) shows real misses | S — a `pre-push` git hook (`.githooks/` already exists for the secrets hook, same mechanism) | **Fix now** — cheap, closes a real hole |
| 3 | **Truncated/garbled replies from weak or low-quant backends aren't retried.** The fully-empty case already gets a silent one-shot retry (`test_empty_reply.py`); a reply that ends mid-word/mid-clause with no closing punctuation does not. Documented as a known gap in `KNOWN-ISSUES.md` (Medium). | Bug (partial fix exists) | **Med–High if you use Horde or small/quantized models**, low if you stay on a strong backend | S — same retry heuristic, different trigger condition (no terminal punctuation) | **Fix now if you use Horde/small models, else skip** |
| 4 | **Scroll-to-bottom button missing.** Scroll up on a long chat and there's no way back down except manual scrolling. Listed as the one unbuilt item from the animation pass (`ROADMAP.md`, item K). | Gap | **Med** — small daily friction on any chat that runs long | S | **Fix now** — cheap, used constantly |
| 5 | **A large imported card can eat an entire Horde budget on Mini/Standard**, reading as "no memory of the conversation." Architecture is working as designed (§7.1: only the conversation is trimmed, never the card) and partially mitigated already — `card_compression.py` (AI-assisted shrink) and a roster size warning both exist. | Deliberate limitation, already mitigated | **Med**, only for big imported cards on cheap tiers | N/A — fix is trimming the card or using Max tier, not code | **Skip** — tooling to fix it already shipped |
| 6 | **A card's lorebook can misattribute another character's traits** to the active character when a keyed entry lazily writes `{{char}}` for someone else. Not a code defect — the fix is the card's content — but nothing warns you it's happening. | Content/design gap | **Med, only if you import third-party cards**; zero if you author your own | M — heuristic: flag a keyed entry whose body contains a proper name that isn't the card's own | **Fix soon if you pull cards from elsewhere, else skip** |
| 7 | **The craft/writing library is mostly not followed by weak models** — ships ~1,700 tokens of writing rules asking for 4–8 paragraphs; a real chat against a small quant model averaged 2 paragraphs. Not a bug, a tuning mismatch for the backend you're actually running. | Design/tuning | **Low–Med** — cosmetic wasted budget, no correctness harm | S — trim which writing blocks are on per tier (same mechanism the Horde presets already use) | **Fix now if you're running small/local models, skip if you mainly use a strong backend** |
| 8 | **Panels read as documentation** — three lines of prose per control, large gaps between a slider and its note. Named in `ROADMAP.md`'s Polish pass, not started. | Gap (UX) | **Med** — friction every time you open Brain/Settings | M | **Later** |
| 9 | **Pre-pass ("would the character say yes")** — a director pass gated on refusal, run before the reply rather than only auditing after. Discussed, not built; two open design questions first (which tier, how it's worded so it's played not narrated). | Gap (feature) | **Med** — depends how often you hit unwanted compliance/refusal issues today | L | **Later** |
| 10 | **Emotion tracking → bubble animation → portrait swap** (`ROADMAP.md` #35–37). Not started; 36/37 both depend on 35 landing first. | Gap (feature) | **Low–Med** — immersion, not correctness | L | **Later** |
| 11 | **More backends** (Anthropic, Gemini, OpenRouter, Mistral, DeepSeek, KoboldCpp, TabbyAPI, NovelAI) — undecided (#28). Most are thin subclasses of the existing OpenAI-compatible provider per the roadmap's own note. | Undecided | **Depends entirely on whether you're routing through an OpenAI-compatible shim already** — high if not, zero if you are | M (each is a thin subclass) | **Decide first**, then fix soon if you want direct access to one specific provider |
| 12 | **Character card v3 partial-read, v2-only write.** Matters only for cards with embedded v3 assets. | Undecided (#31) | **Low**, unless you import v3-specific cards | M | **Later** |
| 13 | **Custom CSS escape hatch** not built (#33) — past the theme panel. | Undecided | **Low** — the theme panel already covers a lot | M | **Later** |
| 14 | **Vault only gates discovery routes**, not every route reachable by a known chat/character id. Deliberate — the stated threat model is a nosy person browsing the UI, not someone with a bookmarked/shared link. | Deliberate, flagged | **Low today**; revisit only if you ever share a chat link outside the app | — | **Skip unless the threat model changes** |
| 15 | **A reply can quote your own line back.** Measured once in 47 variants. Left alone on purpose — the fix (compare new reply to your last message) would also catch a character legitimately repeating a phrase on purpose. | Low, deliberately unfixed | **Low** — rare, cosmetic | — | **Skip** |
| 16 | **Character names/prose corrupt under low-quantization Horde workers**, and **prompt assembly is O(chat length)** (measured fine to 10k messages, revisit if that changes). Both are backend/scale limits, not app bugs — no code fix exists for the first, the second already has its future fix written down if it's ever needed. | Not a defect | **Low** at your current scale | — | **Skip** |

---

## What I'd actually do first

Rows **1, 2, 4** are all small and close real risk or real daily friction —
worth doing in one pass. Row **3** only if you're actually seeing garbled
Horde replies; row **7** only if you're on a small/local model day to day.
Everything else is either a bigger feature decision (9, 10, 11) or fine to
leave (5, 6, 14, 15, 16) unless your usage pattern changes (importing more
third-party cards, sharing links, wanting a specific hosted provider
directly).
