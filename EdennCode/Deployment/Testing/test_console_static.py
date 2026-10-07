"""Serving the console from the API container (the V2 deployment)."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.auth.middleware import (
    create_auth_middleware,
    is_exempt_path,
)
from EdennCode.Deployment.console_static import CONSOLE_MOUNT_PATH, mount_console


def _built_console(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "index.html").write_text("<!doctype html><title>Edenn 控制台</title>",
                                    encoding="utf-8")
    assets = out / "_next"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('edenn')", encoding="utf-8")
    return out


class TestMounting:
    def test_missing_build_is_not_an_error(self, tmp_path):
        # A backend deploy must not depend on someone having run `next build`.
        app = FastAPI()
        assert mount_console(app, logging.getLogger("t"),
                             directory=tmp_path / "nope") is False
        assert TestClient(app).get("/console/").status_code == 404

    def test_serves_the_export(self, tmp_path):
        app = FastAPI()
        assert mount_console(app, logging.getLogger("t"),
                             directory=_built_console(tmp_path)) is True
        client = TestClient(app)
        page = client.get("/console/")
        assert page.status_code == 200
        assert "Edenn" in page.text
        assert client.get("/console/_next/app.js").status_code == 200

    def test_a_file_instead_of_a_directory_is_ignored(self, tmp_path):
        stray = tmp_path / "out"
        stray.write_text("not a build", encoding="utf-8")
        app = FastAPI()
        assert mount_console(app, logging.getLogger("t"), directory=stray) is False


class TestReachability:
    def test_console_paths_skip_api_key_enforcement(self):
        """The page has no key yet — that is the whole reason it exists."""
        assert is_exempt_path(CONSOLE_MOUNT_PATH) is True
        assert is_exempt_path(f"{CONSOLE_MOUNT_PATH}/") is True
        assert is_exempt_path(f"{CONSOLE_MOUNT_PATH}/_next/app.js") is True

    def test_api_paths_are_still_enforced(self):
        assert is_exempt_path("/api/v1/jobs/video") is False
        assert is_exempt_path("/api/v1/account/keys") is False

    def test_the_page_loads_under_enforce(self, tmp_path):
        app = FastAPI()
        app.middleware("http")(create_auth_middleware(
            mode="enforce", key_store=None, logger=logging.getLogger("t")))
        mount_console(app, logging.getLogger("t"),
                      directory=_built_console(tmp_path))
        assert TestClient(app).get("/console/").status_code == 200
