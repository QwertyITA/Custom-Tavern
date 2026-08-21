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
  animated *out* as well as in. Use the `--ease-*` and `--dur-*` tokens; never
  a bare bezier or a literal duration, never linear (except the three
  continuous ones). See the motion section of CLAUDE.md, including the
  `content-visibility` trap that silently kills animations on message rows.
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
- [x] **3. User personas.** Name, avatar, description. Several of them,
      switchable, one bindable as default per character. `{{user}}` resolves to
      the active one.

## Phase 2 — control over the turn

- [x] **5. Stop generation.** Abort mid-stream, keep what arrived.
- [x] **6. Continue.** Extend the last reply instead of re-rolling it.
- [x] **7. Hide from prompt.** A message stays on screen but leaves the
      context. Visibly marked.
- [x] **4. Author's Note.** Free text injected at a chosen depth from the end.
      Per chat and per character, with depth and frequency.
- [x] **13. Custom stop strings.** Per backend and per character.

## Phase 3 — the prompt, made legible

- [x] **12. Instruct/context template editor.** Editable sequences — system
      prefix/suffix, user and assistant sequences, stop strings — presented so
      someone who has never heard the phrase "instruct template" can tell what
      each box does. Live preview of an assembled prompt.
- [x] **14. Prompt Manager.** Reorder and enable/disable prompt sections, and
      inject custom blocks at a chosen position. Must not break the KV-cache
      rule (§7.1): the volatile suffix stays last, and the UI has to say so.
- [x] **15. Prompt itemisation.** What was actually sent, section by section
      with token counts. Reached from the message's long-press wheel.
- [x] **17. Advanced samplers.** min-p, top-k, typical-p, repetition/frequency/
      presence penalty, DRY, XTC, seed. Per pass, only where the backend
      supports them.
- [x] **16. Regex rules.** User-defined find/replace, scoped to input, output
      or display only. Ordered, toggleable, testable in place.

## Phase 4 — living with it

- [x] **10. Chat management.** Rename, search, export and import a chat.
- [x] **11. Favourites.** Star a character; starred sort first. (Tags and
      folders explicitly not wanted.)
- [x] **19. File attachments.** Images and text files on a message. Text goes
      into context; images are stored and shown, and go to the model only when
      the backend takes them.

## Phase 5 — the big one

- [x] **8. Group chats.** Several characters in one conversation. Needs state
      namespacing per character first (§15). Talkativeness, muting, and a
      turn-order policy that is not just round-robin.

## Phase 6 — passes that use the engine

- [x] **NEW. Random events pass.** Occasionally introduces something into the
      scene — a knock at the door, weather turning, a stranger. Gated on a
      cheap signal so it costs nothing most turns, with frequency in settings.
- [x] **23. Translation.** Translate the reply into the reading language and
      the user's message into the character's, as a pass. Toggleable.
- [x] **24. Web search.** Inject search results into context. Toggleable, off
      by default.

## Phase 7 — emotion

Not started. Overlaps with the existing `expression` canonical pass
(§DESIGN.md §12, §5.3) — that pass already picks one of a fixed emotion set
per turn and uses it to choose a `pfp_set` sprite (§ KNOWN-ISSUES.md, "Emotion
sprites don't go through the cropper"), so 36 and 37 below are as much about
making that mechanism explicit and toggleable as about building it new. Order
matters: 36 and 37 both read whatever 35 lands on.

- [ ] **35. Emotion tracking, closed set.** A fixed, small enum — not
      free-form model output — updated the way other state is (§6): rubric
      levels, not a sentence. Whether this rides on `expression` as-is or gets
      its own slice is the open call; either way "closed set" is the
      requirement, not "the model picks a word."
- [ ] **36. Emotion-driven bubble animation.** Each emotion in the closed set
      gets its own small arrival animation for that bubble — shy lands
      differently than confident. Toggleable off, and the fallback (off, or
      an emotion with no animation assigned) is today's one animation for
      everyone, not no animation at all.
- [ ] **37. Emotion-driven pfp selection.** Swap which `pfp_set` sprite is
      drawn based on the tracked emotion, for a character that has more than
      one. Toggleable independently of 36 — a user may want the picture to
      change without the bubble motion changing, or the reverse. Depends on
      35 for what "the current emotion" actually is.

## The UX pass

- [x] **Audit.** Drive the finished app on a phone-sized screen and find where
      it is bad. Nine findings, written up in [UX-AUDIT.md](UX-AUDIT.md) with
      how to reproduce each one.
- [x] **1. First run is a dead end.** Empty install offers a live composer that
      answers `404`, and advice to restart. Replaced with a *Nobody here yet*
      empty state and a composer that is off until there is someone to talk to.
- [x] **2. Closed folds keep 419 controls in the tab order.**
- [x] **3. Deleting a character or chat arms forever.** The other six armed
      actions disarm after 3s.
- [x] **4. A failed turn cannot be retried,** and Regenerate silently re-rolls
      the wrong reply.
- [x] **5. Touch targets under 44px nearly everywhere.** Worst: the prompt
      layout's reorder arrows at 32×19, now side by side at 38×44.
- [x] **6. The world line truncates to nothing.** Tapping it opens the setting
      in full underneath the header.
- [x] **7. Missing portraits fetch the directory** and 404 on every render.
- [x] **8. Raw HTTP status codes reach the user.**
- [x] **9. Sixteen colour pickers, no contrast check.** Added one — which
      found the shipped default palette failing it in two places.

## Animation pass

Researched and listed in [ANIMATIONS.md](ANIMATIONS.md), which records what
each one measured. The first was a bug rather than a suggestion.

- [x] **A. Reduced motion has gone stale.** The block is an allowlist of 11
      selectors against 33 animations and 43 transitions, so most of the app
      ignores the setting. Invert it to a wildcard with exceptions.
- [x] **B. Duration tokens.** Three easing tokens and none for duration; ~24
      durations in CSS and 11 more hand-synced in JS.
- [x] **C. Springs via `linear()`.** A bezier cannot express a settle with
      more than one bounce. Baseline since 2023, no build step.
- [x] **D. Streaming text has no motion at all.** Most-watched surface in the
      app, least animated.
- [x] **E. Shimmer on the composing label,** so the pass names read as working
      rather than stuck.
- [x] **F. Switching chats cuts,** with no skeleton and no crossfade.
- [x] **G. Pull-to-impersonate ignores velocity.** A flick should commit.
- [ ] **H. View Transitions / `@starting-style`.** Would replace the hand-rolled
      FLIP in four places and the keep-it-mounted-until-it-finishes dance.
      Left alone deliberately: it replaces working code rather than filling a
      gap, and it is the one item that needs the Chrome-only assumption
      confirmed first.
- [x] **I. The rest of ANIMATIONS.md.** Motion dial (§1.3), state bands that
      move (§2.4), message delete collapse and variant swipe direction (§2.5),
      staggered sheet and slider feedback (§3), haptics through one helper
      (§4.2). Scroll-driven animations stay with H.
- [x] **J. GUI sweep.** Five viewport sizes, every panel and sub-panel, long
      content and the action wheel. Two real bugs, both regressions from the
      touch-target work: character names collapsed to **zero width** at 360px,
      and the wheel's widest option hung off the left edge in a corner.
- [ ] **K. Scroll-to-bottom button.** The one item from §2.5 not built: on a
      long chat scrolled up there is no way back down.

## Polish pass

- [x] **Glass theme.** Frosted surfaces so the room stays visible through the
      interface, as an independent switch plus an intensity slider that layers
      over any palette. Turning it on also lets the backdrop wash back off —
      the wash exists to keep text readable, and glass does that job instead.
- [x] **Every interaction animated.** Press feedback existed on 7 selectors out
      of ~40 tappable things; the menu, every link-button, every switch, row,
      chip and tile answered a tap with nothing. Now: press / lift / ring, plus
      a real slider thumb instead of the browser's, a check animation, and a
      focus response on every field.
- [ ] **Density and rhythm.** The panels read as documentation — three lines of
      prose per control, and large vertical voids between a slider and its
      note. Not started.

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

- [x] **UX audit.** Go through everything looking for what is bad, awkward or
      merely tolerable, and report it rather than silently fixing it. Done —
      the findings, and what was done about each, are under *The UX pass*
      above and in full in [UX-AUDIT.md](UX-AUDIT.md).
