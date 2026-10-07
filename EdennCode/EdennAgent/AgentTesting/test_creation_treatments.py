"""CP3 tests: rephrase-original treatment — hermetic (stub ASR/LLM/TTS).

Pins: the ASR gate refuses honestly (None, never garbage), the rewrite stays
inside the word budget, and the service-level render path mixes the synthesized
voice over the cut (real ffmpeg) with the fallback to new narration recorded.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.AssetLibrary import AssetIngestor, InMemoryAulRepository
from EdennCode.EdennAgent.Creation import (
    BundleItem,
    CreationService,
    MusicSource,
    RephraseOriginalTreatment,
    RequestBundle,
    ShortSpec,
    SourceTranscript,
    TranscriptSpan,
    TreatmentKind,
    TreatmentSpec,
)
from EdennCode.EdennAgent.Recompose.Testing.test_recompose_m4_m5 import (
    MULTI_SCENE_TEXTS,
    _render_multi_scene_video,
)


class StubTranscriber:
    """Returns a fixed transcript; records which paths were asked for."""

    def __init__(self, transcript: SourceTranscript) -> None:
        self.transcript = transcript
        self.calls: list[str] = []

    def transcribe(self, path: Path) -> SourceTranscript:
        self.calls.append(str(path))
        return self.transcript


class StubLLM:
    """complete_messages stub returning a canned script."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.calls = 0

    async def complete_messages(self, messages, *, json_schema=None,
                                max_tokens=None):
        self.calls += 1
        return {"script": self.script}, {"total_tokens": 1}


async def stub_tts(*, script: str, voice: str, instructions: str,
                   speed: float, out_path: Path) -> Path:
    """Writes a real (tiny) wav so ffmpeg can mix it."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=300:duration=2", "-ar", "24000",
         str(out_path)], check=True, capture_output=True, timeout=60)
    return out_path


def _speech(conf: float, words: int = 30) -> SourceTranscript:
    text = " ".join(["margarine"] * words)
    return SourceTranscript(spans=[
        TranscriptSpan(start_s=0.0, end_s=10.0, text=text, confidence=conf)])


# ------------------------------------------------------------------ treatment
def test_gate_refuses_low_confidence_and_thin_speech(tmp_path) -> None:
    from EdennCode.EdennAgent.Recompose.domain import RecomposePlan, Knobs

    plan = RecomposePlan(hypothesis="x", knobs=Knobs(), passages=[])
    llm = StubLLM("never used")
    for transcript in (_speech(conf=0.2), _speech(conf=0.9, words=3)):
        t = RephraseOriginalTreatment(
            llm_client=llm, transcriber=StubTranscriber(transcript),
            tts_fn=stub_tts)
        result = asyncio.run(t.synthesize(
            voice_source_path=tmp_path / "v.mp4", plan=plan,
            duration_s=16.0, workdir=tmp_path))
        assert result is None
    assert llm.calls == 0, "a refused gate must not spend a model call"


def test_rephrase_happy_path_respects_word_budget(tmp_path) -> None:
    from EdennCode.EdennAgent.Recompose.domain import RecomposePlan, Knobs

    plan = RecomposePlan(hypothesis="hook first", knobs=Knobs(), passages=[])
    long_script = " ".join(["word"] * 200)      # far over any 16s budget
    t = RephraseOriginalTreatment(
        llm_client=StubLLM(long_script),
        transcriber=StubTranscriber(_speech(conf=0.9)), tts_fn=stub_tts)
    result = asyncio.run(t.synthesize(
        voice_source_path=tmp_path / "v.mp4", plan=plan,
        duration_s=16.0, workdir=tmp_path))
    assert result is not None
    budget = int((16.0 - 1.2) * 2.2)
    assert len(result.script.split()) <= int(budget * 1.2) + 1
    assert result.audio_path.exists() and result.voice == "nova"
    assert result.source_words == 30


# ------------------------------------------------------------- service render
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("cp3")
    video = _render_multi_scene_video(root / "reel.mp4")
    track = root / "beat.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "aevalsrc='0.9*sin(2*PI*82*t)*exp(-28*mod(t,0.5))':d=40:s=44100",
         "-ar", "44100", "-ac", "2", str(track)],
        check=True, capture_output=True, timeout=120)
    scenes = [
        {"scene_index": i, "start_timestamp": i * 4.5, "end_timestamp": (i + 1) * 4.5,
         "visual_summary": s, "key_actions": a, "mood": "energetic"}
        for i, (s, a) in enumerate(MULTI_SCENE_TEXTS)]
    repo = InMemoryAulRepository()
    ing = AssetIngestor(repo)
    video_id = asyncio.run(ing.ingest(video, name="reel.mp4",
                                      observation={"scenes": scenes},
                                      with_signals=True))
    track_id = asyncio.run(ing.ingest(track, kind="audio", name="beat.wav"))
    return {"repo": repo, "root": root, "video_id": video_id, "track_id": track_id}


def _service(world, transcriber) -> CreationService:
    svc = CreationService(
        world["repo"], workdir=world["root"] / "work",
        llm_client=StubLLM("A crisp rephrased pitch that lands the close."),
        rephrase=RephraseOriginalTreatment(
            llm_client=StubLLM("A crisp rephrased pitch that lands the close."),
            transcriber=transcriber, tts_fn=stub_tts))
    return svc


def _plan_and_render(world, svc, kind: TreatmentKind) -> str:
    bundle = RequestBundle(intent="a teaser", items=[
        BundleItem(ref=world["video_id"]), BundleItem(ref=world["track_id"])])
    short = ShortSpec(hypothesis="teaser", duration_s=10.0,
                      treatment=TreatmentSpec(kind=kind,
                                              music=MusicSource.PROVIDED,
                                              music_ref=world["track_id"]))
    resolved = svc.resolve(bundle).bundle
    preview, _ = asyncio.run(svc.plan_short(resolved, short))
    return asyncio.run(svc.render_short(preview.plan_id))


def test_render_with_rephrase_mixes_voice_and_records_script(world) -> None:
    svc = _service(world, StubTranscriber(_speech(conf=0.9)))
    out_id = _plan_and_render(world, svc, TreatmentKind.REPHRASE_ORIGINAL)
    out = world["repo"].get_asset(out_id)
    assert out is not None and out.generated
    assert out.meta.get("narration_script", "").startswith("A crisp rephrased")
    # the mixed output actually carries an audio stream
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", out.uri],
        capture_output=True, text=True, timeout=60)
    assert "audio" in probe.stdout


def test_render_falls_back_to_new_narration_when_gate_refuses(world) -> None:
    svc = _service(world, StubTranscriber(_speech(conf=0.1)))  # gate refuses
    out_id = _plan_and_render(world, svc, TreatmentKind.REPHRASE_ORIGINAL)
    out = world["repo"].get_asset(out_id)
    assert out is not None
    assert out.meta.get("narration_script", "").startswith("A crisp"), \
        "fallback must still narrate (new narration), recorded in lineage"
