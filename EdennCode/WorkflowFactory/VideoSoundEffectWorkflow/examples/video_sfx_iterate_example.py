"""
Iteration-loop demo on a saved video→SFX project — no video re-analysis.

    .venv/bin/python -m EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.examples.video_sfx_iterate_example \
        --project outputs/video_sfx_runs/<run>/ list
        ... regen <event_id> [--prompt "..."] [--variants 3]
        ... variant <event_id> <index>
        ... retime <event_id> <start_s> [<end_s>]
        ... resnap <event_id>
        ... gain <event_id> <db>
        ... mute <event_id> | unmute <event_id>
        ... add <start_s> <end_s> "<sound prompt>"
        ... remove <event_id>
        ... ambience [--prompt "..."] [--gain <db>] [--off|--on]
        ... render

Every mutating command auto-saves; run `render` to get the next revision mp4.
"""

from __future__ import annotations

import argparse
import asyncio

from EdennCode.env import load_env
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow import SfxProjectEditor


def print_events(editor: SfxProjectEditor) -> None:
    project = editor.project
    print(f"project rev {project.revision} — {len(project.events)} events")
    for event in project.events:
        flags = "".join(
            [
                "M" if event.muted else "-",
                "S" if event.refined_start_time is not None else "-",
                "U" if event.source == "user" else "-",
            ]
        )
        print(
            f"  {event.event_id} [{flags}] {event.effective_start:6.2f}-{event.end_time:6.2f}s "
            f"gain {event.gain_db:+.1f}dB v{event.selected_variant + 1}/{len(event.variant_paths)}  "
            f"{event.generation_prompt[:70]}"
        )
    if project.ambience:
        state = "on" if project.ambience.enabled else "off"
        print(f"  ambience [{state}] gain {project.ambience.gain_db:+.1f}dB  {project.ambience.prompt[:70]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("command", nargs="+")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--on", action="store_true")
    args = parser.parse_args()

    load_env()
    editor = SfxProjectEditor(args.project)
    cmd, *rest = args.command

    if cmd == "list":
        print_events(editor)
        return
    if cmd == "regen":
        await editor.regenerate_event(rest[0], prompt=args.prompt, num_variants=args.variants)
    elif cmd == "variant":
        editor.select_variant(rest[0], int(rest[1]))
    elif cmd == "retime":
        editor.retime_event(
            rest[0],
            start_time=float(rest[1]),
            end_time=float(rest[2]) if len(rest) > 2 else None,
        )
    elif cmd == "resnap":
        await editor.resnap_event_timing(rest[0])
    elif cmd == "gain":
        editor.set_event_gain(rest[0], float(rest[1]))
    elif cmd == "mute":
        editor.set_event_muted(rest[0], True)
    elif cmd == "unmute":
        editor.set_event_muted(rest[0], False)
    elif cmd == "add":
        await editor.add_event(
            start_time=float(rest[0]),
            end_time=float(rest[1]),
            description=rest[2],
            sound_prompt=rest[2],
        )
    elif cmd == "remove":
        editor.remove_event(rest[0])
    elif cmd == "ambience":
        enabled = True if args.on else (False if args.off else None)
        await editor.set_ambience(prompt=args.prompt, enabled=enabled, gain_db=args.gain)
    elif cmd == "render":
        result = editor.render(render_sfx_only_debug=True)
        print(f"rendered rev {editor.project.revision}: {result.final_video_path}")
        return
    else:
        raise SystemExit(f"unknown command: {cmd}")

    editor.save()
    print_events(editor)
    print("\nsaved. run `render` to hear it.")


if __name__ == "__main__":
    asyncio.run(main())
