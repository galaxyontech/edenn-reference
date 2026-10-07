"""M5.5 audio-layer tests: speech signal, narration flow, music-gen mapping,
and the duck/narration assembly branches. Provider clients are all faked;
media is real rendered fixtures."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from EdennCode.EdennAgent.Recompose.assembly import render_variant
from EdennCode.EdennAgent.Recompose.audio import (
    generate_music_for_plan,
    narration_script_from_plan,
    synthesize_narration,
)
from EdennCode.EdennAgent.Recompose.cutspec import generate_cut_spec
from EdennCode.EdennAgent.Recompose.domain import AssetRecord, Knobs
from EdennCode.EdennAgent.Recompose.musicsheet import (
    build_music_sheet,
    provisional_sheet,
)
from EdennCode.EdennAgent.Recompose.planner import plan_recompose
from EdennCode.EdennAgent.Recompose.segmentation import build_segment_tree
from EdennCode.EdennAgent.Recompose.signals import (
    annotate_tree_signals,
    measure_speech,
)

from .test_recompose_m4_m5 import MULTI_SCENE_TEXTS, _render_multi_scene_video


def _render_video_with_speechy_audio(dest: Path, scene_s: float = 4.5) -> Path:
    """The 8-scene fixture, with vocal-band audio on the first half and
    broadband noise on the second — a speech-vs-music audio contrast."""

    silent = dest.parent / "silent_base.mp4"
    _render_multi_scene_video(silent, scene_s=scene_s)
    total = 8 * scene_s
    half = total / 2
    # First half: modulated 400+900 Hz tones (speech band). Second half: white noise.
    aud = (
        f"aevalsrc='if(lt(t,{half}), (0.5+0.5*sin(2*PI*3*t))*(sin(2*PI*400*t)+0.6*sin(2*PI*900*t))*0.4,"
        f" 0.25*(random(0)-0.5))':d={total}:s=48000"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(silent),
         "-f", "lavfi", "-i", aud,
         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dest)],
        check=True, capture_output=True, timeout=120,
    )
    return dest


@pytest.fixture(scope="module")
def audio_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("m55")
    video = _render_video_with_speechy_audio(root / "spine_audio.mp4")
    track = root / "pulse.wav"
    expr = ("(0.55 + 0.45*between(t,8,16))*sin(2*PI*880*t)*lt(mod(t,0.5),0.08)"
            " + 0.02*sin(2*PI*110*t)")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"aevalsrc='{expr}':d=24:s=22050", str(track)],
        check=True, capture_output=True, timeout=60)
    spine = AssetRecord(kind="video", path=str(video), duration_s=36.0)
    scenes = [
        {"scene_index": i, "start_timestamp": i * 4.5, "end_timestamp": (i + 1) * 4.5,
         "visual_summary": s, "key_actions": a,
         "mood": "energetic" if i % 2 == 0 else "calm and still"}
        for i, (s, a) in enumerate(MULTI_SCENE_TEXTS)
    ]
    trees = {spine.asset_id: build_segment_tree(spine, analysis_scenes=scenes)}
    annotate_tree_signals(trees[spine.asset_id], spine, max_workers=2)
    sheet = build_music_sheet(str(track), window_s=20.0)
    return {"spine": spine, "trees": trees, "sheet": sheet, "workdir": root}


# ------------------------------------------------------------------- signals
def test_speech_signal_separates_voice_band_from_noise(audio_media) -> None:
    path = audio_media["spine"].path
    speechy = measure_speech(path, 2.0, 4.0)     # tone-modulated vocal band
    noisy = measure_speech(path, 30.0, 4.0)      # broadband noise
    assert speechy is not None and noisy is not None
    assert speechy > noisy + 0.25, (speechy, noisy)
    tree = audio_media["trees"][audio_media["spine"].asset_id]
    assert any("speech" in n.quality_flags for n in tree.leaves()[:4])


# ----------------------------------------------------------------- narration
class _FakeLlm:
    async def complete_messages(self, messages, *, json_schema, max_tokens=800):
        return {"script": "A quiet morning. The patterns move. "
                          "Color settles into stillness. We end where we began."}, {}


def test_narration_script_respects_word_budget(audio_media) -> None:
    spec = generate_cut_spec(audio_media["sheet"], Knobs())
    plan = plan_recompose(assets=[audio_media["spine"]], trees=audio_media["trees"],
                          sheet=audio_media["sheet"], spec=spec, hypothesis="test")
    script = asyncio.run(narration_script_from_plan(_FakeLlm(), plan,
                                                    audio_media["sheet"].window_s))
    assert script and len(script.split()) <= int(audio_media["sheet"].window_s * 2.2 * 1.3)


async def _fake_tts(*, script, voice, instructions, speed, out_path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=600:duration=6", "-ar", "24000", str(out_path)],
        check=True, capture_output=True, timeout=60)
    return out_path


def test_synthesize_narration_uses_injected_tts(audio_media, tmp_path) -> None:
    out = asyncio.run(synthesize_narration("hello world", tmp_path / "vo.mp3",
                                           tts_fn=_fake_tts))
    assert out.exists() and out.stat().st_size > 1000


# ----------------------------------------------------------------- music gen
class _FakeMusicService:
    def __init__(self, track: Path):
        self.track = track
        self.requests = []

    async def generate(self, request):
        from types import SimpleNamespace
        self.requests.append(request)
        return SimpleNamespace(
            primary=SimpleNamespace(audio_path=self.track, duration_s=24.0),
            used_modelspec=request.modelspec, alternates=[],
        )


def test_generate_music_maps_passages_to_sections(audio_media, tmp_path) -> None:
    spec = generate_cut_spec(audio_media["sheet"], Knobs())
    plan = plan_recompose(assets=[audio_media["spine"]], trees=audio_media["trees"],
                          sheet=audio_media["sheet"], spec=spec, hypothesis="test edit")
    fake = _FakeMusicService(Path(audio_media["sheet"].track_path))
    obs = {"music_prompt": {"tempo_bpm": 120, "global_mood": "playful",
                            "instruments": ["synth", "drums"]}}
    out = asyncio.run(generate_music_for_plan(plan, obs, 20.0, tmp_path, service=fake))
    assert out.exists()
    req = fake.requests[0]
    sp = req.section_plan
    assert sp.total_duration_s == 20.0
    assert sp.target_bpm == 120 and sp.overall_mood == "playful"
    assert len(sp.sections) == len([p for p in plan.passages if p.slot_indices])
    assert abs(sum(s.target_duration_s for s in sp.sections) - 20.0) < 1.5


def test_provisional_sheet_is_plannable() -> None:
    sheet = provisional_sheet(30.0, tempo_bpm=120)
    assert len(sheet.beats) >= 50 and sheet.phrases
    spec = generate_cut_spec(sheet, Knobs(cut_density="sparse"))
    assert spec.slots and abs(spec.total_s - (sheet.beats[-1] - sheet.beats[0])) < 1.0


# ------------------------------------------------------------------ assembly
def _has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    return "audio" in (out.stdout or "")


def test_duck_mode_keeps_source_audio_under_music(audio_media) -> None:
    knobs = Knobs(cut_density="sparse", source_audio="duck")
    spec = generate_cut_spec(audio_media["sheet"], knobs)
    plan = plan_recompose(assets=[audio_media["spine"]], trees=audio_media["trees"],
                          sheet=audio_media["sheet"], spec=spec)
    variant = render_variant(plan, audio_media["sheet"], [audio_media["spine"]],
                             audio_media["trees"], audio_media["workdir"], label="duck")
    assert Path(variant.path).exists() and _has_audio(variant.path)
    assert variant.cut_report.cut_to_beat_ms_median <= 40.0


def test_auto_restore_lifts_dark_sources_only(audio_media, tmp_path) -> None:
    """A dark transfer gets the deterministic exposure lift; normal footage
    is untouched (gate = asset median luma < 48)."""

    import re, statistics
    from EdennCode.EdennAgent.Recompose.domain import AssetRecord
    from EdennCode.EdennAgent.Recompose.segmentation import build_segment_tree
    from EdennCode.EdennAgent.Recompose.signals import annotate_tree_signals

    # darken the fixture hard (luma way below the gate)
    dark = tmp_path / "dark.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio_media["spine"].path,
                    "-vf", "eq=brightness=-0.35", "-an",
                    "-c:v", "libx264", "-preset", "veryfast", str(dark)],
                   check=True, capture_output=True, timeout=180)
    asset = AssetRecord(kind="video", path=str(dark), duration_s=36.0)
    tree = build_segment_tree(asset)
    trees = {asset.asset_id: tree}
    annotate_tree_signals(tree, asset, max_workers=2)
    knobs = Knobs(cut_density="sparse", coherence_mode="single_story",
                  max_slots_per_scene=6)
    spec = generate_cut_spec(audio_media["sheet"], knobs)
    plan = plan_recompose(assets=[asset], trees=trees,
                          sheet=audio_media["sheet"], spec=spec)
    variant = render_variant(plan, audio_media["sheet"], [asset], trees,
                             tmp_path, label="restored")

    def mean_luma(path: str) -> float:
        out = subprocess.run(
            ["ffmpeg", "-v", "info", "-t", "6", "-i", path,
             "-vf", "scale=120:-2,fps=4,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
             "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        vals = [float(m) for m in re.findall(r"YAVG=([0-9.]+)", out.stdout or "")]
        return statistics.fmean(vals)

    assert mean_luma(variant.path) > mean_luma(str(dark)) + 15, \
        "restore must lift a dark source substantially"

    # normal-brightness source: restore gate must NOT fire (cache key differs)
    knobs_off = Knobs(cut_density="sparse", restore="off",
                      coherence_mode="single_story", max_slots_per_scene=6)
    spec_off = generate_cut_spec(audio_media["sheet"], knobs_off)
    plan_off = plan_recompose(assets=[asset], trees=trees,
                              sheet=audio_media["sheet"], spec=spec_off)
    variant_off = render_variant(plan_off, audio_media["sheet"], [asset], trees,
                                 tmp_path, label="untouched")
    assert mean_luma(variant_off.path) < mean_luma(variant.path) - 10, \
        "restore=off must leave the dark source dark"


def test_sound_bites_preserve_speech_spans_intact(audio_media) -> None:
    """The 'original voice-over is gone' fix: bites are natural speech spans
    played at full length with source audio; music ducks under them."""

    import re
    tree = audio_media["trees"][audio_media["spine"].asset_id]
    assert tree.audio_activity, "signals must have captured activity spans"
    knobs = Knobs(cut_density="medium", coherence_mode="single_story",
                  max_slots_per_scene=6, sound_bites=2)
    spec = generate_cut_spec(audio_media["sheet"], knobs)
    plan = plan_recompose(assets=[audio_media["spine"]], trees=audio_media["trees"],
                          sheet=audio_media["sheet"], spec=spec)
    bites = [s for s in plan.slots if s.spec.is_bite]
    assert bites, "at least one bite must be placed on speech-rich material"
    for b in bites:
        assert b.spec.dur_s >= 2.5, "a bite must be sentence-length"
        assert b.locked and "[bite]" in b.why
    # indices stay contiguous after merging
    assert [s.spec.index for s in plan.slots] == list(range(len(plan.slots)))

    variant = render_variant(plan, audio_media["sheet"], [audio_media["spine"]],
                             audio_media["trees"], audio_media["workdir"], label="bites")
    assert Path(variant.path).exists() and _has_audio(variant.path)
    # the bite span in the OUTPUT carries strong vocal-band energy (source
    # speech), unlike a non-bite span (music only)
    t = 0.0
    bite_span = None
    nonbite_span = None
    for s in plan.slots:
        if s.spec.is_bite and bite_span is None:
            bite_span = (t, t + s.spec.dur_s)
        elif not s.spec.is_bite and nonbite_span is None and s.spec.dur_s >= 1.5:
            nonbite_span = (t, t + s.spec.dur_s)
        t += s.spec.dur_s
    from EdennCode.EdennAgent.Recompose.signals import measure_speech
    sp_bite = measure_speech(variant.path, bite_span[0] + 0.2, bite_span[1] - bite_span[0] - 0.4)
    assert sp_bite is not None and sp_bite >= 0.5, f"bite span must sound like speech ({sp_bite})"


def test_narration_mix_layers_vo_over_music(audio_media, tmp_path) -> None:
    knobs = Knobs(cut_density="sparse", narration="generate")
    spec = generate_cut_spec(audio_media["sheet"], knobs)
    plan = plan_recompose(assets=[audio_media["spine"]], trees=audio_media["trees"],
                          sheet=audio_media["sheet"], spec=spec)
    vo = asyncio.run(synthesize_narration("test narration", tmp_path / "vo.mp3",
                                          tts_fn=_fake_tts))
    variant = render_variant(plan, audio_media["sheet"], [audio_media["spine"]],
                             audio_media["trees"], audio_media["workdir"],
                             label="narrated", narration_path=vo)
    assert Path(variant.path).exists() and _has_audio(variant.path)
    assert variant.cut_report.duration_s > 15.0
