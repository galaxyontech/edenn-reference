"""Recompose domain objects — the five durable representations.

Pipeline (DESIGN.md §1): AssetRecord -> SegmentTree (textual space) +
MusicSheet -> CutSpec (music x knobs) -> RecomposePlan (passages -> slots)
-> RenderedVariant. Everything here is pure data: JSON-serializable,
versioned, storage-agnostic (local paths now, artifact ids when the
platform wiring lands). Tree BUILD/SPLIT logic lives in segmentation.py;
this module only holds structure and simple queries.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

AssetKind = Literal["video", "image"]
CutDensity = Literal["sparse", "medium", "dense"]
CoherenceMode = Literal["single_story", "spine_and_accents", "interleave"]
EnergyLiteralness = Literal["low", "high"]
SlotRole = Literal["opening", "body", "hero", "closing"]
SourceAudioMode = Literal["mute", "duck"]  # keep-original-only is post-MVP
NarrationMode = Literal["none", "generate"]
MusicMode = Literal["provided", "generate"]
RestoreMode = Literal["off", "auto"]  # deterministic EDIT-tier exposure lift


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------- assets
class AssetRecord(BaseModel):
    schema_version: int = SCHEMA_VERSION
    asset_id: str = Field(default_factory=lambda: new_id("asset"))
    kind: AssetKind
    path: str
    artifact_id: Optional[str] = None  # platform wiring (M6)
    duration_s: Optional[float] = None  # None for images
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    sha256: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------- segment tree
class SegmentNode(BaseModel):
    """One node of the adaptive-depth tree, understood in textual space.

    ``summary``/``mood``/``entities`` are the textual understanding; children
    inherit the parent's text (``text_inherited=True``) until a refine pass
    replaces it. Numeric signals are cheap and computed per node.
    """

    node_id: str = Field(default_factory=lambda: new_id("seg"))
    asset_id: str
    start_s: float
    end_s: float
    level: int = 0
    parent_id: Optional[str] = None
    child_ids: list[str] = Field(default_factory=list)

    # textual space
    summary: str = ""
    mood: str = ""
    key_actions: str = ""
    entities: list[str] = Field(default_factory=list)
    text_inherited: bool = False
    # visual-family identity for anti-repetition: the SEMANTIC scene this node
    # belongs to (observation scene), not the level-1 fragment — sibling
    # fragments of one semantic scene must count as the same scene.
    scene_key: Optional[str] = None

    # numeric signals (None = not measured yet)
    motion: Optional[float] = None
    brightness: Optional[float] = None
    # 0..1 likelihood the segment carries speech/narration in its ORIGINAL
    # audio (silence-activity x vocal-band ratio heuristic). Drives the
    # source_audio knob: chopping mid-sentence is audible when audio is kept.
    speech: Optional[float] = None
    quality_flags: list[str] = Field(default_factory=list)

    is_still: bool = False  # image leaf: duration is unbounded at render time

    @property
    def dur_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def is_leaf(self) -> bool:
        return not self.child_ids

    def text(self) -> str:
        return " ".join(p for p in (self.summary, self.key_actions, self.mood) if p)


class SegmentTree(BaseModel):
    schema_version: int = SCHEMA_VERSION
    asset_id: str
    root_id: str
    nodes: dict[str, SegmentNode]
    # full-file scene-cut caches per sensitivity level, so lazy splits never
    # re-run detection: {"level_name": [timestamps...]}
    cut_cache: dict[str, list[float]] = Field(default_factory=dict)
    # asset-level audio activity spans (silencedetect), the raw material for
    # sound bites; filled by annotate_tree_signals
    audio_activity: list[tuple[float, float]] = Field(default_factory=list)

    def node(self, node_id: str) -> SegmentNode:
        return self.nodes[node_id]

    @property
    def root(self) -> SegmentNode:
        return self.nodes[self.root_id]

    def leaves(self) -> list[SegmentNode]:
        return sorted(
            (n for n in self.nodes.values() if n.is_leaf),
            key=lambda n: (n.start_s, n.end_s),
        )

    def leaves_within(self, start_s: float, end_s: float) -> list[SegmentNode]:
        return [n for n in self.leaves() if n.start_s < end_s and n.end_s > start_s]

    def max_level(self) -> int:
        return max(n.level for n in self.nodes.values())

    def add_children(self, parent_id: str, boundaries: list[float]) -> list[str]:
        """Split ``parent`` at interior ``boundaries`` (strictly inside it).

        Returns the new child ids in temporal order. Structure-only — callers
        (segmentation.split_node) decide boundaries and inherit text.
        """

        parent = self.nodes[parent_id]
        if parent.child_ids:
            raise ValueError(f"node {parent_id} already has children")
        cuts = sorted(b for b in boundaries if parent.start_s < b < parent.end_s)
        if not cuts:
            return []
        edges = [parent.start_s, *cuts, parent.end_s]
        child_ids: list[str] = []
        for a, b in zip(edges, edges[1:]):
            child = SegmentNode(
                asset_id=parent.asset_id,
                start_s=a,
                end_s=b,
                level=parent.level + 1,
                parent_id=parent_id,
                summary=parent.summary,
                mood=parent.mood,
                key_actions=parent.key_actions,
                entities=list(parent.entities),
                text_inherited=True,
                scene_key=parent.scene_key,
                is_still=parent.is_still,
            )
            self.nodes[child.node_id] = child
            child_ids.append(child.node_id)
        parent.child_ids = child_ids
        return child_ids


# ---------------------------------------------------------------- music sheet
class MusicPhrase(BaseModel):
    index: int
    start_s: float
    end_s: float
    energy: float  # mean normalized energy within the phrase


class MusicSheet(BaseModel):
    schema_version: int = SCHEMA_VERSION
    track_path: str
    track_sha256: Optional[str] = None
    tempo_bpm: float
    duration_s: float
    window_start_s: float
    window_s: float
    beats: list[float]  # absolute track times within the window
    beat_energy: list[float]  # normalized 0..1, parallel to beats
    phrases: list[MusicPhrase] = Field(default_factory=list)


# ------------------------------------------------------------------- cut spec
class Knobs(BaseModel):
    """User taste as first-class plan fields (DESIGN.md §1).

    W0 shipped the equivalent of 'dense'; owner feedback set the default to
    'medium'. Locks pin a slot to a node across re-plans.
    """

    cut_density: CutDensity = "medium"
    coherence_mode: CoherenceMode = "spine_and_accents"
    energy_literalness: EnergyLiteralness = "high"
    min_shot_s: float = 0.6
    max_shot_s: float = 5.0
    # Anti-repetition (owner, 2026-07-15): one level-1 scene may serve at most
    # this many slots, and never two consecutive slots (adjacency is banned
    # outright in the planner regardless of this value).
    max_slots_per_scene: int = 2
    locks: dict[int, str] = Field(default_factory=dict)  # slot index -> node_id

    # Audio layer (owner, 2026-07-15): what happens to the ORIGINAL audio
    # (footage may already carry narration/dialogue), whether we narrate on
    # top, and where the music comes from.
    source_audio: SourceAudioMode = "mute"
    narration: NarrationMode = "none"
    narration_voice: str = "nova"
    narration_tone: str = "calm, warm, unhurried"
    narration_speed: float = 1.0
    music: MusicMode = "provided"
    music_volume: float = 0.85
    narration_volume: float = 1.0
    duck_gain_db: float = -9.0

    # Preserve up to N of the source's own voice-over moments as intact
    # sound bites (0 = off). Bites are speech spans played at natural length
    # with the music ducked — cutting THROUGH speech is what destroys it.
    sound_bites: int = 0

    # Underexposed/faded sources (old film transfers): 'auto' applies a
    # deterministic histogram-normalize + gentle gamma/saturation lift to
    # slot renders when the asset's measured median luma is low (2026-07-15:
    # a 'dark' home movie turned out to be daylight buried in the transfer).
    restore: RestoreMode = "auto"


class SlotSpec(BaseModel):
    index: int
    t_start: float  # output-timeline position (window-relative)
    dur_s: float
    energy: float
    role: SlotRole = "body"
    passage_index: int = 0
    # Sound bite: this slot exists to let a natural speech span from the
    # source play INTACT (its full sentence length, source audio at full,
    # music ducked under it) — the fix for "the original voice-over is gone".
    is_bite: bool = False


class CutSpec(BaseModel):
    schema_version: int = SCHEMA_VERSION
    knobs: Knobs
    slots: list[SlotSpec]

    @property
    def total_s(self) -> float:
        return sum(s.dur_s for s in self.slots)


# ----------------------------------------------------------------------- plan
class Passage(BaseModel):
    index: int
    start_s: float
    end_s: float
    semantic_focus: str = ""  # subject/theme cluster; with <=2 assets: spine/accent framing
    arc_note: str = ""
    slot_indices: list[int] = Field(default_factory=list)


class PlannedSlot(BaseModel):
    spec: SlotSpec
    node_id: str
    asset_id: str
    seg_in_s: float  # in-point inside the source asset
    why: str = ""
    locked: bool = False


class RecomposePlan(BaseModel):
    schema_version: int = SCHEMA_VERSION
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    parent_plan_id: Optional[str] = None  # every knob change / re-plan is a child
    hypothesis: str = ""
    knobs: Knobs = Field(default_factory=Knobs)
    passages: list[Passage] = Field(default_factory=list)
    slots: list[PlannedSlot] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    spine_asset_id: Optional[str] = None  # coherence_mode=spine_and_accents
    narration_script: Optional[str] = None  # persisted for lineage/regeneration


# -------------------------------------------------------------------- variant
class CutReport(BaseModel):
    duration_s: float
    n_slots: int
    cut_to_beat_ms_median: Optional[float] = None
    cut_to_beat_ms_p95: Optional[float] = None
    notes: dict[str, Any] = Field(default_factory=dict)


class RenderedVariant(BaseModel):
    schema_version: int = SCHEMA_VERSION
    variant_id: str = Field(default_factory=lambda: new_id("variant"))
    plan_id: str
    path: str
    artifact_id: Optional[str] = None
    cut_report: Optional[CutReport] = None
    slot_renders: dict[int, str] = Field(default_factory=dict)  # slot idx -> cached path
