"""
Real end-to-end run of the video→SFX workflow (network: multimodal analysis +
SFX provider). Requires LocalEnv credentials via `load_env()`.

    .venv/bin/python -m EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.examples.video_sfx_e2e_run \
        --video ~/Downloads/bike_enhanced.mp4 [--prompt "crisp whooshes on transitions"] \
        [--variants 2] [--no-ambience]

The video is uploaded to blob storage for the analysis pass unless --url is
given. Outputs land in outputs/video_sfx_runs/<timestamp>/, including
sfx_project.json — feed that to video_sfx_iterate_example.py or the editor.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from EdennCode.env import load_env
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow import (
    VideoSfxWorkflowOptions,
    VideoSoundEffectWorkflowE2E,
    VideoSoundEffectWorkflowE2EInput,
)


def upload_and_sign(video_path: Path) -> str:
    from EdennCode.Deployment.settings import DeploymentSettings
    from EdennCode.Deployment.storage import create_storage_service

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    blob_name = storage.upload_path(
        container=settings.upload_container,
        path=video_path,
        blob_name=f"jobs/{uuid4().hex}/input/{video_path.name}",
        content_type="video/mp4",
    )
    return storage.generate_sas_url(container=settings.upload_container, blob_name=blob_name)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--url", default="", help="already-uploaded public URL (skips upload)")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--no-ambience", action="store_true")
    parser.add_argument("--no-refine", action="store_true", help="disable motion-onset snapping")
    parser.add_argument("--run-dir", default=None)
    args = parser.parse_args()

    load_env()
    video_path = Path(args.video).expanduser().resolve()
    video_url = args.url or upload_and_sign(video_path)

    workflow = VideoSoundEffectWorkflowE2E()
    output = await workflow.execute(
        VideoSoundEffectWorkflowE2EInput(
            video_path=str(video_path),
            uploaded_public_facing_url=video_url,
            user_prompt=args.prompt,
            run_dir=args.run_dir,
            options=VideoSfxWorkflowOptions(
                num_variants=args.variants,
                enable_ambience=not args.no_ambience,
                enable_timing_refinement=not args.no_refine,
            ),
        )
    )

    print(f"\nscene: {output.project.scene_summary}")
    print(f"events ({len(output.generated_sound_events)}):")
    refinements_by_id = {r.event_id: r for r in output.timing_refinements}
    for event in output.generated_sound_events:
        refinement = refinements_by_id.get(event.event_id)
        snap = (
            f" (snapped {refinement.shift_s:+.2f}s)" if refinement and refinement.snapped else ""
        )
        print(
            f"  {event.event_id} [{event.event_type:>10}] "
            f"{event.effective_start:6.2f}-{event.end_time:6.2f}s{snap}  {event.event_description}"
        )
    if output.project.ambience:
        print(f"ambience: {output.project.ambience.prompt}")
    print(f"\nlatency: {output.latency_seconds:.1f}s")
    print(f"project:  {output.project.project_file}")
    print(f"final:    {output.final_video_path}")
    print(f"sfx-only: {output.project.sfx_only_video_path}")


if __name__ == "__main__":
    asyncio.run(main())
