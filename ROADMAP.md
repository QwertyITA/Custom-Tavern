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
- [x] **38. Talking video avatar.** A lip-synced clip (MuseTalk and similar)
      plays over the portrait for the one reply it was rendered for, from a
      service the user runs on their own GPU — never this app, whose deploy
      target has none (§DESIGN.md §2, §20). `AVATAR-VIDEO-CONTRACT.md` is
      the HTTP contract, `MUSETALK-SETUP.md` walks through standing up a
      reference implementation of it; `app/avatar_video.py` is the client.
      Off by default, opt-in per character, and independent of 35–37 above — it
      reads reply text, not the tracked emotion. Does **not** pull TTS (21)
      into this app: the render request carries plain text, and the
      external service is responsible for its own speech synthesis.

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

- [x] **39. Music controls.** A shared library (`data/music/`, alongside
      portraits and avatars) the person picks from by hand beside the
      composer, plus an automatic side: `music_select` (canonical,
      background tier, gated on `on_text` — a new trigger type, a plain
      regex against this turn's user message and reply, cheaper than a
      signal and the thing that actually matters here: did the story say a
      jukebox/radio/stereo turned on, not whether the model scored the
      turn as emotionally intense, which the first version tried and a
      real chat proved wrong — a card's own jukebox, switched on in the
      roleplay, did nothing because nothing about that is an emotional
      shift) proposes a track — never plays it outright. The chat shows an
      `action_card` ("*Mira* wants to play
      *track*.") with **Allow** / **Decline** / **Just roleplay**; this is
      the first real implementation of `PassOutput.type == "action_card"`,
      sketched but unbuilt since §15/DESIGN.md. Allow starts real playback
      and, for the rest of that track, the reply pass is told a song is
      playing (`state.music`, volatile band) so the character can react to
      it; the client's own `ended` report clears that the moment it's
      over. Just roleplay plays nothing but leaves a one-shot narrative
      nudge (`state.music_roleplay`, consumed the same way `random_event`'s
      own intrusion is). One shared library, not per-character — settled on
      request rather than left open. The multi-device case: exactly one
      now-playing state per chat, so whichever device's `ended` fires first
      is what ends it everywhere, the same answer group chats' shared state
      already gives everything else.
- [ ] **40. Time-in-chat timer, per character.** Tracks how long the user has
      actually been engaged with a character, not just how long the tab has
      sat open. Counts while active, then holds for a 5-minute window from
      the last interaction before it stops — scrolling the chat, sending a
      message, regenerating and deleting a message all count as an
      interaction and restart the window, so a phone left open on the chat
      does not run the clock indefinitely. Open questions before this is
      buildable: where the accumulated time is kept and shown (per
      character? per chat, rolled up to the character?), whether it survives
      across sessions and devices, and how to keep the clock itself cheap —
      a live ticking counter is the wrong shape for something that only
      needs to know "was there activity in the last 5 minutes"; more likely
      a single last-interaction timestamp bumped on each interaction, with
      elapsed time computed from it on read rather than polled continuously.
      Recorded on request; not started, not designed.
- [x] **41. Message reactions.** React to one of the character's own
      replies with one of six fixed emoji (heart/laugh/cry/wow/angry/
      thumbs-up) from the message wheel — the `soon: true` placeholder
      that was already sitting there. Setting one launches
      `message_reaction` (canonical, background tier, `Trigger(type=
      "manual")` — never auto-fires, only ever run through
      `PassScheduler.react_to_message`, a sibling of `run_pass_now`
      targeting one specific message instead of "the last one," same
      tracked `pass_runs`/cost-dashboard path either way): a short
      in-character line noticing the reaction, cached on the reacted
      variant (`message_variants.user_reaction`/`reaction_ack`, per
      variant same as `echoes_user` — §9, a reaction binds to the swipe
      you land on) so it's still there on reopening the chat, not a
      toast that only ever fired once. Deliberately a real tracked pass
      rather than an untracked call the way `character_reactions.py`
      gets away with for its own once-ever lines — a per-message
      reaction can fire far more often, so the cost dashboard is the
      right place for it to show up. Clearing a reaction never launches
      the pass — nothing to acknowledge in an un-reaction.
- [x] **42. Suggest edit.** Rewrite a reply per a note about it —
      "make it shorter," "the perspective isn't right" — rather than
      branching from it, from the message wheel's own `soon: true`
      placeholder. Only ever offered on the literal last message in the
      chat (`canSuggestEdit`, app.js, re-checked server-side in
      `PassScheduler._run_suggest_edit`): an edit to an older reply
      would be revising something everything since has already
      answered, which "shorter"/"longer" can't account for. Three
      canned notes (Shorten / Lengthen / Fix grammar & perspective) or a
      free-text one reach the model the same way — the reply-quality
      (`basic`) backend rewrites the whole message from the surrounding
      context plus the existing text and the note, streamed straight
      into the bubble that's already on screen. The same variant keeps
      its id and its swipe position; only its text changes
      (`repo.update_variant_text(..., edited=True)`), same as a
      hand-typed edit — this is just an AI-assisted way of doing that,
      not a new branch to choose between, so there's no state rollback
      the way a swipe needs. The note also outlives the one message it
      was asked on: `repo.set_edit_note` remembers it against the chat
      for `SUGGEST_EDIT_NOTE_TURNS` (3) more turns, and
      `assembly.build_reply_context` injects it as a standing system
      note — same placement reasoning as the author's note (§
      `AuthorsNote`), but fading on its own rather than staying on,
      since "make it shorter" is almost always a complaint about the
      next few replies, not a permanent rule nobody remembers setting.
      A later suggest-edit's note replaces the one before it rather
      than stacking.

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
