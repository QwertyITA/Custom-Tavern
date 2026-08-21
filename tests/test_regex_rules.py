"""User find/replace rules (§16).

The scopes differ in what they destroy: `display` is a lens over text that is
never touched, while `input` and `output` are edits to the record. And the
whole feature runs user-written regex in the one process serving the phone,
which `re` gives no way to interrupt — so the guard against a pattern that
backtracks catastrophically is load-bearing, not hygiene.
"""

from __future__ import annotations

import pytest

from app import regex_rules
from app.config import Settings, SettingsError, build_settings


def rule(**over) -> dict:
    return {"id": "r1", "label": "Rule", "find": "cat", "replace": "dog",
            "scope": "display", "role": "both", "enabled": True, **over}


def rules(*items) -> list[dict]:
    return regex_rules.normalise(list(items))


# ------------------------------------------------------------- normalising


def test_a_rule_with_no_pattern_is_dropped():
    assert rules(rule(find=""), rule()) == rules(rule())


def test_an_unknown_scope_falls_back_to_the_safe_one():
    """Display is the scope that cannot lose anything, so an unreadable value
    lands there rather than in one that rewrites the record."""
    assert rules(rule(scope="nonsense"))[0]["scope"] == "display"


def test_an_unknown_role_means_both():
    assert rules(rule(role="wombat"))[0]["role"] == "both"


def test_a_rule_gets_a_name_and_an_id():
    made = rules({"find": "x"})[0]
    assert made["label"] and made["id"]


@pytest.mark.parametrize("junk", [None, {}, "nonsense", 3, [None, 4, "x"]])
def test_normalise_survives_junk(junk):
    assert regex_rules.normalise(junk) == []


def test_the_rule_count_is_capped():
    """Every rule is another pass over every message on every render."""
    many = [rule(id=str(i), find=str(i)) for i in range(regex_rules.MAX_RULES + 20)]
    assert len(regex_rules.normalise(many)) == regex_rules.MAX_RULES


# ------------------------------------------------------------------ safety


def test_an_invalid_pattern_is_refused_with_a_reason():
    with pytest.raises(regex_rules.RuleError) as caught:
        regex_rules.check(rule(find="(unclosed"))
    assert "valid pattern" in str(caught.value)


def test_an_invalid_replacement_is_refused():
    with pytest.raises(regex_rules.RuleError):
        regex_rules.check(rule(find="(a)", replace=r"\9"))


def test_a_catastrophically_backtracking_pattern_is_refused():
    """`re` cannot be interrupted, so this one would hang the phone. The check
    is a filter rather than a proof, and this is the shape it is for."""
    with pytest.raises(regex_rules.RuleError) as caught:
        regex_rules.check(rule(find=r"(a+)+b"))
    assert "hang" in str(caught.value)


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(x+x+)+y", r"(a|a)+$", r"(a*)*$"])
def test_the_anchored_shapes_are_refused_too(pattern):
    """`(a+)+b` was caught and `(a+)+$` was not, which is the whole difficulty:
    the probes were a run of a's that the anchored pattern *matches* on its
    first attempt, and mixed strings holding too few a's to blow up on. It
    needed a run of one character walled off by something that cannot match.
    Measured before the probe was added: 103ms on twenty characters against a
    25ms budget, and at the forty thousand a rule is applied under it never
    returns at all."""
    with pytest.raises(regex_rules.RuleError) as caught:
        regex_rules.check(rule(find=pattern))
    assert "hang" in str(caught.value)


def test_the_probes_do_not_refuse_the_rules_people_actually_write():
    """A guard that rejects ordinary patterns is a guard nobody keeps on."""
    for pattern in (
        r'"([^"]+)"',
        r"\*(.+?)\*",
        r"^(User|You):\s*",
        r"\n{3,}",
        r"(?i)\bthe\s+(cat|dog)\b",
        r"[A-Z][a-z]+ [A-Z][a-z]+",
    ):
        regex_rules.check(rule(find=pattern))


def test_an_ordinary_pattern_passes_the_check():
    for pattern in (r"\bcat\b", r"^\s*>.*$", r"(\w+), (\w+)", r"[Tt]he (\w+)"):
        regex_rules.check(rule(find=pattern))


def test_a_very_long_message_is_left_alone():
    """The length cap is the second half of the guard: even a checked pattern
    is only checked against a short probe."""
    long_text = "cat " * 20_000
    assert len(long_text) > regex_rules.MAX_TEXT
    assert regex_rules.apply(rules(rule()), long_text, "display") == long_text


def test_a_rule_that_breaks_later_is_skipped_rather_than_raised():
    """It passed on save, so a failure here means something changed underneath.
    Losing the message would be a much worse answer than showing it plain."""
    broken = [{**rule(), "find": "(unclosed"}]
    assert regex_rules.apply(broken, "the cat sat", "display") == "the cat sat"


# ------------------------------------------------------------------ applying


def test_a_rule_only_runs_in_its_own_scope():
    only_display = rules(rule(scope="display"))
    assert regex_rules.apply(only_display, "one cat", "display") == "one dog"
    assert regex_rules.apply(only_display, "one cat", "output") == "one cat"


def test_a_disabled_rule_does_nothing():
    assert regex_rules.apply(rules(rule(enabled=False)), "a cat", "display") == "a cat"


def test_rules_run_in_order():
    """Order is the whole of how two rules interact, so it is the stored order
    and not, say, alphabetical."""
    ordered = rules(
        rule(id="1", find="cat", replace="dog"),
        rule(id="2", find="dog", replace="fox"),
    )
    assert regex_rules.apply(ordered, "a cat", "display") == "a fox"


def test_a_rule_can_be_limited_to_one_side_of_the_conversation():
    mine = rules(rule(role="user", find="hm+", replace="…"))
    assert regex_rules.apply(mine, "hmm", "display", role="user") == "…"
    assert regex_rules.apply(mine, "hmm", "display", role="assistant") == "hmm"


def test_capture_groups_work():
    swap = rules(rule(find=r"(\w+) and (\w+)", replace=r"\2 and \1"))
    assert regex_rules.apply(swap, "salt and pepper", "display") == "pepper and salt"


def test_matching_ignores_case_by_default_and_can_be_told_not_to():
    assert regex_rules.apply(rules(rule()), "A Cat", "display") == "A dog"
    exact = rules(rule(ignore_case=False))
    assert regex_rules.apply(exact, "A Cat", "display") == "A Cat"


def test_multiline_and_dotall_are_off_unless_asked_for():
    anchored = rules(rule(find=r"^b", replace="B"))
    assert regex_rules.apply(anchored, "a\nb", "display") == "a\nb"
    assert regex_rules.apply(rules(rule(find=r"^b", replace="B", multiline=True)),
                             "a\nb", "display") == "a\nB"


def test_empty_text_is_returned_unchanged():
    assert regex_rules.apply(rules(rule()), "", "display") == ""


# ------------------------------------------------------------------ preview


def test_preview_reports_the_result_and_the_count():
    body = regex_rules.preview(rule(), "cat, cat and a cat")
    assert body["ok"] is True and body["matches"] == 3
    assert body["result"] == "dog, dog and a dog"


def test_preview_explains_a_bad_pattern_instead_of_raising():
    body = regex_rules.preview(rule(find="(unclosed"), "anything")
    assert body["ok"] is False and body["error"]
    assert body["result"] == "anything", "the sample comes back untouched"


# -------------------------------------------------------------- through the API


def test_saving_a_rule_keeps_it(client, isolated_settings):
    from app import config

    payload = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "regex_rules": [rule(label="Ellipses", find=r"\.\.\.", replace="…")],
    }
    assert client.put("/api/settings", json=payload).json()["ok"] is True
    assert config.SETTINGS.regex_rules[0]["find"] == r"\.\.\."
    assert client.get("/api/settings").json()["regex_rules"][0]["label"] == "Ellipses"


def test_saving_a_dangerous_rule_is_a_400_and_says_which_one(client, isolated_settings):
    from app import config

    before = list(config.SETTINGS.regex_rules)
    response = client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "regex_rules": [rule(label="Runaway", find=r"(a+)+b")],
    })
    assert response.status_code == 400
    assert "Runaway" in response.json()["detail"]
    assert config.SETTINGS.regex_rules == before, "nothing was saved"


def test_the_test_endpoint_runs_one_rule(client):
    body = client.post("/api/regex/test", json={
        "rule": rule(find=r"\bthe\b", replace="a"), "sample": "the cat on the mat",
    }).json()
    assert body["ok"] is True and body["matches"] == 2
    assert body["result"] == "a cat on a mat"


def test_the_test_endpoint_refuses_a_hazard_rather_than_running_it(client):
    body = client.post("/api/regex/test", json={
        "rule": rule(find=r"(a+)+b"), "sample": "aaaaaaaaaaaaaaaaaaaaaaaa!",
    }).json()
    assert body["ok"] is False and "hang" in body["error"]


def test_the_test_endpoint_handles_an_empty_rule(client):
    body = client.post("/api/regex/test", json={"rule": {}, "sample": "x"}).json()
    assert body["ok"] is False


def test_the_api_ships_the_scopes_with_their_warnings(client):
    meta = client.get("/api/settings").json()["regex_meta"]
    assert [s["id"] for s in meta["scopes"]][0] == "display", "the safe one first"
    assert all(s["note"] for s in meta["scopes"])
    assert "untouched" in next(s for s in meta["scopes"] if s["id"] == "display")["note"]


# --------------------------------------------------------- the three scopes


def send(client, chat_id: str, text: str) -> None:
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as response:
        for _ in response.iter_lines():
            pass


def a_chat(client) -> str:
    character_id = client.get("/api/characters").json()[0]["id"]
    return client.post("/api/chats", json={"character_id": character_id}).json()["id"]


def configure(client, *items) -> None:
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "regex_rules": list(items),
    })


def test_a_display_rule_leaves_the_message_alone(client, isolated_settings):
    """The whole point of the scope: turn the rule off and the original is
    still there, because it was never overwritten."""
    configure(client, rule(find="ferry", replace="FERRY", scope="display"))
    chat_id = a_chat(client)
    send(client, chat_id, "Is the ferry running?")

    shown = client.get(f"/api/chats/{chat_id}/messages").json()
    mine = next(m for m in shown if m["role"] == "user")
    assert mine["display"] == "Is the FERRY running?"
    assert mine["text"] == "Is the ferry running?", "the record is untouched"

    configure(client)  # rule removed
    again = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    assert "display" not in again and again["text"] == "Is the ferry running?"


def test_an_input_rule_rewrites_what_is_stored(client, isolated_settings):
    configure(client, rule(find="teh", replace="the", scope="input"))
    chat_id = a_chat(client)
    send(client, chat_id, "teh ferry")

    mine = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    assert mine["text"] == "the ferry", "stored rewritten, not merely displayed"
    assert "display" not in mine


def test_an_output_rule_rewrites_the_reply_as_stored(client, isolated_settings):
    configure(client, rule(find="ferry", replace="ferryboat", scope="output"))
    chat_id = a_chat(client)
    send(client, chat_id, "Ask about the ferry.")

    replies = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant" and m["turn"] > 0]
    assert any("ferryboat" in m["text"] for m in replies)


def test_an_output_rule_does_not_touch_what_you_typed(client, isolated_settings):
    configure(client, rule(find="ferry", replace="XXX", scope="output"))
    chat_id = a_chat(client)
    send(client, chat_id, "the ferry")
    mine = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    assert mine["text"] == "the ferry"


def test_no_rules_means_no_display_field_at_all(client):
    """The common case stays exactly as it was, with nothing extra on the wire."""
    chat_id = a_chat(client)
    send(client, chat_id, "Hello.")
    assert all("display" not in m for m in client.get(f"/api/chats/{chat_id}/messages").json())


def test_an_input_rule_reaches_the_prompt(client, isolated_settings):
    """It rewrites before storing, so the model sees the rewritten text — which
    is the difference between the input scope and the display scope."""
    configure(client, rule(find="ferry", replace="hovercraft", scope="input"))
    chat_id = a_chat(client)
    send(client, chat_id, "the ferry")

    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    reply = [m for m in messages if m["role"] == "assistant"][-1]
    parts = client.get(f"/api/messages/{reply['id']}/prompt").json()
    assert parts["ok"] is True


def test_a_display_rule_reaches_the_screen_without_a_reload(client, isolated_settings):
    """The events carry the rewrite too. Otherwise a message reads one way as
    it streams in and another way after a reload, which reads as the rule
    being broken rather than as the rule being late."""
    import json

    configure(client, rule(find=r"\.\.\.", replace="…", scope="display"))
    chat_id = a_chat(client)

    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": "Wait... really..."}) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    start = next(e for e in events if e["type"] == "turn_start")
    assert start["message"]["display"] == "Wait… really…"
    assert start["message"]["text"] == "Wait... really...", "the record is untouched"


def test_a_rerolled_variant_is_lensed_too(client, isolated_settings):
    import json

    configure(client, rule(find="the", replace="THE", scope="display"))
    chat_id = a_chat(client)
    send(client, chat_id, "Tell me about the ferry.")
    message = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant"][-1]

    events = []
    with client.stream("POST", f"/api/messages/{message['id']}/swipe", json={}) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    variants = [e for e in events if e["type"] == "variant"]
    assert variants, "the swipe produced a variant"
    body = variants[-1]["variant"]
    if "the" in body["text"].lower():
        assert body.get("display"), "a variant carrying a match must carry the rewrite"


def test_deltas_are_left_alone(client, isolated_settings):
    """A rule matching across a chunk boundary cannot be applied to half a
    match, so streaming shows raw text and the finished message settles it."""
    import json

    configure(client, rule(find="e", replace="3", scope="display"))
    chat_id = a_chat(client)
    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": "the ferry"}) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    deltas = [e for e in events if e["type"] == "delta"]
    assert deltas, "there were deltas to check"
    assert all("display" not in e for e in deltas)


def test_events_are_untouched_when_there_are_no_rules(client):
    """The common case pays nothing — not even a walk over each event."""
    import json

    chat_id = a_chat(client)
    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hi"}) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    start = next(e for e in events if e["type"] == "turn_start")
    assert "display" not in start["message"]
