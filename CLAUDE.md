# Project instructions

## This repository is PUBLIC — never commit a credential

Everything pushed here is world-readable, immediately and permanently. A key
committed and then removed in a later commit is **still leaked**: it stays in
the history, in forks, and in GitHub's event feed, which is scraped
continuously. Deleting it afterwards is not a fix; rotating it is.

**Never write a real credential into any tracked file.** That includes API
keys, tokens, passwords, private keys, cookies, session ids, and URLs with
credentials embedded (`user:pass@host` style).

Real credentials belong in **`data/settings.json`**, which is gitignored.
`data/settings.example.json` is the tracked template and must only ever contain
empty strings or obvious placeholders. The one exception already in the repo is
`"api_key": "0000000000"` — the AI Horde anonymous key, which is published in
their documentation and is not a secret.

Also never:

- put a credential in a commit message, a branch name, or a PR description;
- paste a key into a test fixture, a docstring, or an example in the README —
  use `sk-EXAMPLE...` or `<your-key>` shapes that the guard recognises;
- add a real `base_url` that embeds a token in the path or query string;
- `git add -f` anything matched by the secrets block in `.gitignore`.

If a credential does reach a commit, **rotate the key first**, then worry about
the history. Tell the user immediately and plainly.

### The guard

`.githooks/pre-commit` blocks commits containing credential-shaped content. It
is enabled automatically by `start.sh`, or by hand:

```bash
git config core.hooksPath .githooks
```

It checks two things: paths that must never be tracked (`data/settings.json`,
`.env`, `*.pem`, `id_rsa*`, …), and secret-shaped strings in the staged diff.
Placeholders, empty values and all-zero values pass. `--no-verify` bypasses it
— only use that after confirming the match is genuinely false, and never to
push a real key.

`tests/test_secrets.py` guards the ignore rules themselves, so the protection
cannot regress silently.

## What this project is

A phone-hosted roleplay frontend built on a conditional multi-pass engine.
**`DESIGN.md` is the source of truth for the architecture** — read the relevant
section before changing engine behaviour, and keep the `§` references in code
comments accurate when you touch the code they describe.

The load-bearing ideas, each of which has a test protecting it:

- The reply pass never tracks state; separate passes do (§1).
- Expensive passes gate on cheap rubric signals from pass 1 — this is the cost
  lever, not an optimisation detail (§5.2).
- Volatile prompt content goes **last** so the stable prefix's KV cache
  survives across turns (§7.1).
- Raw numbers never reach a prompt; bands resolve to guidance text (§6).
- Write arbitration is per-slice by source turn only. No global commit DAG
  (§5.5).
- Messages are dropped only after summary *and* memory have covered them (§7.2).
- State binds only to the swipe variant you land on (§9).
- The markup tokenizer fails soft on unbalanced markup (§8).

## Conventions

- **Dependencies:** the deploy target is a phone. Adding a dependency means
  someone compiles it on an Android CPU — `pydantic-core` already costs ten
  minutes of Rust. Do not add one without a clear reason, and never anything
  needing a build step on the frontend.
- **Tests:** `python3 -m pytest`. Hermetic — no network, no extra dev
  dependencies. The `echo` backend answers every pass deterministically; use it
  rather than mocking providers.
- **Tokenizer parity:** `app/markup.py` and `static/markup.js` must stay
  behaviourally identical. `tests/fixtures/markup_cases.json` is the contract;
  regenerate it if the rules change, and check both sides against it.
- **Frontend:** vanilla JS + Alpine, no build step. Alpine is vendored at
  `static/vendor/`. Render model output with `textContent`, never `innerHTML`.
- **Motion:** nothing moves linearly. Use `--ease-out`, `--ease-in-out` or
  `--ease-back` from `:root`; do not write a bezier inline. A linear transition
  is the one curve nothing physical follows, and it reads as a slide show
  rather than as something moving. The only exception is a continuous rotation,
  where easing makes the spinner hesitate once per turn. Anything that appears,
  moves, grows or leaves gets a transition — including on the way out, which
  usually means keeping the element mounted until it has finished.
- **Icons:** one SVG sprite at the top of `index.html`, referenced with
  `<use href="#i-name">`. Never an emoji: it is drawn by whichever font the
  phone happens to ship, so a row of them arrives in several weights and will
  not take the theme colour.
- **Destructive actions** arm on the first tap and act on the second, and say
  so. A modal over a sheet on a phone is its own problem.
- **Secrets in logs:** `Settings.to_dict()` masks `api_key`, and the
  `/api/settings` endpoint relies on that. Keep it masked.
