from __future__ import annotations

import shutil
import subprocess
from typing import List


class MediaTools:
    """Thin wrappers over ffmpeg/ffprobe to keep the rest of the code clean."""

    @staticmethod
    def ensure(tool_name: str) -> None:
        if shutil.which(tool_name) is None:
            raise RuntimeError(f"Required tool '{tool_name}' not found in PATH. Install it first.")

    @staticmethod
    def run(cmd: List[str]) -> str:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"Command failed ({p.returncode}). Output:\n{p.stdout}")
        return p.stdout

    @staticmethod
    def duration_seconds(path: str) -> float:
        """Return media duration using ffprobe."""
        MediaTools.ensure("ffprobe")
        out = MediaTools.run([
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]).strip()
        try:
            return float(out)
        except ValueError as e:
            raise RuntimeError(f"ffprobe couldn't parse duration for {path}: '{out}'") from e
