import unittest
from typing import Any, Sequence

from EdennCode.Deployment.recommendation_persistence import (
    AlignmentRecord,
    CreativeFeatureSnapshotRecord,
    CreativeRecord,
    GenerationJobRecord,
    MusicAssetRecord,
    RecommendationPersistenceService,
    VideoAssetRecord,
    VideoGenerationRecommendationPayload,
)


class _RecordingPostgresClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any] | None]] = []

    def run_sql(
        self,
        statement: str,
        *,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]] | int:
        self.calls.append((statement, list(params) if params is not None else None))
        return 1


class RecommendationPersistenceServiceTests(unittest.TestCase):
    def test_persists_generation_payload_into_video_music_tables(self) -> None:
        client = _RecordingPostgresClient()
        service = RecommendationPersistenceService(video_music_client=client)
        payload = VideoGenerationRecommendationPayload(
            generation_job=GenerationJobRecord(
                job_id="job-1",
                video_id="video-1",
                creative_id="creative-1",
                primary_music_id="music-1",
                selected_music_id="music-1",
                alignment_id="alignment-1",
                model_spec="edenn_basic",
                include_vocals=False,
                vocal_gender="female",
                user_prompt="make it upbeat",
            ),
            creative=CreativeRecord(
                creative_id="creative-1",
                job_id="job-1",
                video_id="video-1",
                selected_music_id="music-1",
                alignment_id="alignment-1",
                title="Feed title",
            ),
            video_asset=VideoAssetRecord(
                video_id="video-1",
                job_id="job-1",
                duration_s=5.0,
                scene_summary_json=[{"scene_index": 0, "mood": "bright"}],
            ),
            primary_music_asset=MusicAssetRecord(
                music_id="music-1",
                job_id="job-1",
                variant_label="primary",
                matched_audio_url="https://example.test/audio.wav",
            ),
            alignment=AlignmentRecord(
                alignment_id="alignment-1",
                job_id="job-1",
                creative_id="creative-1",
                video_id="video-1",
                music_id="music-1",
                alignment_score=0.88,
                alignment_reason_json={"align_score": 0.88},
            ),
            feature_snapshot=CreativeFeatureSnapshotRecord(
                creative_id="creative-1",
                video_id="video-1",
                selected_music_id="music-1",
                alignment_id="alignment-1",
                job_id="job-1",
                language="ENGLISH_US",
                alignment_score=0.88,
            ),
        )

        service.persist_video_generation(payload)

        insert_statements = [
            statement
            for statement, _params in client.calls
            if statement.strip().startswith("INSERT INTO")
        ]
        self.assertTrue(any("INSERT INTO video_asset" in stmt for stmt in insert_statements))
        self.assertTrue(any("INSERT INTO music_asset" in stmt for stmt in insert_statements))
        self.assertTrue(any("INSERT INTO generation_job" in stmt for stmt in insert_statements))
        self.assertTrue(any("INSERT INTO creative " in stmt for stmt in insert_statements))
        self.assertTrue(any("INSERT INTO music_video_alignment" in stmt for stmt in insert_statements))
        self.assertTrue(any("INSERT INTO creative_feature_snapshot" in stmt for stmt in insert_statements))

        alignment_insert = next(
            params
            for statement, params in client.calls
            if statement.strip().startswith("INSERT INTO music_video_alignment")
        )
        self.assertIsNotNone(alignment_insert)
        self.assertIn("alignment-1", alignment_insert)
        self.assertIn(0.88, alignment_insert)

    def test_generation_job_insert_carries_creator_user_id_and_user_prompt_embedding(
        self,
    ) -> None:
        client = _RecordingPostgresClient()
        service = RecommendationPersistenceService(video_music_client=client)
        embedding = [0.01 * i for i in range(1536)]
        payload = _minimal_payload(
            generation_job_kwargs={
                "creator_user_id": "user-abc",
                "user_prompt_embedding": embedding,
            },
        )

        service.persist_video_generation(payload)

        gj_params = next(
            params
            for statement, params in client.calls
            if statement.strip().startswith("INSERT INTO generation_job")
        )
        self.assertIn("user-abc", gj_params)
        self.assertIn(embedding, gj_params)

    def test_creative_insert_carries_creator_user_id(self) -> None:
        client = _RecordingPostgresClient()
        service = RecommendationPersistenceService(video_music_client=client)
        payload = _minimal_payload(
            creative_kwargs={"creator_user_id": "user-abc"},
        )

        service.persist_video_generation(payload)

        creative_params = next(
            params
            for statement, params in client.calls
            if statement.strip().startswith("INSERT INTO creative ")
        )
        self.assertIn("user-abc", creative_params)

    def test_snapshot_insert_carries_music_embedding(self) -> None:
        client = _RecordingPostgresClient()
        service = RecommendationPersistenceService(video_music_client=client)
        embedding = [0.02 * i for i in range(1536)]
        payload = _minimal_payload(
            snapshot_kwargs={"music_embedding": embedding},
        )

        service.persist_video_generation(payload)

        snapshot_params = next(
            params
            for statement, params in client.calls
            if statement.strip().startswith("INSERT INTO creative_feature_snapshot")
        )
        self.assertIn(embedding, snapshot_params)

    def test_video_music_migration_keeps_split_embedding_columns_on_snapshot(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        migration = (repo_root / "EdennCode" / "Database" / "migrations" / "004_video_music.sql").read_text()

        snapshot_start = migration.index("CREATE TABLE IF NOT EXISTS creative_feature_snapshot")
        snapshot_end = migration.index(");", snapshot_start)
        snapshot_ddl = migration[snapshot_start:snapshot_end]

        self.assertIn("visual_embedding     vector", snapshot_ddl)
        self.assertIn("music_embedding      vector", snapshot_ddl)
        self.assertIn("genre_level1         TEXT", snapshot_ddl)
        self.assertIn("alignment_score      DOUBLE PRECISION", snapshot_ddl)


def _minimal_payload(
    *,
    generation_job_kwargs: dict[str, Any] | None = None,
    creative_kwargs: dict[str, Any] | None = None,
    snapshot_kwargs: dict[str, Any] | None = None,
) -> VideoGenerationRecommendationPayload:
    """Build a tiny but valid payload for testing field-stamping behavior."""

    gj_extra = generation_job_kwargs or {}
    cr_extra = creative_kwargs or {}
    snap_extra = snapshot_kwargs or {}

    return VideoGenerationRecommendationPayload(
        generation_job=GenerationJobRecord(
            job_id="job-1",
            video_id="video-1",
            creative_id="creative-1",
            primary_music_id="music-1",
            selected_music_id="music-1",
            alignment_id="alignment-1",
            model_spec="edenn_basic",
            include_vocals=False,
            vocal_gender="female",
            user_prompt="make it upbeat",
            **gj_extra,
        ),
        creative=CreativeRecord(
            creative_id="creative-1",
            job_id="job-1",
            video_id="video-1",
            selected_music_id="music-1",
            alignment_id="alignment-1",
            title="Feed title",
            **cr_extra,
        ),
        video_asset=VideoAssetRecord(
            video_id="video-1",
            job_id="job-1",
            duration_s=5.0,
        ),
        primary_music_asset=MusicAssetRecord(
            music_id="music-1",
            job_id="job-1",
            variant_label="primary",
        ),
        alignment=AlignmentRecord(
            alignment_id="alignment-1",
            job_id="job-1",
            creative_id="creative-1",
            video_id="video-1",
            music_id="music-1",
            alignment_score=0.5,
        ),
        feature_snapshot=CreativeFeatureSnapshotRecord(
            creative_id="creative-1",
            video_id="video-1",
            selected_music_id="music-1",
            alignment_id="alignment-1",
            job_id="job-1",
            language="ENGLISH_US",
            alignment_score=0.5,
            **snap_extra,
        ),
    )


if __name__ == "__main__":
    unittest.main()
