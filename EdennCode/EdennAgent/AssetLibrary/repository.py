"""AUL persistence — one contract, two backends.

``AulRepository`` is the abstract contract every consumer codes against.
``InMemoryAulRepository`` is the hermetic twin for tests; ``PostgresAulRepository``
is the shared-DB implementation (agentic_audio conventions: fresh client per op
via an injectable factory, idempotent schema bootstrap from the migration file).

Because both subclass the ABC, the twin can never silently drift from the real
store without failing the shared contract.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from .models import Annotation, Asset, AssetSummary, Edge, TimeSpan, new_id
from .refs import Ref

MIGRATION_PATH = Path(__file__).parent / "migrations" / "001_aul.sql"


class AulRepository(ABC):
    """The AUL store contract: assets, annotations, and lineage edges."""

    def ensure_schema(self) -> None:
        """Idempotently make the backing store ready (no-op for in-memory)."""

    # ---- assets
    @abstractmethod
    def upsert_asset(self, asset: Asset) -> Asset: ...

    @abstractmethod
    def get_asset(self, asset_id: str) -> Optional[Asset]: ...

    @abstractmethod
    def list_assets(self, project_id: str = "default") -> list[AssetSummary]: ...

    # ---- annotations
    @abstractmethod
    def add_annotations(self, annotations: list[Annotation]) -> int: ...

    @abstractmethod
    def get_annotations(self, asset_id: str, *, layer: Optional[str] = None,
                        kind: Optional[str] = None) -> list[Annotation]: ...

    @abstractmethod
    def delete_annotations(self, asset_id: str, *,
                           producers: Optional[set[str]] = None) -> int:
        """Remove an asset's annotations (all, or only the given producers).
        Lets re-running a producer replace its rows instead of duplicating them."""

    @abstractmethod
    def search(self, query: str, project_id: str = "default",
               limit: int = 20) -> list[Annotation]: ...

    # ---- edges
    @abstractmethod
    def add_edge(self, edge: Edge) -> Edge: ...

    @abstractmethod
    def edges_from(self, asset_id: str) -> list[Edge]: ...

    @abstractmethod
    def edges_to(self, asset_id: str) -> list[Edge]: ...

    @abstractmethod
    def edges_by_operation(self, operation: str,
                           project_id: str = "default") -> list[Edge]: ...

    @abstractmethod
    def usage_spans(self, asset_id: str) -> list[TimeSpan]: ...

    # ---- shared derivations (identical for every backend)
    def _summarize(self, asset: Asset, layers: list[str], covered_s: float,
                   n_uses: int) -> AssetSummary:
        total = float(asset.duration_s or 0.0)
        used_fraction = round(covered_s / total, 3) if total else 0.0
        return AssetSummary(asset=asset, layers=list(layers),
                            used_fraction=used_fraction, n_uses=n_uses)


class InMemoryAulRepository(AulRepository):
    """Hermetic twin of the Postgres store (same contract, in-process state)."""

    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}
        self._annotations: list[Annotation] = []
        self._edges: list[Edge] = []

    # ---- assets
    def upsert_asset(self, asset: Asset) -> Asset:
        existing = self._assets.get(asset.asset_id)
        if existing is None:
            self._assets[asset.asset_id] = asset
            return asset
        for f in ("project_id", "kind", "sha256", "name", "uri",
                  "duration_s", "width", "height", "created_at"):
            v = getattr(asset, f)
            if v is not None:
                setattr(existing, f, v)
        existing.generated = asset.generated
        existing.meta = asset.meta            # parity with prior wholesale replace
        return existing

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        return self._assets.get(asset_id)

    def list_assets(self, project_id: str = "default") -> list[AssetSummary]:
        out = []
        for asset in self._assets.values():
            if asset.project_id != project_id:
                continue
            layers = {a.layer for a in self._annotations if a.asset_id == asset.asset_id}
            spans = self.usage_spans(asset.asset_id)
            covered = sum(s.duration for s in spans)
            out.append(self._summarize(asset, sorted(layers), covered, len(spans)))
        return out

    # ---- annotations
    def add_annotations(self, annotations: list[Annotation]) -> int:
        for ann in annotations:
            if ann.annotation_id is None:
                ann.annotation_id = new_id("ann")
            self._annotations.append(ann)
        return len(annotations)

    def get_annotations(self, asset_id: str, *, layer: Optional[str] = None,
                        kind: Optional[str] = None) -> list[Annotation]:
        out = [a for a in self._annotations if a.asset_id == asset_id
               and (layer is None or a.layer == layer)
               and (kind is None or a.kind == kind)]
        return sorted(out, key=lambda a: (a.span_start_s if a.span_start_s is not None else -1))

    def delete_annotations(self, asset_id: str, *,
                           producers: Optional[set[str]] = None) -> int:
        before = len(self._annotations)
        self._annotations = [a for a in self._annotations if not (
            a.asset_id == asset_id and (producers is None or a.producer in producers))]
        return before - len(self._annotations)

    def search(self, query: str, project_id: str = "default",
               limit: int = 20) -> list[Annotation]:
        terms = [t for t in query.lower().split() if len(t) > 2]
        scored = []
        for a in self._annotations:
            if a.project_id != project_id or a.layer != "L2":
                continue
            text = json.dumps(a.payload).lower()
            score = sum(1 for t in terms if t in text)
            if score:
                scored.append((score, a))
        scored.sort(key=lambda x: -x[0])
        return [a for _, a in scored[:limit]]

    # ---- edges
    def add_edge(self, edge: Edge) -> Edge:
        if edge.edge_id is None:
            edge.edge_id = new_id("edge")
        self._edges.append(edge)
        return edge

    def edges_from(self, asset_id: str) -> list[Edge]:
        base = Ref.asset_of(asset_id)
        return [e for e in self._edges if e.src_asset_id == base]

    def edges_to(self, asset_id: str) -> list[Edge]:
        base = Ref.asset_of(asset_id)
        return [e for e in self._edges if e.dst_asset_id == base]

    def edges_by_operation(self, operation: str,
                           project_id: str = "default") -> list[Edge]:
        return [e for e in self._edges
                if e.operation == operation and e.project_id == project_id]

    def usage_spans(self, asset_id: str) -> list[TimeSpan]:
        spans = []
        for e in self._edges:
            if (e.src_asset_id == asset_id and "#t=" in e.src_ref
                    and e.operation != "published_as"):
                a, b = e.src_ref.split("#t=")[1].split("-")
                spans.append(TimeSpan(float(a), float(b)))
        return sorted(spans)


class PostgresAulRepository(AulRepository):
    """Postgres-backed store on the shared DB."""

    def __init__(self, *, client_factory: Optional[Callable[[], Any]] = None) -> None:
        if client_factory is None:
            from EdennCode.Deployment.postgres_wrapper import PostgresClient

            client_factory = PostgresClient.from_env
        self._client_factory = client_factory
        self._ensure_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._ensure_lock:
            if self._schema_ready:
                return
            with self._client_factory() as client:
                client.run_sql(MIGRATION_PATH.read_text(encoding="utf-8"))
            self._schema_ready = True

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._client_factory() as client:
            result = client.run_sql(sql, params=params)
            return result if isinstance(result, list) else []

    def _exec(self, sql: str, params: tuple = ()) -> None:
        self.ensure_schema()
        with self._client_factory() as client:
            client.run_sql(sql, params=params)

    # ---- assets
    def upsert_asset(self, asset: Asset) -> Asset:
        self._exec(
            """INSERT INTO aul_assets
               (asset_id, project_id, kind, sha256, name, uri, duration_s, width, height, generated, meta_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (asset_id) DO UPDATE SET
                 name = EXCLUDED.name, uri = EXCLUDED.uri,
                 duration_s = COALESCE(EXCLUDED.duration_s, aul_assets.duration_s),
                 meta_json = aul_assets.meta_json || EXCLUDED.meta_json""",
            (asset.asset_id, asset.project_id, asset.kind, asset.sha256,
             asset.name, asset.uri, asset.duration_s, asset.width, asset.height,
             asset.generated, json.dumps(asset.meta or {})),
        )
        return self.get_asset(asset.asset_id) or asset

    def get_asset(self, asset_id: str) -> Optional[Asset]:
        rows = self._rows("SELECT * FROM aul_assets WHERE asset_id = %s", (asset_id,))
        return Asset.from_row(rows[0]) if rows else None

    def list_assets(self, project_id: str = "default") -> list[AssetSummary]:
        rows = self._rows(
            """SELECT a.*,
                      COALESCE(l.layers, '{}') AS layers,
                      COALESCE(u.n_uses, 0) AS n_uses,
                      COALESCE(u.covered_s, 0) AS covered_s
               FROM aul_assets a
               LEFT JOIN (
                 SELECT asset_id, array_agg(DISTINCT layer) AS layers
                 FROM aul_annotations GROUP BY asset_id
               ) l ON l.asset_id = a.asset_id
               LEFT JOIN (
                 SELECT split_part(src_ref, '#', 1) AS asset_id,
                        count(*) AS n_uses,
                        sum( CAST(split_part(split_part(src_ref,'#t=',2), '-', 2) AS float)
                           - CAST(split_part(split_part(src_ref,'#t=',2), '-', 1) AS float)) AS covered_s
                 FROM aul_edges
                 WHERE src_ref LIKE '%%#t=%%' AND operation != 'published_as'
                 GROUP BY 1
               ) u ON u.asset_id = a.asset_id
               WHERE a.project_id = %s
               ORDER BY a.created_at DESC""",
            (project_id,),
        )
        return [
            self._summarize(Asset.from_row(r), list(r.get("layers") or []),
                            float(r.get("covered_s") or 0), int(r.get("n_uses") or 0))
            for r in rows
        ]

    # ---- annotations
    def add_annotations(self, annotations: list[Annotation]) -> int:
        self.ensure_schema()
        with self._client_factory() as client:
            for ann in annotations:
                client.run_sql(
                    """INSERT INTO aul_annotations
                       (annotation_id, project_id, asset_id, span_start_s, span_end_s,
                        layer, kind, producer, inputs_hash, payload_json)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    params=(ann.annotation_id or new_id("ann"), ann.project_id,
                            ann.asset_id, ann.span_start_s, ann.span_end_s,
                            ann.layer, ann.kind, ann.producer, ann.inputs_hash,
                            json.dumps(ann.payload or {})),
                )
        return len(annotations)

    def get_annotations(self, asset_id: str, *, layer: Optional[str] = None,
                        kind: Optional[str] = None) -> list[Annotation]:
        sql = "SELECT * FROM aul_annotations WHERE asset_id = %s"
        params: list[Any] = [asset_id]
        if layer:
            sql += " AND layer = %s"; params.append(layer)
        if kind:
            sql += " AND kind = %s"; params.append(kind)
        sql += " ORDER BY span_start_s NULLS FIRST, created_at"
        return [Annotation.from_row(r) for r in self._rows(sql, tuple(params))]

    def delete_annotations(self, asset_id: str, *,
                           producers: Optional[set[str]] = None) -> int:
        self.ensure_schema()
        with self._client_factory() as client:
            if producers is None:
                r = client.run_sql("DELETE FROM aul_annotations WHERE asset_id = %s",
                                   params=(asset_id,))
            elif not producers:
                return 0
            else:
                marks = ",".join(["%s"] * len(producers))
                r = client.run_sql(
                    f"DELETE FROM aul_annotations WHERE asset_id = %s AND producer IN ({marks})",
                    params=(asset_id, *producers))
        return r if isinstance(r, int) else 0

    def search(self, query: str, project_id: str = "default",
               limit: int = 20) -> list[Annotation]:
        terms = [t for t in query.lower().split() if len(t) > 2]
        if not terms:
            return []
        like = " OR ".join(["payload_json::text ILIKE %s"] * len(terms))
        rows = self._rows(
            f"""SELECT * FROM aul_annotations
                WHERE project_id = %s AND layer = 'L2' AND ({like})
                ORDER BY created_at DESC LIMIT %s""",
            (project_id, *[f"%{t}%" for t in terms], limit),
        )
        return [Annotation.from_row(r) for r in rows]

    # ---- edges
    def add_edge(self, edge: Edge) -> Edge:
        edge.edge_id = edge.edge_id or new_id("edge")
        self._exec(
            """INSERT INTO aul_edges
               (edge_id, project_id, src_ref, dst_ref, operation, params_json, session_id, job_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (edge.edge_id, edge.project_id, edge.src_ref, edge.dst_ref, edge.operation,
             json.dumps(edge.params or {}), edge.session_id, edge.job_id),
        )
        return edge

    def edges_from(self, asset_id: str) -> list[Edge]:
        return [Edge.from_row(r) for r in self._rows(
            "SELECT * FROM aul_edges WHERE split_part(src_ref,'#',1) = %s ORDER BY created_at",
            (Ref.asset_of(asset_id),))]

    def edges_to(self, asset_id: str) -> list[Edge]:
        return [Edge.from_row(r) for r in self._rows(
            "SELECT * FROM aul_edges WHERE split_part(dst_ref,'#',1) = %s ORDER BY created_at",
            (Ref.asset_of(asset_id),))]

    def edges_by_operation(self, operation: str,
                           project_id: str = "default") -> list[Edge]:
        return [Edge.from_row(r) for r in self._rows(
            "SELECT * FROM aul_edges WHERE project_id = %s AND operation = %s ORDER BY created_at",
            (project_id, operation))]

    def usage_spans(self, asset_id: str) -> list[TimeSpan]:
        rows = self._rows(
            """SELECT split_part(split_part(src_ref,'#t=',2), '-', 1) AS a,
                      split_part(split_part(src_ref,'#t=',2), '-', 2) AS b
               FROM aul_edges
               WHERE split_part(src_ref,'#',1) = %s AND src_ref LIKE '%%#t=%%'
                 AND operation != 'published_as'""",
            (asset_id,))
        return sorted(TimeSpan(float(r["a"]), float(r["b"])) for r in rows)
