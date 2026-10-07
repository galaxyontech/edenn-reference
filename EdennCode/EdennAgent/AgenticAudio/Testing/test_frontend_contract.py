"""Static contract checks on the console front-end.

These run in CI with no browser and no server: they catch the class of defect
that is invisible in review and expensive in production — an asset that ships
but is not served, a stylesheet that references a token nobody defines, a
handler bound to an element id that no longer exists, an upstream provider name
leaking into something a user can read.

The behavioural coverage lives in ``Testing/frontend`` (a real browser drives
the real console). This file is the cheap gate that never flakes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
JS_DIR = FRONTEND / "js"
INDEX = FRONTEND / "index.html"
STYLES = FRONTEND / "styles.css"


#: Third-party bundles. These are built elsewhere, minified, and they define
#: their own custom properties as inline styles at RUNTIME — so the contracts
#: below, which are about the code we write, cannot say anything true about
#: them. Scanning them produces false positives, and a false positive in a
#: contract test is worse than no test: it teaches people to edit the
#: assertion rather than read it.
VENDORED_BUNDLES = {"ai-elements.js"}


def _js_files(*, include_vendored: bool = False) -> list[Path]:
    files = sorted(JS_DIR.glob("*.js"))
    if include_vendored:
        return files
    return [path for path in files if path.name not in VENDORED_BUNDLES]


def _all_frontend_files() -> list[Path]:
    return [INDEX, STYLES, FRONTEND / "mock-backend.js", *_js_files()]


# ---------------------------------------------------------------------------#
# assets: what the page loads must be what the server is willing to serve      #
# ---------------------------------------------------------------------------#


def test_every_local_asset_in_index_is_served_and_exists() -> None:
    from EdennCode.EdennAgent.AgenticAudio.api.router import FRONTEND_ASSETS

    html = INDEX.read_text()
    refs = re.findall(r'(?:src|href)="\./([^"?]+)', html)
    assert refs, "index.html references no local assets — the parser is wrong"

    not_served = sorted({r for r in refs if r not in FRONTEND_ASSETS})
    assert not not_served, (
        "index.html loads assets the production mount will 404: "
        f"{not_served} (add them to FRONTEND_ASSETS in api/router.py)"
    )

    missing = sorted({r for r in refs if not (FRONTEND / r).is_file()})
    assert not missing, f"index.html references files that do not exist: {missing}"


def test_allowlisted_assets_all_exist_on_disk() -> None:
    """The allow-list must not name a file that was renamed or deleted."""
    from EdennCode.EdennAgent.AgenticAudio.api.router import FRONTEND_ASSETS

    missing = sorted(a for a in FRONTEND_ASSETS if not (FRONTEND / a).is_file())
    assert not missing, f"FRONTEND_ASSETS names files that do not exist: {missing}"


def test_cache_busting_version_is_uniform() -> None:
    """One version per deploy.

    Mixed ``?v=`` values ship a new script against a cached stylesheet, which is
    exactly how a layout arrives in production broken for returning users only.
    """
    versions = set(re.findall(r'(?:src|href)="\./[^"]+\?v=([^"]+)"', INDEX.read_text()))
    assert len(versions) <= 1, f"asset versions disagree: {sorted(versions)}"


def test_every_script_is_loaded_by_the_page() -> None:
    """A module in js/ that no page loads is dead weight (or a missed wiring)."""
    html = INDEX.read_text()
    orphans = [p.name for p in _js_files() if f"js/{p.name}" not in html]
    assert not orphans, f"js/ modules never loaded by index.html: {orphans}"


# ---------------------------------------------------------------------------#
# wiring: handlers must bind to elements that exist                            #
# ---------------------------------------------------------------------------#

# Ids the app creates at runtime rather than declaring in the page.
_RUNTIME_IDS = {
    "tl-ph",        # timeline playhead (timeline-mode.js)
    "cv-vp", "cv-world", "cv-chat",  # canvas viewport/world/chat (canvas-mode.js)
}


def test_element_ids_referenced_by_js_exist_in_the_page() -> None:
    """`$("foo")` must find something.

    Every one of these is a silently dead control if the id drifts: the lookup
    returns null and the handler is simply never bound — no error, no symptom
    until a user clicks and nothing happens.
    """
    declared = set(re.findall(r'\bid="([^"]+)"', INDEX.read_text())) | _RUNTIME_IDS
    # Ids created by JS itself count as declared (a module may build its own DOM).
    for js in _js_files() + [FRONTEND / "mock-backend.js"]:
        src = js.read_text()
        declared |= set(re.findall(r'\.id\s*=\s*"([^"]+)"', src))
        declared |= set(re.findall(r'id="([^"]+)"', src))

    missing: dict[str, set[str]] = {}
    for js in _js_files():
        src = js.read_text()
        used = set(re.findall(r'getElementById\("([^"]+)"\)', src))
        used |= set(re.findall(r'(?<![\w.])\$\("([^"]+)"\)', src))
        gone = used - declared
        if gone:
            missing[js.name] = gone

    assert not missing, f"JS binds to element ids that do not exist: {missing}"


def test_right_pane_modules_expose_what_the_shell_calls() -> None:
    """The shell calls into the views by name; the contract must hold both ways."""
    app = (JS_DIR / "app.js").read_text()
    timeline = (JS_DIR / "timeline-mode.js").read_text()
    transform = (JS_DIR / "transform-mode.js").read_text()

    exported = re.search(r"window\.EdennTimeline\s*=\s*\{([^}]*)\}", timeline)
    assert exported, "timeline-mode.js exports no public API"
    names = {n.strip() for n in exported.group(1).split(",") if n.strip()}
    for fn in ("render", "setView", "pause"):
        assert fn in names, f"timeline-mode.js must expose {fn}(): exports {sorted(names)}"

    # render() is reached through the shell's guarded helper so one bad lane
    # cannot abort the whole reconcile; the others are called directly.
    assert 'renderView("EdennTimeline"' in app or "EdennTimeline.render" in app, (
        "app.js never renders the timeline"
    )
    for fn in ("setView", "pause"):
        assert f"EdennTimeline.{fn}" in app, f"app.js never calls EdennTimeline.{fn}()"

    # The cut-shorts flow takes the right pane over; without the inverse, one cut
    # session leaves every later session without a view toggle.
    assert "release" in transform and "releaseRightPane" in transform, (
        "transform-mode.js must expose release() to hand the right pane back"
    )
    assert "EdennTransform.release" in app, (
        "app.js never releases the right pane when leaving a session"
    )


def test_view_toggle_values_are_all_handled() -> None:
    """Every data-view the page offers must be a view the shell understands."""
    offered = set(re.findall(r'data-view="([^"]+)"', INDEX.read_text()))
    app = (JS_DIR / "app.js").read_text()
    assert offered, "the page offers no views"
    unhandled = {v for v in offered if f'"{v}"' not in app}
    assert not unhandled, f"data-view values the shell never handles: {unhandled}"


# ---------------------------------------------------------------------------#
# styling: no reference to a token nobody defines                              #
# ---------------------------------------------------------------------------#


def test_the_vendored_bundles_are_still_where_we_think_they_are() -> None:
    """The exemption above is only honest while the files it names exist. A
    bundle that gets renamed would quietly re-enter the scans, and one that is
    deleted would leave a permanent excuse behind."""
    present = {path.name for path in JS_DIR.glob("*.js")}
    missing = VENDORED_BUNDLES - present
    assert not missing, f"exempted bundles that no longer exist: {sorted(missing)}"


def test_every_css_variable_used_is_defined() -> None:
    css = STYLES.read_text()
    defined = set(re.findall(r'(--[\w-]+)\s*:', css))
    # Only the no-fallback form can resolve to nothing: `var(--x, #ccc)` is
    # always safe, so flagging it would just train people to ignore this test.
    bare = r'var\(\s*(--[\w-]+)\s*\)'
    used = set(re.findall(bare, css))
    for js in _js_files():
        used |= set(re.findall(bare, js.read_text()))
    # Tokens the JS sets itself at runtime.
    defined |= {"--chat-w"}

    undefined = sorted(used - defined)
    assert not undefined, f"CSS variables used but never defined: {undefined}"


def test_lane_colours_exist_for_every_lane() -> None:
    """The three layers are colour-coded across timeline, canvas and cards."""
    css = STYLES.read_text()
    for lane in ("music", "voiceover", "sfx"):
        assert f".lane-{lane}" in css, f"no colour rule for the {lane} lane"


# ---------------------------------------------------------------------------#
# the house rule: never name an upstream provider where a user can see it      #
# ---------------------------------------------------------------------------#

_PROVIDER_NAMES = [
    "model_gateway", "chat-advanced", "chat-standard", "model_vendor_alt", "model_gateway_alt", "azure",
    "provider_a", "provider_a", "provider_c", "provider_b", "stability", "runway",
    "speech_recognition", "cognitiveservices",
]


def test_no_upstream_provider_names_in_the_frontend() -> None:
    """A vendor name in the console is a leak, whatever the surface.

    The backend scrubs provider names out of API payloads; the front-end must
    not reintroduce them in its own copy, identifiers, or comments.
    """
    hits: list[str] = []
    for path in _all_frontend_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            low = line.lower()
            for name in _PROVIDER_NAMES:
                if name in low:
                    hits.append(f"{path.name}:{i}: {line.strip()[:100]}")
                    break
    assert not hits, "upstream provider names in the front-end:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------#
# capability honesty: controls the backend cannot honour must not be offered   #
# ---------------------------------------------------------------------------#


def test_narration_start_slider_is_hidden_for_a_segmented_plan() -> None:
    """A segmented narration bakes its own timing.

    ``compose_mix`` forces ``voiceover_start_s = 0`` whenever the voice-over has
    segments (tools/impls.py), so a global start slider in that state is a
    control that silently does nothing.
    """
    app = (JS_DIR / "app.js").read_text()
    idx = app.find("voiceover_start_s")
    assert idx > 0, "the narration-start slider disappeared — update this test"
    window = app[max(0, idx - 600):idx]
    assert "segments" in window, (
        "the narration-start slider is no longer gated on the absence of a "
        "segment plan; the backend ignores it for segmented narration"
    )


def test_timeline_never_fabricates_a_clock() -> None:
    """With no known duration the view must not draw a time axis.

    The whole premise of the timeline is that an interval is measured or absent;
    a `|| 1` fallback silently renders a one-second video.
    """
    src = (JS_DIR / "timeline-mode.js").read_text()
    assert "if (duration) {" in src, (
        "the tick row is no longer gated on a real duration"
    )
    assert "buildLanes(st, duration)" in src, (
        "buildLanes is being passed a fabricated duration again"
    )


@pytest.mark.parametrize("needle", [
    "Intervals are measured from the audio itself",
    "Waiting on the video",
])
def test_timeline_states_its_own_honesty_to_the_user(needle: str) -> None:
    assert needle in (JS_DIR / "timeline-mode.js").read_text(), (
        f"the timeline no longer tells the user {needle!r}"
    )


# ---------------------------------------------------------------------------#
# the offline mock must not be a second product surface in production          #
# ---------------------------------------------------------------------------#


def test_the_mock_backend_is_not_served_on_a_deployed_console(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """`?backend=mock` fabricates takes and answers as the director.

    On a deployed console that is an unpoliced second product, so the script is
    not served there at all — detected from the deployment's own shape rather
    than a flag somebody has to remember to set.
    """
    from EdennCode.EdennAgent.AgenticAudio.api.router import _mock_backend_allowed

    monkeypatch.delenv("AGENTIC_AUDIO_ALLOW_MOCK", raising=False)
    for marker in ("CONTAINER_APP_NAME", "WEBSITE_HOSTNAME", "EDENN_PUBLIC_BASE_URL"):
        monkeypatch.delenv(marker, raising=False)
    assert _mock_backend_allowed() is True, "a local run keeps the offline mock"

    monkeypatch.setenv("CONTAINER_APP_NAME", "studio-standalone-app")
    assert _mock_backend_allowed() is False, "a deployed console must not serve it"

    monkeypatch.setenv("AGENTIC_AUDIO_ALLOW_MOCK", "1")
    assert _mock_backend_allowed() is True, "a demo can still ask for it explicitly"


def test_the_console_falls_back_when_the_mock_is_absent() -> None:
    """Absent the script, ?backend=mock must not boot a half-app whose every
    control silently does nothing."""
    app = (JS_DIR / "app.js").read_text()
    assert "if (!window.MockBackend)" in app, (
        "the transport picker no longer checks whether the mock actually loaded"
    )


def test_media_elements_carry_the_page_token() -> None:
    """Media elements cannot send an Authorization header, so every same-origin
    media src must go through the mediaSrc() helper — a raw assignment plays
    fine in the tokenless mock and 401s on every authenticated deployment,
    which is exactly the hero-video-won't-load bug shipped on 2026-08-27."""
    import re

    js_dir = FRONTEND / "js"
    offenders: list[str] = []
    console_files = ["app.js", "timeline-mode.js", "canvas-mode.js", "collab-mode.js"]
    raw_src = re.compile(r"\.(src|poster)\s*=\s*(?!mediaSrc\()[a-zA-Z(]")
    for name in console_files:
        text = (js_dir / name).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if "a.src = mediaSrc" in line or "__edennMediaSrc" in line:
                continue
            m = raw_src.search(line)
            if not m:
                continue
            # location.href / audio.src reads, and data-url assignments, are fine.
            if re.search(r"\.(src|poster)\s*=\s*(mediaSrc|window\.__edennMediaSrc)", line):
                continue
            if "location" in line or "URL.createObjectURL" in line or "data:" in line:
                continue
            offenders.append(f"{name}:{i}: {stripped[:90]}")
        assert "__edennMediaSrc" in text or name == "app.js", name
    assert "window.__edennMediaSrc = mediaSrc" in (js_dir / "app.js").read_text(encoding="utf-8")
    assert not offenders, (
        "media src assignments bypassing mediaSrc():\n  " + "\n  ".join(offenders)
    )


def test_new_audio_goes_through_the_token_helper_in_console_files() -> None:
    import re

    js_dir = FRONTEND / "js"
    offenders: list[str] = []
    for name in ["app.js", "timeline-mode.js", "canvas-mode.js", "collab-mode.js"]:
        text = (js_dir / name).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if "new Audio(" in line and "mediaSrc" not in line and "__edennMediaSrc" not in line:
                offenders.append(f"{name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "Audio() constructed without the token helper:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------#
# the gallery shows what the customer made                                     #
# ---------------------------------------------------------------------------#


def test_the_gallery_renders_real_deliverables_not_invented_ones() -> None:
    """It used to render six hardcoded titles whose Preview played a 12-second
    sine-wave arpeggio synthesized in the customer's own browser, complete with
    a BPM and an instrument list — a product that sells generated music showing
    a tone generator as if it were its own work, on the page whose entire
    promise is "what you made"."""
    library = (JS_DIR / "library-ui.js").read_text()

    gallery = library.split('if (page === "gallery") rows.forEach')[1].split("else {")[0]
    assert "final_media_url" in gallery, "the gallery does not read the deliverable"
    assert "sketch(" not in gallery, "the gallery is synthesizing audio again"
    assert "item.bpm" not in gallery, "the gallery is describing invented music"


def test_the_gallery_asks_the_backend_what_exists() -> None:
    """Its old branch never called the backend at all, which is how it came to
    show work nobody had made."""
    library = (JS_DIR / "library-ui.js").read_text()

    assert 'if (page === "sessions") load(); else' not in library
    assert "items.filter(s => s.final_media_url" in library


def test_a_synthesized_preview_never_claims_to_be_the_product() -> None:
    """The example directions are a fine way to start a session. What they must
    never do is present a browser-generated tone as Edenn's output."""
    library = (JS_DIR / "library-ui.js").read_text()

    if "sketch(" in library:
        assert "not generated music" in library, (
            "a synthesized preview exists with nothing saying what it is"
        )


def test_the_list_the_gallery_reads_is_the_list_the_backend_sends() -> None:
    """Mock parity for the one field the gallery is built on."""
    mock = (FRONTEND / "mock-backend.js").read_text()
    assert "final_media_url" in mock


def test_every_session_phase_has_a_name_the_list_can_show() -> None:
    """The status column used to match on words that are not in the enum and
    miss most of the ones that are, so a finished session — the one a customer
    comes back for — showed an em dash."""
    from EdennCode.EdennAgent.AgenticAudio.models import AgenticAudioSessionPhase

    library = (JS_DIR / "library-ui.js").read_text()
    block = library.split("const SESSION_PHASES = {")[1].split("};")[0]

    # A plain constants class, so the members are read off it directly — which
    # also means a new phase can be added with nothing noticing, and this is
    # what notices.
    phases = [
        value for name, value in vars(AgenticAudioSessionPhase).items()
        if name.isupper() and isinstance(value, str)
    ]
    assert phases, "the phase vocabulary could not be read"
    missing = [value for value in phases if f"{value}:" not in block]
    assert not missing, f"phases the session list cannot name: {missing}"


def test_the_status_column_is_not_a_regex_over_invented_words() -> None:
    library = (JS_DIR / "library-ui.js").read_text()
    assert "/generating|rendering|analyzing/" not in library


def test_the_access_token_does_not_stay_in_the_address_bar() -> None:
    """It has to arrive on the URL — a browser cannot put an Authorization
    header on a WebSocket handshake or a <video> src. It does not have to STAY
    there: the address bar is copied into chat messages, screenshots and
    browser history, and each of those hands over the credential with the
    link."""
    app = (JS_DIR / "app.js").read_text()

    assert "window.__edennToken" in app, "the token is not held in memory"
    assert 'fromUrl.delete("token")' in app, "the token is never removed from the URL"
    assert "history.replaceState" in app
    # ...and nothing reads it back off the URL afterwards, which would defeat it.
    assert app.count('.get("token")') == 1, "the token is still being read from the URL"


def test_an_upload_that_cannot_be_accepted_is_refused_before_it_is_sent() -> None:
    """Uploading is the first thing this product asks of someone, and it was a
    blind multi-minute wait that could end in a bare status code — on a phone,
    their data too. The limits checked here are the backend's own, so the
    message is the one they would have got afterwards rather than a second
    opinion."""
    app = (JS_DIR / "app.js").read_text()

    assert "UPLOAD_MAX_BYTES = 300 * 1024 * 1024" in app
    assert "UPLOAD_MAX_SECONDS = 150" in app
    assert "UPLOAD_MIN_SECONDS = 15" in app
    assert "async function checkUploadable(file)" in app


def test_an_upload_reports_progress_and_can_be_abandoned() -> None:
    """fetch cannot do either, which is why this one uses XHR."""
    app = (JS_DIR / "app.js").read_text()

    assert "xhr.upload.onprogress" in app
    assert "xhr.abort()" in app
    # Cancelling is the customer's decision, not a failure to apologise for.
    assert "err.aborted" in app
    assert '"cancelled"' in app


def test_the_offline_console_enforces_the_same_upload_limits() -> None:
    """A limit the mock does not enforce is a limit the customer meets for the
    first time in production."""
    app = (JS_DIR / "app.js").read_text()
    mock_upload = app.split("async uploadVideo(file, onProgress) {")[1][:400]
    assert "checkUploadable" in mock_upload


def test_the_console_asks_the_server_where_sign_in_lives() -> None:
    """A hard-coded path is a deployment assumption in the client. This one was
    the platform console's, and on the standalone host it was a 404 behind the
    only button a signed-out visitor has."""
    app = (JS_DIR / "app.js").read_text()

    signin = app.split("function showSignIn(")[1].split("\n  function ")[0]
    assert "cfg.signin_url" in signin, (
        "the console still decides for itself where sign-in is"
    )
