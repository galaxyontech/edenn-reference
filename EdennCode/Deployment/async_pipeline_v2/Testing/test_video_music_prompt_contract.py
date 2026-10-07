"""v2 video-music prompt contract: presence-keyed lyrics_prompt, no verbose flag.

The v2 contract is user_prompt (+ modelspec) with an optional freestanding
lyrics_prompt that turns vocals on. verbose_instruction is an accepted no-op
and music_style_prompt a deprecated alias for user_prompt, so pre-collapse
clients keep working. lyrics_prompt requires a modelspec with a lyrics channel
(edenn_enhanced / edenn_studio); edenn_basic — including the silent default
when modelspec is omitted — rejects with coded 400 error 10009.

Every case runs through BOTH the JSON and the multipart form branch of
POST /api/v2/jobs/video-music: the endpoint has two parsing paths and the
contract must hold on each. Accepted cases also assert the STORED payload
shape — lyric-directed jobs are persisted in the legacy verbose shape
(verbose flag on, resolved user prompt in the style slot) so any deployed
worker generation reads them identically.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_pipeline_v2.models import AsyncV2Artifact, TaskEnvelope


class _DisabledStorage:
    enabled = False


class _MemRepo:
    def __init__(self):
        self.jobs, self.artifacts, self.events = {}, {}, []

    def create_job(self, *, job_id, job_type, request_json, status, **k):
        self.jobs[job_id] = SimpleNamespace(
            job_id=job_id, job_type=job_type, request_json=request_json, status=status
        )
        return self.jobs[job_id]

    def get_job(self, jid):
        return self.jobs.get(jid)

    def get_artifact(self, aid):
        return self.artifacts.get(aid)

    def add_artifact(self, *, artifact_id, job_id, artifact_type, role, **k):
        a = AsyncV2Artifact(
            artifact_id=artifact_id, job_id=job_id, artifact_type=artifact_type,
            role=role, container=None, blob_name=None, url=k.get("url"),
            content_type="video/mp4", local_path="/x",
            metadata_json=k.get("metadata_json") or {},
        )
        self.artifacts[artifact_id] = a
        return a

    def update_job_status(self, jid, **k):
        pass

    def update_job_status_checked(self, jid, **k):
        return SimpleNamespace(job_id=jid, status=k.get("status")), True

    def add_event(self, **k):
        self.events.append(k)

    @contextmanager
    def transaction(self):
        yield None


class _MemQueue:
    def __init__(self):
        self.envelopes = []

    def enqueue(self, envelope: TaskEnvelope):
        self.envelopes.append(envelope)
        return envelope.task_id


def _client(tmp_path):
    repo = _MemRepo()
    queue = _MemQueue()
    context = SimpleNamespace(
        storage=_DisabledStorage(),
        settings=SimpleNamespace(
            workdir=tmp_path, upload_container="uploads",
            audio_container_name="audio", output_container="video",
            async_v2_queue_namespace=None,
        ),
    )
    app = FastAPI()
    app.include_router(create_async_pipeline_v2_router(
        context, repository=repo, queue=queue, artifact_staging=None))
    return TestClient(app), repo, queue


EP = "/api/v2/jobs/video-music"
VIDEO_URL = "https://x/source.mp4"


def _post(client, body: dict, *, branch: str):
    """Send the same logical request through the JSON or multipart branch."""
    if branch == "json":
        return client.post(EP, json={"video_url": VIDEO_URL, **body})
    form = {"video_url": VIDEO_URL}
    for key, value in body.items():
        form[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return client.post(EP, data=form)


def _stored_payload(repo):
    assert len(repo.jobs) == 1, "expected exactly one created job"
    return next(iter(repo.jobs.values())).request_json


BRANCHES = ("json", "form")


@pytest.mark.parametrize("branch", BRANCHES)
class TestLyricsModelspecGate:
    def test_lyrics_with_enhanced_accepted(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_enhanced",
                           "user_prompt": "warm folk",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        assert _stored_payload(repo)["lyrics_prompt"] == "themes of home"

    def test_lyrics_with_studio_accepted(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "lyrics_prompt": "a song about the sea"}, branch=branch)
        assert r.status_code == 200, r.text
        assert _stored_payload(repo)["lyrics_prompt"] == "a song about the sea"

    def test_lyrics_with_explicit_basic_rejected_10009(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_basic",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert detail["error_code"] == 10009
        assert detail["retryable"] is False
        # Self-repairing message: names the field and the exact fix.
        assert "lyrics_prompt" in detail["message"]
        assert "edenn_enhanced" in detail["message"]
        assert "edenn_studio" in detail["message"]
        assert not repo.jobs

    def test_lyrics_with_omitted_modelspec_rejected_10009(self, tmp_path, branch):
        # The silent-default footgun: no modelspec resolves to edenn_basic, so
        # the caller most likely to hit this never typed "basic".
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error_code"] == 10009
        assert not repo.jobs

    def test_whitespace_lyrics_treated_as_absent(self, tmp_path, branch):
        # Whitespace-only lyric direction is absent: no vocal force, and no
        # 10009 even on the basic default.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"user_prompt": "calm piano",
                           "lyrics_prompt": "   "}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert "lyrics_prompt" not in payload
        assert payload["verbose_instruction"] is False


@pytest.mark.parametrize("branch", BRANCHES)
class TestStoredPayloadShape:
    def test_lyric_job_stored_in_legacy_verbose_shape(self, tmp_path, branch):
        # Worker-compat invariant: lyric-directed jobs persist in the shape
        # every deployed worker generation understands — verbose flag on, the
        # resolved user prompt in the style slot, user_prompt empty.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "user_prompt": "epic orchestral",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is True
        assert payload["music_style_prompt"] == "epic orchestral"
        assert payload["user_prompt"] == ""
        assert payload["lyrics_prompt"] == "themes of home"

    def test_lyric_job_without_user_prompt_stores_no_style(self, tmp_path, branch):
        # Lyrics-only is a complete request: the style slot stays empty and the
        # workflow derives style from the video analysis.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_enhanced",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is True
        assert "music_style_prompt" not in payload
        assert payload["user_prompt"] == ""

    def test_plain_job_stored_in_default_shape(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "user_prompt": "calm piano"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is False
        assert payload["user_prompt"] == "calm piano"
        assert "music_style_prompt" not in payload
        assert "lyrics_prompt" not in payload


@pytest.mark.parametrize("branch", BRANCHES)
class TestLegacyClientCompat:
    """Pre-collapse payloads keep working; the four former 400s are now defined."""

    def test_legacy_verbose_vocal_call_unchanged(self, tmp_path, branch):
        # Old contract: verbose flag + style + lyrics + empty user_prompt.
        # Must still be accepted and stored exactly as before.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_enhanced",
                           "verbose_instruction": True,
                           "music_style_prompt": "epic orchestral",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is True
        assert payload["music_style_prompt"] == "epic orchestral"
        assert payload["lyrics_prompt"] == "themes of home"

    def test_legacy_style_only_keeps_explicit_direction_path(self, tmp_path, branch):
        # Old verbose style-only (the pre-collapse way to give explicit style
        # direction, vocal or instrumental): stored verbatim so it keeps the
        # explicit-direction processing it always had. The fold-into-
        # user_prompt alias applies only when the flag is absent.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "verbose_instruction": True,
                           "music_style_prompt": "epic orchestral"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is True
        assert payload["music_style_prompt"] == "epic orchestral"
        assert payload["user_prompt"] == ""
        assert "lyrics_prompt" not in payload

    def test_style_alias_without_flag_folds_into_default_path(self, tmp_path, branch):
        # music_style_prompt WITHOUT the verbose flag is deprecated-alias
        # usage: folds into user_prompt and runs the default inferred path.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "music_style_prompt": "epic orchestral"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is False
        assert payload["user_prompt"] == "epic orchestral"
        assert "music_style_prompt" not in payload

    def test_formerly_rejected_verbose_plus_user_prompt_now_accepted(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "verbose_instruction": True,
                           "user_prompt": "calm piano"}, branch=branch)
        assert r.status_code == 200, r.text
        assert _stored_payload(repo)["user_prompt"] == "calm piano"

    def test_formerly_rejected_split_fields_without_flag_now_accepted(self, tmp_path, branch):
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_enhanced",
                           "music_style_prompt": "epic orchestral",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        # Style aliases into the style slot, lyrics keeps the lyric channel.
        assert payload["music_style_prompt"] == "epic orchestral"
        assert payload["lyrics_prompt"] == "themes of home"

    def test_legacy_verbose_style_on_basic_still_rejected(self, tmp_path, branch):
        # The pre-collapse contract 400'd verbose+style on edenn_basic at
        # submit; that stays (now as the coded 10009) — the legacy passthrough
        # would otherwise accept a job the worker gate then fails post-200.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_basic",
                           "verbose_instruction": True,
                           "music_style_prompt": "calm piano"}, branch=branch)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error_code"] == 10009
        assert not repo.jobs

    def test_lyrics_gate_runs_after_legacy_alias_mapping(self, tmp_path, branch):
        # Legacy vendor-named modelspec aliases normalize BEFORE the lyrics
        # gate: an alias mapping to a lyrics-capable tier is accepted, one
        # mapping to the basic tier gets the coded 400.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "provider_c",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        assert _stored_payload(repo)["modelspec"] == "edenn_studio"

        client2, repo2, _ = _client(tmp_path)
        r2 = _post(client2, {"modelspec": "provider_a",
                             "lyrics_prompt": "themes of home"}, branch=branch)
        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"]["error_code"] == 10009
        assert not repo2.jobs

    def test_triple_user_prompt_style_and_lyrics(self, tmp_path, branch):
        # All three sent: user_prompt wins the style slot, the style alias is
        # discarded, lyrics keep the lyric channel.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_enhanced",
                           "user_prompt": "calm piano",
                           "music_style_prompt": "epic orchestral",
                           "lyrics_prompt": "themes of home"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["verbose_instruction"] is True
        assert payload["music_style_prompt"] == "calm piano"
        assert payload["lyrics_prompt"] == "themes of home"

    def test_user_prompt_wins_over_style_alias_when_both_sent(self, tmp_path, branch):
        # The alias is honored only when user_prompt is empty; sending both is
        # a new (formerly impossible) combination and user_prompt wins.
        client, repo, _ = _client(tmp_path)
        r = _post(client, {"modelspec": "edenn_studio",
                           "user_prompt": "calm piano",
                           "music_style_prompt": "epic orchestral"}, branch=branch)
        assert r.status_code == 200, r.text
        payload = _stored_payload(repo)
        assert payload["user_prompt"] == "calm piano"
        assert "music_style_prompt" not in payload
