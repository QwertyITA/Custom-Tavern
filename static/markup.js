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
  function render(container, text) {
    container.textContent = "";
    for (const run of parse(text)) {
      const span = document.createElement("span");
      span.className = ["run"].concat(run.styles.map((s) => "mk-" + s)).join(" ");
      span.textContent = run.text;
      container.appendChild(span);
    }
    return container;
  }

  const api = { parse, render, DIALOGUE, ACTION, STRONG };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Markup = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
