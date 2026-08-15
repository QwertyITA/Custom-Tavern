# Roadmap

Agreed work, in order. Tick a box only when the thing is **built, verified in a
browser, tested where it can be, documented and committed** — not when the code
is written.

This file is the memory across sessions. If you are picking this up cold: read
the unticked items, check `git log` for where the last one stopped, and carry
on. Keep the ordering — later items lean on earlier ones.

## Standing requirements

These apply to every item, not just the ones that mention them.

- **Minimal surface, complete behaviour.** Every feature has to earn its
  control. Prefer one thing that does the job over three that nearly do.
- **Motion.** Everything that appears, moves, grows or leaves is animated, and
  animated *out* as well as in. Use `--ease-out` / `--ease-in-out` /
  `--ease-back`; never a bare bezier, never linear (except continuous spin).
- **Verify by measurement.** Animations get a per-frame trace, not a look.
  Behaviour gets a browser run, not an assumption.
- **Tests** for anything with a server side. The suite is hermetic.
- **Docs.** README for anything a user touches; CLAUDE.md for a convention.

---

## Phase 1 — foundations

Everything else leans on these.

- [x] **1. Macros.** `{{char}}`, `{{user}}`, `{{persona}}`, `{{time}}`,
      `{{date}}`, `{{random:a,b}}`, `{{roll:d6}}`, `{{idle_duration}}`,
      `{{newline}}`. Substituted everywhere a card's text reaches a prompt or
      the screen. Without this, imported cards talk about someone called
      `{{user}}`.
- [x] **2. Card fields we currently drop.** `alternate_greetings` (pick the
      opening message, swipeable on the greeting) and
      `post_history_instructions` (injected after the history).
- [ ] **3. User personas.** Name, avatar, description. Several of them,
      switchable, one bindable as default per character. `{{user}}` resolves to
      the active one.

## Phase 2 — control over the turn

- [ ] **5. Stop generation.** Abort mid-stream, keep what arrived.
- [ ] **6. Continue.** Extend the last reply instead of re-rolling it.
- [ ] **7. Hide from prompt.** A message stays on screen but leaves the
      context. Visibly marked.
- [ ] **4. Author's Note.** Free text injected at a chosen depth from the end.
      Per chat and per character, with depth and frequency.
- [ ] **13. Custom stop strings.** Per backend and per character.

## Phase 3 — the prompt, made legible

- [ ] **12. Instruct/context template editor.** Editable sequences — system
      prefix/suffix, user and assistant sequences, stop strings — presented so
      someone who has never heard the phrase "instruct template" can tell what
      each box does. Live preview of an assembled prompt.
- [ ] **14. Prompt Manager.** Reorder and enable/disable prompt sections, and
      inject custom blocks at a chosen position. Must not break the KV-cache
      rule (§7.1): the volatile suffix stays last, and the UI has to say so.
- [ ] **15. Prompt itemisation.** What was actually sent, section by section
      with token counts. Reached from the message's long-press wheel.
- [ ] **17. Advanced samplers.** min-p, top-k, typical-p, repetition/frequency/
      presence penalty, DRY, XTC, seed. Per pass, only where the backend
      supports them.
- [ ] **16. Regex rules.** User-defined find/replace, scoped to input, output
      or display only. Ordered, toggleable, testable in place.

## Phase 4 — living with it

- [ ] **10. Chat management.** Rename, search, export and import a chat.
- [ ] **11. Favourites.** Star a character; starred sort first. (Tags and
      folders explicitly not wanted.)
- [ ] **19. File attachments.** Images and text files on a message. Text goes
      into context; images are stored and shown, and go to the model only when
      the backend takes them.

## Phase 5 — the big one

- [ ] **8. Group chats.** Several characters in one conversation. Needs state
      namespacing per character first (§15). Talkativeness, muting, and a
      turn-order policy that is not just round-robin.

## Phase 6 — passes that use the engine

- [ ] **NEW. Random events pass.** Occasionally introduces something into the
      scene — a knock at the door, weather turning, a stranger. Gated on a
      cheap signal so it costs nothing most turns, with frequency in settings.
- [ ] **23. Translation.** Translate the reply into the reading language and
      the user's message into the character's, as a pass. Toggleable.
- [ ] **24. Web search.** Inject search results into context. Toggleable, off
      by default.

## Not wanted

Recorded so they are not proposed again: checkpoints/branches (9), vector
storage and RAG (18), quick replies (25), objective/dice/timelines (27), tags
and folders (part of 11).

## Not yet — revisit later

Image generation (20), TTS (21), speech to text (22), slash commands and
STscript (26).

## Undecided — needs a call

Answers stopped at 28, so these were never ruled in or out:

- [ ] **28. More backends.** Anthropic, Gemini, OpenRouter, Mistral, DeepSeek,
      KoboldCpp, TabbyAPI, NovelAI. Most are thin subclasses of the existing
      OpenAI-compatible provider.
- [ ] **29. Connection profiles.** Save and switch whole API configs. Backends
      plus tiers already get most of the way.
- [ ] **30. Backup / restore.** Everything — chats, characters, settings — as
      one file.
- [ ] **31. Character card V3.** Assets and embedded lorebooks. We read V3
      partially and write V2.
- [ ] **32. Chub browsing.** Download cards from inside the app.
- [ ] **33. Custom CSS.** An escape hatch past the theme panel.
- [ ] **34. Markdown / code / LaTeX rendering.** Would replace our own inline
      markup — a change of direction rather than a gap.

---

## Final pass

- [ ] **UX audit.** Go through everything looking for what is bad, awkward or
      merely tolerable, and report it rather than silently fixing it.
