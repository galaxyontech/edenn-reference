"""Typed contracts for multi-source agentic creation (AGENTIC_CREATION.md).

Design rules encoded here:

* **Refs stay dumb; roles stay per-request.** A ref is only ``asset_id`` or
  ``asset_id#t=a-b`` (the AUL grammar). What an asset *does* in a session is a
  :class:`SourceRole` carried by the request bundle — never a property of the
  ref or the asset.
* **Ask first, then propose (owner rule).** Resolution never silently defaults:
  an ambiguous bundle yields :class:`AmbiguityQuestion` s inside a
  :class:`BundleResolution` whose ``status`` is ``needs_input``.
* **Treatment is audio policy; knobs are cut taste.** :class:`TreatmentSpec`
  wraps the recompose :class:`~...recompose.domain.Knobs` rather than extending
  them, so the planner stays flow-agnostic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from ..AssetLibrary.refs import Ref
from ..Recompose.domain import Knobs

# Owner-approved ceiling (2026-07-25): at most this many non-exclude sources per
# request. Raise only with evidence + a capability discussion.
MAX_BUNDLE_SOURCES: int = 4

# The single supported narrator voice for rephrase/new-narration until the
# voice-clone gate (provider capability + rights) is opened. "nova" matches
# the voiceover worker's default, so redubs sound like the platform's voice.
HOUSE_VOICE: str = "nova"


class SourceRole(StrEnum):
    """What a referenced source *does* in this request (owner-approved set).

    Extending this vocabulary is a capability discussion, not a flow feature
    (AGENTIC_CREATION.md §"Multi-source"). Values are lowercase so they can ride
    JSON payloads verbatim.
    """

    SPINE = "spine"      #: the primary footage a cut is organized around
    ACCENT = "accent"    #: b-roll / secondary material woven into the cut
    MUSIC = "music"      #: an audio asset used (or varied) as the score
    VOICE = "voice"      #: a source whose SPEECH is the message (bites/rephrase)
    STYLE = "style"      #: a prior output whose plan parameters seed this one
    EXCLUDE = "exclude"  #: explicitly barred from use


class TreatmentKind(StrEnum):
    """The audio treatment applied to one output (AGENTIC_CREATION.md §3)."""

    KEEP_ORIGINAL = "keep_original"          #: sound bites; music ducks under
    REPHRASE_ORIGINAL = "rephrase_original"  #: transcribe → rewrite → house TTS
    NEW_NARRATION = "new_narration"          #: generated script + TTS
    MUSIC_ONLY = "music_only"                #: no voice layer


class MusicSource(StrEnum):
    """Where an output's music comes from."""

    PROVIDED = "provided"      #: use the referenced track as-is
    VARIANT_OF = "variant_of"  #: generate a variant seeded by the referenced track
    GENERATE = "generate"      #: generate fresh (spend-gated)


class BundleItem(BaseModel):
    """One ``@`` reference in a request, with an optional explicit role.

    ``ref`` uses the AUL grammar (``asset_ab12`` or ``asset_ab12#t=a-b``) and is
    validated on construction. ``role=None`` means "infer it" — the resolver
    will either infer confidently or ask (never silently default).
    """

    ref: str = Field(description="AUL ref: asset id or asset_id#t=a-b span")
    role: Optional[SourceRole] = Field(
        default=None, description="Explicit role; None = infer or ask")
    note: Optional[str] = Field(
        default=None, description="Free-text user hint carried into the plan")

    @model_validator(mode="after")
    def _validate_ref_grammar(self) -> "BundleItem":
        """Reject malformed refs at the boundary (refs stay dumb but valid)."""

        Ref.parse(self.ref)  # raises ValueError on bad grammar
        return self

    @property
    def asset_id(self) -> str:
        """The bare asset id of this ref (span suffix stripped)."""

        return Ref.asset_of(self.ref)


class RequestBundle(BaseModel):
    """The multi-source request: what the user referenced plus their intent.

    ``intent`` is the raw natural-language direction; the resolver only uses it
    for coarse contradiction checks (e.g. "fresh music" against an explicit
    music ref) — full creative interpretation belongs to the planner LLM.
    """

    items: list[BundleItem] = Field(default_factory=list)
    intent: str = Field(default="", description="Raw NL direction for this request")

    @property
    def active_items(self) -> list[BundleItem]:
        """Items that participate in creation (excludes ``EXCLUDE``-role refs)."""

        return [i for i in self.items if i.role is not SourceRole.EXCLUDE]

    def items_with_role(self, role: SourceRole) -> list[BundleItem]:
        """All items explicitly tagged with ``role``."""

        return [i for i in self.items if i.role is role]


class ResolvedSource(BaseModel):
    """A bundle item after resolution: role decided, with the reason recorded.

    ``reason`` is user-facing ("only video in the bundle → spine") and is what
    the plan's role-tagged chips display so inference is never invisible.
    """

    ref: str
    role: SourceRole
    kind: str = Field(description="Asset kind: video | image | audio")
    reason: str = Field(description="Human-readable why this role was assigned")
    explicit: bool = Field(
        default=False, description="True if the user set the role themselves")


class ResolvedBundle(BaseModel):
    """A fully-resolved request bundle — every source has exactly one role."""

    sources: list[ResolvedSource]
    intent: str = ""

    def refs_with_role(self, role: SourceRole) -> list[str]:
        """Refs holding ``role`` (may be empty)."""

        return [s.ref for s in self.sources if s.role is role]

    @property
    def spine_ref(self) -> Optional[str]:
        """The spine source's ref, if one was assigned."""

        refs = self.refs_with_role(SourceRole.SPINE)
        return refs[0] if refs else None


class AmbiguityOption(BaseModel):
    """One selectable answer to an :class:`AmbiguityQuestion`."""

    key: str = Field(description="Stable machine key for the choice")
    label: str = Field(description="Short human label (chip text)")
    description: str = Field(default="", description="One-line consequence")


class AmbiguityQuestion(BaseModel):
    """A single disambiguating question the agent must ask BEFORE proposing.

    Owner rule: no silent defaults. Each question names the refs involved and
    offers the concrete options the resolver can see; the surface renders it as
    one short question card.
    """

    kind: Literal["role_conflict", "contradiction", "gate"] = Field(
        description="role_conflict: >1 candidate for a unique role; "
                    "contradiction: explicit ref clashes with stated intent; "
                    "gate: a capability precondition failed (e.g. no speech)")
    question: str = Field(description="The question, phrased for the user")
    refs: list[str] = Field(default_factory=list,
                            description="Refs this question is about")
    options: list[AmbiguityOption] = Field(min_length=2)


class BundleResolution(BaseModel):
    """Outcome of resolving a bundle: either resolved, or questions to ask.

    ``status == "needs_input"`` carries at least one question and no bundle;
    ``status == "resolved"`` carries the bundle and no questions. The invariant
    is enforced so surfaces can branch on ``status`` alone.
    """

    status: Literal["resolved", "needs_input"]
    bundle: Optional[ResolvedBundle] = None
    questions: list[AmbiguityQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_exclusivity(self) -> "BundleResolution":
        """A resolution is exactly one of: a bundle, or questions."""

        if self.status == "resolved" and (self.bundle is None or self.questions):
            raise ValueError("resolved resolutions carry a bundle and no questions")
        if self.status == "needs_input" and (self.bundle is not None or not self.questions):
            raise ValueError("needs_input resolutions carry questions and no bundle")
        return self


class TreatmentSpec(BaseModel):
    """Audio policy for ONE output — wraps cut-taste ``Knobs``, never leaks
    into them.

    The planner keeps consuming plain :class:`Knobs`; treatment-specific fields
    are applied by the creation service (bite counts, narration synthesis,
    track selection) before/after planning. ``voice_source_ref`` and
    ``music_ref`` may be filled from the resolved bundle when omitted.
    """

    kind: TreatmentKind = TreatmentKind.MUSIC_ONLY
    music: MusicSource = MusicSource.PROVIDED
    music_ref: Optional[str] = Field(
        default=None,
        description="Track ref; required when music is provided/variant_of")
    voice_source_ref: Optional[str] = Field(
        default=None,
        description="Speech source ref for keep/rephrase; defaults to the "
                    "bundle's voice-role source")
    voice: str = Field(
        default=HOUSE_VOICE,
        description="TTS voice for rephrase/new narration (house voice until "
                    "the voice-clone gate opens)")
    knobs: Knobs = Field(default_factory=Knobs,
                         description="Cut taste, passed through to the planner")

    @model_validator(mode="after")
    def _music_ref_required(self) -> "TreatmentSpec":
        """A provided/variant score must name its track."""

        if self.music in (MusicSource.PROVIDED, MusicSource.VARIANT_OF) \
                and not self.music_ref:
            raise ValueError(f"music={self.music} requires music_ref")
        return self

    @property
    def wants_source_speech(self) -> bool:
        """True when the treatment consumes the source's own speech."""

        return self.kind in (TreatmentKind.KEEP_ORIGINAL,
                             TreatmentKind.REPHRASE_ORIGINAL)


class ShortSpec(BaseModel):
    """One requested output: a hypothesis, a duration, and its treatment."""

    hypothesis: str = Field(description="Per-output creative direction (one line)")
    duration_s: float = Field(gt=0, le=180,
                              description="Target output duration in seconds")
    treatment: TreatmentSpec = Field(default_factory=TreatmentSpec)
    name: Optional[str] = Field(default=None,
                                description="Display name; derived if omitted")


class CreationRequest(BaseModel):
    """A complete NLP-cutting request: the bundle plus the outputs wanted.

    This is the contract surfaces submit; the creation service resolves the
    bundle (possibly asking first), plans each short under the hard rules, and
    only renders after an explicit lock.
    """

    bundle: RequestBundle
    shorts: list[ShortSpec] = Field(min_length=1, max_length=8)
    project_id: str = "default"
