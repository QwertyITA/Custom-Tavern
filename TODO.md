# Working checklist

Everything asked for in one batch, in the order it is being done. Ticked items
are on `claude/develop-this-tgypnk` and can be pulled. This file is the record —
if a session is cut off mid-way, the next one starts from the first unticked box.

## Fixes and small changes

- [ ] **1. Thinking: a three-way ON | AUTO | OFF control**, visible rather than
      buried in a select. Default **auto** for every backend kind except
      **horde**, which defaults to off.
- [ ] **2. "Write for me" 2.5× less sensitive** — the pull-up past the end of
      the chat has to travel 2.5× as far before it arms.
- [ ] **3. The hold wheel names what is under the finger.** Hovering *Copy*
      shows "Copy".
- [ ] **4. Bug: a slider under a scrolling finger jumps to it.** Scrolling a
      panel must never change a setting.
- [ ] **5. Bug: the hold wheel misses the option you move to** (~75% of the
      time).
- [ ] **6. Bug: scrolling up with the hold wheel open triggers "write for me".**
- [ ] **7. Place / weather / time as short as possible.** "A tavern" → Tavern.
      "Rain streaking the window" → Rainy. Time as Morning / Afternoon /
      Evening / Night / Dusk / Dawn.
- [ ] **8. Streaming 40% slower**, and the bubble reaches its full width
      quickly, before the text arrives at the right-hand edge.
- [ ] **9. Empty replies from GLM-4.7-flash q4.** The model drops its final
      answer often enough to need handling rather than an error message.

## The brain panel, rebuilt

- [ ] **10. Backends first**, existing ones collapsed.
- [ ] **11. Tiers become "Passes"**, three of them, each expandable, each with
      the settings that belong to it moved inside:
      - **Messages** — writes the reply. Not optional.
      - **Refiner** *(the foreground tier)* — secondary, can be switched off,
        explains what it does for narrative drive.
      - **Secondary info generator** *(the background tier)* — weather, time,
        place, summaries, memory. Can be switched off entirely. Recommended
        only with a second backend or a strong one, since it costs usage.
- [ ] **12. Frequency controls** for the secondary passes — run the world every
      *n* messages, summarise every *m*.
