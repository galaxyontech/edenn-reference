"""Library read-model — all the Board / Connections / Performance aggregation.

Pure business logic over the ``AulRepository`` contract (never a backend's
internals) plus the ``AttributionEngine``. The router is a thin HTTP shell over
this; tests can exercise the whole view layer without FastAPI. The view methods
shape the exact payloads the console (frontend/index.html) renders — grouped
campaigns, relationship subtitles, labeled moments, leading/lift.
"""

from __future__ import annotations

import re as _re
from typing import Any, Optional

from ..AdsAdapters.campaign import AttributionEngine
from ..AssetLibrary.models import Annotation, Asset, Edge
from ..AssetLibrary.repository import AulRepository


def _timecode(s: Optional[float]) -> str:
    s = float(s or 0)
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _short(text: str, words: int = 7) -> str:
    return " ".join((text or "").split()[:words])


class LibraryService:
    def __init__(self, repo: AulRepository, *, attribution: AttributionEngine | None = None) -> None:
        self.repo = repo
        self.attribution = attribution or AttributionEngine(repo)
        self._scene_cache: dict[str, list[Annotation]] = {}

    # ---- helpers
    def _scenes(self, asset_id: str) -> list[Annotation]:
        if asset_id not in self._scene_cache:
            self._scene_cache[asset_id] = self.repo.get_annotations(
                asset_id, layer="L2", kind="scene")
        return self._scene_cache[asset_id]

    def _label_span(self, asset_id: str, a: float, b: float) -> str:
        mid = (a + b) / 2
        for s in self._scenes(asset_id):
            if s.span_start_s is not None and s.span_start_s <= mid <= (s.span_end_s or s.span_start_s):
                text = str(s.payload.get("key_actions") or s.payload.get("summary") or "")
                # drop generic lead-ins so the distinctive part shows in a short chip
                text = _re.sub(r"^(an?\s+)?(black[- ]and[- ]white\s+)?"
                               r"(scene|shot|clip|compilation|animation)?\s*", "", text, flags=_re.I)
                return _short(text) or "moment"
        return "moment"

    @staticmethod
    def _badge(asset: Asset) -> str:
        if asset.kind == "image":
            return "PHOTO"
        if asset.kind == "audio":
            role = (asset.meta.get("role") or "").lower()
            return "VOICE" if role == "voice" else ("MUSIC" if asset.generated else "AUDIO")
        return "SHORT" if asset.generated else "VIDEO"

    @staticmethod
    def _orientation(asset: Asset) -> str:
        if asset.width and asset.height:
            return "9:16" if asset.height > asset.width else "16:9"
        return ""

    # ---- flat asset list (raw projection)
    def list_assets(self, project_id: str = "default") -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.repo.list_assets(project_id)]

    # ---- Board (grouped, enriched)
    def board(self, project_id: str = "default") -> dict[str, Any]:
        summaries = self.repo.list_assets(project_id)
        by_id = {s.asset.asset_id: s for s in summaries}

        # lineage: each output -> the source video it was cut from; each track ->
        # the outputs it scores. Used for both relationship subtitles and grouping.
        cut_source: dict[str, str] = {}          # output -> source asset id
        scored_by: dict[str, list[str]] = {}     # track  -> [output ids]
        for s in summaries:
            aid = s.asset.asset_id
            if s.asset.generated and s.asset.kind == "video":
                srcs = {e.src_asset_id for e in self.repo.edges_to(aid) if e.operation == "slot_cut"}
                if srcs:
                    cut_source[aid] = sorted(srcs)[0]
            if s.asset.kind == "audio":
                outs = [e.dst_asset_id for e in self.repo.edges_from(aid) if e.operation == "music_for"]
                if outs:
                    scored_by[aid] = sorted(set(outs))

        def group_of(aid: str) -> str:
            s = by_id[aid]
            if not s.asset.generated and s.asset.kind == "video":
                return aid                                    # a source anchors its own group
            if aid in cut_source:
                return cut_source[aid]                        # output joins its source
            if aid in scored_by:                              # track joins the source of what it scores
                return cut_source.get(scored_by[aid][0], scored_by[aid][0])
            return "_unsorted"

        cards: dict[str, list[dict[str, Any]]] = {}
        for s in summaries:
            a = s.asset
            aid = a.asset_id
            relation, made_count = self._relation(a, cut_source, scored_by, by_id)
            cards.setdefault(group_of(aid), []).append({
                "asset_id": aid, "name": a.name, "kind": a.kind, "generated": a.generated,
                "badge": self._badge(a), "duration_s": a.duration_s,
                "orientation": self._orientation(a), "relation": relation,
                "made_count": made_count, "layers": s.layers,
                "used_fraction": s.used_fraction,
            })

        groups = []
        for gid, items in cards.items():
            items.sort(key=lambda c: (c["generated"], c["name"]))
            if gid == "_unsorted":
                title, sub = "Unsorted", f"{len(items)} assets"
            else:
                title = by_id[gid].asset.name if gid in by_id else "Campaign"
                sub = f"campaign · {len(items)} assets"
            groups.append({"group_id": gid, "title": title, "subtitle": sub, "assets": items})
        groups.sort(key=lambda g: (g["group_id"] == "_unsorted", -len(g["assets"])))

        kinds = [c for g in groups for c in g["assets"]]
        filters = {
            "All": len(kinds),
            "Video": sum(1 for c in kinds if c["kind"] == "video" and not c["generated"]),
            "Photos": sum(1 for c in kinds if c["kind"] == "image"),
            "Music": sum(1 for c in kinds if c["kind"] == "audio"),
            "Made": sum(1 for c in kinds if c["generated"]),
        }
        return {"groups": groups, "filters": filters}

    def _lineage(self, summaries) -> tuple[dict[str, str], dict[str, list[str]]]:
        """(output -> source video, track -> outputs it scores)."""

        cut_source: dict[str, str] = {}
        scored_by: dict[str, list[str]] = {}
        for s in summaries:
            aid = s.asset.asset_id
            if s.asset.generated and s.asset.kind == "video":
                srcs = {e.src_asset_id for e in self.repo.edges_to(aid) if e.operation == "slot_cut"}
                if srcs:
                    cut_source[aid] = sorted(srcs)[0]
            if s.asset.kind == "audio":
                outs = [e.dst_asset_id for e in self.repo.edges_from(aid) if e.operation == "music_for"]
                if outs:
                    scored_by[aid] = sorted(set(outs))
        return cut_source, scored_by

    # ---- Map (lineage graph)
    def graph(self, project_id: str = "default") -> dict[str, Any]:
        summaries = self.repo.list_assets(project_id)
        cut_source, scored_by = self._lineage(summaries)

        def role(a: Asset) -> str:
            if a.kind == "audio":
                return "aud"
            if a.kind == "image":
                return "img"
            return "out" if a.generated else "src"

        nodes = [{"asset_id": s.asset.asset_id, "name": s.asset.name, "kind": s.asset.kind,
                  "badge": self._badge(s.asset), "generated": s.asset.generated,
                  "duration_s": s.asset.duration_s, "role": role(s.asset),
                  "used_fraction": s.used_fraction} for s in summaries]
        edges = [{"src": src, "dst": out, "kind": "slot_cut"} for out, src in cut_source.items()]
        for tr, outs in scored_by.items():
            edges += [{"src": tr, "dst": o, "kind": "music_for"} for o in outs]

        stats = {}
        for s in summaries:
            if role(s.asset) == "src":
                aid = s.asset.asset_id
                made = [o for o, sr in cut_source.items() if sr == aid]
                stats[aid] = {
                    "pieces": len(made), "used_fraction": s.used_fraction,
                    "scored_by": len({t for t, outs in scored_by.items() if set(outs) & set(made)}),
                    "moments": len({round(sp.start, 1) for sp in self.repo.usage_spans(aid)})}
        cluster = next((s.asset.name for s in summaries if role(s.asset) == "src"), "Library")
        return {"nodes": nodes, "edges": edges, "stats": stats, "cluster": cluster}

    # ---- Agent grounded plan (retrospective, real)
    def plan(self, project_id: str = "default") -> dict[str, Any]:
        published = self.repo.edges_by_operation("published_as", project_id)
        variant_ids = sorted({e.src_ref for e in published})
        takes, total, reused = [], 0, 0
        source_id = track_id = None
        moments_by_key: dict[tuple, tuple] = {}
        for vid in variant_ids:
            v = self.repo.get_asset(vid)
            cuts = [e for e in self.repo.edges_to(vid) if e.operation == "slot_cut"]
            total += len(cuts)
            reused += len(cuts)                                   # every slot is library footage
            for e in cuts:
                source_id = source_id or e.src_asset_id
                a = float(e.src_ref.split("#t=")[1].split("-")[0])
                # prefer opening moments as the hero chips
                rank = 0 if e.params.get("role") == "opening" else 1
                moments_by_key.setdefault((e.src_asset_id, round(a, 1)), (e.src_ref, rank))
            for e in self.repo.edges_to(vid):
                if e.operation == "music_for":
                    track_id = track_id or e.src_asset_id
            takes.append({"name": v.name if v else vid,
                          "hypothesis": (v.meta.get("hypothesis") if v else "") or ""})

        chosen = sorted(moments_by_key.items(), key=lambda kv: (kv[1][1], kv[0][1]))[:5]
        moments = []
        for (aid, _key), (ref, _rank) in chosen:
            a, b = (float(x) for x in ref.split("#t=")[1].split("-"))
            moments.append({"label": self._label_span(aid, a, b), "at": _timecode(a),
                            "dur": round(b - a, 1), "ref": ref, "asset_id": aid})
        track = self.repo.get_asset(track_id) if track_id else None
        return {
            "request": "Cut short TikTok takes from what we already have — ~20s, music-led, one hypothesis each.",
            "reused_slots": reused, "total_slots": total, "gap": None,
            "moments": moments,
            "track": {"asset_id": track_id, "name": track.name} if track else None,
            "takes": takes, "source_id": source_id,
        }

    def _relation(self, a: Asset, cut_source: dict[str, str],
                  scored_by: dict[str, list[str]], by_id: dict) -> tuple[str, int]:
        aid = a.asset_id
        if a.kind == "audio":
            outs = scored_by.get(aid, [])
            if not outs:
                return "not used yet", 0
            if len(outs) == 1 and outs[0] in by_id:
                return f"scores “{by_id[outs[0]].asset.name}”", 0
            return f"scores {len(outs)} pieces", 0
        if a.generated:                                       # a cut
            src = cut_source.get(aid)
            src_name = by_id[src].asset.name if src in by_id else "source"
            ori = self._orientation(a)
            return (f"{ori} · from {src_name}" if ori else f"from {src_name}"), 0
        # a source video
        made = sum(1 for out, s in cut_source.items() if s == aid)
        return ("source footage" if made else "source · not used yet"), made

    # ---- Connections (one asset)
    def asset_detail(self, asset_id: str) -> dict[str, Any] | None:
        asset = self.repo.get_asset(asset_id)
        if asset is None:
            return None
        scenes = self._scenes(asset_id)
        outcomes = self.repo.get_annotations(asset_id, layer="outcome", kind="outcome_daily")
        glob = self.repo.get_annotations(asset_id, layer="L2", kind="global")

        cuts = [e for e in self.repo.edges_from(asset_id) if e.operation == "slot_cut"]
        by_output: dict[str, list[Edge]] = {}
        for e in cuts:
            by_output.setdefault(e.dst_ref, []).append(e)

        made_from_this = []
        for out_id, edges in sorted(by_output.items()):
            out = self.repo.get_asset(out_id)
            has_bite = any(e.params.get("is_bite") for e in edges)
            has_music = any(e.operation == "music_for" for e in self.repo.edges_to(out_id))
            arrow = "cut + voices" if has_bite else ("cut + score" if has_music else "cut")
            made_from_this.append({
                "asset_id": out_id, "name": out.name if out else out_id,
                "duration_s": out.duration_s if out else None,
                "slots": sorted(e.params.get("slot", -1) for e in edges),
                "hypothesis": next((e.params.get("hypothesis") for e in edges
                                    if e.params.get("hypothesis")), ""),
                "arrow": arrow,
            })

        appears_together = []
        for out_id in sorted(by_output):
            out_name = (self.repo.get_asset(out_id) or asset).name
            for e in self.repo.edges_to(out_id):
                if e.operation == "music_for" and e.src_asset_id != asset_id:
                    src = self.repo.get_asset(e.src_asset_id)
                    appears_together.append({
                        "asset_id": e.src_asset_id, "name": src.name if src else e.src_asset_id,
                        "relation": f"scores “{out_name}”"})

        # moments: the distinct source spans that were used, labeled by scene
        moments = []
        seen = set()
        for span in self.repo.usage_spans(asset_id):
            key = round(span.start, 1)
            if key in seen:
                continue
            seen.add(key)
            moments.append({"label": self._label_span(asset_id, span.start, span.end),
                            "at": _timecode(span.start),
                            "dur": round(span.end - span.start, 1)})
        moments.sort(key=lambda m: m["at"])

        entities = sorted({e for s in scenes for e in (s.payload.get("entities") or [])})
        description = ""
        if glob:
            description = str(glob[0].payload.get("description") or "")
        if not description and scenes:
            description = str(scenes[0].payload.get("summary") or "")

        return {
            "asset": {**asset.as_dict(), "badge": self._badge(asset),
                      "orientation": self._orientation(asset), "used_in": len(by_output)},
            "description": description,
            "tags": (entities or [asset.meta.get("has_audio") and "has audio"])[:8],
            "scenes": [{"start_s": s.span_start_s, "end_s": s.span_end_s, **s.payload}
                       for s in scenes],
            "made_from_this": made_from_this,
            "appears_together": appears_together,
            "moments": moments[:6],
            "usage_spans": [list(s) for s in self.repo.usage_spans(asset_id)],
            "outcome_days": len(outcomes),
        }

    # ---- Search
    def search(self, query: str, project_id: str = "default") -> dict[str, Any]:
        hits = self.repo.search(query, project_id)
        return {"hits": [{"asset_id": h.asset_id, "start_s": h.span_start_s,
                          "end_s": h.span_end_s, "at": _timecode(h.span_start_s),
                          "summary": _short(str(h.payload.get("summary") or ""), 14)}
                         for h in hits]}

    # ---- Performance
    def performance(self, project_id: str = "default") -> dict[str, Any]:
        published = self.repo.edges_by_operation("published_as", project_id)
        channel_of: dict[str, str] = {}
        for e in published:  # first publish wins (edges come back created_at-ordered)
            channel_of.setdefault(e.src_ref, e.params.get("channel", ""))
        variant_ids = sorted({e.src_ref for e in published})

        variants = []
        for vid in variant_ids:
            asset = self.repo.get_asset(vid)
            totals = self.attribution.variant_totals(vid)
            variants.append({**totals.as_dict(),
                             "name": asset.name if asset else vid,
                             "hypothesis": (asset.meta.get("hypothesis") if asset else "") or "",
                             "channel": channel_of.get(vid, "")})
        avg_ctr = sum(v["ctr"] for v in variants) / len(variants) if variants else 0.0
        top = max((v["ctr"] for v in variants), default=0.0)
        for v in variants:
            v["leading"] = bool(variants) and v["ctr"] == top and top > 0
            v["ctr_lift"] = round((v["ctr"] - avg_ctr) / avg_ctr * 100, 0) if avg_ctr else 0.0

        openings = [self._label_component(c) for c in
                    self.attribution.component_attribution(variant_ids, role="opening")]
        components = [self._label_component(c) for c in
                      self.attribution.component_attribution(variant_ids)[:10]]
        music = [self._label_component(c, is_track=True) for c in
                 self.attribution.component_attribution(variant_ids, operation="music_for")]
        # relative bar widths + lift for the component panels
        for group in (openings, components):
            self._rank(group, avg_ctr)
        self._rank(music, avg_ctr, key="cvr")
        return {"variants": variants, "openings": openings, "components": components,
                "music": music, "evidence": "observational", "avg_ctr": round(avg_ctr, 5)}

    def _label_component(self, c, *, is_track: bool = False) -> dict[str, Any]:
        d = c.as_dict()
        ref = d["ref"]
        base = ref.split("#t=")[0]
        if "#t=" in ref:
            a, b = (float(x) for x in ref.split("#t=")[1].split("-"))
            d["label"] = self._label_span(base, a, b)
            d["at"] = _timecode(a)
        else:
            asset = self.repo.get_asset(base)
            d["label"] = asset.name if asset else base
            d["at"] = ""
        # which variants it opens/appears in
        d["variant_names"] = sorted(
            (self.repo.get_asset(v).name if self.repo.get_asset(v) else v)
            for v in self._variants_using(ref))
        return d

    def _variants_using(self, ref: str) -> set[str]:
        return {e.dst_asset_id for e in self.repo.edges_from(ref.split("#t=")[0])
                if e.src_ref == ref}

    @staticmethod
    def _rank(group: list[dict], avg_ctr: float, key: str = "ctr") -> None:
        top = max((c[key] for c in group), default=0.0)
        for c in group:
            c["bar"] = round(c[key] / top * 100, 0) if top else 0.0
            c["lift"] = round((c["ctr"] - avg_ctr) / avg_ctr * 100, 0) if avg_ctr else 0.0
