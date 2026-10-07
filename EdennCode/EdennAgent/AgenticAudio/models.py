from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_serializer

from EdennCode.Deployment.error_codes import redact_client_keys, redact_error_blob


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_snapshot_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Strip upstream-vendor identity from a serialized session snapshot.

    ``redact_client_keys`` drops provider names / provider_audio_id /
    provider_task_id from anywhere in the payload (candidates, proposals, tool
    I/O, ...); tool-call error blobs are additionally scrubbed of any provider
    name baked into free text. The vendor handles still live in the persisted
    ``state_json`` server-side — only this client-facing projection is cleaned.
    """
    data = redact_client_keys(data)
    tool_calls = data.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if isinstance(tool_call, dict) and tool_call.get("error") is not None:
                tool_call["error"] = redact_error_blob(tool_call["error"])
    return data


class AgenticSessionStatus:
    ACTIVE = "active"
    READY = "ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgenticSessionPhase:
    CREATED = "created"
    OBSERVING = "observing"
    PROPOSING = "proposing"
    AWAITING_PLAN_CHOICE = "awaiting_plan_choice"
    GENERATING_CANDIDATES = "generating_candidates"
    AWAITING_CANDIDATE_CHOICE = "awaiting_candidate_choice"
    COMPOSING = "composing"
    COMPLETED = "completed"
    FAILED = "failed"


AgenticAudioSessionStatus = AgenticSessionStatus
AgenticAudioSessionPhase = AgenticSessionPhase


class AgenticMessageRole:
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class AgenticToolStatus:
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class AgenticAudioSession:
    session_id: str
    source_video_artifact_id: str
    status: str
    phase: str
    creator_user_id: Optional[str] = None
    selected_candidate_id: Optional[str] = None
    linked_job_ids: list[str] = field(default_factory=list)
    state_json: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Advances on every write. Lets a caller that tracks it ask for
    # compare-and-set, and makes a lost update visible after the fact.
    version: int = 0


@dataclass(frozen=True)
class AgenticAudioMessage:
    message_id: str
    session_id: str
    role: str
    content: str
    payload_json: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class AgenticAudioToolCall:
    tool_call_id: str
    session_id: str
    tool_name: str
    status: str
    input_json: dict[str, Any] = field(default_factory=dict)
    output_json: Optional[dict[str, Any]] = None
    error_json: Optional[dict[str, Any]] = None
    linked_job_id: Optional[str] = None
    linked_artifact_ids: list[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class AgenticAudioChoice:
    choice_id: str
    session_id: str
    choice_type: str
    target_id: str
    payload_json: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


# Upper bound on free-text turn input. Unbounded content fed the LLM turn / TTS
# directly (adversarial review P3: 99999-char content accepted); this caps abuse
# while staying far above any legitimate direction or script.
MAX_MESSAGE_CHARS = 8000


class CreateAgenticAudioSessionRequest(BaseModel):
    # Optional at the model layer so an absent/null value reaches the explicit
    # 400 "source_video_artifact_id is required" guard in the router, rather than
    # a raw Pydantic 422 (documented contract: matrix §1.4).
    source_video_artifact_id: Optional[str] = None
    creator_user_id: Optional[str] = None
    initial_message: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)


class CreateAgenticAudioSessionResponse(BaseModel):
    session_id: str
    status: str
    phase: str
    status_url: str
    ws_url: str


class AgenticAudioMessageRequest(BaseModel):
    content: str = Field(max_length=MAX_MESSAGE_CHARS)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgenticAudioChoiceRequest(BaseModel):
    # Structured UI actions, dispatched deterministically (no LLM round-trip).
    # mix      -> tune the blend (compose_mix / adjust_remix) from slider values
    # variation-> branch a new take from a candidate (edit_audio / regenerate)
    # voiceover-> (re)draft the script then generate the narration (TTS)
    # spotting -> reassign who owns one moment; governs every layer, spends nothing
    # sculpt   -> re-cut a take from a different point in its own full track
    # compare  -> put the finished takes side by side by what they SOUND like
    #
    # The last two spend nothing and were, for a while, reachable only by
    # guessing the words in chat: a capability with no affordance is a
    # capability most users never find.
    choice_type: Literal[
        "proposal",
        "candidate",
        "compose",
        "clarification",
        "mix",
        "variation",
        "voiceover",
        "sfx",
        "spotting",
        "sculpt",
        "compare",
    ]
    target_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class MusicProposalCard(BaseModel):
    proposal_id: str
    title: str
    prompt: str
    modelspec: str = "edenn_basic"
    include_vocals: bool = False
    vocal_gender: str = "female"
    music_volume: float = 0.85


class MusicCandidateCard(BaseModel):
    candidate_id: str
    proposal_id: str
    title: str
    prompt: str
    modelspec: str
    include_vocals: bool = False
    linked_job_id: Optional[str] = None
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    status: str = "queued"
    # Phase 2: iterative editing / branching. ``parent_candidate_id`` links a
    # derived candidate back to the one it was edited from; ``version`` increments
    # per branch; ``edit_kind`` records how it was produced (regenerate/extend).
    parent_candidate_id: Optional[str] = None
    version: int = 1
    edit_kind: Optional[str] = None
    # Set when a requested edit_kind couldn't run on this provider and was routed
    # to a different one (e.g. creative_edit on the basic tier -> regenerate).
    requested_edit_kind: Optional[str] = None
    # For extend edits: "native" when routed to a provider's in-place extend
    # (a tier whose provider has a known provider_audio_id), or
    # "regenerate_fallback" when the provider cannot extend in place (the basic
    # tier, or no track id yet).
    extend_mode: Optional[str] = None
    # Current mix parameters and the cheap re-muxed video produced by
    # ``adjust_remix`` (no new generation).
    music_volume: float = 0.85
    preserve_original_audio: bool = False
    remixed_video_url: Optional[str] = None
    # Provider-native identity, hydrated from the linked job result. These are the
    # handles native edit ops (extend / remix / inpaint) target. ``provider`` is
    # derived from the modelspec; ``provider_audio_id``/``provider_task_id`` are
    # None for the basic tier (no addressable track id on the generation path).
    provider: Optional[str] = None
    provider_audio_id: Optional[str] = None
    provider_task_id: Optional[str] = None


class AgenticAudioSessionSnapshot(BaseModel):
    session_id: str
    source_video_artifact_id: str
    status: str
    phase: str
    creator_user_id: Optional[str] = None
    selected_candidate_id: Optional[str] = None
    linked_job_ids: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    choices: list[dict[str, Any]] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize_redacted(self, handler: Any) -> dict[str, Any]:
        # Single egress chokepoint: every serialization of a snapshot (HTTP
        # response_model, WS model_dump, real OR in-memory repo) is scrubbed of
        # upstream-vendor identity here.
        return _redact_snapshot_payload(handler(self))


class AgenticAudioWebSocketEvent(BaseModel):
    event_type: str
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _serialize_redacted(self, handler: Any) -> dict[str, Any]:
        # Cards/events (candidate.cards, proposal.cards, ...) carry the same
        # candidate dicts as the snapshot; scrub vendor identity on every dump.
        return redact_client_keys(handler(self))


# Actions the agent LLM may take on each reasoning step.
AGENT_ACTION_CALL_TOOL = "call_tool"
AGENT_ACTION_PROPOSE = "propose"
AGENT_ACTION_ASK = "ask"
AGENT_ACTION_CLARIFY = "clarify"  # ask a structured question with quick-choice chips
AGENT_ACTION_NOOP = "noop"

# Controlled vocabulary for user-intent detection. The agent classifies each user
# turn into exactly one of these, which drives session memory and routing.
AGENT_INTENT_ANALYZE = "analyze"  # wants the source video understood
AGENT_INTENT_REQUEST_PROPOSALS = "request_proposals"  # wants direction options
AGENT_INTENT_APPROVE_DIRECTION = "approve_direction"  # approve a plan / generate
AGENT_INTENT_ADJUST_MIX = "adjust_mix"  # volume / ducking / keep-original tweak
AGENT_INTENT_RESTYLE = "restyle"  # creative restyle / cover of an existing track
AGENT_INTENT_LENGTHEN = "lengthen"  # extend / fit a longer cut
AGENT_INTENT_NEW_VARIATION = "new_variation"  # regenerate a fresh take
AGENT_INTENT_SELECT_FINAL = "select_final"  # pick / finalize a candidate
AGENT_INTENT_COMPARE = "compare"  # compare options
AGENT_INTENT_ASK_QUESTION = "ask_question"  # a question, no production action
AGENT_INTENT_REVERT = "revert"  # go back to a prior version/candidate
AGENT_INTENT_PLAN_AUDIO = "plan_audio"  # choose how to sequence the audio layers
AGENT_INTENT_ADD_VOICEOVER = "add_voiceover"  # wants narration added
AGENT_INTENT_ADD_SFX = "add_sfx"  # wants timed sound effects added
AGENT_INTENT_OTHER = "other"  # smalltalk / unclear
AGENT_INTENTS = (
    AGENT_INTENT_ANALYZE,
    AGENT_INTENT_REQUEST_PROPOSALS,
    AGENT_INTENT_APPROVE_DIRECTION,
    AGENT_INTENT_ADJUST_MIX,
    AGENT_INTENT_RESTYLE,
    AGENT_INTENT_LENGTHEN,
    AGENT_INTENT_NEW_VARIATION,
    AGENT_INTENT_SELECT_FINAL,
    AGENT_INTENT_COMPARE,
    AGENT_INTENT_ASK_QUESTION,
    AGENT_INTENT_REVERT,
    AGENT_INTENT_PLAN_AUDIO,
    AGENT_INTENT_ADD_VOICEOVER,
    AGENT_INTENT_ADD_SFX,
    AGENT_INTENT_OTHER,
)

# Audio-director production plan: how a session sequences its audio layers, and
# the layer/event types the director composes. (Phase A foundation.)
AGENT_PLAN_MODE_FULL_E2E = "full_e2e"  # plan + generate all layers end-to-end
AGENT_PLAN_MODE_MUSIC_FIRST = "music_first"  # music now, add layers one by one
AGENT_PLAN_MODES = frozenset({AGENT_PLAN_MODE_FULL_E2E, AGENT_PLAN_MODE_MUSIC_FIRST})

AUDIO_LAYER_MUSIC = "music"
AUDIO_LAYER_VOICEOVER = "voiceover"
AUDIO_LAYER_SFX = "sfx"
AUDIO_LAYERS = (AUDIO_LAYER_MUSIC, AUDIO_LAYER_VOICEOVER, AUDIO_LAYER_SFX)

# The voice roster. Each entry is a CHARACTER, not a demographic label — the
# director casts from these descriptions against the footage, the picker shows
# them to the user, and `instructions` is the voice's default delivery before
# any per-line direction. A listener called the old five-entry roster ("Calm
# Male", one-word styles) detached and generic — a casting sheet whose every
# line reads the same casts the same voice every time.
#
# `voice` is the ENGINE voice key: a valid platform-TTS voice name, which the
# speech-provider adapter also maps to a premade voice id (each live-verified).
# The first five ids are load-bearing — stored in existing sessions — and MUST
# stay stable; their names and descriptions may grow, their ids may not.
VOICE_CATALOG = [
    {
        "id": "warm_female",
        "name": "Warm Confidante",
        "voice": "shimmer",
        "gender": "female",
        "style": "close and warm — speaks to one person, not an audience",
        "instructions": (
            "Speak closely and warmly, like telling one person something that "
            "matters. Natural pace, real breaths, nothing announcer-like."
        ),
    },
    {
        "id": "bright_female",
        "name": "Bright Spark",
        "voice": "nova",
        "gender": "female",
        "style": "young, quick, a smile in the voice — reels and product energy",
        "instructions": (
            "Speak with bright, quick energy and a smile in the voice — "
            "youthful and genuine, never salesy."
        ),
    },
    {
        "id": "calm_male",
        "name": "Grounded Anchor",
        "voice": "onyx",
        "gender": "male",
        "style": "low and steady with documentary weight — present, not flat",
        "instructions": (
            "Speak low and steady with documentary weight. Grounded and "
            "present — calm is a performance, not an absence of one."
        ),
    },
    {
        "id": "narrator_male",
        "name": "Storybook Narrator",
        "voice": "echo",
        "gender": "male",
        "style": "warm storyteller's cadence — resonant, unhurried, drawing you in",
        "instructions": (
            "Narrate like a seasoned storyteller: resonant, unhurried, each "
            "sentence shaped with a beginning and a landing."
        ),
    },
    {
        "id": "neutral",
        "name": "Clean Slate",
        "voice": "alloy",
        "gender": "neutral",
        "style": "clear and even — explainer clarity, no persona in the way",
        "instructions": "Speak clearly and evenly, letting the words carry it.",
    },
    {
        "id": "gravel_trailer",
        "name": "Gravel & Thunder",
        "voice": "ash",
        "gender": "male",
        "style": "gravel and coiled threat — trailer weight, action and epic scale",
        "instructions": (
            "Speak with gravel and restrained power, like a film trailer that "
            "does not need to shout. Let menace sit under the words."
        ),
    },
    {
        "id": "velvet_female",
        "name": "Velvet Hour",
        "voice": "coral",
        "gender": "female",
        "style": "smoky and unhurried — late-night intimacy, fashion and glamour",
        "instructions": (
            "Speak low, smoky and unhurried, like late-night radio — every "
            "word chosen, nothing rushed."
        ),
    },
    {
        "id": "worn_storyteller",
        "name": "Worn Leather",
        "voice": "ballad",
        "gender": "male",
        "style": "raspy and lived-in — texture of someone who was actually there",
        "instructions": (
            "Speak with a worn, textured voice that has lived the story it "
            "tells. Dry, honest, no polish."
        ),
    },
    {
        "id": "commanding_female",
        "name": "Editor-in-Chief",
        "voice": "sage",
        "gender": "female",
        "style": "assured and precise — leads the room without raising her voice",
        "instructions": (
            "Speak with assured precision, like the most senior person in the "
            "room stating what is true. Authority without volume."
        ),
    },
    {
        "id": "easy_male",
        "name": "Friend Next Door",
        "voice": "fable",
        "gender": "male",
        "style": "easy and conversational — a friend explaining, zero performance",
        "instructions": (
            "Speak casually and naturally, like a friend explaining something "
            "over coffee. Contractions, ease, no narrator voice."
        ),
    },
]
DEFAULT_VOICE_ID = "warm_female"
VOICE_IDS = frozenset(preset["id"] for preset in VOICE_CATALOG)


def voice_preset(voice_id: Optional[str]) -> dict[str, Any]:
    """Resolve a voice id to its preset, falling back to the default voice."""

    wanted = (voice_id or "").strip().lower()
    for preset in VOICE_CATALOG:
        if preset["id"] == wanted:
            return dict(preset)
    for preset in VOICE_CATALOG:
        if preset["id"] == DEFAULT_VOICE_ID:
            return dict(preset)
    return dict(VOICE_CATALOG[0])
# How many recent turns/intents to feed back into the agent context each turn.
AGENT_MEMORY_RECENT_TURNS = 8

# Tools the agent may dispatch. Coarse-first set (Phase 1) + iterative
# editing (Phase 2).
AGENT_TOOL_ANALYZE_VIDEO = "analyze_video"
AGENT_TOOL_APPROVE_DIRECTION = "approve_direction"  # in-process; records explicit user approval
AGENT_TOOL_GENERATE_CANDIDATES = "generate_candidates"
AGENT_TOOL_FINALIZE = "finalize"
AGENT_TOOL_ADJUST_REMIX = "adjust_remix"
AGENT_TOOL_EDIT_AUDIO = "edit_audio"
AGENT_TOOL_SET_PRODUCTION_PLAN = "set_production_plan"  # in-process; records mode+layers
AGENT_TOOL_PROPOSE_SCRIPT = "propose_script"  # in-process; records a VO script draft (free)
AGENT_TOOL_GENERATE_VOICEOVER = "generate_voiceover"  # heavy; TTS the approved script
AGENT_TOOL_COMPOSE_MIX = "compose_mix"  # in-process; layer music + VO with ducking/ratio/position
AGENT_TOOL_PLAN_SFX = "plan_sfx"  # in-process; spots timed sound-effect moments (free, editable)
AGENT_TOOL_GENERATE_SFX = "generate_sfx"  # heavy; renders a SFX variant from the spotted plan
AGENT_TOOL_SCULPT_AUDIO = "sculpt_audio"  # in-process; re-shapes an EXISTING take (free)
AGENT_TOOL_COMPARE_TAKES = "compare_takes"  # in-process; side-by-side of the takes on record (free)

# How ``sculpt_audio`` re-shapes a take the session already paid for. Every kind
# here is free and instant: it re-presents existing audio rather than asking a
# provider for new audio, which is what separates it from ``edit_audio``.
SCULPT_KIND_SHIFT_WINDOW = "shift_window"
#: Build the take from SEVERAL windows of its own track, in an order the user
#: chose. Moving one window can put the drop somewhere else; it cannot open on
#: the quiet part AND land the drop on the product shot.
SCULPT_KIND_SPLICE = "splice"
SCULPT_KINDS = frozenset({SCULPT_KIND_SHIFT_WINDOW, SCULPT_KIND_SPLICE})
#: Enough pieces to arrange a short-form cut; past this it is a DAW session.
#: How a sound-effects take is MADE. Two peer routes, not a default and a
#: fallback:
#:
#: * ``video_native`` — the engine watches the footage and answers the picture.
#:   That is what "video to sound effects" means, and what this product is for.
#: * ``text`` — each effect is written from a prompt the agent composed after
#:   watching the video itself. A real product with its own strengths: exact
#:   control over what each hit IS, and it runs on any deployment.
#:
#: ``auto`` lets the render decide from what is reachable. It used to be the
#: only behaviour, and it made the difference invisible: a deployment with no
#: fetchable URL for the clip quietly produced the second and called it the
#: first.
SFX_ROUTE_AUTO = "auto"
SFX_ROUTE_VIDEO_NATIVE = "video_native"
SFX_ROUTE_TEXT = "text"
SFX_ROUTES: tuple[str, ...] = (SFX_ROUTE_AUTO, SFX_ROUTE_VIDEO_NATIVE, SFX_ROUTE_TEXT)

MAX_SPLICE_SEGMENTS = 8
#: The longest a take may be asked to become. The deliverable is audio under
#: the user's own footage, and no tier renders past a few minutes; an unbounded
#: number reaches a PAID render either to be refused after we have paid, or —
#: worse — satisfied, handing back minutes of music nobody asked for. Five is
#: generous against any clip this studio accepts.
MAX_EXTEND_SECONDS = 300.0

# How ``edit_audio`` derives a new candidate from an existing one. ``regenerate``
# re-runs generation with a tweaked prompt; ``extend`` lengthens the track;
# ``creative_edit`` restyles/reinterprets the existing audio (cover) via the
# AudioCreativeEditWorkflow.
AGENT_EDIT_KIND_REGENERATE = "regenerate"
AGENT_EDIT_KIND_EXTEND = "extend"
AGENT_EDIT_KIND_CREATIVE_EDIT = "creative_edit"
AGENT_EDIT_KINDS = frozenset(
    {AGENT_EDIT_KIND_REGENERATE, AGENT_EDIT_KIND_EXTEND, AGENT_EDIT_KIND_CREATIVE_EDIT}
)

# Music providers backing each modelspec. Used to route native edit ops to the
# right provider and to know which capabilities exist (e.g. only the enhanced
# and studio tiers expose a native extend by track id; the basic tier has no
# addressable track id).
PROVIDER_A = "provider_a"
PROVIDER_B = "provider_b"
PROVIDER_C = "provider_c"
PROVIDER_BY_MODELSPEC = {
    "edenn_basic": PROVIDER_A,
    "edenn_enhanced": PROVIDER_B,
    "edenn_studio": PROVIDER_C,
}
# Providers that support a native "extend an existing track" operation keyed by a
# provider-side track/audio id. The basic tier is intentionally excluded.
NATIVE_EXTEND_PROVIDERS = frozenset({PROVIDER_B, PROVIDER_C})

#: Whether anything in this stack actually PERFORMS a native extension.
#:
#: The provider clients can extend a track, and takes now carry the handle an
#: extension needs. What does not exist is the piece in the middle: no job
#: consumer reads the extend mode and calls that API, so an "extend" job runs
#: the ordinary generation path and returns a NEW piece of music.
#:
#: This flag is what keeps that from being a lie. With the handle captured, the
#: capability test would otherwise pass and every extend would be labelled
#: native while still quietly regenerating — a worse failure than the one being
#: fixed, because the label would look like a guarantee. Flip it in the same
#: change that wires the consumer, never before: capture and consumer ship
#: together.
NATIVE_EXTEND_CONSUMER_AVAILABLE = False

# Providers that support audio-to-audio creative edit (restyle/cover from the
# existing track) via the AudioCreativeEditWorkflow. The basic tier has no
# source-audio conditioning path, so creative_edit falls back to regenerate.
# The studio provider's restyle pulls the source track server-side, so it needs
# a URL a vendor can actually fetch. Nothing supplied one for a long time and the
# studio branch failed every single time; the renderer now mints a signed,
# expiring read URL on the blob copy (the same mechanism the SFX video-native
# route uses). Where no blob storage is configured that route is unavailable and
# the job fails honestly rather than silently restyling nothing.
CREATIVE_EDIT_PROVIDERS = frozenset({PROVIDER_B, PROVIDER_C})


def provider_for_modelspec(modelspec: Optional[str]) -> Optional[str]:
    """Map a modelspec (edenn_basic/enhanced/studio) to its music provider."""

    return PROVIDER_BY_MODELSPEC.get((modelspec or "").strip().lower())


def _normalized_spec(modelspec: str) -> str:
    """Local normalization: the canonical helper lives in tools.media, which
    imports THIS module — a lower layer cannot call up without a cycle."""

    normalized = (modelspec or "edenn_basic").strip().lower() or "edenn_basic"
    return normalized if normalized in PROVIDER_BY_MODELSPEC else "edenn_basic"


def music_modelspec_available(modelspec: str) -> bool:
    """Does THIS process hold a key for the modelspec's music provider?

    Availability is a fact about the box, and it belongs where the tier
    vocabulary lives: the director was proposing premium tiers whose provider
    had no key here, generation silently fell back to a different (slower)
    provider, and the user heard music that matched no label on screen.
    """

    import os
    import re as _re

    provider = provider_for_modelspec(_normalized_spec(modelspec))
    if provider == PROVIDER_C:
        if os.getenv("PROVIDER_C_API_KEY"):
            return True
        return any(
            _re.match(r"^PROVIDER_C_API_KEY_\d+$", k) and (v or "").strip()
            for k, v in os.environ.items()
        )
    if provider == PROVIDER_B:
        if os.getenv("PROVIDER_B_API_KEY") or os.getenv("EDENN_ENHANCED_PROVIDER_B_API_KEY"):
            return True
        return any(
            _re.match(r"^PROVIDER_B_API_KEY_\d+$", k) and (v or "").strip()
            for k, v in os.environ.items()
        )
    if provider == PROVIDER_A:
        # Parity with the builder, which accepts the numbered key pool too —
        # checking only the bare name made this box withhold a tier it could
        # actually render. Availability may be conservative, never wrong.
        if os.getenv("PROVIDER_A_API_KEY"):
            return True
        return any(
            _re.match(r"^PROVIDER_A_API_KEY_\d+$", k) and (v or "").strip()
            for k, v in os.environ.items()
        )
    return False


def usable_music_modelspec(modelspec: str) -> str:
    """The requested tier when its provider can run here; else the best tier
    that can. Deciding this at PROPOSE time keeps every label the user reads
    true — deciding it at render time is a silent substitution."""

    wanted = _normalized_spec(modelspec)
    if music_modelspec_available(wanted):
        return wanted
    for fallback in ("edenn_studio", "edenn_basic", "edenn_enhanced"):
        if music_modelspec_available(fallback):
            return fallback
    return wanted  # nothing available: keep the request; render degrades loudly


# Default number of candidate takes generated per approved direction, by model.
# Basic returns a single take; Enhanced and Studio default to two so the user
# can A/B and lock the better one. Callers may still
# override with an explicit ``count`` or a proposal's ``candidate_count``.
DEFAULT_CANDIDATE_COUNT_BY_MODELSPEC = {
    "edenn_basic": 1,
    "edenn_enhanced": 2,
    "edenn_studio": 2,
}


def default_candidate_count_for_modelspec(modelspec: Optional[str]) -> int:
    """How many takes a modelspec generates by default (basic 1, others 2)."""

    return DEFAULT_CANDIDATE_COUNT_BY_MODELSPEC.get((modelspec or "").strip().lower(), 2)

@dataclass(frozen=True)
class ArgField:
    """One argument a tool reads, and how wrong it is allowed to be.

    Two tiers, deliberately. ``enforced=True`` fields are the ones where a bad
    value either crashes the call or is silently accepted as something the user
    did not mean — those are worth refusing, because the alternative is a 500
    or an effect on the wrong frame. Everything else is DOCUMENTED but not
    refused: tools clamp their own ranges, ignore what they do not recognise,
    and treat an empty string as "not supplied". Refusing those would widen
    refusals, and a needless refusal costs a quarter of the turn's step budget.
    """

    name: str
    #: number | int | bool | string | enum | list | list_of_dicts | dict
    kind: str
    enforced: bool = True
    #: Refuse negatives (a duration or an offset that cannot run backwards).
    allow_negative: bool = True
    #: Refuse values above this. Only for the numbers that reach a paid render,
    #: where "too big" is not clamped by anything downstream.
    max_value: Optional[float] = None
    #: Permitted values. REFUSED when ``kind="enum"``; on any other kind it is
    #: advisory — the generated schema shows the roster, the validator stays
    #: out of it, because those fields have documented fallbacks for a value
    #: they do not recognise.
    enum: tuple[str, ...] = ()
    #: Per-element fields for ``kind="list_of_dicts"``.
    nested: tuple["ArgField", ...] = ()
    #: One line for the generated per-tool block in the prompt.
    doc: str = ""
    #: Only for fields INSIDE a list entry: the schema names it required, so
    #: the model is told an entry without it is incomplete. Never a refusal —
    #: presence is the tool's business, and several read a missing field as
    #: "not supplied" on purpose.
    required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for one agent-facing tool's identity & contract.

    The decision-schema tool enum, the heavy/generation classifications, the
    live status label, the argument rules the dispatcher enforces, and the
    mechanical half of the prompt's tool documentation are all DERIVED from
    these specs, so adding or renaming a tool happens in exactly one place.
    The runtime ``ToolRegistry`` binds each spec name to its implementation and
    REFUSES TO BUILD if a spec has not said what its arguments are — the
    difference between a checklist and a gate.
    """

    name: str
    # Short live status shown while the tool runs (one live beat per step).
    status_label: str
    # Heavy tools enqueue async work and END the turn (result hydrates later).
    is_heavy: bool = False
    # Generation tools spend money / run a provider — only allowed after explicit
    # user approval (invariant #2). The agent must never call these proactively.
    is_generation: bool = False
    # What this tool reads out of tool_args. A tool that reads NOTHING must say
    # so with reads_no_args: silence is what let two tools ship with no rules at
    # all, and one of them turned a mistyped argument into a 500.
    args: tuple[ArgField, ...] = ()
    reads_no_args: bool = False

    def __post_init__(self) -> None:
        if bool(self.args) == bool(self.reads_no_args):
            raise ValueError(
                f"ToolSpec({self.name!r}) must declare either args or "
                f"reads_no_args=True — exactly one. A tool whose arguments "
                f"nobody has written down is a tool nobody is checking."
            )


#: The candidate every re-presentation verb targets. Read through
#: ``ctx.resolve_candidate``, which falls back to the session's selection, so a
#: missing value is normal and only a non-string is worth naming.
_CANDIDATE_ID = ArgField(
    "candidate_id", "string", enforced=False,
    doc="which take to act on (defaults to the locked/only one)",
)

#: Mix knobs. Every one already refuses non-numbers, NaN and Inf inside
#: ``_coerce_mix_param`` — the good example the validator was modelled on — so
#: they are documented here and deliberately not re-enforced.
_MIX_FIELDS: tuple[ArgField, ...] = (
    _CANDIDATE_ID,
    ArgField("music_volume", "number", enforced=False, doc="0.0-1.0, 0 mutes the music"),
    ArgField("voiceover_volume", "number", enforced=False, doc="0.0-1.0"),
    ArgField("sfx_volume", "number", enforced=False, doc="0.0-1.0"),
    ArgField("voiceover_start_s", "number", enforced=False, doc="where the narration starts"),
    ArgField("duck_gain_db", "number", enforced=False, doc="how far music dips under a line"),
    # "false" is truthy in Python, and this flag decides whether the video's
    # own audio stays under the music. Refused for the same reason force_music
    # and allow_overlap are: the string form turns the gate the wrong way.
    ArgField(
        "preserve_original_audio", "bool",
        doc="keep the video's own audio under the mix",
    ),
    # Level moves the user placed, as [{start_s, end_s, gain_db}]. Malformed
    # entries are dropped rather than refused — see normalize_music_envelope —
    # so only the container's shape is worth naming here.
    ArgField(
        "music_envelope", "list_of_dicts",
        nested=(
            ArgField("start_s", "number", allow_negative=False, doc="fade starts"),
            ArgField("end_s", "number", allow_negative=False, doc="fade ends"),
            ArgField("gain_db", "number", doc="how far down, in dB (negative)"),
        ),
        doc="fades: where the music dips and comes back",
    ),
)

# The canonical tool table. ``adjust_remix`` is intentionally NOT heavy: it
# re-muxes existing audio onto the source video in-process (no generation, no
# cost) and keeps the loop going.
#
# Every row declares what it reads. ``enforced=True`` marks the arguments whose
# bad values either crash the call or are accepted as something the user never
# meant; everything else is documented for the prompt and left to the tool's own
# clamping, because a needless refusal costs a quarter of the turn's budget.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        AGENT_TOOL_ANALYZE_VIDEO, "Watching your video…",
        args=(
            ArgField("user_prompt", "string", enforced=False, doc="the user's own direction words"),
            ArgField("modelspec", "string", enforced=False, doc="tier hint; unknown values fall back"),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_APPROVE_DIRECTION, "Locking in your approval…",
        args=(
            ArgField("proposal_id", "string", enforced=False, doc="the direction being approved"),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_GENERATE_CANDIDATES,
        "Composing your tracks…",
        is_heavy=True,
        is_generation=True,
        args=(
            # int({"n": 2}) raises TypeError, which nothing catches -> a 500;
            # and the tool coerces with int(), so "2.5" dies inside it.
            ArgField("count", "int", allow_negative=True, doc="how many takes (whole number)"),
            ArgField("proposal_id", "string", enforced=False, doc="which direction to render"),
            # Both of these reach a paid render. A non-string prompt arrives at
            # the provider as a Python repr; "false" for vocals is truthy and
            # puts a vocal on a take the user asked to keep instrumental.
            ArgField("prompt", "string", doc="inline direction when no proposal"),
            ArgField("include_vocals", "bool", doc="vocal or instrumental"),
            ArgField("modelspec", "string", enforced=False, doc="tier to render on"),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_FINALIZE, "Locking in the final mix…",
        args=(
            _CANDIDATE_ID,
            # The user heard the fault and wants it anyway. Their call to make,
            # but not one to make on their behalf by silence.
            ArgField(
                "acknowledge_faults", "bool",
                doc="lock the mix even though it has measured faults",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_ADJUST_REMIX, "Adjusting the mix…",
        args=_MIX_FIELDS,
    ),
    ToolSpec(
        AGENT_TOOL_SCULPT_AUDIO, "Re-cutting the track…",
        args=(
            _CANDIDATE_ID,
            ArgField(
                "sculpt_kind", "enum", enum=tuple(sorted(SCULPT_KINDS)),
                doc="how to re-shape the take",
            ),
            ArgField(
                "segments", "list_of_dicts",
                nested=(
                    ArgField(
                        "start_s", "number", allow_negative=False,
                        doc="where in the full track this piece begins",
                    ),
                    ArgField(
                        "duration_s", "number", allow_negative=False,
                        doc="how long to take from there",
                    ),
                ),
                doc="for splice: the pieces to build the take from, in order",
            ),
            # "soon" silently becomes 0.0 and hands back the very window the
            # user is trying to move away from.
            ArgField(
                "window_start_s", "number", allow_negative=False,
                doc="seconds into the FULL track to start the cut",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_COMPARE_TAKES, "Comparing your takes…",
        args=(
            # A bare string iterates per character, matches no take, and the
            # tool reports having nothing to compare.
            ArgField(
                "candidate_ids", "list",
                nested=(ArgField("", "string", doc="a take id"),),
                doc="takes to compare (omit for all finished ones)",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_EDIT_AUDIO,
        "Reworking the track…",
        is_heavy=True,
        is_generation=True,
        args=(
            _CANDIDATE_ID,
            ArgField(
                "edit_kind", "enum", enum=tuple(sorted(AGENT_EDIT_KINDS)),
                doc="regenerate, extend, or restyle",
            ),
            # A number typed into a card and a number invented by a model both
            # arrive here, and both reach a paid render. Nothing downstream
            # clamps it.
            ArgField(
                "extend_seconds", "number", allow_negative=False,
                max_value=MAX_EXTEND_SECONDS,
                doc=f"how much longer to make it (up to {MAX_EXTEND_SECONDS:.0f}s)",
            ),
            ArgField("prompt", "string", doc="direction for the new take"),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_SET_PRODUCTION_PLAN, "Planning your audio…",
        args=(
            # `for layer in 5` raises TypeError; the STRING "music" iterates per
            # character, drops every one, and the plan silently loses music.
            ArgField(
                "layers", "list",
                nested=(
                    ArgField("", "string", enum=AUDIO_LAYERS, doc="music, voiceover or sfx"),
                ),
                doc="which layers this session builds",
            ),
            # force_music=false is how a plan drops music; as a string it means yes.
            ArgField("force_music", "bool", doc="keep music even when not asked for"),
            ArgField(
                "mode", "string", enforced=False, enum=AGENT_PLAN_MODES,
                doc="advisory; unknown values fall back",
            ),
            # Read, and read as PROVENANCE: it only counts when the call also
            # arrived through a user control, so the model cannot launder its
            # own plan as the user's by writing the word. Declared because the
            # tool reads it — an argument nobody declares is one nobody checks.
            ArgField(
                "source", "string", enforced=False,
                doc="provenance; only honoured on the user's own click",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_PROPOSE_SCRIPT, "Drafting the narration…",
        args=(
            ArgField(
                "narration_segments", "list_of_dicts",
                nested=(
                    ArgField(
                        "start_s", "number", required=True,
                        doc="when the line is spoken",
                    ),
                    ArgField(
                        "text", "string", enforced=False, required=True,
                        doc="what is said",
                    ),
                    ArgField("delivery", "string", enforced=False, doc="how it is read"),
                ),
                doc="the timed script",
            ),
            ArgField(
                "hold_silent", "list",
                nested=(
                    ArgField(
                        "moment_id", "string", enforced=False, required=True,
                        doc="the moment being left alone",
                    ),
                    ArgField("reason", "string", enforced=False, doc="why"),
                ),
                doc="moments to leave unnarrated",
            ),
            ArgField(
                "voice_id", "string", enforced=False, enum=VOICE_IDS,
                doc="which voice reads it",
            ),
            ArgField("language", "string", enforced=False, doc="language to read in"),
            # A flat script with no timing. Read for months without being
            # declared, so the generated prompt block never named it and the
            # model was left to infer that the drafting tool takes a draft.
            ArgField(
                "script", "string", enforced=False,
                doc="the flat script, when the lines carry no timing",
            ),
            ArgField("tone", "string", enforced=False, doc="how the read should feel"),
            ArgField(
                "voice_rationale", "string", enforced=False,
                doc="why this voice suits the footage",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_GENERATE_VOICEOVER,
        "Recording the voice-over…",
        is_heavy=True,
        is_generation=True,
        args=(
            # float({}) raises TypeError -> a 500.
            ArgField("speed", "number", allow_negative=False, doc="delivery rate"),
            ArgField(
                "voice_id", "string", enforced=False, enum=VOICE_IDS,
                doc="which voice records it",
            ),
            ArgField("language", "string", enforced=False, doc="language to read in"),
            ArgField("tone", "string", enforced=False, doc="how the read should feel"),
            ArgField(
                "segment_id", "string", enforced=False,
                doc="re-read only this line; the rest are kept as recorded",
            ),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_COMPOSE_MIX, "Mixing the layers…",
        args=_MIX_FIELDS,
    ),
    ToolSpec(
        AGENT_TOOL_PLAN_SFX, "Spotting sound moments…",
        args=(
            ArgField(
                "sfx_events", "list_of_dicts",
                nested=(
                    # Silently becoming 0.0 puts an effect on the first frame.
                    ArgField(
                        "start_s", "number", required=True,
                        doc="when the effect hits",
                    ),
                    # max(0.05, inf) is still infinity, and it reaches a paid render.
                    ArgField(
                        "duration_s", "number", allow_negative=False,
                        doc="how long it runs — a hit or a smear",
                    ),
                    ArgField(
                        "label", "string", enforced=False, required=True,
                        doc="what it is",
                    ),
                    ArgField(
                        "prompt", "string", enforced=False, required=True,
                        doc="how it should sound",
                    ),
                    ArgField("event_type", "string", enforced=False, doc="TRANSITION, IMPACT…"),
                    # Read by the tool and never declared: the on-screen
                    # motivation, and whether a human placed this hit (which is
                    # what stops the renderer re-snapping it to nearby motion).
                    ArgField(
                        "reason", "string", enforced=False,
                        doc="what in the footage earns this sound",
                    ),
                    ArgField(
                        "timing_authority", "string", enforced=False,
                        doc="who placed it; a person's timing is not re-snapped",
                    ),
                ),
                doc="the moments to place",
            ),
            # Truthy anything here walks past the narration-collision gate.
            ArgField("allow_overlap", "bool", doc="let effects sit under narration"),
            ArgField(
                "sfx_route", "enum", enum=SFX_ROUTES, enforced=False,
                doc="video_native (the engine watches) | text (written from prompts)",
            ),
            ArgField(
                "treatment", "dict",
                nested=(
                    ArgField("label", "string", enforced=False, doc="the chosen treatment"),
                    ArgField("register", "string", enforced=False, doc="how prominent"),
                    ArgField("notes", "string", enforced=False, doc="anything else stated"),
                ),
                doc="the register and density card",
            ),
            ArgField("sfx_ambience", "string", enforced=False, doc="a continuous bed"),
            ArgField("sfx_summary", "string", enforced=False, doc="one line on the approach"),
        ),
    ),
    ToolSpec(
        AGENT_TOOL_GENERATE_SFX,
        "Designing the sound effects…",
        is_heavy=True,
        is_generation=True,
        args=(
            # Redo only these hits; every other effect in the bed is kept as
            # rendered. Omitted means the whole bed, which is the first render
            # and every deliberate re-take of the lot.
            ArgField(
                "event_ids", "list",
                nested=(ArgField("", "string", doc="a planned event id"),),
                doc="which hits to redo (omit for the whole bed)",
            ),
            ArgField(
                "sfx_route", "enum", enum=SFX_ROUTES, enforced=False,
                doc="override the route this take renders on",
            ),
        ),
    ),
)
TOOL_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}
TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)

# Derived classifications — never hand-maintained alongside the table above.
AGENT_HEAVY_TOOLS = frozenset(spec.name for spec in TOOL_SPECS if spec.is_heavy)
AGENT_GENERATION_TOOLS = frozenset(spec.name for spec in TOOL_SPECS if spec.is_generation)
# Live status label per tool (used by the reasoning-beat event). The single home
# for these strings (was duplicated in agent._STATUS_BY_TOOL).
STATUS_BY_TOOL = {spec.name: spec.status_label for spec in TOOL_SPECS}


def _field_schema(field: ArgField) -> dict[str, Any]:
    """One ``ArgField`` as the JSON-Schema fragment the model is shown.

    The translation is mechanical on purpose: every rule the model is taught
    about an argument comes from the row that also teaches the validator and
    the prompt, so the three cannot describe different tools.
    """

    if field.kind == "int":
        return {"type": "integer"}
    if field.kind == "number":
        # An upper bound is advisory here and refused by the validator; the
        # model is shown it so it can get the call right the first time.
        out: dict[str, Any] = {"type": "number"}
        if not field.allow_negative:
            out["minimum"] = 0
        if field.max_value is not None:
            out["maximum"] = field.max_value
        return out
    if field.kind == "bool":
        return {"type": "boolean"}
    if field.kind == "dict":
        out = {"type": "object"}
        if field.nested:
            out["properties"] = {
                nested.name: _field_schema(nested) for nested in field.nested
            }
            out["additionalProperties"] = False
        return out
    if field.kind in ("list", "list_of_dicts"):
        out = {"type": "array"}
        unnamed = [nested for nested in field.nested if not nested.name]
        named = [nested for nested in field.nested if nested.name]
        if unnamed:
            # The element IS the value: a list of ids.
            out["items"] = _field_schema(unnamed[0])
        elif named:
            item: dict[str, Any] = {
                "type": "object",
                "properties": {
                    nested.name: _field_schema(nested) for nested in named
                },
                "additionalProperties": False,
            }
            required = [nested.name for nested in named if nested.required]
            if required:
                item["required"] = required
            out["items"] = item
        elif field.kind == "list_of_dicts":
            out["items"] = {"type": "object"}
        return out
    # Everything else is a string; a roster on the field becomes the enum, with
    # "" kept because every tool reads empty as "not supplied".
    out = {"type": "string"}
    if field.enum:
        out["enum"] = [*field.enum, ""]
    return out


def _tool_args_schema() -> dict[str, Any]:
    """The ``tool_args`` object, derived from the tool table.

    Hand-typed beside the table, this block was the drift source two reviews
    found independently: ``sculpt_kind`` offered one of its kinds, eight
    arguments the validator enforces were absent entirely, and the sound-effect
    entries forbade two fields the spec declares — so the model was being told
    a call it can legally make is malformed.

    Flat rather than a per-tool union, deliberately. The discriminator
    (``tool_name``) is a SIBLING of ``tool_args``, so keying one on the other
    means turning ``action`` into a seventeen-branch ``anyOf`` that rides every
    step of every turn — and the per-tool mapping is already generated into the
    prompt from this same table (see ``prompts._tool_contract_block``), where
    the model actually reads it. The schema's job here is that no argument is
    described with the wrong type or a short roster.
    """

    properties: dict[str, Any] = {}
    owners: dict[str, list[str]] = {}
    for spec in TOOL_SPECS:
        for field in spec.args:
            owners.setdefault(field.name, []).append(spec.name)
            rendered = _field_schema(field)
            previous = properties.get(field.name)
            if previous is not None and previous != rendered:
                # Two tools reading one name two different ways is a manifest
                # bug, and a silent merge here is how it would survive.
                raise ValueError(
                    f"tool_args field {field.name!r} is declared two ways: "
                    f"{previous} vs {rendered}"
                )
            properties[field.name] = rendered
    for name in owners:
        # The one line the table already keeps for this field. WHICH tools read
        # it is deliberately not repeated here: the prompt carries that mapping,
        # generated from this same table, and this object rides every step of
        # every turn — a duplicated sentence is a duplicated bill.
        doc = next(
            (
                field.doc
                for spec in TOOL_SPECS
                for field in spec.args
                if field.name == name and field.doc
            ),
            "",
        )
        if doc:
            properties[name]["description"] = doc
    return {
        "type": "object",
        "properties": properties,
        # Unknown keys stay legal: tools ignore what they do not read, and the
        # validator refuses values rather than names.
        "additionalProperties": True,
    }


def build_agent_decision_schema() -> dict[str, Any]:
    """Strict JSON schema the agent LLM must emit on every reasoning step.

    The model returns one decision: optionally a user-facing message plus a
    single action (call a tool, propose music plans, ask a question, or stop).
    """

    return {
        "name": "agentic_audio_decision",
        "schema": {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "intent": {
                    "type": "string",
                    "enum": [*AGENT_INTENTS, ""],
                    "description": (
                        "The user's intent for THIS turn (classify their latest "
                        "message). Use 'other' if it is smalltalk or unclear."
                    ),
                },
                "memory_update": {
                    "type": "object",
                    "description": (
                        "Optional durable session memory to remember across turns: "
                        "evolving creative direction and learned user preferences."
                    ),
                    "properties": {
                        "creative_direction": {"type": "string"},
                        "style_keywords": {"type": "array", "items": {"type": "string"}},
                        "avoid": {"type": "array", "items": {"type": "string"}},
                        "preferences": {"type": "object", "additionalProperties": True},
                    },
                    "additionalProperties": True,
                },
                "assistant_message": {"type": "string"},
                "action": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                AGENT_ACTION_CALL_TOOL,
                                AGENT_ACTION_PROPOSE,
                                AGENT_ACTION_ASK,
                                AGENT_ACTION_CLARIFY,
                                AGENT_ACTION_NOOP,
                            ],
                        },
                        "clarification": {
                            "type": "object",
                            "description": (
                                "When type == clarify: a question plus 2-4 quick "
                                "options the user can tap instead of typing."
                            ),
                            "properties": {
                                "question": {"type": "string"},
                                # Tags the card so the answer can be recorded
                                # durably (e.g. the SFX treatment choice).
                                "topic": {
                                    "type": "string",
                                    "enum": ["general", "sfx_treatment"],
                                },
                                "options": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "label": {"type": "string"},
                                            "hint": {"type": "string"},
                                            # Marks the agent's own pick so the
                                            # card can badge it ("my pick").
                                            "recommended": {"type": "boolean"},
                                        },
                                        "required": ["label"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "additionalProperties": False,
                        },
                        "tool_name": {
                            "type": "string",
                            # Derived from the canonical TOOL_SPECS table — adding a
                            # tool there is enough; this enum tracks it automatically.
                            "enum": [*TOOL_NAMES, ""],
                        },
                        # Derived from the tool table — see _tool_args_schema.
                        "tool_args": _tool_args_schema(),
                        "proposals": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "proposal_id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "prompt": {"type": "string"},
                                    "modelspec": {"type": "string"},
                                    "include_vocals": {"type": "boolean"},
                                    "vocal_gender": {"type": "string"},
                                    "music_volume": {"type": "number"},
                                },
                                "required": ["proposal_id", "title", "prompt", "modelspec"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            },
            "required": ["thought", "intent", "assistant_message", "action"],
            "additionalProperties": False,
        },
        "strict": False,
    }


AGENT_DECISION_SCHEMA = build_agent_decision_schema()


__all__ = [
    "AGENT_ACTION_ASK",
    "AGENT_ACTION_CALL_TOOL",
    "AGENT_ACTION_NOOP",
    "AGENT_ACTION_PROPOSE",
    "AGENT_DECISION_SCHEMA",
    "AGENT_EDIT_KIND_EXTEND",
    "AGENT_EDIT_KIND_REGENERATE",
    "AGENT_EDIT_KINDS",
    "AGENT_GENERATION_TOOLS",
    "AGENT_HEAVY_TOOLS",
    "STATUS_BY_TOOL",
    "ToolSpec",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "TOOL_SPECS_BY_NAME",
    "AGENT_TOOL_ADJUST_REMIX",
    "AGENT_TOOL_ANALYZE_VIDEO",
    "AGENT_TOOL_EDIT_AUDIO",
    "AGENT_TOOL_FINALIZE",
    "AGENT_TOOL_GENERATE_CANDIDATES",
    "AGENT_TOOL_PLAN_SFX",
    "AGENT_TOOL_GENERATE_SFX",
    "AGENT_INTENT_ADD_SFX",
    "build_agent_decision_schema",
    "AgenticAudioChoice",
    "AgenticAudioChoiceRequest",
    "AgenticAudioMessage",
    "AgenticAudioMessageRequest",
    "AgenticAudioSession",
    "AgenticAudioSessionPhase",
    "AgenticAudioSessionStatus",
    "AgenticAudioSessionSnapshot",
    "AgenticAudioToolCall",
    "AgenticMessageRole",
    "AgenticSessionStatus",
    "AgenticToolStatus",
    "AgenticAudioWebSocketEvent",
    "CreateAgenticAudioSessionRequest",
    "CreateAgenticAudioSessionResponse",
    "MusicCandidateCard",
    "MusicProposalCard",
    "utc_now",
]
