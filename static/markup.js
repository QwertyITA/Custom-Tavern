// Inline markup tokenizer (§8) — behavioural mirror of app/markup.py.
// tests/fixtures/markup_cases.json is the shared contract between the two.

(function (root) {
  "use strict";

  const DIALOGUE = "dialogue";
  const ACTION = "action";
  const STRONG = "strong";
  const STYLE_ORDER = [DIALOGUE, ACTION, STRONG];

  const PARAGRAPH_BREAK = /\n[ \t]*\n/g;

  function paragraphs(text) {
    const spans = [];
    let cursor = 0;
    PARAGRAPH_BREAK.lastIndex = 0;
    let match;
    while ((match = PARAGRAPH_BREAK.exec(text)) !== null) {
      spans.push([cursor, match.index]);
      cursor = match.index + match[0].length;
    }
    spans.push([cursor, text.length]);
    return spans.filter(([a, b]) => b > a);
  }

  function isSpace(ch) {
    return ch !== "" && /\s/.test(ch);
  }

  function flanking(text, start, end) {
    const prev = start > 0 ? text[start - 1] : "";
    const next = end < text.length ? text[end] : "";
    return [next !== "" && !isSpace(next), prev !== "" && !isSpace(prev)];
  }

  function lex(text, start, end) {
    const markers = [];
    let i = start;
    while (i < end) {
      const ch = text[i];
      if (ch === "*") {
        let runEnd = i;
        while (runEnd < end && text[runEnd] === "*") runEnd += 1;
        let count = runEnd - i;
        const [canOpen, canClose] = flanking(text, i, runEnd);
        let cursor = i;
        while (count >= 2) {
          markers.push({ style: STRONG, start: cursor, end: cursor + 2, canOpen, canClose, keep: false });
          cursor += 2;
          count -= 2;
        }
        if (count === 1) {
          markers.push({ style: ACTION, start: cursor, end: cursor + 1, canOpen, canClose, keep: false });
        }
        i = runEnd;
        continue;
      }
      if (ch === '"' || ch === "“" || ch === "”") {
        let [canOpen, canClose] = flanking(text, i, i + 1);
        if (ch === "“") { canOpen = true; canClose = false; }
        else if (ch === "”") { canOpen = false; canClose = true; }
        markers.push({ style: DIALOGUE, start: i, end: i + 1, canOpen, canClose, keep: true });
        i += 1;
        continue;
      }
      i += 1;
    }
    return markers;
  }

  function pair(markers) {
    const stacks = { [DIALOGUE]: [], [ACTION]: [], [STRONG]: [] };
    const spans = [];
    for (const marker of markers) {
      const stack = stacks[marker.style];
      if (marker.canClose && stack.length) {
        const opener = stack.pop();
        if (marker.keep) {
          spans.push({ style: marker.style, start: opener.start, end: marker.end, consumed: [] });
        } else {
          spans.push({ style: marker.style, start: opener.end, end: marker.start, consumed: [opener, marker] });
        }
      } else if (marker.canOpen) {
        stack.push(marker);
      }
    }
    return spans;
  }

  function parse(text) {
    if (!text) return [];
    const length = text.length;
    const styles = new Array(length);
    for (let i = 0; i < length; i += 1) styles[i] = new Set();
    const hidden = new Array(length).fill(false);

    for (const [paraStart, paraEnd] of paragraphs(text)) {
      for (const span of pair(lex(text, paraStart, paraEnd))) {
        for (let i = Math.max(0, span.start); i < Math.min(length, span.end); i += 1) {
          styles[i].add(span.style);
        }
        for (const marker of span.consumed) {
          for (let i = marker.start; i < marker.end; i += 1) hidden[i] = true;
        }
      }
    }

    const runs = [];
    const flush = (buffer, key) => {
      if (buffer.length) runs.push({ text: buffer.join(""), styles: key ? key.split(",") : [] });
    };
    let buffer = [];
    let current = null;
    for (let i = 0; i < length; i += 1) {
      if (hidden[i]) continue;
      const key = STYLE_ORDER.filter((s) => styles[i].has(s)).join(",");
      if (current === null || key !== current) {
        flush(buffer, current);
        buffer = [];
        current = key;
      }
      buffer.push(text[i]);
    }
    flush(buffer, current);
    return runs.filter((r) => r.text.length > 0);
  }

  // Renders runs into a container as styled spans. Text is set via textContent,
  // so model output can never inject markup into the page.
  //
  // `revealFrom` is a character offset: everything at or past it is text that
  // arrived since the last render, and gets `mk-new` so it can fade in. -1
  // means "all of this is settled", which is the case for every finished
  // message and for the first paint of a streaming one.
  //
  // A run can straddle the boundary — the tail of a sentence arriving inside
  // dialogue that started three frames ago — so the run containing it is split
  // rather than classed one way or the other.
  function appendRuns(container, text, offset, revealFrom) {
    let at = offset;
    for (const run of parse(text)) {
      const styles = ["run"].concat(run.styles.map((s) => "mk-" + s));
      const end = at + run.text.length;
      const pieces =
        revealFrom > at && revealFrom < end
          ? [[run.text.slice(0, revealFrom - at), false],
             [run.text.slice(revealFrom - at), true]]
          : [[run.text, revealFrom >= 0 && at >= revealFrom]];
      for (const [piece, fresh] of pieces) {
        if (!piece) continue;
        const span = document.createElement("span");
        span.className = fresh ? styles.concat("mk-new").join(" ") : styles.join(" ");
        span.textContent = piece;
        container.appendChild(span);
      }
      at = end;
    }
  }

  // Everything before the last paragraph break can never change again.
  //
  // Markers only pair *inside* a paragraph (see `paragraphs` above), so no
  // token that arrives later can restyle a paragraph that is already closed —
  // a stray asterisk three paragraphs up stays a literal asterisk however the
  // reply ends. That is what bounds the damage a stray marker can do, and it
  // is also what makes the settled prefix below safe to leave untouched: nothing
  // arriving after it can change how it should have been drawn.
  //
  // Returns the offset just past the last separator, in RAW text — the same
  // coordinate space `text` itself is in, unlike a run offset (see below).
  const LAST_BREAK = /[\s\S]*\n[ \t]*\n/;
  function settledUpTo(text) {
    const match = LAST_BREAK.exec(text);
    return match ? match[0].length : 0;
  }

  // What each container is currently showing, keyed by RAW offset — never by a
  // run offset. `parse()` consumes markup characters that carry no meaning
  // (asterisks; kept quotes still count themselves), so a run's `text.length`
  // is shorter than the source span it came from wherever markup was stripped.
  // Walking `at` by run length and then using it to slice the *source* string
  // is exactly the bug this had on the first pass: two stripped asterisks put
  // every offset after them two characters short, which sliced into the
  // middle of the following paragraph break and rendered its tail twice.
  // `settledUpTo` sidesteps this because it is a plain regex over the raw
  // string and never touches a run.
  const settled = new WeakMap();

  function render(container, text, revealFrom = -1) {
    const boundary = settledUpTo(text);
    const prior = settled.get(container);
    // A genuine continuation of what this container showed last time: the
    // settled prefix is unchanged, so the DOM built for it still applies and
    // does not need to be touched, let alone rebuilt.
    const reusable =
      prior && prior.boundary === boundary && prior.text === text.slice(0, boundary)
        && container.childNodes.length >= prior.nodes;

    if (reusable) {
      while (container.childNodes.length > prior.nodes) {
        container.removeChild(container.lastChild);
      }
    } else {
      // Either the boundary just advanced (a paragraph closed since the last
      // render — happens once per paragraph, not once per token) or this is
      // not a continuation at all (an edit, a swipe, a container Alpine handed
      // to a different message). Either way the settled prefix is rebuilt once
      // from nothing; nothing here runs again until the *next* paragraph closes.
      container.textContent = "";
      appendRuns(container, text.slice(0, boundary), 0, revealFrom);
    }

    const settledNodes = container.childNodes.length;
    // The open paragraph: the only part that can still change, so the only
    // part rebuilt every frame. Bounded by paragraph length rather than by
    // the whole reply, which is the actual fix — a stray marker three
    // paragraphs back cost the same either way; a thousand-word reply no
    // longer does.
    appendRuns(container, text.slice(boundary), boundary, revealFrom);

    settled.set(container, { boundary, text: text.slice(0, boundary), nodes: settledNodes });
    return container;
  }

  // Rendering used to rebuild the whole subtree, which is right for a finished
  // message and quadratic for a streaming one: a token arrives, the entire
  // reply so far is re-parsed and re-created, and the reply keeps growing.
  // Measured streaming an 800-word reply, same browser, same steps: 4.8s
  // total and 6.7ms for the last frame against 137ms and 0.1ms now — the
  // difference between a phone dropping frames through the second half of
  // every long reply and not. Only the unfinished paragraph is redrawn.
  //
  // Coalescing to one render per frame is the other half, and is both cheaper
  // and indistinguishable: tokens arrive faster than frames, and nothing can
  // be seen between them anyway.
  //
  // The first render of an empty container is done immediately: deferring it
  // would leave a message blank for a frame, and a message appearing empty and
  // then filling in is exactly the flicker this is meant to avoid.
  const pending = new WeakMap();
  // What this container last showed, so an append can be told from a rewrite.
  const shown = new WeakMap();

  // Where the new text starts, or -1 if this is not an append.
  //
  // The test is `startsWith` rather than a length comparison, which matters
  // because containers are reused: Alpine hands the same element to a
  // different message when the list re-renders, and an edit or a swipe
  // replaces the text wholesale. Only text that literally continues what is
  // already on screen counts as having arrived — everything else is a rewrite
  // and should appear without a flourish, because nothing is streaming.
  function appendedAt(container, text) {
    const before = shown.get(container);
    shown.set(container, text);
    // An empty previous value counts: a streaming bubble is painted empty
    // first, so the very first tokens append onto "" and are exactly the ones
    // that should be seen arriving.
    return typeof before === "string" && text.startsWith(before)
      && text.length > before.length
      ? before.length
      : -1;
  }

  function schedule(container, text) {
    if (!container.firstChild) {
      shown.set(container, text);
      return render(container, text);
    }
    const queued = pending.get(container);
    pending.set(container, { text });
    if (queued) return container;      // a frame is already booked
    requestAnimationFrame(() => {
      const latest = pending.get(container);
      pending.delete(container);
      if (latest) render(container, latest.text, appendedAt(container, latest.text));
    });
    return container;
  }

  const api = { parse, render, schedule, DIALOGUE, ACTION, STRONG };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Markup = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
