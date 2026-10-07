"""
PostgresAnnotationStore — production-grade annotation store backed by PostgreSQL.

Two tables are maintained:

* ``annotation_events`` — append-only event log for every annotation event type.
  Payload is stored as JSONB so the schema can evolve without migrations.

* ``taxonomy_enrichments`` — flat, typed columns for LLM taxonomy extraction
  results.  Written automatically whenever a ``taxonomy_enrichment`` event is
  stored.  Array fields are stored as JSONB for compatibility with the
  ``postgres_wrapper`` adapter and support GIN-indexed containment queries.

Both tables are created idempotently via :meth:`create_tables`.

This store is synchronous at the psycopg2 level; all ``async`` methods simply
delegate to the synchronous :class:`~EdennCode.Deployment.postgres_wrapper.PostgresClient`.
This is acceptable for fire-and-forget background tasks that do not block the
main generation pipeline.
"""
from __future__ import annotations

import dataclasses
import logging
import types
from typing import Any, Dict, List

from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Deployment.postgres_wrapper import PostgresClient

logger = logging.getLogger(__name__)

_ANNOTATION_EVENTS_TABLE = "annotation_events"
_TAXONOMY_ENRICHMENTS_TABLE = "taxonomy_enrichments"
_VISUAL_TAXONOMY_TABLE = "visual_taxonomy"
_MUSIC_AUDIO_FEATURES_TABLE = "music_audio_features"

# Top-level columns promoted out of payload
_EVENT_TOP_LEVEL = frozenset(
    {"event_id", "job_id", "event_type", "schema_version", "timestamp_utc"}
)


class _DBEvent:
    """
    Lightweight proxy that exposes a DB row as an :class:`~EdennCode.Annotation.core.annotation_event.AnnotationEvent`-compatible object.

    Top-level columns (``event_id``, ``job_id``, ``event_type``, etc.) and all
    JSONB ``payload`` keys are merged into instance attributes.  List-of-dict
    values (e.g. ``scenes``) are converted to lists of :class:`types.SimpleNamespace`
    so that ``getattr(event, "scenes", [])`` yields objects with dotted access.
    """

    def __init__(self, row: Dict[str, Any]) -> None:
        for key in _EVENT_TOP_LEVEL:
            setattr(self, key, row.get(key))
        payload: Dict[str, Any] = row.get("payload") or {}
        for key, value in payload.items():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
            ):
                setattr(self, key, [types.SimpleNamespace(**item) for item in value])
            else:
                setattr(self, key, value)


class PostgresAnnotationStore(AnnotationStore):
    """
    :class:`~EdennCode.Annotation.core.annotation_store.AnnotationStore` backed by
    a PostgreSQL database via :class:`~EdennCode.Deployment.postgres_wrapper.PostgresClient`.

    Parameters
    ----------
    client:
        A connected (or auto-connecting) :class:`~EdennCode.Deployment.postgres_wrapper.PostgresClient`.
    """

    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def create_tables(self) -> None:
        """
        Create ``annotation_events`` and ``taxonomy_enrichments`` tables and
        their indexes if they do not already exist.  Safe to call on every
        startup.
        """
        self._client.create_table(
            _ANNOTATION_EVENTS_TABLE,
            {
                "id":             "BIGSERIAL PRIMARY KEY",
                "event_id":       "TEXT NOT NULL",
                "job_id":         "TEXT NOT NULL",
                "event_type":     "TEXT NOT NULL",
                "schema_version": "TEXT NOT NULL DEFAULT 'v1'",
                "timestamp_utc":  "DOUBLE PRECISION NOT NULL",
                "payload":        "JSONB NOT NULL DEFAULT '{}'",
                "created_at":     "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            },
        )
        for idx_sql in [
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_event_id     ON {_ANNOTATION_EVENTS_TABLE} (event_id)",
            f"CREATE INDEX        IF NOT EXISTS idx_ann_job_id       ON {_ANNOTATION_EVENTS_TABLE} (job_id)",
            f"CREATE INDEX        IF NOT EXISTS idx_ann_event_type   ON {_ANNOTATION_EVENTS_TABLE} (event_type)",
            f"CREATE INDEX        IF NOT EXISTS idx_ann_job_type     ON {_ANNOTATION_EVENTS_TABLE} (job_id, event_type)",
        ]:
            self._client.run_sql(idx_sql)

        self._client.create_table(
            _TAXONOMY_ENRICHMENTS_TABLE,
            {
                "id":                        "BIGSERIAL PRIMARY KEY",
                "job_id":                    "TEXT NOT NULL",
                "extraction_prompt_version": "TEXT NOT NULL",
                "extraction_model":          "TEXT NOT NULL DEFAULT ''",
                # Track identity
                "provider_name":             "TEXT NOT NULL DEFAULT ''",
                "model_spec":                "TEXT NOT NULL DEFAULT ''",
                "music_filename":            "TEXT NOT NULL DEFAULT ''",
                # Mood and affect
                "mood_tags":                 "JSONB NOT NULL DEFAULT '[]'",
                "sentiment":                 "TEXT NOT NULL DEFAULT 'neutral'",
                "energy_level":              "FLOAT NOT NULL DEFAULT 0.5",
                # Music character — hierarchical genre (primary retrieval dimensions)
                "genre_level1":              "TEXT NOT NULL DEFAULT 'Unknown'",
                "genre_level2":              "TEXT NOT NULL DEFAULT 'Unknown'",
                "genre_tags":                "JSONB NOT NULL DEFAULT '[]'",
                "instrument_tags":           "JSONB NOT NULL DEFAULT '[]'",
                "tempo_class":               "TEXT NOT NULL DEFAULT 'unknown'",
                "vocal_style":               "TEXT NOT NULL DEFAULT 'unknown'",
                # Content themes
                "theme_tags":                "JSONB NOT NULL DEFAULT '[]'",
                "activity_tags":             "JSONB NOT NULL DEFAULT '[]'",
                # Visual context
                "location_types":            "JSONB NOT NULL DEFAULT '[]'",
                "subject_types":             "JSONB NOT NULL DEFAULT '[]'",
                "motion_class":              "TEXT NOT NULL DEFAULT 'unknown'",
                # Lyric-specific
                "lyric_themes":              "JSONB NOT NULL DEFAULT '[]'",
                "lyric_sentiment":           "TEXT NOT NULL DEFAULT 'none'",
                # Metadata
                "extraction_latency_s":      "FLOAT NOT NULL DEFAULT 0",
                "token_usage":               "JSONB NOT NULL DEFAULT '{}'",
                "failed":                    "BOOLEAN NOT NULL DEFAULT FALSE",
                "error_message":             "TEXT",
                "created_at":                "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            },
        )
        # Idempotent migrations — add columns introduced after initial deploy
        self._client.add_columns(
            _TAXONOMY_ENRICHMENTS_TABLE,
            {
                "provider_name": "TEXT NOT NULL DEFAULT ''",
                "model_spec":    "TEXT NOT NULL DEFAULT ''",
                "music_filename": "TEXT NOT NULL DEFAULT ''",
                "genre_level1":  "TEXT NOT NULL DEFAULT 'Unknown'",
                "genre_level2":  "TEXT NOT NULL DEFAULT 'Unknown'",
            },
            if_not_exists=True,
        )
        for idx_sql in [
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_tax_job_version  ON {_TAXONOMY_ENRICHMENTS_TABLE} (job_id, extraction_prompt_version)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_genre_l1      ON {_TAXONOMY_ENRICHMENTS_TABLE} (genre_level1)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_genre_l1_l2   ON {_TAXONOMY_ENRICHMENTS_TABLE} (genre_level1, genre_level2)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_tempo         ON {_TAXONOMY_ENRICHMENTS_TABLE} (tempo_class)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_sentiment     ON {_TAXONOMY_ENRICHMENTS_TABLE} (sentiment)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_energy        ON {_TAXONOMY_ENRICHMENTS_TABLE} (energy_level)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_genre_tags    ON {_TAXONOMY_ENRICHMENTS_TABLE} USING GIN (genre_tags)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_mood_tags     ON {_TAXONOMY_ENRICHMENTS_TABLE} USING GIN (mood_tags)",
            f"CREATE INDEX        IF NOT EXISTS idx_tax_activity_tags ON {_TAXONOMY_ENRICHMENTS_TABLE} USING GIN (activity_tags)",
        ]:
            self._client.run_sql(idx_sql)

        self._client.create_table(
            _VISUAL_TAXONOMY_TABLE,
            {
                "id":                        "BIGSERIAL PRIMARY KEY",
                "job_id":                    "TEXT NOT NULL",
                "extraction_prompt_version": "TEXT NOT NULL",
                "extraction_model":          "TEXT NOT NULL DEFAULT ''",
                # Deterministic
                "pacing_class":              "TEXT NOT NULL DEFAULT 'unknown'",
                "avg_scene_duration_s":      "FLOAT NOT NULL DEFAULT 0",
                "scene_density_per_min":     "FLOAT NOT NULL DEFAULT 0",
                "platform_hint":             "TEXT NOT NULL DEFAULT 'generic'",
                "aspect_ratio_class":        "TEXT NOT NULL DEFAULT 'unknown'",
                "resolution_class":          "TEXT NOT NULL DEFAULT 'unknown'",
                # LLM semantic
                "setting_type":              "TEXT NOT NULL DEFAULT 'unknown'",
                "time_of_day":               "TEXT NOT NULL DEFAULT 'unknown'",
                "lighting_mood":             "TEXT NOT NULL DEFAULT 'unknown'",
                "color_mood":                "TEXT NOT NULL DEFAULT 'unknown'",
                "content_type":              "TEXT NOT NULL DEFAULT 'unknown'",
                "subject_focus":             "TEXT NOT NULL DEFAULT 'unknown'",
                "motion_intensity":          "TEXT NOT NULL DEFAULT 'unknown'",
                "visual_style":              "TEXT NOT NULL DEFAULT 'unknown'",
                "camera_motion":             "TEXT NOT NULL DEFAULT 'unknown'",
                "dominant_colors":           "JSONB NOT NULL DEFAULT '[]'",
                "visual_tags":               "JSONB NOT NULL DEFAULT '[]'",
                # Metadata
                "deterministic_only":        "BOOLEAN NOT NULL DEFAULT FALSE",
                "extraction_latency_s":      "FLOAT NOT NULL DEFAULT 0",
                "token_usage":               "JSONB NOT NULL DEFAULT '{}'",
                "failed":                    "BOOLEAN NOT NULL DEFAULT FALSE",
                "error_message":             "TEXT",
                "created_at":                "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            },
        )
        for idx_sql in [
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_vis_job_version   ON {_VISUAL_TAXONOMY_TABLE} (job_id, extraction_prompt_version)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_pacing         ON {_VISUAL_TAXONOMY_TABLE} (pacing_class)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_platform       ON {_VISUAL_TAXONOMY_TABLE} (platform_hint)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_content_type   ON {_VISUAL_TAXONOMY_TABLE} (content_type)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_setting        ON {_VISUAL_TAXONOMY_TABLE} (setting_type)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_visual_tags    ON {_VISUAL_TAXONOMY_TABLE} USING GIN (visual_tags)",
            f"CREATE INDEX        IF NOT EXISTS idx_vis_dominant_colors ON {_VISUAL_TAXONOMY_TABLE} USING GIN (dominant_colors)",
        ]:
            self._client.run_sql(idx_sql)

        self._client.create_table(
            _MUSIC_AUDIO_FEATURES_TABLE,
            {
                "id":                   "BIGSERIAL PRIMARY KEY",
                "job_id":               "TEXT NOT NULL",
                "music_filename":       "TEXT NOT NULL DEFAULT ''",
                "provider_name":        "TEXT NOT NULL DEFAULT ''",
                "model_spec":           "TEXT NOT NULL DEFAULT ''",
                # Temporal
                "bpm_actual":           "FLOAT NOT NULL DEFAULT 0",
                # Loudness
                "rms_energy_db":        "FLOAT NOT NULL DEFAULT -60",
                # Timbre
                "spectral_brightness":  "FLOAT NOT NULL DEFAULT 0",
                # Acoustic vs electronic
                "acousticness":         "FLOAT NOT NULL DEFAULT 0.5",
                # Rhythmic strength
                "danceability":         "FLOAT NOT NULL DEFAULT 0.5",
                # Vocal prominence
                "vocal_energy_ratio":   "FLOAT NOT NULL DEFAULT 0",
                # Duration
                "duration_s":           "FLOAT NOT NULL DEFAULT 0",
                # Metadata
                "extraction_latency_s": "FLOAT NOT NULL DEFAULT 0",
                "failed":               "BOOLEAN NOT NULL DEFAULT FALSE",
                "error_message":        "TEXT",
                "created_at":           "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            },
        )
        for idx_sql in [
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_aud_job_id        ON {_MUSIC_AUDIO_FEATURES_TABLE} (job_id)",
            f"CREATE INDEX        IF NOT EXISTS idx_aud_provider       ON {_MUSIC_AUDIO_FEATURES_TABLE} (provider_name)",
            f"CREATE INDEX        IF NOT EXISTS idx_aud_model_spec     ON {_MUSIC_AUDIO_FEATURES_TABLE} (model_spec)",
            f"CREATE INDEX        IF NOT EXISTS idx_aud_bpm            ON {_MUSIC_AUDIO_FEATURES_TABLE} (bpm_actual)",
            f"CREATE INDEX        IF NOT EXISTS idx_aud_energy         ON {_MUSIC_AUDIO_FEATURES_TABLE} (rms_energy_db)",
        ]:
            self._client.run_sql(idx_sql)

        logger.info(
            "PostgresAnnotationStore: tables and indexes ready (%s, %s, %s, %s)",
            _ANNOTATION_EVENTS_TABLE,
            _TAXONOMY_ENRICHMENTS_TABLE,
            _VISUAL_TAXONOMY_TABLE,
            _MUSIC_AUDIO_FEATURES_TABLE,
        )

    # ------------------------------------------------------------------
    # AnnotationStore interface
    # ------------------------------------------------------------------

    async def write(self, event: AnnotationEvent) -> None:
        d = dataclasses.asdict(event)
        payload = {k: v for k, v in d.items() if k not in _EVENT_TOP_LEVEL}
        row = {
            "event_id":       d["event_id"],
            "job_id":         d["job_id"],
            "event_type":     d["event_type"],
            "schema_version": d["schema_version"],
            "timestamp_utc":  d["timestamp_utc"],
            "payload":        payload,
        }
        try:
            self._client.insert_row(_ANNOTATION_EVENTS_TABLE, row)
        except Exception:
            # Ignore duplicate event_id (idempotent re-writes)
            logger.debug(
                "PostgresAnnotationStore: skipping duplicate event_id=%s", d["event_id"]
            )

        if event.event_type == "taxonomy_enrichment":
            self._write_enrichment(d)
        elif event.event_type == "visual_taxonomy_enrichment":
            self._write_visual_taxonomy(d)
        elif event.event_type == "music_audio_features":
            self._write_audio_features(d)

        logger.debug(
            "PostgresAnnotationStore: wrote %s job_id=%s",
            event.event_type,
            event.job_id,
        )

    async def query(
        self,
        event_type: str,
        limit: int = 100,
    ) -> List[AnnotationEvent]:
        rows = self._client.fetch_rows(
            _ANNOTATION_EVENTS_TABLE,
            where_clause="event_type = %s ORDER BY id DESC",
            where_params=[event_type],
            limit=limit,
        )
        rows.reverse()
        return [_DBEvent(r) for r in rows]  # type: ignore[return-value]

    async def get_by_job(self, job_id: str) -> List[AnnotationEvent]:
        rows = self._client.fetch_rows(
            _ANNOTATION_EVENTS_TABLE,
            where_clause="job_id = %s ORDER BY id",
            where_params=[job_id],
        )
        return [_DBEvent(r) for r in rows]  # type: ignore[return-value]

    async def get_by_job_and_type(
        self,
        job_id: str,
        event_type: str,
    ) -> List[AnnotationEvent]:
        rows = self._client.fetch_rows(
            _ANNOTATION_EVENTS_TABLE,
            where_clause="job_id = %s AND event_type = %s ORDER BY id",
            where_params=[job_id, event_type],
        )
        return [_DBEvent(r) for r in rows]  # type: ignore[return-value]

    async def all_events(self) -> List[AnnotationEvent]:
        rows = self._client.fetch_rows(
            _ANNOTATION_EVENTS_TABLE,
            where_clause="TRUE ORDER BY id",
            where_params=[],
        )
        return [_DBEvent(r) for r in rows]  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_enrichment(self, d: Dict[str, Any]) -> None:
        """Write flat typed columns to ``taxonomy_enrichments``."""
        taxonomy: Dict[str, Any] = d.get("taxonomy") or {}
        row = {
            "job_id":                    d["job_id"],
            "extraction_prompt_version": d.get("extraction_prompt_version", "v1"),
            "extraction_model":          d.get("extraction_model", ""),
            # Track identity
            "provider_name":             d.get("provider_name", ""),
            "model_spec":                d.get("model_spec", ""),
            "music_filename":            d.get("music_filename", ""),
            # Mood and affect
            "mood_tags":                 taxonomy.get("mood_tags", []),
            "sentiment":                 taxonomy.get("sentiment", "neutral"),
            "energy_level":              taxonomy.get("energy_level", 0.5),
            # Music character — hierarchical genre
            "genre_level1":              taxonomy.get("genre_level1", "Unknown"),
            "genre_level2":              taxonomy.get("genre_level2", "Unknown"),
            "genre_tags":                taxonomy.get("genre_tags", []),
            "instrument_tags":           taxonomy.get("instrument_tags", []),
            "tempo_class":               taxonomy.get("tempo_class", "unknown"),
            "vocal_style":               taxonomy.get("vocal_style", "unknown"),
            # Content themes
            "theme_tags":                taxonomy.get("theme_tags", []),
            "activity_tags":             taxonomy.get("activity_tags", []),
            # Visual context
            "location_types":            taxonomy.get("location_types", []),
            "subject_types":             taxonomy.get("subject_types", []),
            "motion_class":              taxonomy.get("motion_class", "unknown"),
            # Lyric-specific
            "lyric_themes":              taxonomy.get("lyric_themes", []),
            "lyric_sentiment":           taxonomy.get("lyric_sentiment", "none"),
            # Metadata
            "extraction_latency_s":      d.get("extraction_latency_s", 0.0),
            "token_usage":               d.get("token_usage") or {},
            "failed":                    d.get("failed", False),
            "error_message":             d.get("error_message"),
        }
        try:
            self._client.insert_row(_TAXONOMY_ENRICHMENTS_TABLE, row)
        except Exception as exc:
            logger.warning(
                "PostgresAnnotationStore: failed to write taxonomy enrichment "
                "for job_id=%s: %s",
                d["job_id"],
                exc,
            )

    def _write_visual_taxonomy(self, d: Dict[str, Any]) -> None:
        """Write flat typed columns to ``visual_taxonomy``."""
        t: Dict[str, Any] = d.get("taxonomy") or {}
        row = {
            "job_id":                    d["job_id"],
            "extraction_prompt_version": d.get("extraction_prompt_version", "v1"),
            "extraction_model":          d.get("extraction_model", ""),
            # Deterministic
            "pacing_class":              t.get("pacing_class", "unknown"),
            "avg_scene_duration_s":      t.get("avg_scene_duration_s", 0.0),
            "scene_density_per_min":     t.get("scene_density_per_min", 0.0),
            "platform_hint":             t.get("platform_hint", "generic"),
            "aspect_ratio_class":        t.get("aspect_ratio_class", "unknown"),
            "resolution_class":          t.get("resolution_class", "unknown"),
            # LLM semantic
            "setting_type":              t.get("setting_type", "unknown"),
            "time_of_day":               t.get("time_of_day", "unknown"),
            "lighting_mood":             t.get("lighting_mood", "unknown"),
            "color_mood":                t.get("color_mood", "unknown"),
            "content_type":              t.get("content_type", "unknown"),
            "subject_focus":             t.get("subject_focus", "unknown"),
            "motion_intensity":          t.get("motion_intensity", "unknown"),
            "visual_style":              t.get("visual_style", "unknown"),
            "camera_motion":             t.get("camera_motion", "unknown"),
            "dominant_colors":           t.get("dominant_colors", []),
            "visual_tags":               t.get("visual_tags", []),
            # Metadata
            "deterministic_only":        d.get("deterministic_only", False),
            "extraction_latency_s":      d.get("extraction_latency_s", 0.0),
            "token_usage":               d.get("token_usage") or {},
            "failed":                    d.get("failed", False),
            "error_message":             d.get("error_message"),
        }
        try:
            self._client.insert_row(_VISUAL_TAXONOMY_TABLE, row)
        except Exception as exc:
            logger.warning(
                "PostgresAnnotationStore: failed to write visual taxonomy "
                "for job_id=%s: %s",
                d["job_id"],
                exc,
            )

    def _write_audio_features(self, d: Dict[str, Any]) -> None:
        """Write flat typed columns to ``music_audio_features``."""
        row = {
            "job_id":               d["job_id"],
            "music_filename":       d.get("music_filename", ""),
            "provider_name":        d.get("provider_name", ""),
            "model_spec":           d.get("model_spec", ""),
            "bpm_actual":           d.get("bpm_actual", 0.0),
            "rms_energy_db":        d.get("rms_energy_db", -60.0),
            "spectral_brightness":  d.get("spectral_brightness", 0.0),
            "acousticness":         d.get("acousticness", 0.5),
            "danceability":         d.get("danceability", 0.5),
            "vocal_energy_ratio":   d.get("vocal_energy_ratio", 0.0),
            "duration_s":           d.get("duration_s", 0.0),
            "extraction_latency_s": d.get("extraction_latency_s", 0.0),
            "failed":               d.get("failed", False),
            "error_message":        d.get("error_message"),
        }
        try:
            self._client.insert_row(_MUSIC_AUDIO_FEATURES_TABLE, row)
        except Exception as exc:
            logger.warning(
                "PostgresAnnotationStore: failed to write audio features "
                "for job_id=%s: %s",
                d["job_id"],
                exc,
            )
