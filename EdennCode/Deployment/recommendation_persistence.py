from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import uuid4

from EdennCode.Deployment.postgres_wrapper import PostgresClient

logger = logging.getLogger("edenn.recommendation_persistence")


class RecommendationPostgresClient(Protocol):
    """Small DB client protocol required by recommendation persistence."""

    def run_sql(
        self,
        statement: str,
        *,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]] | int:
        """Execute SQL and return rows or affected row count."""
        ...


@dataclass(frozen=True)
class RecommendationAssetIds:
    """Stable identifiers shared between generation, annotation, and feed serving."""

    job_id: str
    video_id: str
    creative_id: str
    primary_music_id: str
    selected_music_id: str
    alignment_id: str
    secondary_music_id: Optional[str] = None

    @classmethod
    def create(cls, *, job_id: str) -> "RecommendationAssetIds":
        """Create durable serving IDs for one video generation request."""

        primary_music_id = uuid4().hex
        return cls(
            job_id=job_id,
            video_id=uuid4().hex,
            creative_id=uuid4().hex,
            primary_music_id=primary_music_id,
            secondary_music_id=None,
            selected_music_id=primary_music_id,
            alignment_id=uuid4().hex,
        )


@dataclass(frozen=True)
class VideoAssetRecord:
    """Canonical persisted video-side facts used by recommendation retrieval."""

    video_id: str
    job_id: str
    source_video_blob: Optional[str] = None
    source_video_url: Optional[str] = None
    source_video_path: Optional[str] = None
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    content_type: Optional[str] = None
    scene_summary_json: list[dict[str, Any]] = field(default_factory=list)
    visual_feature_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MusicAssetRecord:
    """Canonical persisted music-side facts for one generated music variant."""

    music_id: str
    job_id: str
    variant_label: str
    provider_task_id: Optional[str] = None
    provider_audio_id: Optional[str] = None
    full_audio_blob: Optional[str] = None
    full_audio_url: Optional[str] = None
    full_audio_path: Optional[str] = None
    matched_audio_blob: Optional[str] = None
    matched_audio_url: Optional[str] = None
    matched_audio_path: Optional[str] = None
    lyrics_text: Optional[str] = None
    lyrics_timestamp_json: list[dict[str, Any]] = field(default_factory=list)
    music_feature_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationJobRecord:
    """Generation request metadata keyed by the same job_id used by annotations."""

    job_id: str
    video_id: str
    creative_id: str
    selected_music_id: str
    alignment_id: str
    model_spec: str
    include_vocals: bool
    vocal_gender: str
    user_prompt: str
    primary_music_id: Optional[str] = None
    secondary_music_id: Optional[str] = None
    user_requested_language: str = ""
    preserve_original_audio: bool = False
    music_volume: float = 1.0
    status: str = "completed"
    token_usage_json: dict[str, Any] = field(default_factory=dict)
    job_received_timestamp: Optional[int] = None
    job_finished_timestamp: Optional[int] = None
    creator_user_id: Optional[str] = None
    user_prompt_embedding: Optional[list[float]] = None


@dataclass(frozen=True)
class AlignmentRecord:
    """Persisted music-video alignment signal for the selected creative."""

    alignment_id: str
    job_id: str
    creative_id: str
    video_id: str
    music_id: str
    alignment_score: float = 0.0
    selected_clip_start_s: float = 0.0
    selected_clip_duration_s: Optional[float] = None
    matching_used_track: Optional[str] = None
    alignment_reason_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CreativeRecord:
    """Final feed-serving object: a rendered video with selected music."""

    creative_id: str
    job_id: str
    video_id: str
    selected_music_id: str
    alignment_id: str
    title: str = ""
    description: str = ""
    creator_user_id: Optional[str] = None
    visibility: str = "private"
    result_video_blob: Optional[str] = None
    result_video_url: Optional[str] = None
    result_video_path: Optional[str] = None
    thumbnail_blob: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_path: Optional[str] = None
    legacy_work_id: Optional[str] = None
    render_status: str = "completed"


@dataclass(frozen=True)
class CreativeFeatureSnapshotRecord:
    """Denormalized recommendation snapshot optimized for coarse retrieve and rank."""

    creative_id: str
    video_id: str
    selected_music_id: str
    alignment_id: str
    job_id: str
    language: str = ""
    music_model_spec: str = ""
    include_vocals: bool = False
    vocal_gender: str = ""
    genre_level1: Optional[str] = None
    content_type: Optional[str] = None
    tempo_bpm: Optional[float] = None
    energy_level: Optional[str] = None
    overall_mood: Optional[str] = None
    platform_hint: Optional[str] = None
    alignment_score: float = 0.0
    visual_feature_json: dict[str, Any] = field(default_factory=dict)
    music_feature_json: dict[str, Any] = field(default_factory=dict)
    scene_summary_json: list[dict[str, Any]] = field(default_factory=list)
    music_prompt_json: dict[str, Any] = field(default_factory=dict)
    music_embedding: Optional[list[float]] = None
    visual_embedding: Optional[list[float]] = None


@dataclass(frozen=True)
class VideoGenerationRecommendationPayload:
    """Complete payload persisted after a successful video-music generation."""

    generation_job: GenerationJobRecord
    creative: CreativeRecord
    video_asset: VideoAssetRecord
    primary_music_asset: MusicAssetRecord
    alignment: AlignmentRecord
    feature_snapshot: CreativeFeatureSnapshotRecord
    secondary_music_asset: Optional[MusicAssetRecord] = None


class RecommendationPersistenceService:
    """
    Persist recommendation-serving records into the Video-Music database.

    This service is intentionally separate from the annotation dispatcher. The
    dispatcher records pipeline events for audit/enrichment, while this service
    writes canonical entities used by feed retrieval and ranking.
    """

    def __init__(self, *, video_music_client: RecommendationPostgresClient) -> None:
        self._client = video_music_client

    @classmethod
    def from_video_music_db(cls) -> "RecommendationPersistenceService":
        """Create a persistence service using the Video-Music DB environment.

        Schema is owned by ``EdennCode/Database/migrations/004_video_music.sql`` and applied via
        ``python -m EdennCode.Database.migrations.apply``. This service assumes the tables exist
        and never issues DDL.
        """

        return cls(video_music_client=PostgresClient.from_env())

    def _legacy_create_video_music_tables(self) -> None:
        """Legacy ad-hoc DDL retained only for unit tests pre-migration runner.

        DO NOT call from production code. The canonical schema lives in
        ``EdennCode/Database/migrations/004_video_music.sql``.
        """

        self._client.run_sql("CREATE EXTENSION IF NOT EXISTS vector")
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS video_asset (
                video_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                source_video_blob TEXT,
                source_video_url TEXT,
                source_video_path TEXT,
                duration_s DOUBLE PRECISION,
                width INTEGER,
                height INTEGER,
                fps DOUBLE PRECISION,
                content_type TEXT,
                scene_summary_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                visual_feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS music_asset (
                music_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                variant_label TEXT NOT NULL,
                provider_task_id TEXT,
                provider_audio_id TEXT,
                full_audio_blob TEXT,
                full_audio_url TEXT,
                full_audio_path TEXT,
                matched_audio_blob TEXT,
                matched_audio_url TEXT,
                matched_audio_path TEXT,
                lyrics_text TEXT,
                lyrics_timestamp_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                music_feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS generation_job (
                job_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                creative_id TEXT NOT NULL,
                primary_music_id TEXT,
                secondary_music_id TEXT,
                selected_music_id TEXT NOT NULL,
                alignment_id TEXT NOT NULL,
                model_spec TEXT NOT NULL,
                include_vocals BOOLEAN NOT NULL DEFAULT false,
                vocal_gender TEXT NOT NULL DEFAULT '',
                user_prompt TEXT NOT NULL DEFAULT '',
                user_requested_language TEXT NOT NULL DEFAULT '',
                preserve_original_audio BOOLEAN NOT NULL DEFAULT false,
                music_volume DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'completed',
                token_usage_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                job_received_timestamp BIGINT,
                job_finished_timestamp BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS creative (
                creative_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                selected_music_id TEXT NOT NULL,
                alignment_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                creator_user_id TEXT,
                visibility TEXT NOT NULL DEFAULT 'private',
                result_video_blob TEXT,
                result_video_url TEXT,
                result_video_path TEXT,
                thumbnail_blob TEXT,
                thumbnail_url TEXT,
                thumbnail_path TEXT,
                legacy_work_id TEXT,
                render_status TEXT NOT NULL DEFAULT 'completed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS music_video_alignment (
                alignment_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                creative_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                music_id TEXT NOT NULL,
                alignment_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                selected_clip_start_s DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                selected_clip_duration_s DOUBLE PRECISION,
                matching_used_track TEXT,
                alignment_reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE TABLE IF NOT EXISTS creative_feature_snapshot (
                creative_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                selected_music_id TEXT NOT NULL,
                alignment_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                music_model_spec TEXT NOT NULL DEFAULT '',
                include_vocals BOOLEAN NOT NULL DEFAULT false,
                vocal_gender TEXT NOT NULL DEFAULT '',
                genre_level1 TEXT,
                content_type TEXT,
                tempo_bpm DOUBLE PRECISION,
                energy_level TEXT,
                overall_mood TEXT,
                platform_hint TEXT,
                alignment_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                visual_feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                music_feature_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                scene_summary_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                music_prompt_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                visual_embedding vector,
                music_embedding vector,
                refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        self._client.run_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_generation_job_video
            ON generation_job (video_id)
            """
        )
        self._client.run_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_music_asset_job
            ON music_asset (job_id)
            """
        )
        self._client.run_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_creative_feature_snapshot_coarse
            ON creative_feature_snapshot (
                language,
                genre_level1,
                content_type,
                energy_level,
                include_vocals
            )
            """
        )
        self._client.run_sql(
            """
            CREATE INDEX IF NOT EXISTS idx_creative_feature_snapshot_alignment
            ON creative_feature_snapshot (alignment_score DESC)
            """
        )

    def persist_video_generation(self, payload: VideoGenerationRecommendationPayload) -> None:
        """Persist a successful generation result for future recommendation serving."""

        self._upsert(
            "video_asset",
            _dataclass_mapping(payload.video_asset),
            conflict_columns=("video_id",),
        )
        self._upsert(
            "music_asset",
            _dataclass_mapping(payload.primary_music_asset),
            conflict_columns=("music_id",),
        )
        if payload.secondary_music_asset is not None:
            self._upsert(
                "music_asset",
                _dataclass_mapping(payload.secondary_music_asset),
                conflict_columns=("music_id",),
            )
        self._upsert(
            "generation_job",
            _dataclass_mapping(payload.generation_job),
            conflict_columns=("job_id",),
        )
        self._upsert(
            "creative",
            _dataclass_mapping(payload.creative),
            conflict_columns=("creative_id",),
        )
        self._upsert(
            "music_video_alignment",
            _dataclass_mapping(payload.alignment),
            conflict_columns=("alignment_id",),
        )
        self._upsert(
            "creative_feature_snapshot",
            _dataclass_mapping(payload.feature_snapshot),
            conflict_columns=("creative_id",),
            timestamp_column="refreshed_at",
        )

    _ALLOWED_TABLES = frozenset({
        "video_asset",
        "music_asset",
        "generation_job",
        "creative",
        "music_video_alignment",
        "creative_feature_snapshot",
    })

    def _upsert(
        self,
        table_name: str,
        values: Mapping[str, Any],
        *,
        conflict_columns: Sequence[str],
        timestamp_column: str = "updated_at",
    ) -> None:
        """Run an idempotent insert/update for one fixed recommendation table."""

        if table_name not in self._ALLOWED_TABLES:
            raise ValueError(f"Table '{table_name}' is not a managed recommendation table.")
        if not values:
            raise ValueError("Recommendation upsert requires at least one column.")

        columns = tuple(values.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        column_sql = ", ".join(columns)
        conflict_sql = ", ".join(conflict_columns)
        update_columns = [col for col in columns if col not in conflict_columns]
        assignments = [f"{col} = EXCLUDED.{col}" for col in update_columns]
        if timestamp_column:
            assignments.append(f"{timestamp_column} = now()")

        statement = (
            f"INSERT INTO {table_name} ({column_sql}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {', '.join(assignments)}"
        )
        self._client.run_sql(statement, params=list(values.values()))


def _dataclass_mapping(instance: Any) -> dict[str, Any]:
    """Return a plain dictionary without importing dataclasses.asdict recursion."""

    return {
        field_name: getattr(instance, field_name)
        for field_name in getattr(instance, "__dataclass_fields__", {})
    }


__all__ = [
    "AlignmentRecord",
    "CreativeFeatureSnapshotRecord",
    "CreativeRecord",
    "GenerationJobRecord",
    "MusicAssetRecord",
    "RecommendationAssetIds",
    "RecommendationPersistenceService",
    "VideoAssetRecord",
    "VideoGenerationRecommendationPayload",
]
