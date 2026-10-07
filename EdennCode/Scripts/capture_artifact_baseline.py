#!/usr/bin/env python3
"""Capture the media a pipeline actually produces, so a refactor can be proven inert.

The 5-block response refactor is supposed to change only how results are *described*,
never what gets generated. Providers are stochastic, so we cannot diff bytes — instead we
run the real pipelines against real providers, download every asset the response points
at, and probe each into a manifest of objective properties (geometry, codec, duration,
loudness). Run this on the pre-change commit to get a baseline, run it again after, and
diff the two with ``compare_artifact_baseline.py``.

Usage:
    python3 EdennCode/Scripts/capture_artifact_baseline.py --out EdennCode/TestSuites/golden/artifact_baseline_before
    # ... apply the change ...
    python3 EdennCode/Scripts/capture_artifact_baseline.py --out EdennCode/TestSuites/golden/artifact_baseline_after
    python3 EdennCode/Scripts/compare_artifact_baseline.py \\
        EdennCode/TestSuites/golden/artifact_baseline_before EdennCode/TestSuites/golden/artifact_baseline_after

Credentials come from EdennCode.env.load_env() (LocalEnv/.env). This makes REAL, PAID
provider calls.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# The response is nested now and was flat before, and this script has to run on BOTH
# sides of the change to produce a comparable manifest — so every asset is located by
# searching for the key wherever it lives.
ASSET_KEYS = (
    "video_url",
    "thumbnail_url",
    "audio_url",
    "complete_audio_url",
)


def _walk_urls(payload: Any, out: dict[str, str]) -> dict[str, str]:
    """Collect every asset URL in the response, whatever shape it is."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ASSET_KEYS and isinstance(value, str) and value.startswith("http"):
                out.setdefault(key, value)
            elif key == "full_tracks" and isinstance(value, list):
                # Pre-change only: alternate takes used to be surfaced here.
                for index, track in enumerate(value):
                    url = (track or {}).get("url") if isinstance(track, dict) else None
                    if isinstance(url, str) and url.startswith("http"):
                        out.setdefault(f"full_tracks[{index}]", url)
            else:
                _walk_urls(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _walk_urls(item, out)
    return out


def _ffprobe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _integrated_loudness(path: Path) -> Optional[float]:
    """Integrated loudness in LUFS — the check that would catch a changed audio chain."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    stderr = proc.stderr
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return float(json.loads(stderr[start : end + 1])["input_i"])
    except Exception:
        return None


def probe(path: Path) -> dict[str, Any]:
    """Objective properties of one asset. Bytes vary run to run; these should not."""
    info = _ffprobe(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    entry: dict[str, Any] = {
        "size_bytes": path.stat().st_size,
        "duration_s": round(float(fmt.get("duration", 0.0) or 0.0), 3),
        "format": fmt.get("format_name"),
    }
    if video is not None:
        entry["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": video.get("r_frame_rate"),
            "nb_frames": video.get("nb_frames"),
        }
    if audio is not None:
        entry["audio"] = {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
        }
        # Still images (thumbnails) have no meaningful loudness.
        if video is None or entry["duration_s"] > 0.5:
            entry["audio"]["loudness_lufs"] = _integrated_loudness(path)
    return entry


def capture(*, name: str, payload: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Download and probe every asset a single job response points at."""
    media_dir = out_dir / name
    media_dir.mkdir(parents=True, exist_ok=True)

    urls = _walk_urls(payload, {})
    assets: dict[str, Any] = {}
    for key, url in sorted(urls.items()):
        suffix = Path(url.split("?")[0]).suffix or ".bin"
        local = media_dir / f"{key.replace('[', '_').replace(']', '')}{suffix}"
        with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
            response.raise_for_status()
            with local.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        assets[key] = probe(local)
        assets[key]["fetched"] = True
        print(f"  {key}: {assets[key]['duration_s']}s, {assets[key]['size_bytes']}B")

    (media_dir / "response.json").write_text(json.dumps(payload, indent=2))
    return {"assets": assets, "asset_count": len(assets)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--response",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="A job response JSON already captured from a real run, e.g. "
             "video_v2_monolith=/tmp/resp.json. Repeatable.",
    )
    args = parser.parse_args()

    if not args.response:
        parser.error(
            "Pass at least one --response NAME=PATH. Produce those JSON files by running "
            "the real pipelines, e.g.:\n"
            "  pytest -m remote_integration "
            "EdennCode/Deployment/Testing/test_api_video_generation_golden_remote_integration.py\n"
            "  python3 EdennCode/WorkflowFactory/VideoMusicWorkflow/run_video_music_workflow.py "
            "--video <f.mp4> --upload-results --output-json /tmp/video_sync.json"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"runs": {}}
    for spec in args.response:
        name, _, path = spec.partition("=")
        payload = json.loads(Path(path).read_text())
        print(f"{name}:")
        manifest["runs"][name] = capture(name=name, payload=payload, out_dir=args.out)

    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nWrote {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
