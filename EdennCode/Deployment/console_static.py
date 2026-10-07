"""The console's server side: its static files, and its runtime configuration.

The console is a folder of static files, so the API can hand it out directly and
page and API end up same-origin — no CORS, no second hostname, and no runtime to
keep alive beside the API.

Mounting is best-effort by design: the backend deploys on its own schedule and
must not require anyone to have run ``next build`` first. Absent a build, the
path simply 404s.

Firebase's public client config is **served, not bundled**. Baking it into the
export would put the same five values in two places — the build args and the env
block that already holds ``FIREBASE_PROJECT_ID`` — and the failure mode when
those drift is a token minted for one project being verified against another:
an opaque 401 with nothing in either log naming the mismatch. Serving them means
one copy, and changing Firebase projects is an env var rather than a 15-minute
image rebuild.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

CONSOLE_MOUNT_PATH = "/console"
CONSOLE_CONFIG_PATH = "/api/v1/console/config"
CONSOLE_DIR_ENV = "CONSOLE_STATIC_DIR"
# Relative to the repository root, which is three levels above this file.
DEFAULT_CONSOLE_DIR = Path(__file__).resolve().parents[2] / "Frontend" / "console" / "out"


def resolve_console_dir() -> Path:
    configured = os.getenv(CONSOLE_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CONSOLE_DIR


def mount_console(
    app: Any,
    logger: logging.Logger,
    *,
    directory: Optional[Path] = None,
) -> bool:
    """True when the export was found and mounted at ``/console``."""
    target = Path(directory) if directory is not None else resolve_console_dir()
    if not target.is_dir():
        logger.info(
            "Console static build not found at '%s'; /console will 404. "
            "Build it with `pnpm --dir Frontend/console build`.", target,
        )
        return False
    try:
        from fastapi.staticfiles import StaticFiles

        # html=True serves index.html for the directory itself, which is what
        # the exported single-page console needs.
        app.mount(
            CONSOLE_MOUNT_PATH,
            StaticFiles(directory=str(target), html=True),
            name="console",
        )
    except Exception:  # noqa: BLE001 - a missing console must never break boot
        logger.warning("Console static mount failed for '%s'", target,
                       exc_info=True)
        return False
    logger.info("Console served from '%s' at %s", target, CONSOLE_MOUNT_PATH)
    return True


def create_console_config_router(
    *, settings: Any, logger: logging.Logger,
) -> APIRouter:
    """Public endpoint returning Firebase's client config, and nothing else."""
    router = APIRouter()

    def _value(name: str) -> str:
        return str(getattr(settings, name, "") or "").strip()

    @router.get(CONSOLE_CONFIG_PATH)
    async def console_config() -> Any:
        project_id = _value("firebase_project_id")
        api_key = _value("firebase_api_key")
        if not project_id or not api_key:
            # Not an error: the console renders "not configured yet" instead of
            # a blank page whose only explanation is in the browser console.
            return {"configured": False, "firebase": None}
        # Built key by key rather than dumped from settings, so a field added to
        # the settings object later cannot silently become public.
        return {
            "configured": True,
            "firebase": {
                "projectId": project_id,
                "apiKey": api_key,
                # Firebase's own default; saves configuring a fifth value for
                # every project that has not set a custom auth domain.
                "authDomain": _value("firebase_auth_domain")
                or f"{project_id}.firebaseapp.com",
                "appId": _value("firebase_app_id"),
                "messagingSenderId": _value("firebase_messaging_sender_id"),
            },
        }

    return router


__all__ = [
    "CONSOLE_CONFIG_PATH",
    "CONSOLE_DIR_ENV",
    "CONSOLE_MOUNT_PATH",
    "DEFAULT_CONSOLE_DIR",
    "create_console_config_router",
    "mount_console",
    "resolve_console_dir",
]
