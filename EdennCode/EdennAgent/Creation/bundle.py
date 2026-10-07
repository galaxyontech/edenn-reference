"""Bundle resolution: roles inferred where certain, asked where not.

:class:`BundleResolver` is deliberately **rule-based and deterministic** — it
implements the owner's ask-first policy structurally rather than guessing with
a model. The rules (AGENTIC_CREATION.md §"Multi-source"):

* explicit roles are honored (after a kind-compatibility check);
* a SINGLE video with no explicit spine → spine (certain, stated in ``reason``);
* MULTIPLE role-less videos → ask which is the spine (no silent default);
* images → accent (certain);
* a single role-less audio → music, UNLESS the intent asks for fresh music
  alongside it or mentions using its voice — then ask;
* multiple role-less audios → ask (music vs voice cannot be assumed);
* ``style``/``exclude`` are only ever explicit;
* more than :data:`~.domain.MAX_BUNDLE_SOURCES` active sources → typed error
  (a hard product cap, not an ambiguity).

The resolver never touches the network or the store: callers supply each ref's
asset kind, which keeps this class trivially unit-testable and reusable from
any surface.
"""

from __future__ import annotations

import re
from typing import Mapping

from .domain import (
    MAX_BUNDLE_SOURCES,
    AmbiguityOption,
    AmbiguityQuestion,
    BundleItem,
    BundleResolution,
    RequestBundle,
    ResolvedBundle,
    ResolvedSource,
    SourceRole,
)

# Roles each asset kind can legally hold. STYLE is kind-agnostic (a prior
# output of any kind can seed parameters); EXCLUDE likewise.
_KIND_ROLES: dict[str, set[SourceRole]] = {
    "video": {SourceRole.SPINE, SourceRole.ACCENT, SourceRole.VOICE,
              SourceRole.STYLE, SourceRole.EXCLUDE},
    "image": {SourceRole.ACCENT, SourceRole.STYLE, SourceRole.EXCLUDE},
    "audio": {SourceRole.MUSIC, SourceRole.VOICE, SourceRole.STYLE,
              SourceRole.EXCLUDE},
}

# Coarse intent probes. These gate ASKING, never silent decisions, so false
# negatives cost one extra question at worst.
_FRESH_MUSIC_RE = re.compile(
    r"\b(fresh|new|different|another|generate[d]?)\b.{0,24}\b(music|track|score|song)\b"
    r"|\b(music|track|score|song)\b.{0,24}\b(fresh|new|different|generate[d]?)\b",
    re.IGNORECASE | re.DOTALL)
_VOICE_INTENT_RE = re.compile(
    r"\b(voice|voice-?over|narrat\w*|announcer|speech|spoken|dialogue|rephras\w*)\b",
    re.IGNORECASE)


class BundleCapExceededError(ValueError):
    """Raised when a bundle exceeds the owner-approved source ceiling."""

    def __init__(self, n_active: int) -> None:
        super().__init__(
            f"{n_active} active sources exceed the cap of {MAX_BUNDLE_SOURCES} "
            f"(AGENTIC_CREATION.md) — drop sources or mark some as 'exclude'")
        self.n_active: int = n_active


class BundleResolver:
    """Assigns a :class:`SourceRole` to every bundle item — or asks.

    Stateless; safe to share. The single public entry point is
    :meth:`resolve`.
    """

    def resolve(self, bundle: RequestBundle,
                kinds: Mapping[str, str]) -> BundleResolution:
        """Resolve ``bundle`` into roles, or return the questions to ask first.

        Args:
            bundle: the user's request (items + raw intent).
            kinds: mapping of BARE asset id → kind (``video|image|audio``) for
                every item in the bundle; the caller (service) looks these up.

        Returns:
            A :class:`BundleResolution` — ``resolved`` with every source
            role-tagged and reasoned, or ``needs_input`` with 1+
            :class:`AmbiguityQuestion` s and no bundle (ask first, then
            propose).

        Raises:
            BundleCapExceededError: more than :data:`MAX_BUNDLE_SOURCES`
                active (non-exclude) sources.
            KeyError: an item's asset id is missing from ``kinds``.
            ValueError: an explicit role is impossible for the asset's kind
                (e.g. ``music`` on an image) — a malformed request, not an
                ambiguity.
        """

        active = bundle.active_items
        if len(active) > MAX_BUNDLE_SOURCES:
            raise BundleCapExceededError(len(active))

        for item in bundle.items:
            kind = kinds[item.asset_id]
            if item.role is not None and item.role not in _KIND_ROLES[kind]:
                raise ValueError(
                    f"role '{item.role}' is impossible for {kind} asset "
                    f"{item.ref!r}")

        questions: list[AmbiguityQuestion] = []
        questions += self._spine_questions(bundle, kinds)
        questions += self._audio_questions(bundle, kinds)
        if questions:
            return BundleResolution(status="needs_input", questions=questions)

        return BundleResolution(
            status="resolved",
            bundle=ResolvedBundle(intent=bundle.intent,
                                  sources=self._assign(bundle, kinds)))

    # ------------------------------------------------------------- questions
    def _spine_questions(self, bundle: RequestBundle,
                         kinds: Mapping[str, str]) -> list[AmbiguityQuestion]:
        """Spine must be singular: ask when >1 candidate and none (or several)
        are explicit."""

        explicit_spine = bundle.items_with_role(SourceRole.SPINE)
        free_videos = [i for i in bundle.items
                       if i.role is None and kinds[i.asset_id] == "video"]
        if len(explicit_spine) > 1:
            return [AmbiguityQuestion(
                kind="role_conflict",
                question="Two sources are marked as the spine — which one "
                         "should lead? The other becomes b-roll.",
                refs=[i.ref for i in explicit_spine],
                options=[AmbiguityOption(key=i.ref, label=i.ref,
                                         description="Lead the cut")
                         for i in explicit_spine])]
        if explicit_spine or len(free_videos) <= 1:
            return []
        return [AmbiguityQuestion(
            kind="role_conflict",
            question="Which video should lead the cut? The others become b-roll.",
            refs=[i.ref for i in free_videos],
            options=[AmbiguityOption(key=i.ref, label=i.ref,
                                     description="Use as the spine; the rest "
                                                 "weave in as accents")
                     for i in free_videos])]

    def _audio_questions(self, bundle: RequestBundle,
                         kinds: Mapping[str, str]) -> list[AmbiguityQuestion]:
        """Ambiguous audio roles and music contradictions → ask, don't guess."""

        questions: list[AmbiguityQuestion] = []
        free_audio = [i for i in bundle.items
                      if i.role is None and kinds[i.asset_id] == "audio"]
        voice_intent = bool(_VOICE_INTENT_RE.search(bundle.intent))

        if len(free_audio) > 1 or (free_audio and voice_intent):
            questions.append(AmbiguityQuestion(
                kind="role_conflict",
                question="How should the referenced audio be used?",
                refs=[i.ref for i in free_audio],
                options=[
                    AmbiguityOption(key="music", label="As the score",
                                    description="Plays under the cut"),
                    AmbiguityOption(key="voice", label="As the voice source",
                                    description="Its speech carries the message "
                                                "(bites or rephrase)"),
                ]))

        referenced_music = ([i.ref for i in bundle.items_with_role(SourceRole.MUSIC)]
                            + [i.ref for i in free_audio])
        if referenced_music and _FRESH_MUSIC_RE.search(bundle.intent):
            questions.append(AmbiguityQuestion(
                kind="contradiction",
                question="You referenced a track but asked for fresh music — "
                         "which should it be?",
                refs=referenced_music,
                options=[
                    AmbiguityOption(key="provided", label="Use the track",
                                    description="Score with the referenced track"),
                    AmbiguityOption(key="variant_of", label="Make a variant of it",
                                    description="Generate music seeded by it"),
                    AmbiguityOption(key="generate", label="Generate fresh",
                                    description="New music, track kept aside"),
                ]))
        return questions

    # -------------------------------------------------------------- assignment
    def _assign(self, bundle: RequestBundle,
                kinds: Mapping[str, str]) -> list[ResolvedSource]:
        """Assign roles once :meth:`resolve` has established there is no
        ambiguity left. Every assignment records its human-readable reason."""

        has_explicit_spine = bool(bundle.items_with_role(SourceRole.SPINE))
        out: list[ResolvedSource] = []
        for item in bundle.items:
            kind = kinds[item.asset_id]
            if item.role is not None:
                out.append(ResolvedSource(
                    ref=item.ref, role=item.role, kind=kind,
                    reason="set by you", explicit=True))
            elif kind == "video" and has_explicit_spine:
                out.append(ResolvedSource(
                    ref=item.ref, role=SourceRole.ACCENT, kind=kind,
                    reason="a spine is already chosen — this weaves in as b-roll"))
            elif kind == "video":
                out.append(ResolvedSource(
                    ref=item.ref, role=SourceRole.SPINE, kind=kind,
                    reason="only video in the bundle — it leads the cut"))
            elif kind == "image":
                out.append(ResolvedSource(
                    ref=item.ref, role=SourceRole.ACCENT, kind=kind,
                    reason="stills weave in as accents"))
            else:  # audio, unambiguous by construction here
                out.append(ResolvedSource(
                    ref=item.ref, role=SourceRole.MUSIC, kind=kind,
                    reason="audio with no voice ask — used as the score"))
        return out
