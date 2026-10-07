"""Asset Understanding Layer — controlled lineage + reference system.

Layers: value objects (refs, models) -> repository contract (one ABC, two
backends) -> services (ingest, tree view, lineage). Consumers depend on the
``AulRepository`` contract, never a concrete backend.
"""

from .ingest import AssetIngestor, AudioBeatGridProducer, TechProbe, VideoUnderstandingProducer
from .lineage import LineageRecorder
from .models import Annotation, Asset, AssetSummary, Edge, TimeSpan
from .refs import AssetIdentity, Ref
from .repository import AulRepository, InMemoryAulRepository, PostgresAulRepository
from .treeview import SegmentTreeAssembler

__all__ = [
    "Ref", "AssetIdentity",
    "Asset", "AssetSummary", "Annotation", "Edge", "TimeSpan",
    "AulRepository", "InMemoryAulRepository", "PostgresAulRepository",
    "AssetIngestor", "TechProbe", "VideoUnderstandingProducer", "AudioBeatGridProducer",
    "SegmentTreeAssembler", "LineageRecorder",
]
