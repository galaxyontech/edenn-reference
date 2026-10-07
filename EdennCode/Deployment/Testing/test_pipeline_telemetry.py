"""Tests for v1 pipeline telemetry persistence (pipeline_runs / pipeline_stages)."""
from __future__ import annotations

import unittest

from EdennCode.Deployment.pipeline_telemetry import (
    PipelineRunRecorder,
    provider_for_modelspec,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    _log_pipeline_timing,
    reset_pipeline_recorder,
    set_pipeline_recorder,
)


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run_sql(self, sql, params=None):
        self._sink.append((sql, params))
        return []


def _recorder(sink, **kw):
    return PipelineRunRecorder(
        job_id="2a9275a63eb9448cbe10f4794fa1022a",
        modelspec="edenn_enhanced",
        client_factory=lambda: _FakeClient(sink),
        **kw,
    )


class PipelineTelemetryTest(unittest.TestCase):
    def test_provider_mapping(self):
        self.assertEqual(provider_for_modelspec("edenn_enhanced"), "provider_b")
        self.assertEqual(provider_for_modelspec("edenn_studio"), "provider_c")
        self.assertEqual(provider_for_modelspec("edenn_basic"), "provider_a")
        self.assertIsNone(provider_for_modelspec(None))

    def test_run_id_is_uuid_from_hex_job_id(self):
        rec = _recorder([])
        # 32-char hex job id round-trips to a canonical UUID
        self.assertEqual(rec.run_id, "2a9275a6-3eb9-448c-be10-f4794fa1022a")

    def test_start_record_finish_sql(self):
        sink: list = []
        rec = _recorder(sink)
        rec.start()
        rec.record_stage("scene_segmentation", 7.95, ts_start=1_700_000_000.0, provider=None)
        rec.record_stage("music_generation", 96.99, ts_start=1_700_000_010.0, provider="provider_b")
        rec.finish(status="completed", duration_s=109.5, music_provider="provider_b",
                   video_summary="a calm clip", music_prompt={"style": "ambient"})
        joined = " ".join(s for s, _ in sink)
        self.assertIn("INSERT INTO pipeline_runs", joined)
        self.assertEqual(sum("INSERT INTO pipeline_stages" in s for s, _ in sink), 2)
        self.assertIn("UPDATE pipeline_runs", joined)
        # stage_index increments
        stage_rows = [p for s, p in sink if "pipeline_stages" in s]
        self.assertEqual(stage_rows[0][2], 0)
        self.assertEqual(stage_rows[1][2], 1)

    def test_total_stage_is_not_persisted(self):
        sink: list = []
        rec = _recorder(sink)
        rec.record_stage("total", 109.5, ts_start=1_700_000_000.0)
        self.assertEqual([s for s, _ in sink], [])

    def test_db_failure_never_raises(self):
        def boom():
            raise RuntimeError("db down")
        rec = PipelineRunRecorder(job_id="abc", modelspec="x", client_factory=boom)
        # none of these should raise
        rec.start()
        rec.record_stage("scene_segmentation", 1.0)
        rec.finish(status="completed", duration_s=1.0)

    def test_log_pipeline_timing_persists_via_contextvar(self):
        sink: list = []
        rec = _recorder(sink)
        token = set_pipeline_recorder(rec)
        try:
            _log_pipeline_timing("scene_segmentation", 7.95, "job", provider=None, ts_start=1_700_000_000.0)
            _log_pipeline_timing("total", 100.0, "job")  # must be skipped
        finally:
            reset_pipeline_recorder(token)
        stage_inserts = [s for s, _ in sink if "pipeline_stages" in s]
        self.assertEqual(len(stage_inserts), 1)
        # after reset, no recorder bound -> no persistence
        _log_pipeline_timing("video_understanding", 1.0, "job")
        self.assertEqual(len([s for s, _ in sink if "pipeline_stages" in s]), 1)


if __name__ == "__main__":
    unittest.main()
