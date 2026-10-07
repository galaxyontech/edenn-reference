from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.MusicGenerationCore.audio import measure_audio_duration
from EdennCode.MusicGenerationCore.models import (
    MusicGenerationOptions,
    MusicGenerationRequest,
    MusicModelSpec,
    MusicSection,
    NarrativeCue,
    SectionPlan,
    normalize_modelspec,
)
from EdennCode.MusicGenerationCore.provider_registry import (
    build_default_music_generation_service,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_music_provider,
)
from EdennCode.Util.MediaUtils import (
    get_video_duration,
    has_audio_stream,
    overlay_music_on_video,
    resolve_ffmpeg_binary,
)
from EdennCode.env import load_env


DEFAULT_PROMPT = (
    "Instrumental background music for a polished health-tech product demo "
    "and slide presentation about Lumi, an IBS and digestive health companion. "
    "The score should feel calm, trustworthy, optimistic, modern, and lightly "
    "playful. Keep it unobtrusive under the original voice track: no vocals, "
    "no lyrics, no vocal chops, no dramatic drops. Use warm pads, soft piano, "
    "gentle plucks, subtle electronic percussion, and a round low bass pulse."
)


def _build_section_plan(duration_s: float, prompt: str) -> SectionPlan:
    labels = [
        (
            "Problem context",
            "Open with a gentle, empathetic bed for digestive health statistics.",
            "soft pads, sparse piano",
        ),
        (
            "Market gap",
            "Add a quiet pulse while explaining why simple symptom trackers are insufficient.",
            "muted plucks, restrained percussion",
        ),
        (
            "Daily check-in",
            "Brighten the tone for the mobile check-in flow without drawing focus.",
            "warm mallets, light synth arpeggio",
        ),
        (
            "Personalized chat",
            "Create a friendly conversational feel for voice chat and coaching screens.",
            "soft piano, airy pads",
        ),
        (
            "Logging and insights",
            "Add mild forward motion for food logging and ingredient breakdowns.",
            "subtle beat, round bass",
        ),
        (
            "Wearable integration",
            "Close with a clean, confident lift for Apple Watch integration.",
            "brighter synth layers, gentle rhythmic pulse",
        ),
    ]
    section_count = len(labels)
    base_duration = duration_s / section_count
    durations = [base_duration for _ in labels]
    durations[-1] = duration_s - sum(durations[:-1])

    cues: list[NarrativeCue] = []
    sections: list[MusicSection] = []
    for idx, ((label, objective, instrumentation), section_duration) in enumerate(
        zip(labels, durations), start=1
    ):
        cue_id = f"cue_{idx}"
        section_id = f"section_{idx}"
        cues.append(
            NarrativeCue(
                cue_id=cue_id,
                label=label,
                role="presentation chapter",
                target_duration_s=section_duration,
                emotion="calm confidence",
                description=objective,
                transition_hint="smooth low-contrast transition",
                source_refs=[section_id],
            )
        )
        sections.append(
            MusicSection(
                section_id=section_id,
                label=label,
                target_duration_s=section_duration,
                objective=objective,
                energy_start=0.22 + (idx - 1) * 0.04,
                energy_end=0.28 + (idx - 1) * 0.04,
                cue_ids=[cue_id],
                instrumentation_focus=[
                    item.strip() for item in instrumentation.split(",")
                ],
                lyric_lines=[],
            )
        )

    return SectionPlan(
        summary=(
            "A six-minute product presentation for a digestive health app, "
            "moving from audience/problem framing into app flows for check-ins, "
            "personalized chat, meal logging, and Apple Watch integration."
        ),
        total_duration_s=duration_s,
        overall_mood="calm, trustworthy, optimistic, polished health-tech",
        target_bpm=92,
        primary_instruments=[
            "warm synth pads",
            "soft piano",
            "gentle plucks",
            "subtle electronic percussion",
            "round bass",
        ],
        cues=cues,
        sections=sections,
        music_prompt_summary=prompt,
    )


def _music_length_ms_for_basic(duration_s: float) -> int:
    return max(10_000, min(300_000, int(duration_s * 1000)))


async def _generate_basic(
    *,
    prompt: str,
    duration_s: float,
    output_format: str | None,
) -> dict[str, Any]:
    provider = build_music_provider()
    path, _ = await provider.generate(
        prompt,
        music_length_ms=_music_length_ms_for_basic(duration_s),
        with_timestamps=False,
        output_format=output_format,
    )
    return {
        "provider": "provider_a",
        "audio_path": path,
        "job_ref": {"provider": "provider_a"},
        "prompt_summary": prompt,
    }


async def _generate_core_model(
    *,
    modelspec: MusicModelSpec,
    section_plan: SectionPlan,
    output_dir: Path,
) -> dict[str, Any]:
    service = build_default_music_generation_service()
    request = MusicGenerationRequest(
        request_id=f"weixin_bgm_{modelspec.value}_{int(time.time())}",
        modelspec=modelspec,
        section_plan=section_plan,
        options=MusicGenerationOptions(
            include_vocals=False,
            max_variants=1,
            require_word_timestamps=False,
        ),
        output_dir=output_dir / modelspec.value,
    )
    result = await service.generate(request)
    return {
        "provider": result.job_ref.provider,
        "audio_path": result.primary.audio_path,
        "job_ref": asdict(result.job_ref),
        "prompt_summary": result.prompt_summary,
        "prompt_manifest": result.prompt_manifest,
    }


def _loop_audio_to_duration(
    audio_path: Path,
    *,
    target_duration_s: float,
    output_path: Path,
) -> Path:
    current_duration = measure_audio_duration(audio_path, fallback_s=0.0)
    if current_duration + 0.25 >= target_duration_s:
        return audio_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg_binary(),
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(audio_path),
        "-t",
        f"{target_duration_s:.3f}",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def _mix_with_original(
    *,
    video_path: Path,
    music_path: Path,
    output_path: Path,
    music_volume: float,
) -> Path:
    return overlay_music_on_video(
        video_path,
        music_path,
        output_path,
        music_volume=music_volume,
        preserve_original_audio=True,
    )


async def _run(args: argparse.Namespace) -> int:
    load_env()

    video_path = args.video.expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not has_audio_stream(video_path):
        raise RuntimeError(f"Input video has no original audio stream: {video_path}")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OUTPUT_AUDIO_DIR", str(output_dir / "provider_raw"))

    duration_s = get_video_duration(video_path)
    if duration_s <= 0:
        raise RuntimeError(f"Could not determine video duration: {video_path}")

    prompt = args.prompt.strip() if args.prompt else DEFAULT_PROMPT
    section_plan = _build_section_plan(duration_s, prompt)
    requested_models = (
        [
            MusicModelSpec.EDENN_BASIC,
            MusicModelSpec.EDENN_ENHANCED,
            MusicModelSpec.EDENN_STUDIO,
        ]
        if args.modelspec == "all"
        else [normalize_modelspec(args.modelspec)]
    )

    records: list[dict[str, Any]] = []
    for modelspec in requested_models:
        model_start = time.monotonic()
        print(f"START {modelspec.value}", flush=True)
        record: dict[str, Any] = {
            "modelspec": modelspec.value,
            "status": "failed",
            "video_duration_s": duration_s,
        }
        try:
            if modelspec == MusicModelSpec.EDENN_BASIC:
                generated = await _generate_basic(
                    prompt=prompt,
                    duration_s=duration_s,
                    output_format=args.audio_output_format,
                )
            else:
                generated = await _generate_core_model(
                    modelspec=modelspec,
                    section_plan=section_plan,
                    output_dir=output_dir,
                )

            raw_audio_path = Path(generated["audio_path"]).expanduser().resolve()
            raw_duration_s = measure_audio_duration(
                raw_audio_path,
                fallback_s=duration_s,
            )
            extended_audio_path = _loop_audio_to_duration(
                raw_audio_path,
                target_duration_s=duration_s,
                output_path=output_dir
                / modelspec.value
                / f"{raw_audio_path.stem}_looped_to_video.wav",
            )
            final_audio_duration_s = measure_audio_duration(
                extended_audio_path,
                fallback_s=duration_s,
            )
            remixed_video_path = output_dir / f"{video_path.stem}_{modelspec.value}_bgm.mp4"
            _mix_with_original(
                video_path=video_path,
                music_path=extended_audio_path,
                output_path=remixed_video_path,
                music_volume=args.music_volume,
            )

            record.update(
                {
                    "status": "completed",
                    "provider": generated["provider"],
                    "raw_audio_path": str(raw_audio_path),
                    "raw_audio_duration_s": raw_duration_s,
                    "extended_audio_path": str(extended_audio_path),
                    "final_audio_duration_s": final_audio_duration_s,
                    "remixed_video_path": str(remixed_video_path),
                    "music_volume": args.music_volume,
                    "preserve_original_audio": True,
                    "elapsed_s": round(time.monotonic() - model_start, 3),
                    "job_ref": generated.get("job_ref"),
                    "prompt_summary": generated.get("prompt_summary"),
                    "prompt_manifest": generated.get("prompt_manifest"),
                }
            )
            print(
                "DONE "
                f"{modelspec.value} provider={record['provider']} "
                f"raw={raw_duration_s:.2f}s final={final_audio_duration_s:.2f}s "
                f"video={remixed_video_path}",
                flush=True,
            )
        except Exception as exc:
            record.update(
                {
                    "error": str(exc),
                    "elapsed_s": round(time.monotonic() - model_start, 3),
                }
            )
            print(f"FAILED {modelspec.value}: {exc}", flush=True)
        records.append(record)

        summary_path = output_dir / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "source_video": str(video_path),
                    "video_duration_s": duration_s,
                    "prompt": prompt,
                    "results": records,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"WROTE {summary_path}", flush=True)

    return 0 if any(record["status"] == "completed" for record in records) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate instrumental BGM variants and mix them under original video audio.",
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument(
        "--modelspec",
        choices=["all", "edenn_basic", "edenn_enhanced", "edenn_studio"],
        default="all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/video_bgm/weixin_lumi"),
    )
    parser.add_argument("--music-volume", type=float, default=0.35)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--audio-output-format", default=None)
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
