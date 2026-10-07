#!/usr/bin/env python3
"""Build push copies of the wireframes with _wire.js inlined.

The the design tool pane does not resolve relative <script src>, so the repo
files reference _wire.js (single source of truth for local editing/serving)
and this script produces _push/ copies with the runtime inlined for upload.
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PUSH = HERE / "_push"
TAG = '<script src="_wire.js"></script>'

PAGES = [
    "s1-home.html",
    "s2-library.html",
    "s2b-ingest-organize.html",
    "s3-brief.html",
    "s4-creation.html",
    "s4c-canvas.html",
    "s5-launch.html",
    "s6-console.html",
    "s6c-canvas-performance.html",
    "w0-loop-map.html",
    "w8-capability-map.html",
]


def main() -> int:
    runtime = (HERE / "_wire.js").read_text(encoding="utf-8")
    inlined = "<script>\n" + runtime + "\n</script>"
    PUSH.mkdir(exist_ok=True)
    failures = []
    for name in PAGES:
        src = HERE / name
        html = src.read_text(encoding="utf-8")
        if TAG not in html:
            failures.append(name)
            continue
        (PUSH / name).write_text(html.replace(TAG, inlined), encoding="utf-8")
        print(f"inlined -> _push/{name}")
    if failures:
        print(f"ERROR: no {TAG} tag in: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
