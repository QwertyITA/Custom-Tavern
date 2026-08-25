"""Pydantic models: the pass/toggle/card schemas and the API payloads (§5.1, §11)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Tier = Literal["blocking", "foreground", "background"]
PassKind = Literal["canonical", "custom"]
RunStatus = Literal["pending", "running", "done", "failed", "stale", "skipped"]

# Rubric levels — models self-score these far more stably than floats (§5.2).
SIGNAL_LEVELS = ("none", "minor", "major")


class Trigger(BaseModel):
    """When a pass is eligible to run (§5.2)."""

    type: Literal[
        "every_turn", "every_n", "on_signal", "timer", "manual", "chance", "over_budget"
    ] = "every_turn"
    n: int = 1
    signal: str = ""
    op: Literal[">=", ">", "==", "!=", "<", "<="] = ">="
    threshold: str | float = "minor"
    seconds: float = 0.0
    # For `chance`: how often it fires, 0..1. Zero is off, which is the honest
    # way to switch a pass like this off — the alternative is a second flag
    # that has to agree with the frequency.
    probability: float = 0.0


class Sampling(BaseModel):
    """What to ask the backend for (§17).

    Every field below has a neutral value at which it does nothing, and is left
    out of the request entirely while it sits there — see `app/samplers.py`,
    which is the one place the ranges, the neutral values and the per-backend
    names are written down.

    The first four are deliberately *not* at neutral: they are the conventional
    defaults this app has always sent, and moving them to neutral would change
    everyone's output on upgrade for no reason anybody asked for. Everything
    added after them starts neutral and stays off until it is moved.
    """

    temp: float = 0.8
    top_p: float = 0.95
    top_k: int = 40
    rep_penalty: float = 1.1
    max_tokens: int = 512
    stop: list[str] = Field(default_factory=list)

    # Truncation, beyond top-p/top-k.
    min_p: float = 0.0
    typical_p: float = 1.0
    tfs: float = 1.0

    # Repetition, beyond the plain penalty.
    rep_range: int = 0
    freq_penalty: float = 0.0
    presence_penalty: float = 0.0

    # DRY — penalises repeated sequences rather than repeated words.
    dry_multiplier: float = 0.0
    dry_base: float = 1.75
    dry_allowed_length: int = 2
    dry_range: int = 0

    # XTC — sometimes drops the most obvious candidate.
    xtc_probability: float = 0.0
    xtc_threshold: float = 0.1

    seed: int = -1


class PassOutput(BaseModel):
    """Where a pass's result goes (§5.4)."""

    type: Literal["none", "state_modifier", "gui_panel", "action_card", "reply"] = "none"
    target: str = ""  # slice name / panel id / card type


class PassDef(BaseModel):
    id: str
    kind: PassKind = "custom"
    label: str = ""
    enabled: bool = True
    trigger: Trigger = Field(default_factory=Trigger)
    model_tier: Tier = "background"
    sampling: Sampling = Field(default_factory=Sampling)
    blocking: bool = False
    prompt: str = ""
    output: PassOutput = Field(default_factory=PassOutput)
    depends_on: list[str] = Field(default_factory=list)  # DATA dependency only (§5.5)
    writes_slice: str = ""
    animation: str = ""  # canonical passes carry their own; else cogs/ambient
    expects_json: bool = True
    retries: int | None = None

    model_config = {"protected_namespaces": ()}

    @property
    def resolved_animation(self) -> str:
        """canonical ? own : (blocking ? cogs : ambient) — §5.3."""
        if self.animation:
            return self.animation
        if self.kind == "canonical":
            return self.id
        return "cogs" if self.blocking else "ambient"


class Toggle(BaseModel):
    """Declarative behaviour object (§10)."""

    id: str
    label: str
    target_pass: str = "basic"
    injection: str = ""
    output: Literal["none", "state_modifier", "gui_panel"] = "none"
    scope: Literal["global", "per_character", "per_chat"] = "global"
    enables_pass: str = ""  # a toggle may switch a whole pass on instead of injecting
    default_on: bool = False


class Band(BaseModel):
    range: tuple[float, float]
    label: str
    guidance: str


class VariableSchema(BaseModel):
    """One tracked variable, defined once on the character card (§6)."""

    min: float = 0
    max: float = 10
    baseline: float = 5
    decay: float = 0.0
    bands: list[Band] = Field(default_factory=list)
    label: str = ""

    def clamp(self, value: float) -> float:
        return max(self.min, min(self.max, value))

    def band_for(self, value: float) -> Band | None:
        value = self.clamp(value)
        for band in self.bands:
            lo, hi = band.range
            if lo <= value <= hi:
                return band
        return self.bands[-1] if self.bands else None


class LorebookEntry(BaseModel):
    keys: list[str] = Field(default_factory=list)
    content: str = ""
    insertion_depth: int = 0
    constant: bool = False
    token_budget: int = 200
    enabled: bool = True
    case_sensitive: bool = False


class AuthorsNote(BaseModel):
    """A standing instruction placed inside the recent history.

    The point is the placement. Put at the end it reads as the newest thing
    said and the model answers it; put at the top it is buried under everything
    since. `depth` messages from the end is close enough to still carry weight
    and far enough back that the model treats it as a standing condition rather
    than a line of dialogue.

    Depth costs cache: everything after the insertion point has to be
    recomputed each turn (§7.1). Depth 0 — right at the end — is both the
    strongest and the cheapest, which is why it is the default.
    """

    text: str = ""
    # Messages from the end to insert before. 0 puts it after everything.
    depth: int = 0
    # Turns between insertions. 1 is every turn; higher costs less and fades.
    frequency: int = 1

    def active_on(self, turn: int) -> bool:
        if not self.text.strip():
            return False
        every = max(1, self.frequency)
        return turn % every == 0


class PfpEffect(BaseModel):
    """A CSS filter recipe over the portrait — belongs to the character, the
    same reasoning as `pfp_shape` below: a colour treatment chosen for one
    drawing looks wrong on the next, so it travels with the card rather than
    living in app settings, and is drawn wherever the picture is (§12)."""

    preset: str = ""
    hue: float = 0          # deg, hue-rotate
    saturate: float = 1     # 0 grey .. 3 vivid, 1 is unchanged
    brightness: float = 1   # 0.5 .. 1.5, 1 is unchanged
    contrast: float = 1     # 0.5 .. 1.5, 1 is unchanged
    sepia: float = 0        # 0 .. 1
    grayscale: float = 0    # 0 .. 1


REACTION_KEYS = ("starred", "unstarred", "killed")


class CharacterReactions(BaseModel):
    """Three short in-character lines the story never asks for directly but
    the app needs: how they'd take being starred, unstarred, or deleted
    (§ app/character_reactions.py). Generation only ever fills in whichever of
    these is empty — a line already here, generated or typed by hand, is
    never overwritten."""

    starred: str = ""
    unstarred: str = ""
    killed: str = ""


class AvatarVideo(BaseModel):
    """A talking-video avatar over the portrait (AVATAR-VIDEO-CONTRACT.md) —
    belongs to the character, same reasoning as `pfp_shape`/`pfp_effect`
    above. Off by default: rendering needs a service configured in Settings
    *and* this switched on, and either being missing falls back to the
    ordinary static portrait with no error shown anywhere.

    `idle_video` and `prep_status` are only ever written by the upload
    endpoint and the prepare step it kicks off (app/avatar_video.py) — never
    by a plain character edit, the same way a generated reaction line can be
    edited by hand but `pfp_set` cannot be pointed at an arbitrary URL."""

    enabled: bool = False
    idle_video: str = ""
    # Opaque to this app; forwarded to the avatar service as-is (§ contract).
    voice: str = ""
    prep_status: Literal["none", "pending", "ready", "failed"] = "none"


class Character(BaseModel):
    id: str
    name: str
    version: int = 1
    persona: str = ""
    first_mes: str = ""
    # Extra opening messages the card offers. They become swipe variants of the
    # greeting, so choosing between them is the gesture that already exists.
    alternate_greetings: list[str] = Field(default_factory=list)
    example_dialogue: str = ""
    scenario: str = ""
    system_prompt: str = ""
    # Injected after the history rather than before it — the last thing the
    # model reads, which is where a card puts an instruction it wants obeyed
    # over anything the conversation has drifted into.
    post_history_instructions: str = ""
    # A standing note steered *into* the recent conversation rather than
    # appended to it (§7.1). See AuthorsNote for what depth and frequency mean.
    authors_note: AuthorsNote = Field(default_factory=lambda: AuthorsNote())
    # Sequences that end this character's replies — a narrator label they keep
    # writing, a scene break they overuse.
    stop_strings: list[str] = Field(default_factory=list)
    pfp_set: dict[str, str] = Field(default_factory=dict)  # emotion -> image
    # How the picture is framed, and it belongs to the character rather than to
    # the app: a card drawn as a standing figure is ruined by a square crop, and
    # a face shot is wasted in a tall one. Cropped to this on the way in, drawn
    # to it everywhere after.
    pfp_shape: Literal["portrait", "square"] = "portrait"
    # A colour treatment over the portrait — same "belongs to the character"
    # reasoning as the shape above, and drawn everywhere the shape is.
    pfp_effect: PfpEffect = Field(default_factory=PfpEffect)
    # Starred/unstarred/deleted lines the app writes once and keeps (§ above).
    reactions: CharacterReactions = Field(default_factory=CharacterReactions)
    # Talking-video avatar, off by default (§ AvatarVideo above).
    avatar_video: AvatarVideo = Field(default_factory=AvatarVideo)
    # On by default, unlike avatar_video/the feature toggles above: extraction
    # needs no outside service, just whatever backend already runs the
    # background tier, so there is nothing unconfigured to hide behind. Off
    # stops the memory pass from extracting anything new about this
    # character *and* stops retrieval from pulling in what is already
    # stored (§app/memory.py, app/passes/scheduler.py) — a person turning
    # this off wants a character with no memory, not one still quietly
    # collecting it in the background with no way to see it.
    memory_enabled: bool = True
    backgrounds: list[dict[str, Any]] = Field(default_factory=list)  # {img, metadata}
    state_schema: dict[str, VariableSchema] = Field(default_factory=dict)
    # Rule-based keyword/regex nudges, applied before any model pass (§6).
    nudges: list[dict[str, Any]] = Field(default_factory=list)
    lorebook: list[LorebookEntry] = Field(default_factory=list)
    default_toggles: list[str] = Field(default_factory=list)
    colours: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------ API I/O


class CreateChatRequest(BaseModel):
    character_id: str
    title: str = ""


class SendMessageRequest(BaseModel):
    text: str
    # Staged attachments to bind to this turn's message (§19).
    attachments: list[str] = Field(default_factory=list)
    # Who should answer, when the chat's policy is "you choose" (roadmap 8).
    speaker_id: str = ""


class EditMessageRequest(BaseModel):
    text: str
    reaudit: bool = False


class ToggleRequest(BaseModel):
    enabled: bool
    scope: Literal["global", "per_character", "per_chat"] = "global"
    scope_id: str = ""


class PassRunView(BaseModel):
    id: str
    pass_id: str
    kind: str
    tier: str
    model: str
    status: RunStatus
    animation: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    attempts: int = 0
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
