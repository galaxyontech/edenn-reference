from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / "LocalEnv" / ".env"


def load_env(*, override: bool = False) -> bool:
    """
    Load environment variables from EdennCode/LocalEnv/.env if it exists.
    Falls back to the default dotenv search if the file is missing.
    """
    if ENV_PATH.exists():
        return load_dotenv(dotenv_path=ENV_PATH, override=override)
    return load_dotenv(override=override)
