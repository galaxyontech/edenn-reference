"""SegmentTree as a VIEW over persisted annotations (UNDERSTANDING_LAYER §1).

Node ids derive from spans (AssetIdentity.node_id), so a plan made in one
process re-renders in another — the fix for the node-id instability gap.
"""

from __future__ import annotations

from typing import Any

from ..Recompose.domain import SegmentNode, SegmentTree
from .models import Annotation
from .refs import AssetIdentity
from .repository import AulRepository


class SegmentTreeAssembler:
    """Rebuilds a recompose ``SegmentTree`` from an asset's stored annotations."""

    def __init__(self, repo: AulRepository) -> None:
        self.repo = repo

    def build(self, asset_id: str) -> SegmentTree:
        asset = self.repo.get_asset(asset_id)
        if asset is None:
            raise KeyError(f"unknown asset: {asset_id}")
        duration = float(asset.duration_s or 0.0)

        root = SegmentNode(
            node_id=AssetIdentity.node_id(asset_id, 0.0, duration),
            asset_id=asset_id, start_s=0.0, end_s=duration, level=0,
            is_still=asset.kind == "image",
        )
        tree = SegmentTree(asset_id=asset_id, root_id=root.node_id,
                           nodes={root.node_id: root})

        semantics = self._by_span(self.repo.get_annotations(asset_id, layer="L2", kind="scene"))
        signals = self._by_span(self.repo.get_annotations(asset_id, layer="L1", kind="signals"))

        child_ids = []
        for shot in self.repo.get_annotations(asset_id, layer="L1", kind="shot"):
            a, b = round(shot.span_start_s, 3), round(shot.span_end_s, 3)
            sem = semantics.get((a, b), {})
            sig = signals.get((a, b), {})
            node = SegmentNode(
                node_id=AssetIdentity.node_id(asset_id, a, b),
                asset_id=asset_id, start_s=a, end_s=b, level=1,
                parent_id=root.node_id,
                summary=str(sem.get("summary") or ""),
                mood=str(sem.get("mood") or ""),
                key_actions=str(sem.get("key_actions") or ""),
                entities=list(sem.get("entities") or []),
                scene_key=sem.get("scene_key") or (shot.payload or {}).get("scene_key"),
                motion=sig.get("motion"), brightness=sig.get("brightness"),
                speech=sig.get("speech"),
                quality_flags=list(sig.get("flags") or []),
                text_inherited=not bool(sem),
            )
            tree.nodes[node.node_id] = node
            child_ids.append(node.node_id)
        root.child_ids = child_ids

        tree.audio_activity = [
            (a.span_start_s, a.span_end_s)
            for a in self.repo.get_annotations(asset_id, layer="L1", kind="audio_activity")
        ]
        return tree

    @staticmethod
    def _by_span(annotations: list[Annotation]) -> dict[tuple[float, float], dict[str, Any]]:
        return {(round(a.span_start_s, 3), round(a.span_end_s, 3)): a.payload
                for a in annotations}
