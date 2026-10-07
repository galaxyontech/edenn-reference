from __future__ import annotations

import os


def beat_aware_enabled() -> bool:
    return os.getenv("BEAT_AWARE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
