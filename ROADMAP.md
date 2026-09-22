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
      already gives everything else. Later fixed to run *before* the reply
      instead of after it, whenever the person's own message is what asked
      for music (`PassScheduler._run_music_pick`, called from `_answer`
      right where the web search step already runs pre-reply) — reported
      live: firing only after the fact, as it originally did, meant the
      reply had already gone out generic ("turns on some music") by the
      time a track was even picked, so the character could never once name
      what it had just put on. `pending_music` now feeds the pick straight
      back into that same turn's reply, scoped to it exactly the way
      `search_block` scopes its own results, so it never lingers into a
      later reply that never asked. A reply that brings up music on its
      own initiative has nothing to pre-empt — it still goes through the
      pass the usual way, after the fact. Also fixed: `trigger_fires`
      (scheduler.py) now refuses `music_select` outright — before either
      the pre-reply pick or the ordinary background one ever spends a
      model call — whenever `state.music` already holds "playing" or an
      unanswered "proposed". Reported live: a track picked by hand, then
      "here, listen to this song" in chat, got a *second*, different
      track proposed right over the one just started — the trigger
      pattern has no way to tell "play something" from "listen to what I
      just put on" apart, so this checks the one thing that actually
      does: whether something is already in play.
- [x] **40. Time-in-chat timer, per character.** Tracks how long the user has
      actually been engaged with a character, not just how long the tab has
      sat open. The open questions, answered: time is kept **per chat** and
      rolled up to the character through `chat_members`, so a group chat
      counts in full for each of its members (an hour with three of them is
      an hour with each, and the roster already lists a group chat in every
      member's history for the same reason); it survives across sessions and
      devices because it lives in the database rather than in a tab; and the
      clock is not a clock at all but a table of *sittings*
      (`chat_sessions`, migration 18), each worth its last heartbeat minus
      its first. Nothing anywhere reads the wall clock to decide what a
      sitting is worth, which is the one property that stops an abandoned
      tab accruing until somebody notices — the most an interrupted sitting
      can cost is the single interval between its final two beats, so the
      total under-counts slightly and can never over-count.

      A beat goes out every 20s, and only while the page is **visible** and
      something has been touched in the last **two minutes**. Both halves
      matter and neither is redundant: visibility is what stops a phone in a
      pocket, the idle window is what stops one face-up on a desk, and
      leaving a tab open satisfies neither. The window exists at all because
      reading is engagement — a long reply takes a minute to read, and not
      touching anything while you read it is not absence. Tapping, typing,
      scrolling, dragging and returning to the tab all count as touching;
      a narrower list would have penalised whichever way of using the app
      was left off it. A gap wider than 90s is not inside a sitting, so an
      absence is never counted *and* splits the sitting in two, which is
      what makes the average honest. The client never sends a timestamp —
      one that could name the time could name an afternoon of it.

      Shown where the question gets asked: total beside a character's chat
      count in the roster, each chat's own total on its row in that
      character's history, and total / sittings / average at the head of
      that list. No endpoint of its own — the two lists the roster already
      fetches carry it.
- [x] **40b. The tavern bell.** Optionally rings every 15, 30, 45 or 60
      minutes (Settings → Time in the tavern). Synthesised through the Web
      Audio API rather than shipped as an audio file: a struck bell is two
      decaying sine partials, which is a dozen lines against an asset to
      ship, decode and keep in the repo. Counts rings rather than running a
      timer, so changing the length mid-visit cannot strand one and the
      same stretch can never ring twice.

      Deliberately **not** gated on presence the way the counters in 40
      are — corrected on report, having first been built to share their
      active-time clock. It counts wall-clock time from when the app was
      opened, minimising included: it is a "you have been at this a while"
      nudge, and one that only counted foreground seconds would never
      arrive. An accumulator of active ticks could not have survived it
      either way, since a backgrounded tab's timers are frozen on Android —
      so elapsed is read off the clock and the gap closes itself on the way
      back. The *ring* still waits for a visible page (a frozen tab cannot
      play audio at all), and the count is only advanced when it actually
      sounds — written the other way round first, and coming back found the
      bell already marked rung and said nothing about the hour that passed.
      An hour away is one bell, not four.
- [x] **40c. Testing and replacing the bell sound.** A **Test bell** button
      rings it on demand, whatever the length is set to and even with the
      bell off: the question worth answering is "will I hear this", and a
      quarter of an hour is no way to ask it. **Use my own sound** uploads a
      replacement (`POST /api/bell`, served at `/bell`, 2 MB cap against the
      music library's 20 — this is a doorbell, not a song) with a link back
      to the built-in one. Its own directory holding exactly one file, so
      uploading replaces rather than accumulates and there is no list to
      manage; derived into `settings.bell_sound` from the filesystem rather
      than stored, so nothing can disagree with what is actually on disk.
      Kept out of the music library on purpose: that is the story's
      soundtrack, offered to `music_select` and pickable by hand in a chat.
- [x] **43. Memory quality.** Reported live: a store filling with "the user
      said hello to the character". The old pass did ask for durable facts
      only — and a model asked for facts produces *something*, because
      producing something feels like doing the job. Two things were missing,
      and neither was a better sentence in the prompt.

      **A place to put small talk.** Every candidate now arrives labelled as
      one of six kinds (identity, relationship, commitment, event,
      preference, possession) and rated 1-5, plus a seventh label `chatter`
      that is never stored. Naming the reject bin is most of the fix: it
      gives a model that feels obliged to answer somewhere honest to put a
      greeting instead of dressing one up as a fact.

      **A threshold it cannot argue with.** The floor is applied in
      `memory.store`, never asked for in the prompt — a model told in prose
      to hold a bar will rationalise its way under it. Chatter, anything
      rated 1, and anything handed over unrated are all dropped on the
      floor; the rating is the contract, and a reply that skips it skipped
      the thinking. A memory typed in by hand is never rated and never
      gated: that judgement has already been made by someone who counts.

      **Evidence, not guesswork.** Retrieval records what it actually used
      (`uses`, `last_used_turn`), which is the only honest signal this
      system can collect about whether a memory earned its place — and among
      memories that match, importance now breaks the tie, so junk sharing
      one word stops crowding out the fact that matters.
- [x] **43b. Memory compression.** A second canonical pass
      (`memory_compress`) that re-reads the whole store and merges, rewords
      and deletes — the thing plain dedupe can never do, since two memories
      drifting towards the same claim, or a later fact quietly contradicting
      an earlier one, are both invisible to a similarity threshold.

      Fired on evidence rather than on a timer: at least `COMPRESS_AT` (30)
      memories *and* at least `COMPRESS_EVERY` (12) added since it last
      looked. Both, never either — re-reading an unchanged store is paying
      to be told the same thing twice, and a dozen new facts in a small
      store is not yet a mess, so a quiet character never pays for it.
      Also on demand from the panel's Tidy button
      (`POST /api/characters/{id}/memories/tidy`), awaited there rather than
      fired and forgotten, since the caller is redrawing the list it
      rewrites.

      Three rules the model does not get a vote on, in
      `memory.apply_compression`: it may never touch a memory someone wrote
      or edited by hand (editing marks it yours — § `memory.update`); a plan
      that would empty the store is refused outright; and anything the plan
      does not mention is kept, because deleting by omission is the worst
      failure available here. The trade on the first rule is deliberate and
      visible: a hand-written memory a later fact contradicts stays until
      you resolve it yourself.
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
- [x] **43. Separate paragraphs.** A reply with more than one paragraph
      renders as more than one bubble, one per paragraph, each appearing
      a beat after the last instead of all at once — like a burst of
      short texts rather than one long one. Client-side only, same as
      `realistic_chat_speed` right above it in Settings: the split and
      the pacing both happen in `static/app.js`, never in what is
      generated or stored — `splitParagraphs` on blank-line boundaries,
      a reveal loop in `runStream` that reads `target.text` (never
      writes it, so it can't desync the pacer) and advances
      `streamingParagraphsShown` once every `paragraphPauseMs()` for as
      long as more paragraphs exist than are currently shown. Realistic
      chat speed being on stretches that pause the same way it already
      stretches the reply's own pacing, rather than the two reasoning
      about time independently. The outer `.bubble` stops drawing its
      own shape when this is on (`.bubble.split`) and each `.para-bubble`
      draws its own instead — the wheel, the marks and the reaction
      corner all still anchor to the one unchanged `.bubble` element, so
      continue/swipe/edit/react/suggest-edit and everything else keep
      treating the reply as the one message it has always been in the
      data model; only how it is shown changes.
- [x] **44. Group chats, started as one.** A new button beside the
      roster's "Characters" heading picks at least two characters up
      front and starts one chat with all of them in it from the first
      line — the other way in, growing a solo chat into a group by
      adding members one at a time from "Who is here" mid-conversation
      (§ groups.add_member), still works exactly as before. `POST
      /api/chats/group` is a sibling of the solo `POST /api/chats`:
      the first character id is who the chat is created from (their
      greeting plays, same as a solo chat's own character_id), the
      rest join as `chat_members` before anyone has said a word.
      `repo.list_chats` now joins against `chat_members` instead of
      filtering on the chat's own `character_id` column, so a group
      chat turns up in *every* participant's own "Recent chats" — not
      only the one it happened to be created from — each carrying a
      `member_ids` list and an `is_group` flag the roster draws a
      small people-icon badge from.
- [x] **45. Music: ask instead of going quiet.** When nothing in the
      library fits (`music_select` would have picked "none"), the
      character asks the person to add a track instead — one short
      line in their own voice and personality, never a canned prompt.
      Never silent either way: the prompt leans toward picking a real
      track over "none" in the first place (an imperfect fit beats
      nothing), and the moment it does answer "none" it must also
      supply the "ask" line — a model that ignores that instruction
      still gets a generic fallback line here rather than the feature
      quietly doing nothing (`_MUSIC_ASK_FALLBACKS`, scheduler.py). The
      trigger itself widened for the same reason, reported against a
      real chat: "listen to" was missing from `music_select`'s on_text
      verb list entirely, so a dozen turns of talking about specific
      songs never once fired it — share/recommend/suggest joined it too.
      It's a real chat message, not a floating card: `music_ask`
      (`message_variants`, migration 17) marks it 'pending', and the
      message itself carries the ask so declining or scrolling past it
      reads like any other line. Saying no withdraws the ask and always
      plays something anyway — a random pick from the same allowed
      list, not a second model judgment call repeating the first one
      that already said nothing fit. Saying yes flips the mark to
      'awaiting_upload' and an upload button appears in its place,
      reusing the existing manual `POST /api/music`; the upload
      resolves the mark and starts the new track playing in one
      follow-through call. No separate "dismiss" affordance — deleting
      the message is how the offer withdraws, since `music_ask` lives
      on that message's own row and cascades away with it. A `/music`
      command forces the same on-demand path background/expression
      already use, for testing this without waiting on the trigger.
- [x] **46. Music: an artist field, next to the title.** Each track in
      the library now has its own `artist`, alongside the existing
      `label`/description — a new input in the Music panel, same raw-
      value-binds-the-input shape the title field already uses (no
      filename-style fallback to snap back to, so no placeholder trick
      needed here). Never shown in the title's own editable field, only
      in text a person or the model actually *reads*: the proposal
      card, the currently-playing bar, and the "Currently playing: …"
      prompt line all now read `config.music_display`/`musicDisplay`
      (title, plus " — Artist" when one is set) instead of the title
      alone. `music_select`'s own track list also passes the artist to
      the model alongside the description, so it can be part of the
      pick, not just the write-up afterward.
- [x] **47. A homepage.** Every launch lands here now, not silently
      resuming whatever chat was last open (§ showHome, app.js) — New
      chat and New group chat pickers, a compact backend-per-tier
      assignment, quick on/off for Post-process and the Secondary info
      generator (with Memory extraction folded under the latter), and a
      shortcut straight to Brain's Presets tab, all one tap deep. A
      "Continue" card sits above the two New buttons when there's
      actually somewhere to resume, so the convenience of auto-resume
      isn't lost, just no longer silent. Reuses rather than reimplements
      wherever it can: the New chat/New group chat pickers and their
      state are the same ones the roster's own buttons already drove,
      and the tier-assignment dropdowns read the same `tier_groups`/
      `backends` Brain's own Backends tab does. The one new thing is
      immediate persistence — everywhere else in the app a settings
      change previews live and waits for a Save button, but the
      homepage has none of its own, so its toggles and dropdowns save
      the instant they're touched instead.
- [x] **48. A way into a group chat's own settings.** Reported live as
      missing: no way to turn a character in the current group on or
      off, or add and remove them. Every one of those controls already
      existed and had since 8 — mute, remove, add, how readily each
      speaks up, whose turn it is — all of it in Story under *Who is
      here*, which is ☰ → Story and then a scroll past Quick options
      and the whole toggle list. A control nobody can find is missing,
      so what was actually built is a second door and not a second
      implementation: a people button in the header itself, carrying
      the number of people in the room who are not muted, which opens
      Story and scrolls that section under the thumb with the heading
      briefly marked so the eye lands on it rather than on wherever the
      panel stopped. Same reasoning and same place as the
      Brain/Theme/Settings buttons promoted into the header on the
      homepage (§47). Only shown with more than one person in the room:
      a solo chat has nothing here to change, and a button opening a
      list of one is worse than no button.
- [x] **49. The wrong face against a message.** Reported live. Three
      separate causes, all of them the same mistake — asking the chat's
      *membership* a question it does not answer. Membership is who can
      speak next; a transcript needs who already did.
      (a) `repo.add_message` returned the one message object in the app
      with no `speaker_id` on it, so the `reply` event the client swaps
      its streaming bubble for carried no speaker and the finished reply
      fell back to the chat's nominal character — right face while it
      streamed, wrong face the moment it landed, correct again only
      after reopening the chat. Stored correctly the whole time, simply
      not reported.
      (b) `turn_start` has always carried the answering character's id
      "so the placeholder can carry their name and portrait instead of
      the chat's nominal character" — the client read only the name, so
      the bubble streamed under the wrong face for the whole generation.
      (c) The faces, frames, colour treatments and speaker labels were
      all looked up in `cast`, which holds current members only. Remove
      somebody from a group and every line they ever said lost its face
      and its name to whoever was left; shrink the group back to one and
      every line in the chat did. A new `groups.voices` serves everyone
      with a line in the transcript, and an unknown speaker now draws
      the blank placeholder rather than borrowing a face.
- [x] **50. "Next turn, X answers."** The server has always honoured a
      named speaker under any policy (`groups.choose_speaker`'s
      `forced`), but the only control for it was bound to the "you
      choose" policy — using it once meant switching the whole chat over
      and then being asked every single turn thereafter. The row above
      the composer is now offered under every policy: required under
      "you choose" as before, an optional one-turn override otherwise,
      spent as the message goes so it never quietly becomes a rule.
      Which also exposed that `.composer` never wrapped, so its two
      full-width rows — this one and the staged attachments — were being
      squeezed onto the text box's own line.
- [x] **51. Not your words back.** Reported live: replies that hand your
      own message back, quoted or reworded, before answering it. Two
      lines about this already existed, buried inside *Their turn is
      theirs* under a label about not acting for the user, where nobody
      would find them to strengthen or switch them off. Its own section
      now, and a fuller one — the ban, what to write instead, and a test
      the model can apply to its own sentence. Adapted from the same
      preset family the rest of the writing library came from (§
      prompt_layout's own header). `postprocess.find_echoed_phrase`
      already measured the same thing from the other end and marked the
      variant; there is now an instruction against it and a reading of
      whether it worked.
- [x] **52. A group chat that reads as a room.** Reported live, and
      bluntly: unusable. Three complaints, one shape — something was
      asking the *chat* a question only a message or a member could
      answer. Researched against SillyTavern's own group code
      (`public/scripts/group-chats.js`, `formatMessageHistoryItem` and
      openai.js's `names_behavior`) and driven end to end through the
      Horde provider against a local stand-in for it, since
      aihorde.net is blocked from the machine this was built on.
      (a) **The model could not tell the cast apart.** Every character's
      reply went into the prompt as an unlabelled `assistant` turn, so a
      four-way conversation arrived as one undivided voice and came back
      as one: the cast mixed into a single person, questions answered
      that had been put to somebody else, a line contradicted two
      messages after it was written. Every message is labelled with its
      speaker now, your own included. With labels come the three things
      labels need — the other members' descriptions in the prompt (how
      much is a setting: names, a few lines, or the whole card), a
      volatile `## Your turn` block pinned to the end saying whose line
      this is, stop sequences at `\nOther:`, and a postprocessor that
      takes a self-written name label back off and cuts a reply that
      carried on into somebody else's.
      (b) **A turn was exactly one reply.** Nobody could react to what
      anybody else had just said, and naming two people got you neither:
      `addressed` returned None and it fell through to a weighted guess.
      `groups.plan` returns a list now — everyone named, in the order
      they were named, then whoever rolls against their own
      talkativeness, capped by the chat's own "how many answer" setting
      (four at most, two by default; every extra reply is another whole
      generation). Each reply assembles after the previous one is
      stored, which is what makes the second character an answer to the
      first. A new *everyone gets a turn* policy is SillyTavern's pooled
      order. Talkativeness became a chance of speaking up rather than a
      share of one contested slot, and the 0.25 last-speaker penalty
      became a real ban, liftable in settings and always lifted by
      naming them — a weight is not a rule, and a group of three
      regularly read as one person talking to themselves.
      (c) **Re-rolling a group reply answered as the wrong character.**
      `_run_swipe`, `_run_continue`, `_run_suggest_edit` and `reaudit`
      all resolved the chat's *nominal* character, so re-rolling
      Harrow's line rewrote it as Mira, in Mira's voice, against Mira's
      state schema, storing Mira's state writes for it. The same
      mistake 49 found in the transcript's faces, one layer down.
      Impersonate and a hand-run pass read the last voice instead, and a
      reaction reads the speaker of the message it is about. The swipe
      prompt now also stops at the message being re-rolled, since a turn
      can hold a later reply that answers it.
      And the settings themselves, which is where the report started:
      the controls moved out of the Story panel into a sheet of their
      own that the people button opens directly, with the three new ones
      beside the old. 48's door opened a panel and scrolled to a heading
      buried under the whole toggle list; this is the room, not a second
      door onto somewhere else.
- [x] **53. An empty box, and a row that stays out of the way.** Two
      reports about the same strip of screen. The *Next turn* row sat
      over the text box permanently, which reads as a decision waiting
      to be made on every single turn when it is really a one-turn
      override — it is opened from **+ → Who answers next** now and
      closes itself the moment somebody is picked, except under *You
      choose*, where it is the policy rather than an override and
      picking is required before a message can go at all. And an empty
      message box in a group greyed the send button out, which is the
      wrong answer to "I want to hear what they say to each other": the
      button is a **let them carry on** in that state, running a turn
      that answers nothing (`POST /api/chats/{id}/proceed`,
      `scheduler._run_proceed`) with the same planner, the same
      settings and the same per-chat lock as any other. Nothing of
      yours is stored for it — the whole point is not having to put
      words in the scene that were only ever there to ask for the next
      line — so it takes a turn number of its own and its `turn_resume`
      carries no message at all. Three states on one button, decided in
      one place (`sendMode`), because the bug being fixed was its icon,
      its label, its enabled state and its tap disagreeing.
      Which turned up the real reason the reported example — *hello*
      answered by two characters — could not happen: 52's self-response
      ban was unconditional, where SillyTavern's is `!isUserInput &&`
      (activateNaturalOrder). A blanket ban makes a room of two
      alternate strictly for ever, which is the round-robin mechanism
      the default policy exists to avoid. It is gated now: you spoke, so
      anybody may answer you; nobody spoke and the room is carrying on
      by itself, so whoever just finished sits out — which is where the
      rule was always earning its keep.
- [x] **54. "The tavern's server is not answering."** Reported live,
      after about a minute in a chat, with the reporter's own read
      that something was slowing it down. The server was answering. It
      was waiting for a worker.
      `_stream` had no keepalive. The ambient bus has pinged every 20
      seconds since it was written, with a comment saying it "keeps the
      connection from idling out" — the *turn* stream, which idles far
      longer because it is silent for the whole time the backend is
      thinking (on the Horde, a queue wait in minutes), had none at
      all. A phone drops an idle connection long before that; the fetch
      then rejects with a bare TypeError, and `errorText` reports the
      one thing that had not happened. Every stream pings at 15s now,
      and closes the generator it was reading when the reader hangs up
      — what was being awaited there is a whole turn.
      Measured rather than guessed at, which is how the second half
      turned up: a chat of 400 messages driven in a real browser for a
      minute showed nothing at all (no long tasks, no leak, no failed
      requests), so the cost had to be in a turn. Against a stand-in
      Horde with a six-second worker, one message in a group of three
      cost **7 backend calls with one speaker and 13 with two**, four
      of them in flight at once — because 52 launched each speaker's
      background passes as soon as their reply landed, so the first
      speaker's passes were competing with the *second speaker's reply*
      for the backend. That reply is the one thing the person is
      actually sitting there waiting for. Background work is background
      by definition: it waits for the turn's last reply now. Same
      passes, same per-character split (§15), none of it dropped —
      simply not in front of a blocking generation any more.
- [x] **55. A glance is not a sitting.** Reported live: the average
      sitting had collapsed. Every chat from before roadmap 40 has no
      time saved, and the heartbeat samples every 20 seconds (§ app.js
      PRESENCE_BEAT_MS) — so opening a few old chats to see what was in
      them wrote one row apiece whose `started_at` and `last_seen_at`
      were the same instant. Zero seconds, counted as a sitting: one
      more in the denominator, nothing in the total. Eight glances
      turned an hour over one real sitting into an average of three
      minutes. Zero-length rows are now left out of the count and the
      average, which leaves the totals untouched (a zero-length row
      adds zero seconds) and makes a glanced chat absent rather than
      present with a zero — "0s over 4 sittings" is two wrong numbers
      where nothing at all is the honest answer. Filtered when read
      rather than refused when written, for two reasons: the row is
      what the next beat extends (§ mark_active), so a glance that
      turns into a real visit still counts from where it started; and
      filtering at read time fixes the databases that already have
      these rows without a migration.
- [x] **56. The shell arrives compressed.** Reported as a slow boot
      against how it felt two days earlier. Measured first, on a
      phone-sized install (20 characters, 80 chats, 2.7k messages) in a
      real browser at 6x CPU throttle, against the exact commit that
      "two days ago" means: **no regression to find** — boot to roster
      1302ms then against 1397ms now, opening a chat 2367ms then
      against 1927ms now, the shell 2% bigger, the server's own paths
      3-9ms, and a minute sitting idle in a 400-message chat with no
      long task, no leak and no failed request.
      What the measuring did find is real and was always there. The
      service worker is network-first on purpose (cache-first meant
      every update landed a reload late), so **the first boot after a
      `git pull` re-fetches the whole shell** — 928 KB of HTML, CSS and
      JS, uncompressed, which is the boot a person notices and the one
      that had been happening several times a day while this work
      landed. It gzips to 263 KB. Scoped by path to the shell rather
      than added app-wide: `/api/**` is where the SSE lives, and a
      compressor that buffered a turn stream would undo 54's keepalive
      by holding back the very bytes that prove the connection is
      alive. Steady-state boots were already free on the wire (ETag,
      304) and are dominated by parsing the shell, not fetching it —
      which is the next thing to go at, and a different job.

- [x] **57. One prompt for the room.** Asked live, and correctly:
      "does the group chat change the prompt at the very beginning? I
      feel like it re-caches everything then answers." It did.
      §7.1's cache rule puts volatile content last so the stable prefix
      survives between turns — but in a group the prefix is rebuilt for
      whoever is speaking, and it opened with *You are Mira*. Measured
      on a room of two with a thirty-message history: Mira's prompt and
      Harrow's shared **8 characters out of 9,114**, so a backend whose
      KV cache is a prefix match could reuse nothing, and two
      characters taking turns re-read the card, all fourteen writing
      blocks and the whole transcript on every single reply — about
      3,800 tokens of prefill per turn, bought back for nothing.
      SillyTavern's answer is its APPEND generation mode
      (`getGroupCharacterCardsLazy`), and this is that: a per-chat
      **Whose card goes in the prompt** setting. *Everyone's, every
      time* joins every member's description, scenario, examples and
      constant lore in join order, so the prompt comes out
      byte-identical whoever is about to speak; the main instruction
      names nobody, and the only thing saying whose turn it is stays in
      the volatile band at the very end (§ turn_note — the same job
      ST's trailing `Name:` does). *Only whoever is speaking* is the
      old arrangement, kept because which one wins depends on the
      backend: joining sends every card every turn, and on the Horde,
      where each reply lands on a different worker and no cache
      survives between them, that is simply a bigger prompt.
      A room of three, forty messages: 4,334 tokens with 2 shared,
      against 4,455 with **4,088 shared** — 121 more tokens in the
      prompt to turn a 4,332-token re-read into a 367-token one.
      Two things fell out of doing it properly. The cast note is gone
      in a joined room, because everyone is already described in full
      and it was the one block left that still differed per speaker.
      And constant lorebook entries became the *room's* rather than the
      speaker's — one member carrying a world entry and another not was
      enough to split the prompt again a hundred tokens in, and ST
      reads every group member's book in a group chat for the same
      reason. A scenario shared by the whole room is also written once
      rather than once per member, which ST does not do.

- [x] **58. The typing cue's face answers a tap.** Reported live: with
      the cue on screen its portrait could not be opened. It never
      could — it was the one assistant row in the app whose face was
      decoration, with no handler, no role and no keys. That was
      invisible for as long as it drew the chat's nominal character and
      obvious the moment 53 made it carry whoever is actually about to
      answer, because then it looks exactly like the faces above it and
      behaved differently. Same handlers, same keys, same enlargement
      and same fill-the-screen button as any row in the transcript, off
      the stand-in row `cueRow` already has. The enlargement clears
      when the cue moves on — at the top of a turn and again for each
      later speaker of one — since a face blown up for Mira is not an
      instruction about Harrow, and a new turn should not open with a
      portrait already filling the column. Checked in a real browser at
      phone width: 34px to 148px, the right speaker's picture, and the
      second tap puts it back.
- [x] **59. What the screen does while a reply arrives.** Two
      reports, both about being shown the wrong thing during a stream.
      **The view followed the bottom for the whole generation**, so a
      long reply dragged the reader down a line at a time and the only
      thing on screen was the last line and the cursor — you could not
      read the reply until it had stopped being written. It follows for
      the first six lines now and then lets go, into exactly the state
      scrolling up already produces (`stick` false), so the
      scroll-to-bottom button is the way back and nothing new had to be
      invented. The budget is how far *this reply* pushed the bottom
      down rather than how tall the row is, because a regeneration
      streams into a bubble that is already several lines tall.
      **And the bubble was the wrong width, twice over.** It was pinned
      to the full column from its first token, so a short answer
      streamed in a box a third too big and snapped shut when it landed
      — 368px while streaming against 284px after, measured on real
      rows in a browser. The pin only goes on once the reply actually
      needs a second line now; before that there is one line, and a
      single line getting longer re-wraps nothing. The pin itself was
      also wrong: `min-width: var(--bubble-max)` is a percentage of the
      *row*, while the bubble only gets the part of the flex line the
      portrait and the gap leave — so a wrapped reply asked for 368px
      of a 350px space, overflowed its own row and shrank back at the
      end. It grows into the line instead, which is exactly where a
      wrapped bubble settles: 350px throughout, no snap at all.
      Fixed along the way: the keepalive teardown from 54 called
      `aclose()` on a generator whose own `__anext__` was still in
      flight, which is the one thing an async generator refuses to do.
      Every phone that walked away mid-reply left a RuntimeError in the
      log, and driven directly it hung. Cancelling the call already
      stops the turn; that is the whole of it.

- [x] **60. Editing a message and enlarging its portrait, together.**
      Reported live: "even when editing a message and the pfp is
      expanded, things get a bit messed up." Measured in a real
      browser: tapping a portrait to enlarge it *while* that message
      was being edited left the edit box rendered past the edge of the
      screen — 438px on a 412px viewport, clipped rather than scrolled
      to. Two features were pinning the same bubble to two different
      widths at once. `startEdit` reads the bubble's rendered width and
      pins it there with an inline `min-width`, so the edit box
      (`width: 100%`) does not collapse when it replaces the message
      body. An enlarged portrait narrows that same bubble by CSS. A
      `min-width` wider than a narrower `max-width` wins outright, so
      the bubble refused to narrow, and the portrait's 148px plus the
      still-full-width bubble no longer fit the row between them.
      The two states are exclusive for one row now: enlarging a
      portrait is refused for whichever message is being edited (and
      stops presenting as tappable, so the row doesn't lie about what a
      tap will do), and starting to edit a message whose portrait is
      already enlarged shrinks it first. The shrink took two more finds
      to get right, both from measuring real rows rather than trusting
      the CSS: it is a transition, not instant, so reading the width
      right away pinned the box to a mid-animation value — and it
      turned out to be *two* transitions, the bubble's own narrowing
      and the portrait's own width, both needing to be turned off for
      the one frame the reflow takes, because the row is a flex line
      and the bubble's available space depends on how much room the
      picture has actually given back, not on the bubble's own
      max-width in isolation. Missing the second one alone left the box
      pinned at 236px instead of the 350px it should have settled at.

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
