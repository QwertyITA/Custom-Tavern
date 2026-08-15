"""Web search results injected into the prompt (roadmap 24).

Off by default, and off until it is *configured* — there is no bundled search
provider, because every free one either needs a key, rate-limits a phone into
uselessness, or is an HTML page whose shape changes without warning. What there
is instead is a URL template you point at something you trust:

    http://192.168.1.5:8888/search?q={q}&format=json     (SearXNG, self-hosted)
    https://api.search.brave.com/res/v1/web/search?q={q} (with a key header)

The response is read leniently. Search APIs disagree about almost everything —
`results` vs `web.results` vs `items`, `content` vs `snippet` vs `description` —
so rather than a schema per provider there is one reader that looks in the
places results are usually found. A shape it cannot read comes back empty,
which reads as "found nothing" rather than as an error, because that is what it
means to the person reading the chat.

**No model call.** The search is one HTTP request and the snippets go straight
into the prompt. Distilling them through a model first would double the cost of
the feature to make the results shorter, and the volatile band is the cheapest
place in the prompt to put something anyway.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

# Where results tend to live, in the order worth trying.
RESULT_KEYS = ("results", "items", "organic_results", "data")
TITLE_KEYS = ("title", "name", "heading")
SNIPPET_KEYS = ("content", "snippet", "description", "excerpt", "text", "body")
URL_KEYS = ("url", "link", "href", "displayed_link")

MAX_RESULTS = 8
MAX_SNIPPET_CHARS = 320
TIMEOUT_SECONDS = 8.0


class SearchError(RuntimeError):
    """The search could not be run. Never fatal to a turn."""


def configured(settings) -> bool:
    return bool((getattr(settings, "search_url", "") or "").strip())


def build_url(template: str, query: str) -> str:
    """Put the query into the template.

    `{q}` is the placeholder; a template without one gets the query appended as
    `q=`, because that is what almost every search endpoint wants and guessing
    right is friendlier than refusing.
    """
    escaped = quote(query.strip(), safe="")
    if "{q}" in template:
        return template.replace("{q}", escaped)
    joiner = "&" if "?" in template else "?"
    return f"{template}{joiner}q={escaped}"


def _first(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _rows(payload: Any) -> list[dict]:
    """The result list, wherever this provider decided to put it."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in RESULT_KEYS:
        found = payload.get(key)
        if isinstance(found, list):
            return [r for r in found if isinstance(r, dict)]
    # Brave and friends nest it one level down.
    for value in payload.values():
        if isinstance(value, dict):
            nested = _rows(value)
            if nested:
                return nested
    return []


def parse(payload: Any, limit: int = 4) -> list[dict[str, str]]:
    """Normalise whatever came back into title/snippet/url rows."""
    out: list[dict[str, str]] = []
    for row in _rows(payload):
        snippet = _first(row, SNIPPET_KEYS)
        title = _first(row, TITLE_KEYS)
        if not snippet and not title:
            continue
        out.append({
            "title": title[:160],
            "snippet": snippet[:MAX_SNIPPET_CHARS],
            "url": _first(row, URL_KEYS)[:300],
        })
        if len(out) >= max(1, min(limit, MAX_RESULTS)):
            break
    return out


async def search(settings, query: str) -> list[dict[str, str]]:
    """Run one search. Returns [] on anything going wrong.

    Never raises: a search that failed should leave the turn working without
    it, not take the reply down with it.
    """
    if not configured(settings) or not query.strip():
        return []
    url = build_url(settings.search_url, query)
    headers = {"Accept": "application/json"}
    if getattr(settings, "search_key", ""):
        # The two conventions worth covering without asking anyone to choose.
        headers["Authorization"] = f"Bearer {settings.search_key}"
        headers["X-Subscription-Token"] = settings.search_key
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    return parse(payload, getattr(settings, "search_results", 4))


def render(results: list[dict[str, str]]) -> str:
    """The block that goes into the prompt.

    Sources are named. A character repeating something it half-read is a
    different thing from one citing where it came from, and the second is what
    anyone turning this on actually wants.
    """
    if not results:
        return ""
    lines = []
    for row in results:
        head = row["title"] or row["url"] or "Result"
        lines.append(f"- **{head}** — {row['snippet']}" if row["snippet"] else f"- **{head}**")
        if row["url"]:
            lines.append(f"  ({row['url']})")
    return (
        "## Looked up just now\n"
        + "\n".join(lines)
        + "\nUse these only where they are relevant, say where something came "
        "from, and say so plainly if they do not answer the question."
    )
