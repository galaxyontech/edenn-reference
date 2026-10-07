"""
Batch evaluation runner for saved video→SFX projects.

Usage (offline — never calls a model):

    python -m EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.eval.eval_runner \
        --project outputs/video_sfx_runs/20260812_101500 \
        --labels  eval_sets/bike_clip.labels.json \
        --output  outputs/video_sfx_eval

Each --project may repeat. Labels are optional; without them only the
audio↔visual alignment report is produced. The runner writes one JSON per
project plus a combined markdown summary table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.VideoSoundEffectDataModel.datamodel import (
    SfxProject,
)
from EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.eval.alignment_metrics import (
    load_labels,
    measure_event_detection,
    measure_project_alignment,
)


def evaluate_project(project_path: Path, labels_path: Optional[Path]) -> dict:
    project = SfxProject.load(project_path)
    alignment = measure_project_alignment(project)
    result = {
        "project": str(project_path),
        "video_path": project.video_path,
        "events": len(project.events),
        "revision": project.revision,
        "alignment": alignment.to_dict(),
    }
    if labels_path is not None:
        labels = load_labels(labels_path)
        detection = measure_event_detection(project.events, labels)
        result["detection"] = detection.to_dict()
    return result


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def build_summary_markdown(results: List[dict]) -> str:
    lines = [
        "| project | events | measured | mean abs err (s) | median (s) | ≤100ms | ≤250ms | det. F1@0.3 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        alignment = result["alignment"]
        tolerance = alignment.get("within_tolerance", {})
        detection = result.get("detection", {})
        f1 = detection.get("per_threshold", {}).get("iou_0.3", {}).get("f1")
        lines.append(
            "| {name} | {events} | {measured} | {mean} | {median} | {t100} | {t250} | {f1} |".format(
                name=Path(result["project"]).name,
                events=result["events"],
                measured=alignment.get("measured_events", 0),
                mean=_fmt(alignment.get("mean_abs_error_s")),
                median=_fmt(alignment.get("median_abs_error_s")),
                t100=_fmt(tolerance.get("100ms")),
                t250=_fmt(tolerance.get("250ms")),
                f1=_fmt(f1),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", required=True, help="project dir or sfx_project.json")
    parser.add_argument("--labels", action="append", default=[], help="labels JSON, positionally matched to --project")
    parser.add_argument("--output", default="outputs/video_sfx_eval", help="report output directory")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    results: List[dict] = []
    for idx, project_arg in enumerate(args.project):
        labels_path = Path(args.labels[idx]) if idx < len(args.labels) else None
        result = evaluate_project(Path(project_arg), labels_path)
        results.append(result)
        report_path = output_dir / f"{Path(project_arg).name}.eval.json"
        report_path.write_text(json.dumps(result, indent=2))
        print(f"wrote {report_path}")

    summary = build_summary_markdown(results)
    summary_path = output_dir / "summary.md"
    summary_path.write_text(summary)
    print(f"wrote {summary_path}\n")
    print(summary)


if __name__ == "__main__":
    main()
