"""AUL domain entities and read projections.

These are the currency of the repository layer — typed records instead of raw
dict rows. Each knows how to (de)serialize itself: ``from_row`` maps a DB/store
row in, ``as_dict`` maps a JSON-ready view out (preserving the ``*_json`` key
names the persistence layer and API clients already use).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TimeSpan(NamedTuple):
    """A half-open ``[start, end)`` span in seconds. Unpacks like a tuple."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Asset:
    """A stored asset (source or generated). Identity is content-addressed."""

    asset_id: str
    kind: str                       # video | image | audio
    sha256: str
    name: str
    uri: str
    project_id: str = "default"
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    generated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: Any = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Asset":
        return cls(
            asset_id=row["asset_id"], kind=row["kind"], sha256=row.get("sha256", ""),
            name=row["name"], uri=row["uri"], project_id=row.get("project_id", "default"),
            duration_s=row.get("duration_s"), width=row.get("width"), height=row.get("height"),
            generated=bool(row.get("generated", False)),
            meta=dict(row.get("meta_json") or row.get("meta") or {}),
            created_at=row.get("created_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id, "project_id": self.project_id, "kind": self.kind,
            "sha256": self.sha256, "name": self.name, "uri": self.uri,
            "duration_s": self.duration_s, "width": self.width, "height": self.height,
            "generated": self.generated, "meta_json": self.meta,
        }


@dataclass
class AssetSummary:
    """An asset plus the rollups the Board needs (layers present, footage used)."""

    asset: Asset
    layers: list[str] = field(default_factory=list)
    used_fraction: float = 0.0
    n_uses: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Board view — the curated fields the console renders (no uri/sha leak)."""

        a = self.asset
        return {
            "asset_id": a.asset_id, "name": a.name, "kind": a.kind,
            "generated": a.generated, "duration_s": a.duration_s,
            "width": a.width, "height": a.height,
            "layers": sorted(self.layers), "n_uses": self.n_uses,
            "used_fraction": self.used_fraction,
        }


@dataclass
class Annotation:
    """One producer's fact about an asset or a span of it (append-only)."""

    asset_id: str
    layer: str                      # L0 | L1 | L2 | L3 | outcome
    kind: str                       # tech | shot | scene | signals | beat_grid | outcome_daily | ...
    producer: str                   # "scene_detector@v1", ...
    project_id: str = "default"
    span_start_s: Optional[float] = None
    span_end_s: Optional[float] = None
    inputs_hash: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    annotation_id: Optional[str] = None
    created_at: Any = None

    @property
    def span(self) -> Optional[TimeSpan]:
        if self.span_start_s is None or self.span_end_s is None:
            return None
        return TimeSpan(self.span_start_s, self.span_end_s)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Annotation":
        return cls(
            asset_id=row["asset_id"], layer=row["layer"], kind=row["kind"],
            producer=row["producer"], project_id=row.get("project_id", "default"),
            span_start_s=row.get("span_start_s"), span_end_s=row.get("span_end_s"),
            inputs_hash=row.get("inputs_hash"),
            payload=dict(row.get("payload_json") or row.get("payload") or {}),
            annotation_id=row.get("annotation_id"), created_at=row.get("created_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id, "project_id": self.project_id,
            "asset_id": self.asset_id, "span_start_s": self.span_start_s,
            "span_end_s": self.span_end_s, "layer": self.layer, "kind": self.kind,
            "producer": self.producer, "inputs_hash": self.inputs_hash,
            "payload_json": self.payload,
        }


@dataclass
class Edge:
    """A lineage edge between two refs (an operation with parameters)."""

    src_ref: str
    dst_ref: str
    operation: str                  # slot_cut | music_for | narration_for | published_as | ...
    project_id: str = "default"
    params: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    job_id: Optional[str] = None
    edge_id: Optional[str] = None
    created_at: Any = None

    @property
    def src_asset_id(self) -> str:
        return self.src_ref.split("#", 1)[0]

    @property
    def dst_asset_id(self) -> str:
        return self.dst_ref.split("#", 1)[0]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Edge":
        return cls(
            src_ref=row["src_ref"], dst_ref=row["dst_ref"], operation=row["operation"],
            project_id=row.get("project_id", "default"),
            params=dict(row.get("params_json") or row.get("params") or {}),
            session_id=row.get("session_id"), job_id=row.get("job_id"),
            edge_id=row.get("edge_id"), created_at=row.get("created_at"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id, "project_id": self.project_id,
            "src_ref": self.src_ref, "dst_ref": self.dst_ref,
            "operation": self.operation, "params_json": self.params,
            "session_id": self.session_id, "job_id": self.job_id,
        }
