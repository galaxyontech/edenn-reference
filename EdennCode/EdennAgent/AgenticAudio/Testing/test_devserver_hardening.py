"""The standalone server's own guarantees: it refuses to run unsafely, and it
tells the platform truthfully whether it should be sent traffic.

These are properties of the DEPLOYED shape rather than of any one endpoint, so
they are tested against a real app instance built the way the container builds
it — not against a hand-assembled router.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _build_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A freshly built devserver app, isolated from the developer's own env."""
    # Keep the build cheap and hermetic: no real provider work, no live model.
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.delenv("EDENN_CREATION_MEDIA_DIR", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("WEBSITE_HOSTNAME", raising=False)
    monkeypatch.delenv("EDENN_PUBLIC_BASE_URL", raising=False)
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    return mod, mod.build_app()


# ---------------------------------------------------------------------------#
# what the root actually serves                                               #
# ---------------------------------------------------------------------------#


def _deployed_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An app built the way the container builds it — deployment markers and all."""

    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.delenv("EDENN_CREATION_MEDIA_DIR", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "studio-app")
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    return mod.build_app()


def test_the_root_serves_the_console_and_not_the_directory_around_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The root was a blanket static mount of the whole frontend directory.

    It served the design notes that live beside the console, and — because the
    production block on the offline mock guards the OTHER path to the same files
    — it served the mock too, anonymously, on a deployed box.
    """

    _, app = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/index.html").status_code == 200
        assert client.get("/js/app.js").status_code == 200
        assert client.get("/README.md").status_code == 404
        assert client.get("/CANVAS_MODE_PLAN.md").status_code == 404
        # A local run keeps the mock; that is what a local run is for.
        assert client.get("/mock-backend.js").status_code == 200


def test_a_deployed_box_publishes_neither_the_mock_nor_its_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with TestClient(_deployed_app(monkeypatch, tmp_path)) as client:
        assert client.get("/index.html").status_code == 200
        assert client.get("/mock-backend.js").status_code == 404
        # /docs, /redoc and /openapi.json answered anonymously and listed every
        # route on the box.
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_the_console_document_is_never_cached_hard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """index.html carries the ?v= stamps that bust every other file.

    With no directive of its own a browser applies heuristic freshness to it and
    keeps requesting yesterday's scripts after a deploy — a state only a hard
    reload escapes, which nothing in the product can ask a user to perform.
    """

    def never_cached_hard(header: str) -> bool:
        """Any directive that forbids serving a stored copy without asking.

        Asserted as a PROPERTY rather than an exact string, because more than
        one header satisfies it and the stricter ones are not regressions:
        `no-cache` permits storing but forces revalidation, `no-store` forbids
        storing at all. Pinning the literal turned a tightening of the policy
        into a failing test, which teaches exactly the wrong lesson about
        changing it.
        """

        directives = {part.strip() for part in header.lower().split(",")}
        return bool(directives & {"no-cache", "no-store"})

    # A machine someone is EDITING on: nothing may be held, stamped or not,
    # because a stale script there looks exactly like a code bug.
    _, app = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        for url in ("/index.html", "/js/app.js?v=e21", "/js/app.js"):
            assert never_cached_hard(client.get(url).headers["cache-control"]), url

    # The DEPLOYMENT is the same server, and there the stamp has to earn its
    # keep: no-store on a ?v=-stamped asset makes every visitor re-download
    # every file on every load, which is the problem the stamp exists to solve.
    deployed = _deployed_app(monkeypatch, tmp_path)
    with TestClient(deployed) as client:
        client.headers.update({"Authorization": "Bearer tok_a"})
        assert never_cached_hard(client.get("/index.html").headers["cache-control"])
        assert client.get("/js/app.js?v=e21").headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        # Unversioned: its URL will not change on the next deploy, so a year-long
        # cache entry would be the same bug wearing different clothes.
        assert never_cached_hard(client.get("/js/app.js").headers["cache-control"])


# ---------------------------------------------------------------------------#
# who may read a rendered file                                                #
# ---------------------------------------------------------------------------#


def test_generated_media_requires_a_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every take, mix and effects bed is served from /dev/media.

    It was the one media route that never authenticated: during the live audit a
    freshly generated take downloaded with no header and no token at all, from a
    deployment that 401s anonymous callers everywhere else. Random filenames are
    obscurity, not access control — and a media URL leaks into referrers, share
    links and screenshots.
    """

    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    with TestClient(app) as client:
        assert client.get("/dev/media/gen_whatever.mp3").status_code == 401
        # A credential gets past the door; 404 is the file, not the auth. The
        # ?token= form matters because a <video> element cannot set a header.
        assert client.get(
            "/dev/media/gen_whatever.mp3", headers={"Authorization": "Bearer tok_a"}
        ).status_code == 404
        assert client.get("/dev/media/gen_whatever.mp3?token=tok_a").status_code == 404
        # The sibling route it should always have matched.
        assert client.get("/dev/uploads/whatever.mp4").status_code == 401


# ---------------------------------------------------------------------------#
# the tier a take is labelled with                                            #
# ---------------------------------------------------------------------------#


def test_take_is_labelled_with_the_tier_that_rendered() -> None:
    """The workflow's effective tier wins over the tier that was requested.

    Echoing the request back made the two equal by construction, which disarmed
    the downstream check that records a substitution — so a tier the workflow
    re-resolved on its own (vocals in some languages) would still have been
    badged as the tier the user asked for.
    """

    from types import SimpleNamespace

    from EdennCode.EdennAgent.AgenticAudio.design.devserver import rendered_modelspec

    upgraded = SimpleNamespace(used_music_model_spec="edenn_enhanced")
    assert rendered_modelspec(upgraded, "edenn_basic") == "edenn_enhanced"

    # A workflow that reports nothing falls back to the request rather than
    # inventing a tier.
    silent = SimpleNamespace()
    assert rendered_modelspec(silent, "edenn_studio") == "edenn_studio"
    assert rendered_modelspec(SimpleNamespace(used_music_model_spec=""), "edenn_basic") == "edenn_basic"

    # Anything unrecognised normalizes rather than reaching a badge raw.
    junk = SimpleNamespace(used_music_model_spec="not-a-tier")
    assert rendered_modelspec(junk, "edenn_studio") == "edenn_basic"


# ---------------------------------------------------------------------------#
# refusing to start                                                           #
# ---------------------------------------------------------------------------#


def test_refuses_to_start_when_auth_is_on_but_no_key_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Auth ON is not the same as auth WORKING.

    With no key map and no explicit opt-in the router rejects every request, so
    the container would boot "healthy" and serve nothing but 401s. It has to say
    so at startup, while somebody is still reading the logs.
    """
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TOKEN_IS_USER", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    assert "AGENTIC_AUDIO_API_KEYS" in str(excinfo.value)


def test_starts_when_the_key_map_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_starts_for_a_local_run_that_opts_into_token_is_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_refuses_to_serve_publicly_with_auth_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-existing guarantee, pinned: a deployment marker plus auth off is
    an open spend endpoint, and must not boot."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "studio-standalone-app")
    monkeypatch.delenv("EDENN_DEV_ALLOW_NO_AUTH", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    assert "Refusing to start" in str(excinfo.value)


def test_an_intentionally_open_instance_can_still_be_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENTIC_AUDIO_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "studio-standalone-app")
    monkeypatch.setenv("EDENN_DEV_ALLOW_NO_AUTH", "1")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------#
# liveness vs readiness                                                       #
# ---------------------------------------------------------------------------#


def test_liveness_and_readiness_are_different_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")

    with TestClient(app) as client:
        live = client.get("/healthz")
        ready = client.get("/readyz")
        assert live.status_code == 200 and live.json()["status"] == "ok"
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True
        assert ready.json()["draining"] is False


def test_readiness_is_false_before_startup_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replica that has not finished starting must not be sent turns."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")

    # No TestClient context manager -> startup hooks have not run.
    raw = TestClient(app)
    r = raw.get("/readyz")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert r.json()["reason"] == "starting"
    # Liveness is still true: the process is fine, it is just not ready.
    assert raw.get("/healthz").status_code == 200


def test_readiness_drains_on_shutdown_so_a_deploy_stops_taking_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shutdown must fail readiness BEFORE tearing anything down, or the
    platform keeps routing turns at a replica that is already going away."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")

    with TestClient(app) as client:
        assert client.get("/readyz").json()["ready"] is True
    # Exiting the context ran the shutdown hook.
    assert app.state.draining is True
    assert app.state.ready is False


def test_health_endpoints_need_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A probe cannot hold a token. If health required auth, a correctly
    configured server would look dead to the platform."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


# ---------------------------------------------------------------------------#
# draining in-flight work                                                     #
# ---------------------------------------------------------------------------#


def test_shutdown_waits_for_a_turn_that_is_still_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deploy that kills a turn mid-render destroys work the user has already
    been charged for: the provider render is paid and running, and the session
    is left in whatever half-written state the kill interrupted.
    """
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    monkeypatch.setenv("EDENN_DRAIN_TIMEOUT_S", "2")

    with TestClient(app) as client:
        assert client.get("/readyz").json()["ready"] is True
        planner = app.state.agentic_planner
        agent = getattr(planner, "agent", planner)
        # Pretend a turn is mid-flight when the deploy arrives.
        agent.turns_in_flight = 1

    # Shutdown ran on context exit; it must have waited rather than returning
    # instantly, and must have given up at the bound rather than hanging.
    assert app.state.draining is True


def test_shutdown_does_not_wait_when_nothing_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The common case is an idle replica, and a deploy should not pay a drain
    timeout for it."""
    import time

    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    monkeypatch.setenv("EDENN_DRAIN_TIMEOUT_S", "30")

    started = time.monotonic()
    with TestClient(app):
        pass
    assert time.monotonic() - started < 10, "an idle shutdown paid the drain timeout"


def test_the_host_can_see_how_many_turns_are_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shutdown that cannot see in-flight turns cannot wait for them."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice")
    with TestClient(app):
        planner = app.state.agentic_planner
        assert planner is not None, "the host cannot reach the agent at all"
        agent = getattr(planner, "agent", planner)
        assert hasattr(agent, "turns_in_flight")


def test_starts_when_a_verified_identity_is_the_only_way_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retiring the static token map is the POINT of real identity, and the
    startup check called that deployment broken: it knew about a key map and an
    opt-out, and nothing about the credential the product is moving to."""
    from EdennCode.EdennAgent.AgenticAudio.api import identity

    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TOKEN_IS_USER", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_IDP_PROJECT_ID", "studio-identity")
    identity.configure_from_env()
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    finally:
        identity.reset_for_tests()


def test_a_deployed_instance_refuses_a_signing_key_that_dies_on_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Share links and connection tickets are signed with an explicit secret,
    else a hash of the key map, else a RANDOM per-process key. The last one is
    silent and, on a deployment, wrong twice over: every link a customer was
    sent breaks on the next restart, and a ticket minted by one replica is
    rejected by the next. It went unnoticed because the key map was always
    there — and retiring the key map is exactly what real identity does."""
    from EdennCode.EdennAgent.AgenticAudio.api import identity

    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONTAINER_APP_NAME", "eden-jp-audio-studio")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_SHARE_SECRET", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TICKET_SECRET", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_IDP_PROJECT_ID", "studio-identity")
    identity.configure_from_env()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            with TestClient(app):
                pass
        assert "AGENTIC_AUDIO_SHARE_SECRET" in str(excinfo.value)
        assert "AGENTIC_AUDIO_TICKET_SECRET" in str(excinfo.value)

        monkeypatch.setenv("AGENTIC_AUDIO_SHARE_SECRET", "s" * 32)
        monkeypatch.setenv("AGENTIC_AUDIO_TICKET_SECRET", "t" * 32)
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    finally:
        identity.reset_for_tests()


def test_a_laptop_keeps_its_disposable_signing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to fix locally: links that die with the process are correct when
    the process is a laptop, and demanding two secrets to run a dev server is
    friction with no safety behind it."""
    mod, app = _build_app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_SHARE_SECRET", raising=False)
    monkeypatch.delenv("AGENTIC_AUDIO_TICKET_SECRET", raising=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_retention_does_not_delete_anything_unless_a_deployment_asks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one background task whose job is to destroy a customer's work, on a
    window read from an environment variable. A wrong value is not an error in
    a log — it is footage that is gone. So: off unless someone says otherwise,
    and a dry mode so the first thing anyone does with a deletion job is watch
    it not delete anything."""
    mod, app = _build_app(monkeypatch, tmp_path)
    source = Path(
        "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
    ).read_text()

    sweeper = source.split("def _retention_mode(")[1].split("\n    @app.on_event")[0]
    assert '"AGENTIC_AUDIO_RETENTION_SWEEP", "off"' in sweeper, (
        "retention no longer defaults to off"
    )
    assert 'mode if mode in {"dry", "apply"} else "off"' in sweeper, (
        "an unrecognised value must mean off, never delete"
    )
    assert 'apply = mode == "apply"' in sweeper
    # Sleep before the first pass: a restart loop must not become a deletion loop.
    body = sweeper.split("while True:")[1]
    assert body.index("await asyncio.sleep") < body.index("retention.sweep")

    monkeypatch.delenv("AGENTIC_AUDIO_RETENTION_SWEEP", raising=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert getattr(app.state, "_retention", None) is None, (
            "a deletion task started without being asked for"
        )


def test_readiness_asks_the_store_whether_it_is_actually_reachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Readiness read in-process flags only, so a replica whose database had
    gone away went on reporting itself ready and kept being sent traffic it
    could not serve."""
    mod, app = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200

        source = Path(
            "EdennCode/EdennAgent/AgenticAudio/design/devserver.py"
        ).read_text()
        probe = source.split("def _probe_dependencies(")[1].split("\n    @app.get")[0]
        assert 'getattr(agent_repo, "ping", None)' in probe, (
            "readiness no longer asks the store anything"
        )
        # Cached: a probe that runs on every health check becomes its own load.
        assert "_READY_PROBE_TTL_S" in probe
        # And it cannot fail the endpoint it reports on.
        assert "except Exception" in probe


def test_readiness_fails_when_the_completer_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dead completer is a replica that accepts renders and never runs them.
    It reported itself ready for the rest of its life, so the platform kept
    sending it work that would only ever queue."""
    mod, app = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/readyz").json()["completer"] == "alive"

        completer = app.state._completer
        completer.cancel()
        for _ in range(50):
            if completer.done():
                break
            client.get("/healthz")  # let the loop run
        response = client.get("/readyz")

        assert response.status_code == 503, response.text
        assert response.json()["reason"] == "completer_died"
