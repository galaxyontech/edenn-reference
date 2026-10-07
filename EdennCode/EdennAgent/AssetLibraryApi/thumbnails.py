"""On-demand thumbnail rendering (a single frame at a timestamp), cached to disk."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..AssetLibrary.models import Asset


class ThumbnailRenderer:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or Path(tempfile.gettempdir()) / "aul_thumbs"

    def render(self, asset: Asset, t: float = 1.0) -> Path:
        """Return a cached JPEG frame of ``asset`` at ``t`` seconds.

        Raises ``FileNotFoundError`` if ffmpeg cannot produce a frame (e.g. an
        audio-only asset).
        """

        self.cache_dir.mkdir(exist_ok=True)
        out = self.cache_dir / f"{asset.asset_id}_{t:.1f}.jpg"
        if not out.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", asset.uri,
                 "-frames:v", "1", "-vf", "scale=320:-2", str(out)],
                check=False, capture_output=True, timeout=60)
        if not out.exists():
            raise FileNotFoundError(f"no thumbnail for {asset.asset_id} at t={t}")
        return out
