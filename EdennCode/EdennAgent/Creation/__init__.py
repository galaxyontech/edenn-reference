"""Agentic creation — the capability layer that composes the verticals.

This package implements the capability-anchored contracts from
``AGENTIC_CREATION.md``: multi-source ``@`` request bundles with per-request
roles, ask-first ambiguity resolution, and per-output audio treatments. It sits
ABOVE the machinery packages (``recompose`` for planning/rendering, ``aul`` for
refs/lineage) and below the surfaces (API routers, consoles): surfaces speak in
these types; machinery never sees them.

Layering:
    domain.py     value objects + typed contracts (roles, bundles, treatments)
    bundle.py     BundleResolver — roles inferred, ambiguity surfaced (ask-first)
"""

from .bundle import BundleCapExceededError, BundleResolver
from .domain import (
    AmbiguityOption,
    AmbiguityQuestion,
    BundleItem,
    BundleResolution,
    CreationRequest,
    MusicSource,
    RequestBundle,
    ResolvedBundle,
    ResolvedSource,
    ShortSpec,
    SourceRole,
    TreatmentKind,
    TreatmentSpec,
)
from .preview import PlanPreview, PlanPreviewBuilder, PreviewSlot, PreviewSource
from .router import create_creation_router
from .service import CreationService, ShortPlan
from .treatments import (
    RephraseOriginalTreatment,
    RephraseResult,
    SourceTranscript,
    TranscriptSpan,
    SpeechRecognitionTranscriber,
)

__all__ = [
    "SourceRole", "TreatmentKind", "MusicSource",
    "BundleItem", "RequestBundle", "ResolvedSource", "ResolvedBundle",
    "AmbiguityOption", "AmbiguityQuestion", "BundleResolution",
    "TreatmentSpec", "ShortSpec", "CreationRequest",
    "BundleResolver", "BundleCapExceededError",
    "PlanPreview", "PlanPreviewBuilder", "PreviewSlot", "PreviewSource",
    "CreationService", "ShortPlan", "create_creation_router",
    "RephraseOriginalTreatment", "RephraseResult", "SpeechRecognitionTranscriber",
    "SourceTranscript", "TranscriptSpan",
]
