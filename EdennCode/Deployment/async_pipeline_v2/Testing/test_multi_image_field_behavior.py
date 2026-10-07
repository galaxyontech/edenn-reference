"""Production-grade field verification for multi-image v2: every input param's
EFFECT and every output field's population, proven at runtime.

Part A (behavioral): runs the real pipeline via MultiImageMonolithWorker.process_one
with a FAKE stable provider (offline/free) and asserts field effects — video length
follows images x per_image_duration, vocals produce lyrics, titles/mirrors/N-A fields.
Part B (validation): drives the real endpoint through a TestClient with injected
in-memory fakes and asserts the 400/422 guards.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.models import (
    AsyncV2Artifact, AsyncV2Task, JobStatus, TaskEnvelope, TaskStatus,
)
from EdennCode.Deployment.async_pipeline_v2.workers.multi_image_worker import MultiImageMonolithWorker
from EdennCode.Deployment.multi_image_workflows import (
    MultiImageGenerationOrchestrator, MultiImageGenerationResult,
)
from EdennCode.MusicGenerationCore.models import (
    MusicGenerationResult, MusicVariant, ProviderJobRef, SectionTiming, TimestampedWord,
)
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage import (
    _choose_music_start_s,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.UserIntentUnderstandingStage.user_prompt_preprocessor import (
    UserPromptPreprocessorAgent,
)
from EdennCode.Util.MediaUtils.ffmpeg_utils import resolve_ffmpeg_binary
from EdennCode.TestSuites.helpers.paths import MULTI_IMAGE_DESIGN_IMAGES_DIR

E2E = "EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MultiImageE2EGenerationStage.multi_image_generation_e2e_stage"

_FAKE_PLAN = {
    "video_title": "City Frames", "music_title": "Stable Horizon", "video_description": "A calm reel.",
    "image_order": [], "storyline_summary": "A steady arc.", "overall_mood": "uplifting",
    "target_bpm": 120, "primary_instruments": ["synth"], "music_prompt_summary": "fake instrumental",
    "image_beats": [], "music_sections": [],
}
_DUMMY_WAV: Optional[Path] = None


def _dummy_wav(tmp: Path) -> Path:
    global _DUMMY_WAV
    if _DUMMY_WAV and _DUMMY_WAV.exists():
        return _DUMMY_WAV
    p = tmp / "dummy.wav"
    subprocess.run([resolve_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", "20", str(p)],
                   check=True, capture_output=True)
    _DUMMY_WAV = p
    return p


class _FakePlanClient:
    def __init__(self): self.last_messages = None

    async def complete_messages(self, messages, json_schema=None):
        self.last_messages = messages
        return (dict(_FAKE_PLAN), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class _FakeMusicService:
    def __init__(self, dummy_wav: Path, *, duration_override: Optional[float] = None):
        self.dummy_wav = dummy_wav
        # When set, the primary variant reports this length instead of the video's
        # duration — used to exercise the windowed-audio path (track > video).
        self.duration_override = duration_override

    async def generate(self, request):
        total = self.duration_override or getattr(
            request.section_plan, "total_duration_s", None
        ) or sum(s.target_duration_s for s in request.section_plan.sections) or 6.0
        out_dir = Path(request.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "fake.wav"; shutil.copy(self.dummy_wav, out)
        # vocals -> lyric timings; instrumental -> none. Proves include_vocals flows through.
        # word-level = per-word; line-level = coarser per-line; full_lyrics = plain text.
        word_level = ([TimestampedWord(text="la", startS=0.0, endS=0.5, i=0),
                       TimestampedWord(text="la", startS=0.5, endS=1.0, i=1)]
                      if request.options.include_vocals else [])
        line_level = ([TimestampedWord(text="la la", startS=0.0, endS=1.0, i=0)]
                      if request.options.include_vocals else [])
        full_lyrics = "la la" if request.options.include_vocals else None
        # record what the music service was asked for so tests can assert plumbing
        self.last_options = request.options
        self.last_request = request
        timeline, t = [], 0.0
        for s in request.section_plan.sections:
            timeline.append(SectionTiming(section_id=s.section_id, expected_start_s=t,
                                          expected_end_s=t + s.target_duration_s))
            t += s.target_duration_s
        variant = MusicVariant(variant_id="v", audio_path=out, duration_s=float(total),
                               lyrics_timestamps=word_level,
                               line_level_lyrics_timestamps=line_level,
                               full_lyrics=full_lyrics, section_timeline=timeline)
        return MusicGenerationResult(used_modelspec=request.modelspec, primary=variant, alternates=[],
                                     job_ref=ProviderJobRef(provider="fake"), prompt_manifest={},
                                     prompt_summary="fake", vocal_id_used=request.options.vocal_id)


class _DisabledStorage:
    enabled = False


class _RecordingStorage:
    """Enabled storage that records the blob names the worker asks it to upload.

    The response no longer carries any filename, so the provider-neutral naming
    guarantee is observed where it actually happens: at the upload boundary.
    """

    enabled = True

    def __init__(self): self.blob_names: list[str] = []

    def upload_path(self, *, container, path, blob_name, content_type):
        self.blob_names.append(blob_name)
        return blob_name

    def generate_sas_url(self, *, container, blob_name):
        return f"https://fake.local/{container}/{blob_name}?sig=x"


class _WorkerRepo:
    def __init__(self):
        self.jobs, self.artifacts, self.job_status = {}, {}, {}

    def get_job(self, jid): return self.jobs.get(jid)
    def get_artifact(self, aid): return self.artifacts.get(aid)

    def add_artifact(self, *, artifact_id, job_id, artifact_type, role, container, blob_name,
                     url, content_type, local_path, metadata_json):
        a = AsyncV2Artifact(artifact_id=artifact_id, job_id=job_id, artifact_type=artifact_type,
                            role=role, container=container, blob_name=blob_name, url=url,
                            content_type=content_type, local_path=local_path, metadata_json=metadata_json or {})
        self.artifacts[artifact_id] = a
        return a

    def start_stage_run(self, **k): return SimpleNamespace(stage_run_id="sr")
    def update_stage_run(self, *a, **k): pass
    def update_job_status(self, jid, *, status, **k): self.job_status[jid] = {"status": status, **k}

    def update_job_status_checked(self, jid, *, status, **k):
        self.update_job_status(jid, status=status, **k)
        return SimpleNamespace(job_id=jid, status=status), True

    def add_event(self, **k): pass

    @contextmanager
    def transaction(self):
        yield None


class _WorkerQueue:
    def __init__(self, task): self._task, self._leased = task, False
    def lease(self, **k):
        if self._leased: return None
        self._leased = True; return self._task
    def heartbeat(self, **k): pass
    def complete(self, *, task_id, worker_id, client=None): return SimpleNamespace(task_id=task_id, status=TaskStatus.COMPLETED)
    def fail(self, **k): return SimpleNamespace(status="dead_lettered")


def _seed_images(repo: _WorkerRepo, job_id: str, dest: Path, image_files) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    ids = []
    for i, img in enumerate(image_files):
        p = dest / f"s{i}{img.suffix}"; p.write_bytes(img.read_bytes())
        aid = f"{job_id}:image:{i}"
        repo.artifacts[aid] = AsyncV2Artifact(artifact_id=aid, job_id=job_id, artifact_type="source_image",
            role=f"image_{i}", container=None, blob_name=None, url=None, content_type="image/jpeg",
            local_path=str(p), metadata_json={})
        ids.append(aid)
    return ids


def _run_job(*, image_files, per_image_duration, include_vocals, modelspec, tmp_path, dummy,
             extra_request: Optional[dict] = None, capture: Optional[dict] = None,
             music_duration_override: Optional[float] = None, storage: Any = None) -> dict:
    repo = _WorkerRepo()
    job_id = f"j{id(image_files)}{per_image_duration}{include_vocals}{id(extra_request)}"
    ids = _seed_images(repo, job_id, tmp_path / f"src_{job_id}", image_files)
    request_json = {
        "modelspec": modelspec, "per_image_duration": per_image_duration,
        "include_vocals": include_vocals, "align_to_beats": False, "user_prompt": ""}
    request_json.update(extra_request or {})
    repo.jobs[job_id] = SimpleNamespace(job_id=job_id, request_json=request_json)
    task = AsyncV2Task(task_id=f"{job_id}:t", job_id=job_id, queue_name="q",
                       task_type="multi_image_monolith", status=TaskStatus.LEASED,
                       payload_json={"image_artifact_ids": ids}, attempt=1, max_attempts=1)
    worker = MultiImageMonolithWorker(
        repository=repo, queue=_WorkerQueue(task), orchestrator=MultiImageGenerationOrchestrator(),
        settings=SimpleNamespace(workdir=tmp_path / f"work_{job_id}", audio_container_name="audio",
                                 output_container="video"),
        storage=storage or _DisabledStorage(), plan_cache=None)
    plan_client = _FakePlanClient()
    music_service = _FakeMusicService(dummy, duration_override=music_duration_override)
    if capture is not None:
        capture["plan"], capture["music"] = plan_client, music_service
    with patch(f"{E2E}.build_azure_client", return_value=plan_client), \
         patch(f"{E2E}.build_music_generation_service", return_value=music_service):
        processed = asyncio.run(worker.process_one())
    assert processed is not None and processed.status == TaskStatus.COMPLETED, repo.job_status.get(job_id)
    return repo.job_status[job_id]["result_json"]


class _SpyOrchestrator:
    """Records the kwargs the worker passes to run(); returns a minimal valid result."""
    def __init__(self, out_dir: Path): self.calls, self.out = [], out_dir

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        self.out.mkdir(parents=True, exist_ok=True)
        music = self.out / "m.wav"; music.write_bytes(b"RIFF0000WAVE")
        video = Path(kwargs["output_path"]); video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return MultiImageGenerationResult(
            final_video_path=video, silent_video_path=video, generated_music_path=music,
            full_track_paths=[music], processed_image_paths=[], planning_metadata={},
            music_prompt="p", lyrics_timestamps=[], section_timeline=[], video_title="t",
            music_title="m", video_description="d", include_vocals=False, vocal_gender="female",
            lyrics_language="", user_requested_language="EN", used_music_model_spec="edenn_enhanced",
            compression_applied=False, vocal_id_used=None, job_received_timestamp=1, job_finished_timestamp=2)


def _run_with_spy(*, request_json, image_files, tmp_path):
    repo = _WorkerRepo(); job_id = "spy"
    ids = _seed_images(repo, job_id, tmp_path / "spy_src", image_files)
    repo.jobs[job_id] = SimpleNamespace(job_id=job_id, request_json=request_json)
    task = AsyncV2Task(task_id="spy:t", job_id=job_id, queue_name="q",
                       task_type="multi_image_monolith", status=TaskStatus.LEASED,
                       payload_json={"image_artifact_ids": ids}, attempt=1, max_attempts=1)
    spy = _SpyOrchestrator(tmp_path / "spy_out")
    worker = MultiImageMonolithWorker(
        repository=repo, queue=_WorkerQueue(task), orchestrator=spy,
        settings=SimpleNamespace(workdir=tmp_path / "spy_work", audio_container_name="a", output_container="v"),
        storage=_DisabledStorage(), plan_cache=None)
    assert asyncio.run(worker.process_one()) is not None
    return spy.calls[0]


# ============================ Part A — behavioral ============================

class TestFieldBehavior:
    @pytest.fixture(scope="class")
    def imgs(self):
        return sorted(p for p in MULTI_IMAGE_DESIGN_IMAGES_DIR.iterdir() if p.is_file())[:3]

    def test_video_length_scales_with_image_count_and_duration(self, imgs, tmp_path):
        d = _dummy_wav(tmp_path)
        r_2x3 = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                         modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=d)
        r_3x3 = _run_job(image_files=imgs[:3], per_image_duration=3.0, include_vocals=False,
                         modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=d)
        r_2x5 = _run_job(image_files=imgs[:2], per_image_duration=5.0, include_vocals=False,
                         modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=d)
        dur = lambda r: r["video_metadata"]["geometry"]["duration"]
        assert dur(r_2x3) == pytest.approx(6.0, abs=0.3), dur(r_2x3)      # 2 x 3s
        assert dur(r_3x3) == pytest.approx(9.0, abs=0.3), dur(r_3x3)      # 3 x 3s (more images = longer)
        assert dur(r_2x5) == pytest.approx(10.0, abs=0.3), dur(r_2x5)     # 2 x 5s (longer per image = longer)
        geo = r_2x3["video_metadata"]["geometry"]
        # duration_s is input-only now (mirrored into duration, excluded from the response)
        assert geo["duration"] and "duration_s" not in geo
        assert geo["width"] and geo["height"]

    def test_music_title_present_distinct_and_mirrored(self, imgs, tmp_path):
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        music_title = r["audio_metadata"]["music_title"]
        summary = r["video_metadata"]["video_summary"]
        video_title = summary["video_title"]
        assert music_title == "Stable Horizon"
        assert video_title == "City Frames"
        assert music_title != video_title
        # music_title is surfaced once (audio_metadata) — NOT duplicated in the summary
        assert "music_title" not in summary

    def test_complete_audio_mirrors_primary_track_when_whole_track_fits(self, imgs, tmp_path):
        # No distinct window: the fake reports the track length == video length, so
        # the whole track is the "selected part" and audio_* == complete_*.
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        a = r["audio_metadata"]
        assert a["complete_audio_url"] == a["audio_url"]
        assert a["complete_audio_duration_s"] == a["audio_duration_s"]
        assert a["complete_audio_size_bytes"] == a["audio_size_bytes"]
        assert a["audio_duration_s"] and a["audio_duration_s"] > 0
        assert a["audio_size_bytes"] and a["audio_size_bytes"] > 0

    def test_audio_url_is_windowed_when_track_longer_than_video(self, imgs, tmp_path):
        # Track (30s) is far longer than the ~6s slideshow, so only the selected
        # window is surfaced as audio_*, while complete_* stays the full track.
        # Storage is disabled here, so the split is observed via the locally-probed
        # duration/size metrics.
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                     music_duration_override=30.0)
        video_len = r["video_metadata"]["geometry"]["duration"]
        a = r["audio_metadata"]
        # audio_* = windowed clip (~ video length), strictly shorter than the full track.
        assert a["audio_duration_s"] == pytest.approx(video_len, abs=0.5), a["audio_duration_s"]
        assert a["complete_audio_duration_s"] > a["audio_duration_s"] + 1.0
        assert a["audio_size_bytes"] and a["audio_size_bytes"] < a["complete_audio_size_bytes"]

    def test_lyrics_only_present_with_vocals(self, imgs, tmp_path):
        d = _dummy_wav(tmp_path)
        instru = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                          modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=d)
        vocal = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=True,
                         modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=d)
        assert instru["audio_metadata"]["lyrics_timestamps"] == []
        assert vocal["audio_metadata"]["lyrics_timestamps"], "vocals should produce lyric timings"
        # full_lyrics_timestamps mirrors lyrics_timestamps in both cases
        assert (vocal["audio_metadata"]["full_lyrics_timestamps"]
                == vocal["audio_metadata"]["lyrics_timestamps"])

    def test_section_plan_describes_the_music_arc(self, imgs, tmp_path):
        # The section arc is no longer echoed in the response, so it is asserted
        # where it is produced: the plan the pipeline hands to the music service.
        cap: dict = {}
        _run_job(image_files=imgs[:3], per_image_duration=3.0, include_vocals=False,
                 modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                 capture=cap)
        plan = cap["music"].last_request.section_plan
        assert plan.sections, "the music request should describe the music arc as sections"
        assert plan.total_duration_s > 0
        s = plan.sections[0]
        assert s.section_id and s.target_duration_s > 0

    def test_music_volume_zero_is_preserved_not_promoted(self, imgs, tmp_path):
        # Regression: worker used `float(x or 1.0)` which turned a 0.0 (silence) into 1.0.
        call = _run_with_spy(request_json={"modelspec": "edenn_enhanced", "music_volume": 0.0},
                             image_files=imgs[:2], tmp_path=tmp_path)
        assert call["music_volume"] == 0.0

    def test_music_volume_passthrough_and_default(self, imgs, tmp_path):
        c_half = _run_with_spy(request_json={"modelspec": "edenn_enhanced", "music_volume": 0.5},
                               image_files=imgs[:2], tmp_path=tmp_path / "a")
        c_none = _run_with_spy(request_json={"modelspec": "edenn_enhanced"},
                               image_files=imgs[:2], tmp_path=tmp_path / "b")
        assert c_half["music_volume"] == 0.5      # explicit value passes through
        assert c_none["music_volume"] == 1.0      # omitted -> default 1.0

    def test_params_reach_orchestrator(self, imgs, tmp_path):
        call = _run_with_spy(request_json={
            "modelspec": "edenn_studio", "user_prompt": "cinematic", "align_to_beats": False,
            "per_image_duration": 4.0, "water_mark": True, "include_vocals": True,
            "vocal_gender": "male", "lyrics_language": "EN", "vocal_id": "voice-1",
            "transition_mode": "custom", "transitions": ["fade", "dissolve"],
            "transition_duration_s": 0.6},
            image_files=imgs[:2], tmp_path=tmp_path)
        assert call["modelspec"] == "edenn_studio"
        assert call["user_prompt"] == "cinematic"
        assert call["align_to_beats"] is False
        assert call["per_image_duration"] == 4.0
        assert call["water_mark"] is True
        assert call["include_vocals"] is True
        assert call["vocal_gender"] == "male"
        assert call["lyrics_language"] == "EN"
        assert call["vocal_id"] == "voice-1"
        # Feature U (ffmpeg xfade transitions) reaches the orchestrator on the v2 path.
        assert call["transition_mode"] == "custom"
        assert call["transitions"] == ["fade", "dissolve"]
        assert call["transition_duration_s"] == 0.6

    def test_transition_defaults_when_job_predates_field(self, imgs, tmp_path):
        # A job enqueued before transitions existed has no transition_* keys; the
        # worker must fall back to hard cuts rather than crash on a missing field.
        call = _run_with_spy(request_json={"modelspec": "edenn_enhanced"},
                             image_files=imgs[:2], tmp_path=tmp_path)
        assert call["transition_mode"] == "none"
        assert call["transitions"] is None
        assert call["transition_duration_s"] == 0.4

    def test_modelspec_echoed(self, imgs, tmp_path):
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_studio", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        assert r["modelspec"] == "edenn_studio"

    def test_na_video_parity_fields_are_null_or_empty(self, imgs, tmp_path):
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        # thumbnail_url is null here only because the test storage is disabled;
        # a real deploy populates it.
        assert r["video_metadata"]["thumbnail_url"] is None
        # scenes was removed from the job response entirely (lives only on the preview).
        assert "scenes" not in r["video_metadata"]
        # No vocals -> no lyric timestamps at any level.
        assert r["audio_metadata"]["word_level_lyrics_timestamps"] == []
        # cost_metadata is a default object (not null), mirroring the video shape
        assert isinstance(r["cost_metadata"], dict) and "creation_cost" in r["cost_metadata"]

    def test_lyrics_leveling_word_line_and_full_text(self, imgs, tmp_path):
        # Vocal job: word-level and line-level must be distinct, full text populated.
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=True,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        a = r["audio_metadata"]
        # word level = per-word (2 "la"); line level = per-line (1 "la la")
        assert [w["text"] for w in a["word_level_lyrics_timestamps"]] == ["la", "la"]
        assert [w["text"] for w in a["lyrics_timestamps"]] == ["la la"]
        # full_* mirror the same convention; plain text is populated
        assert [w["text"] for w in a["full_lyrics_timestamps"]] == ["la la"]
        assert [w["text"] for w in a["full_word_level_lyrics_timestamps"]] == ["la", "la"]
        assert a["full_lyrics"] == "la la"
        # Timestamps are emitted in MILLISECONDS, matching the video pipeline. The
        # source words are seconds (la @ 0.0-0.5s, 0.5-1.0s) -> x1000 in the response.
        assert [w["endS"] for w in a["word_level_lyrics_timestamps"]] == [500.0, 1000.0]
        assert a["word_level_lyrics_timestamps"][1]["startS"] == 500.0

    def test_uploaded_audio_filenames_are_provider_neutral(self, imgs, tmp_path):
        # The raw provider filename encodes internal processing (tail-trim, variant,
        # watermark) — nothing that leaves the service may expose it. The response
        # carries no filename any more, so the guarantee is asserted at the upload
        # boundary, where the blob name is minted.
        storage = _RecordingStorage()
        _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=True,
                 modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                 storage=storage)
        names = [Path(b).name for b in storage.blob_names]
        assert names, "the worker should upload the generated assets"
        assert "complete_audio.wav" in names   # the full track, neutrally named
        for nm in names:
            low = nm.lower()
            assert not any(tok in low for tok in
                           ("trimmed", "tail", "watermark", "alt", "provider_b", "provider_c", "eleven", "enhanced"))

    def test_cost_and_token_num_populated(self, imgs, tmp_path):
        r = _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path))
        # cost object present with the planning tokens and a real (finite) token_cost
        assert r["cost_metadata"]["token_num"] == 2
        assert isinstance(r["cost_metadata"]["token_cost"], (int, float))

    def test_audio_output_format_reaches_music_options(self, imgs, tmp_path):
        cap: dict = {}
        _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=True,
                 modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                 extra_request={"audio_output_format": "wav"}, capture=cap)
        assert cap["music"].last_options.output_format == "wav"

    def test_user_lyrics_prompt_reaches_planning_when_vocals(self, imgs, tmp_path):
        cap: dict = {}
        _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=True,
                 modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                 extra_request={"user_lyrics_prompt": "sing about neon rivers"}, capture=cap)
        blob = json.dumps(cap["plan"].last_messages, ensure_ascii=False)
        assert "neon rivers" in blob

    def test_user_lyrics_prompt_turns_on_vocals_by_itself(self, imgs, tmp_path):
        # A non-empty user_lyrics_prompt is now a vocal request in its own right:
        # the E2E stage turns vocals on for it, so the lyric direction reaches
        # planning even when include_vocals is unset (it is no longer gated on it).
        cap: dict = {}
        _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                 modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                 extra_request={"user_lyrics_prompt": "sing about neon rivers"}, capture=cap)
        blob = json.dumps(cap["plan"].last_messages, ensure_ascii=False)
        assert "neon rivers" in blob

    def test_user_lyrics_prompt_is_sanitized_before_planning(self, imgs, tmp_path):
        # The lyric direction gets the same sanitizer pass as video-music's
        # lyrics_prompt: the planning LLM sees the sanitized text, not the raw.
        cap: dict = {}
        sanitize_mock = AsyncMock(
            return_value=SimpleNamespace(transformed_prompt="sanitized river chorus")
        )
        with patch.object(UserPromptPreprocessorAgent, "preprocess_lyrics_prompt",
                          new=sanitize_mock):
            _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                     extra_request={"user_lyrics_prompt": "raw neon rivers"}, capture=cap)
        sanitize_mock.assert_awaited_once_with("raw neon rivers")
        blob = json.dumps(cap["plan"].last_messages, ensure_ascii=False)
        assert "sanitized river chorus" in blob
        assert "raw neon rivers" not in blob

    def test_user_lyrics_prompt_sanitizer_failure_falls_back_to_raw(self, imgs, tmp_path):
        # Sanitization failure must never fail a paid job: the raw direction
        # is used instead.
        cap: dict = {}
        with patch.object(UserPromptPreprocessorAgent, "preprocess_lyrics_prompt",
                          new=AsyncMock(side_effect=RuntimeError("sanitizer down"))):
            _run_job(image_files=imgs[:2], per_image_duration=3.0, include_vocals=False,
                     modelspec="edenn_enhanced", tmp_path=tmp_path, dummy=_dummy_wav(tmp_path),
                     extra_request={"user_lyrics_prompt": "sing about neon rivers"}, capture=cap)
        blob = json.dumps(cap["plan"].last_messages, ensure_ascii=False)
        assert "neon rivers" in blob


class TestMusicWindowSelection:
    """Unit tests for the vocal-window anchor that fixes the dead-intro bug."""

    def _st(self):
        return [SectionTiming(section_id=f"S{i}", expected_start_s=5 * i, expected_end_s=5 * (i + 1),
                              actual_start_s=66.0 * i, actual_end_s=66.0 * (i + 1)) for i in range(3)]

    def _words(self, n, start):
        return [TimestampedWord(text=str(i), startS=start + 0.4 * i, endS=start + 0.4 * i + 0.36, i=i)
                for i in range(n)]

    def test_vocal_track_skips_dead_intro(self):
        # vocals begin at 11.92s in a 203s track, 15s window -> anchor ~1.5s lead-in
        start = _choose_music_start_s(video_duration_s=15.0, track_duration_s=203.0,
                                      lyrics_timestamps=self._words(41, 11.92), section_timeline=self._st())
        assert 9.0 <= start <= 11.0        # ~10.42: just before the first sung word

    def test_instrumental_keeps_natural_start(self):
        assert _choose_music_start_s(video_duration_s=15.0, track_duration_s=203.0,
                                     lyrics_timestamps=[], section_timeline=self._st()) == 0.0

    def test_short_track_no_offset(self):
        assert _choose_music_start_s(video_duration_s=15.0, track_duration_s=15.2,
                                     lyrics_timestamps=self._words(41, 11.92), section_timeline=self._st()) == 0.0

    def test_deterministic(self):
        vals = {round(_choose_music_start_s(video_duration_s=15.0, track_duration_s=203.0,
                                            lyrics_timestamps=self._words(41, 11.92),
                                            section_timeline=self._st()), 3) for _ in range(5)}
        assert len(vals) == 1


# ============================ Part B — validation ============================

class _MemRepo:
    def __init__(self): self.jobs, self.artifacts, self.events = {}, {}, []
    def create_job(self, *, job_id, job_type, request_json, status, **k):
        self.jobs[job_id] = SimpleNamespace(job_id=job_id, job_type=job_type,
                                            request_json=request_json, status=status)
        return self.jobs[job_id]
    def get_artifact(self, aid): return self.artifacts.get(aid)
    def add_artifact(self, *, artifact_id, job_id, artifact_type, role, **k):
        a = AsyncV2Artifact(artifact_id=artifact_id, job_id=job_id, artifact_type=artifact_type,
                            role=role, container=None, blob_name=None, url=None,
                            content_type="image/png", local_path="/x", metadata_json={})
        self.artifacts[artifact_id] = a; return a
    def update_job_status(self, jid, **k): pass
    def update_job_status_checked(self, jid, **k):
        return SimpleNamespace(job_id=jid, status=k.get("status")), True
    def add_event(self, **k): self.events.append(k)
    @contextmanager
    def transaction(self):
        yield None


class _MemQueue:
    def __init__(self): self.envelopes = []
    def enqueue(self, envelope: TaskEnvelope): self.envelopes.append(envelope); return envelope.task_id


class _FakeStaging:
    def __init__(self, repo): self.repo, self.n = repo, 0
    def _art(self, job_id, atype, role):
        self.n += 1
        return SimpleNamespace(artifact=self.repo.add_artifact(
            artifact_id=f"art{self.n}", job_id=job_id, artifact_type=atype, role=role))
    def stage_image_bytes(self, *, job_id, index=0, **k): return self._art(job_id, "source_image", f"image_{index}")
    async def stage_image_url(self, *, job_id, index=0, **k): return self._art(job_id, "source_image", f"image_{index}")
    def stage_vocal_sample_bytes(self, *, job_id, **k): return self._art(job_id, "vocal_sample", "vocal")
    async def stage_vocal_sample_url(self, *, job_id, **k): return self._art(job_id, "vocal_sample", "vocal")


def _client(tmp_path):
    repo = _MemRepo(); queue = _MemQueue()
    context = SimpleNamespace(
        storage=_DisabledStorage(),
        settings=SimpleNamespace(workdir=tmp_path, upload_container="uploads",
                                 audio_container_name="audio", output_container="video",
                                 async_v2_queue_namespace=None))
    app = FastAPI()
    app.include_router(create_async_pipeline_v2_router(
        context, repository=repo, queue=queue, artifact_staging=_FakeStaging(repo)))
    return TestClient(app), repo, queue


EP = "/api/v2/jobs/multi-image-music"

# The minimum a multi-image job now accepts (MULTI_IMAGE_MIN_IMAGES == 3).
IMGS3 = ",".join(f"https://x/{c}.jpg" for c in "abc")


class TestValidation:
    def test_too_few_images_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        # No images and 1-2 images both fall below the 3-image minimum.
        assert client.post(EP, data={"modelspec": "edenn_basic"}).status_code == 400
        r = client.post(EP, data={"image_urls": "https://x/a.jpg,https://x/b.jpg"})
        assert r.status_code == 400 and "at least 3" in r.text.lower(), r.text

    def test_too_many_images_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        urls = ",".join(f"https://x/{i}.jpg" for i in range(11))  # > MULTI_IMAGE_MAX_IMAGES (10)
        r = client.post(EP, data={"image_urls": urls})
        assert r.status_code == 400 and "many" in r.text.lower()

    def test_total_duration_over_limit_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        # 10 images x 20s = 200s > 150s cap (per-image 20s is within the le=60 form bound).
        urls = ",".join(f"https://x/{i}.jpg" for i in range(10))
        r = client.post(EP, data={"image_urls": urls, "per_image_duration": 20.0})
        assert r.status_code == 400 and "duration" in r.text.lower(), r.text

    def test_total_duration_at_limit_accepted(self, tmp_path):
        client, _, _ = _client(tmp_path)
        # 10 images x 15s = 150s is exactly the cap and must be accepted.
        urls = ",".join(f"https://x/{i}.jpg" for i in range(10))
        r = client.post(EP, data={"image_urls": urls, "per_image_duration": 15.0})
        assert r.status_code == 200, r.text

    def test_vocal_clone_fields_are_ignored(self, tmp_path):
        # vocal_id / vocal_sample_url were removed from this endpoint. FastAPI
        # ignores unknown form fields, so posting them together no longer trips the
        # old mutual-exclusion 400 — the request succeeds and the fields do not
        # change the job (vocals are inferred downstream from user_prompt).
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "vocal_id": "v1",
                                  "vocal_sample_url": "https://x/voice.m4a"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["include_vocals"] is False
        assert req["vocal_id"] is None

    def test_invalid_modelspec_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "modelspec": "not_a_tier"})
        assert r.status_code == 400 and "modelspec" in r.text.lower()

    @pytest.mark.parametrize("field,value", [
        ("max_attempts", 0), ("max_attempts", 11), ("priority", 2000),
        ("per_image_duration", 0), ("per_image_duration", -1), ("per_image_duration", 120),
    ])
    def test_numeric_bounds_rejected(self, tmp_path, field, value):
        client, _, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, field: value})
        assert r.status_code == 422, r.text

    def test_vocal_clone_no_longer_tier_gated(self, tmp_path):
        # The old vocal-clone tier gate (vocal_id / vocal_sample_url require the
        # enhanced tier) is gone: those fields were removed from the endpoint, so
        # they are ignored on every tier and never 400.
        client, _, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "modelspec": "edenn_basic", "vocal_id": "v1"})
        assert r.status_code == 200, r.text
        r2 = client.post(EP, data={"image_urls": IMGS3, "modelspec": "edenn_studio",
                                   "vocal_sample_url": "https://x/voice.m4a"})
        assert r2.status_code == 200, r2.text
        r3 = client.post(EP, data={"image_urls": IMGS3, "modelspec": "edenn_enhanced", "vocal_id": "v1"})
        assert r3.status_code == 200, r3.text

    def test_unknown_image_artifact_id_rejected(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # Three ids so the count check passes; the first unknown one is then
        # rejected with 404 (parity with video-music's source-artifact lookup),
        # BEFORE any job row exists — a client-side mistake must not leave a
        # phantom FAILED job behind.
        r = client.post(EP, data={"image_artifact_ids": "does-not-exist,x2,x3"})
        assert r.status_code == 404 and "artifact" in r.text.lower()
        assert not repo.jobs

    def test_out_of_range_music_volume_rejected_at_submit(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
            "music_volume": "5.0",
        })
        assert r.status_code == 400 and "music_volume" in r.text
        assert not repo.jobs

    def test_image_order_defaults_auto_and_accepts_fixed(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["image_order"] == "auto"

        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
            "image_order": "fixed",
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["image_order"] == "fixed"

    def test_image_order_rejects_unknown_value(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
            "image_order": "shuffle",
        })
        assert r.status_code == 400 and "image_order" in r.text
        assert not repo.jobs

    def test_water_mark_defaults_on(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
        })
        assert r.status_code == 200, r.text
        job = repo.jobs[r.json()["job_id"]]
        assert job.request_json["water_mark"] is True

    def test_water_mark_explicit_false_respected(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
            "water_mark": "false",
        })
        assert r.status_code == 200, r.text
        job = repo.jobs[r.json()["job_id"]]
        assert job.request_json["water_mark"] is False

    def test_happy_submit_enqueues_multi_image_task(self, tmp_path):
        client, repo, queue = _client(tmp_path)
        r = client.post(EP, data={"image_urls": '["https://x/a.jpg","https://x/b.jpg","https://x/c.jpg"]',
                                  "modelspec": "provider_c", "per_image_duration": 4.0, "max_attempts": 1})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["job_id"] and body["status"] == "queued"
        # job created as multi_image with the normalized (legacy-aliased) tier
        job = repo.jobs[body["job_id"]]
        assert job.job_type == "multi_image"
        assert job.request_json["modelspec"] == "edenn_studio"    # provider_c -> edenn_studio
        # 3 x 4s = 12s sits under the 15s billing floor, so the uniform value is
        # bumped (+1s each) to 5s at submit.
        assert job.request_json["per_image_duration"] == 5.0
        assert job.request_json["per_image_durations"] is None
        # one multi_image_monolith task enqueued with all three image ids in the payload
        assert len(queue.envelopes) == 1
        env = queue.envelopes[0]
        assert env.task_type == "multi_image_monolith"
        assert env.max_attempts == 1
        assert len(env.payload_json["image_artifact_ids"]) == 3

    def test_new_input_params_are_persisted(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "modelspec": "edenn_enhanced",
                                  "include_vocals": "true", "audio_output_format": "wav",
                                  "user_lyrics_prompt": "  a hopeful chorus  "})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["audio_output_format"] == "wav"
        assert req["user_lyrics_prompt"] == "a hopeful chorus"   # trimmed

    def test_lyrics_prompt_kept_and_include_vocals_always_false(self, tmp_path):
        # include_vocals was removed from the endpoint (vocals are inferred
        # downstream), so it is always stored False. A user_lyrics_prompt is no
        # longer gated on it — it is kept, because a lyric direction now turns
        # vocals on in its own right. The removed include_vocals form field is
        # ignored and cannot suppress it.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "include_vocals": "false",
                                  "user_lyrics_prompt": "a hopeful chorus"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["include_vocals"] is False
        assert req["user_lyrics_prompt"] == "a hopeful chorus"

    def test_canonical_lyrics_prompt_stored_under_internal_key(self, tmp_path):
        # lyrics_prompt is the canonical cross-endpoint name; the payload keeps
        # the historical user_lyrics_prompt key so workers need no change.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "lyrics_prompt": "  a hopeful chorus  "})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["user_lyrics_prompt"] == "a hopeful chorus"   # trimmed

    def test_canonical_lyrics_prompt_wins_over_alias(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "lyrics_prompt": "canonical direction",
                                  "user_lyrics_prompt": "alias direction"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["user_lyrics_prompt"] == "canonical direction"

    def test_whitespace_canonical_falls_back_to_alias(self, tmp_path):
        # A whitespace-only canonical value is absent; the alias still counts.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "lyrics_prompt": "   ",
                                  "user_lyrics_prompt": "alias direction"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["user_lyrics_prompt"] == "alias direction"

    def test_transition_defaults_to_random(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "random"      # 'auto' default resolves to random
        assert req["transitions"] is None
        # No duration provided -> None here; the 0.4s default is applied downstream.
        assert req["transition_duration_s"] is None

    def test_transition_types_persisted_and_parsed(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": "fade, dissolve",
                                  "transition_duration_s": 0.5})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        # explicit per-boundary effects -> resolved "custom" mode with the list
        assert req["transition_mode"] == "custom"
        assert req["transitions"] == ["fade", "dissolve"]
        # a single duration broadcasts to one blend length per boundary (3 imgs -> 2)
        assert req["transition_duration_s"] == [0.5, 0.5]

    def test_invalid_transition_name_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_types": "not_an_effect"})
        assert r.status_code == 400 and "transition type" in r.text.lower(), r.text

    def test_transition_types_wrong_length_rejected(self, tmp_path):
        # 3 images -> 2 boundaries; a list that is neither 1 nor 2 entries is a 400.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": "fade, dissolve, wipeup"})
        assert r.status_code == 400 and "transition_types" in r.text, r.text
        assert not repo.jobs

    def test_transition_duration_out_of_bounds_rejected(self, tmp_path):
        client, _, _ = _client(tmp_path)
        # A per-boundary blend length is bounds-checked regardless of mode; a single
        # value broadcasts to every boundary, so 5.0s exceeds the 2.0s max.
        r = client.post(EP, data={"image_urls": IMGS3, "transition_duration_s": 5.0})
        assert r.status_code == 400 and "between" in r.text.lower(), r.text

    def test_transition_duration_s_with_random_still_accepted(self, tmp_path):
        # transition_duration_s applies to every boundary even in the default
        # (random) mode — it is not mode-gated. A single value broadcasts to a
        # per-boundary list of floats.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_duration_s": 0.6})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "random"
        assert req["transition_duration_s"] == [0.6, 0.6]

    def test_transition_length_field_is_ignored(self, tmp_path):
        # 'transition_length' was removed (replaced by per-boundary
        # transition_duration_s). FastAPI ignores unknown form fields, so posting
        # it is a no-op now rather than a 400, and it sets no blend length.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_length": "0.8"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "random"
        assert req["transition_duration_s"] is None

    def test_transition_length_ignored_with_none_mode(self, tmp_path):
        # transition_mode=none hard-cuts; the removed transition_length field is
        # ignored (no 400) and sets nothing.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_mode": "none",
                                  "transition_length": "0.8"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "none"
        assert req["transitions"] is None
        assert req["transition_duration_s"] is None

    def test_named_effect_with_blend_length(self, tmp_path):
        # A single named effect broadcasts to every boundary, and a single blend
        # length broadcasts alongside it (transition_types + transition_duration_s
        # replace the old transition + transition_length pair).
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_types": "fade",
                                  "transition_duration_s": "0.8"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "custom"
        assert req["transitions"] == ["fade", "fade"]
        assert req["transition_duration_s"] == [0.8, 0.8]

    def test_explicit_list_with_blend_length(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "transition_types": "fade, dissolve",
                                  "transition_duration_s": "0.7"})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transitions"] == ["fade", "dissolve"]
        assert req["transition_duration_s"] == [0.7, 0.7]

    # ---- Feature: repeated form fields (native lists) ---------------------
    # Clients handed a real array (axios, requests, httpx) serialize it as the
    # form key repeated once per value. Each of the three list-shaped fields
    # must accept that encoding as an equivalent of the JSON/comma string.

    def test_transition_fields_accept_repeated_form_fields(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": ["fade", "dissolve"],
                                  "transition_duration_s": ["0.5", "0.9"]})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transition_mode"] == "custom"
        assert req["transitions"] == ["fade", "dissolve"]
        assert req["transition_duration_s"] == [0.5, 0.9]

    def test_repeated_single_entry_still_broadcasts(self, tmp_path):
        # A one-element repeated field behaves exactly like the one-value string:
        # it broadcasts to every boundary.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": ["fade"],
                                  "transition_duration_s": ["0.8"]})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["transitions"] == ["fade", "fade"]
        assert req["transition_duration_s"] == [0.8, 0.8]

    def test_image_urls_accept_repeated_form_fields(self, tmp_path):
        # A real URL array (repeated form key) stages every URL — previously the
        # scalar field kept only the last occurrence and silently dropped the rest.
        client, repo, queue = _client(tmp_path)
        r = client.post(EP, data={"image_urls": [
            "https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"]})
        assert r.status_code == 200, r.text
        assert len(queue.envelopes[0].payload_json["image_artifact_ids"]) == 3

    def test_per_image_durations_accepts_repeated_form_fields(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": ["4", "5", "6"],
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["per_image_durations"] == [4.0, 5.0, 6.0]

    def test_repeated_fields_still_validated(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # Count mismatch (3 images, 2 durations) is the same 400 as the string form.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": ["4", "5"],
        })
        assert r.status_code == 400 and "per_image_durations" in r.text, r.text
        # Non-numeric entries are the same clean 400.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": ["4", "abc", "6"],
        })
        assert r.status_code == 400 and "numbers" in r.text.lower(), r.text
        # Bad effect names in a repeated list are rejected identically.
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": ["fade", "not_an_effect"]})
        assert r.status_code == 400 and "transition type" in r.text.lower(), r.text
        assert not repo.jobs

    def test_repeated_empty_json_array_entry_rejected_not_dropped(self, tmp_path):
        # A repeated entry of exactly "[]" json-parses to zero items; it must NOT
        # silently vanish (which would bypass the count checks and fire a billed
        # job) — the comma-string form of the same content is a 400, and the
        # repeated form must match.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": ["4", "[]", "5", "6"],
        })
        assert r.status_code == 400 and "numbers" in r.text.lower(), r.text
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_duration_s": ["0.7", "[]"]})
        assert r.status_code == 400 and "numbers" in r.text.lower(), r.text
        r = client.post(EP, data={"image_urls": IMGS3,
                                  "transition_types": ["fade", "[]"]})
        assert r.status_code == 400 and "transition type" in r.text.lower(), r.text
        assert not repo.jobs

    # ---- Feature: fixed-order explicit per-image timing -------------------

    def test_per_image_durations_honored_and_beats_off(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # Each value >= 3s and the sum is at the 15s billing floor, so the
        # caller's seconds are stored verbatim.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": "[4,5,6]",
        })
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["per_image_durations"] == [4.0, 5.0, 6.0]

    def test_total_duration_split_evenly_then_bumped_to_floor(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # 12s splits into [4,4,4]; the 15s billing floor then adds +1s per image
        # round-robin from the first image.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed", "total_duration_s": "12",
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["per_image_durations"] == [5.0, 5.0, 5.0]

    def test_durations_require_fixed_order(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "per_image_durations": "[4,2,3]"})
        assert r.status_code == 400 and "fixed" in r.text.lower(), r.text
        assert not repo.jobs

    def test_durations_count_must_match_images(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed", "per_image_durations": "[4,2]",
        })
        assert r.status_code == 400 and "per_image_durations" in r.text, r.text
        assert not repo.jobs

    def test_durations_and_total_mutually_exclusive(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": "[4,2,3]", "total_duration_s": "12",
        })
        assert r.status_code == 400, r.text
        assert not repo.jobs

    def test_no_durations_leaves_field_absent(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={"image_urls": IMGS3, "image_order": "fixed"})
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["per_image_durations"] is None

    def test_high_uniform_duration_ignored_when_explicit_timing_wins(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # per_image_duration=60 would trip the uniform 150s cap (3 x 60 = 180s),
        # but explicit total_duration_s is the only timing that matters here, so
        # the uniform value must not gate the request.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_duration": 60, "total_duration_s": "90",
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["per_image_durations"] == [30.0, 30.0, 30.0]

    def test_empty_durations_list_rejected(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # An explicit empty list is a client mistake, not a request for default
        # timing — it must be a clean 400, not a silent fallback.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed", "per_image_durations": "[]",
        })
        assert r.status_code == 400 and "per_image_durations" in r.text, r.text
        assert not repo.jobs

    # ---- Feature: 3s per-image minimum (error 10008) + 15s billing floor ---

    def test_uniform_duration_below_3s_rejected_with_code_10008(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # 2.5 passes the loose Form gt=0 bound, so the shared validator rejects
        # it with the coded 400 (not a 422) and no job row is created.
        r = client.post(EP, data={"image_urls": IMGS3, "per_image_duration": 2.5})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["error_code"] == 10008 and detail["retryable"] is False
        assert "3" in detail["message"]
        assert not repo.jobs

    def test_explicit_duration_below_3s_rejected_with_code_10008(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": "[4,2.5,6]",
        })
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error_code"] == 10008
        assert not repo.jobs

    def test_total_duration_share_below_3s_rejected_with_code_10008(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # 8s over 3 images is ~2.67s per image — below the 3s per-image floor,
        # so this is a coded reject, not a bump.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed", "total_duration_s": "8",
        })
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error_code"] == 10008
        assert not repo.jobs

    def test_uniform_bump_stays_uniform_and_keeps_beats(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # 3 images x 3s = 9s -> +1s round-robin twice around = 5s each. The bump
        # lands equally, so timing stays uniform and beat alignment survives
        # (per_image_durations remains unset).
        r = client.post(EP, data={"image_urls": IMGS3, "per_image_duration": 3.0})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["per_image_duration"] == 5.0
        assert req["per_image_durations"] is None

    def test_uniform_bump_goes_explicit_when_uneven(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # 4 images x 3s = 12s -> +1s to images 1-3 reaches 15s, so the plan
        # becomes an explicit per-image list (exact timing, beats off).
        urls = ",".join(f"https://x/{c}.jpg" for c in "abcd")
        r = client.post(EP, data={"image_urls": urls, "per_image_duration": 3.0})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["per_image_durations"] == [4.0, 4.0, 4.0, 3.0]
        assert req["per_image_duration"] == 3.0

    def test_explicit_durations_bumped_round_robin_from_first(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # [3,4,3] = 10s -> five +1s steps walking from the first image:
        # [4,4,3] [4,5,3] [4,5,4] [5,5,4] [5,6,4] = 15s.
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed",
            "per_image_durations": "[3,4,3]",
        })
        assert r.status_code == 200, r.text
        assert repo.jobs[r.json()["job_id"]].request_json["per_image_durations"] == [5.0, 6.0, 4.0]

    def test_uniform_at_floor_untouched(self, tmp_path):
        client, repo, _ = _client(tmp_path)
        # The v2 default (3 x 5s = 15s) sits exactly on the floor: no bump.
        r = client.post(EP, data={"image_urls": IMGS3})
        assert r.status_code == 200, r.text
        req = repo.jobs[r.json()["job_id"]].request_json
        assert req["per_image_duration"] == 5.0
        assert req["per_image_durations"] is None

    @pytest.mark.parametrize("field,value", [
        ("per_image_durations", "nan,4,4"),
        ("per_image_durations", "4,4,inf"),
        ("total_duration_s", "nan"),
    ])
    def test_non_finite_durations_rejected(self, tmp_path, field, value):
        # NaN passes every one-sided comparison, so it needs an explicit
        # finiteness reject — it must never become a billed job.
        client, repo, _ = _client(tmp_path)
        r = client.post(EP, data={
            "image_urls": IMGS3, "image_order": "fixed", field: value,
        })
        assert r.status_code == 400, r.text
        assert not repo.jobs
