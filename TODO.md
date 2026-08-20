# Working checklist

Reported in one batch, in the order they are being done. Ticked items are on
`claude/develop-this-tgypnk` and can be pulled.

- [x] **1. Slider under a scrolling finger still moves.** The first attempt
      relied on `preventDefault` on pointerdown, which a range input on Android
      ignores. Scroll must never move a slider; moving a slider must never
      scroll.
- [x] **2. The backend editor closes while you type in it**, and a newly added
      backend should open by itself.
- [x] **3. The second pencil** in the message tools does nothing. Remove it.
- [x] **4. An empty band appears under the composer** when the keyboard opens.
- [x] **5. World info reads as nonsense** — "Room by a". The place shortener
      cuts at three words rather than at the end of the phrase, and the prompt
      needs to ask for one word per field.
- [x] **6. Theme presets do not change the text colour** — pink text on the
      yellow preset.
- [x] **7. The model field is a text box.** It should be a list of what the
      backend actually serves.
- [x] **8. No way to give a character a picture.** Upload one.
- [x] **9. The picture belongs to the left of every one of their messages**,
      with a generic one when there is none.
