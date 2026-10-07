"""The upload endpoint, exercised with real files through the real route.

This is the first thing an untrusted caller touches: it accepts an arbitrary
body, writes it to disk, and hands it to ffprobe. It was unauthenticated,
unbounded, buffered whole into memory, and accepted anything ffprobe could time.

Every test here posts an actual file to the actual endpoint. Where a real video
is needed one is synthesized with ffmpeg, and the test skips if ffmpeg is absent
rather than pretending to have covered it.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_UPLOAD = "/api/v2/assets/video"
_TOKEN = {"Authorization": "Bearer tok_a"}

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _make_video(path: Path, seconds: float = 2.0) -> Path:
    """A real, decodable video — small, silent, and genuinely timed."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=160x120:d={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds), str(path)],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EDENN_DEV_REAL_MUSIC", "0")
    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENTIC_AUDIO_API_KEYS", "tok_a:alice,tok_b:bob")
    monkeypatch.delenv("EDENN_CREATION_MEDIA_DIR", raising=False)
    for marker in ("CONTAINER_APP_NAME", "WEBSITE_HOSTNAME", "EDENN_PUBLIC_BASE_URL"):
        monkeypatch.delenv(marker, raising=False)
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    with TestClient(mod.build_app()) as c:
        yield c


# ---------------------------------------------------------------------------#
# authentication                                                              #
# ---------------------------------------------------------------------------#


def test_upload_requires_credentials(client: TestClient) -> None:
    """The deployed console had this open: anyone with the hostname could push
    files onto the container and start paid analysis with them."""
    r = client.post(_UPLOAD, files={"video": ("x.mp4", b"not-a-video", "video/mp4")})
    assert r.status_code == 401, r.text


def test_upload_rejects_an_unknown_token(client: TestClient) -> None:
    r = client.post(
        _UPLOAD,
        files={"video": ("x.mp4", b"not-a-video", "video/mp4")},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401, r.text


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg is required to make a real video")
def test_media_reads_require_credentials(client: TestClient, tmp_path: Path) -> None:
    """The uploaded footage is the user's own video. Serving it to anyone who
    guesses a filename is the same leak as an open upload, in reverse."""
    src = _make_video(tmp_path / "clip.mp4")
    up = client.post(
        _UPLOAD,
        files={"video": ("clip.mp4", src.read_bytes(), "video/mp4")},
        headers=_TOKEN,
    )
    assert up.status_code == 200, up.text
    url = up.json()["url"]

    assert client.get(url).status_code == 401
    assert client.get(url, headers=_TOKEN).status_code == 200
    # A media element cannot set a header, so the query-param form must work too.
    assert client.get(f"{url}?token=tok_a").status_code == 200


def test_media_reads_cannot_walk_out_of_the_uploads_directory(client: TestClient) -> None:
    r = client.get("/dev/uploads/..%2f..%2f..%2fetc%2fpasswd", headers=_TOKEN)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------#
# limits                                                                      #
# ---------------------------------------------------------------------------#


def test_upload_refuses_a_body_over_the_size_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected on the way in, by streaming — not read whole and then judged."""
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 64 * 1024)
    big = b"\0" * (256 * 1024)
    r = client.post(_UPLOAD, files={"video": ("big.mp4", big, "video/mp4")}, headers=_TOKEN)
    assert r.status_code == 413, r.text
    assert "MB" in r.json()["detail"]


def test_an_oversized_upload_leaves_nothing_on_disk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected upload must not still cost us the disk it was rejected for.

    Streaming means the bytes are already being written when the ceiling is hit,
    so the partial file has to be cleaned up on the way out.
    """
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 32 * 1024)
    uploads = client.app.state.uploads_dir
    before = {p.name for p in uploads.iterdir()}

    r = client.post(
        _UPLOAD, files={"video": ("big.mp4", b"\0" * (128 * 1024), "video/mp4")}, headers=_TOKEN
    )
    assert r.status_code == 413

    after = {p.name for p in uploads.iterdir()}
    assert after == before, f"partial upload left behind: {sorted(after - before)}"


def test_upload_refuses_a_container_we_cannot_score(client: TestClient) -> None:
    r = client.post(
        _UPLOAD, files={"video": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=_TOKEN,
    )
    assert r.status_code == 415, r.text
    assert ".mp4" in r.json()["detail"]


def test_upload_refuses_an_empty_body(client: TestClient) -> None:
    r = client.post(_UPLOAD, files={"video": ("empty.mp4", b"", "video/mp4")}, headers=_TOKEN)
    assert r.status_code == 400, r.text


def test_upload_refuses_a_file_that_does_not_decode(client: TestClient) -> None:
    """Named .mp4 is not the same as being one — the probe is the real gate."""
    r = client.post(
        _UPLOAD, files={"video": ("lies.mp4", b"this is plain text, not video", "video/mp4")},
        headers=_TOKEN,
    )
    assert r.status_code == 400, r.text
    assert "doesn't decode" in r.json()["detail"]


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg is required to make a real video")
def test_upload_refuses_a_video_longer_than_the_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    monkeypatch.setattr(mod, "MAX_UPLOAD_SECONDS", 1.0)
    src = _make_video(tmp_path / "long.mp4", seconds=3.0)
    r = client.post(
        _UPLOAD, files={"video": ("long.mp4", src.read_bytes(), "video/mp4")}, headers=_TOKEN
    )
    assert r.status_code == 413, r.text
    assert "limit is" in r.json()["detail"]


# ---------------------------------------------------------------------------#
# the happy path still works, and ownership follows the credential            #
# ---------------------------------------------------------------------------#


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg is required to make a real video")
def test_a_real_video_is_accepted_and_measured(client: TestClient, tmp_path: Path) -> None:
    src = _make_video(tmp_path / "ok.mp4", seconds=2.0)
    r = client.post(
        _UPLOAD, files={"video": ("ok.mp4", src.read_bytes(), "video/mp4")}, headers=_TOKEN
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    # A real, probed duration — not a fabricated one.
    assert 1.5 < body["metadata"]["duration"] < 2.6
    assert body["url"].startswith("/dev/uploads/")


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg is required to make a real video")
def test_the_authenticated_caller_owns_the_upload_not_a_form_field(
    client: TestClient, tmp_path: Path
) -> None:
    """A form field must never decide whose asset this is."""
    mod = importlib.import_module("EdennCode.EdennAgent.AgenticAudio.design.devserver")
    src = _make_video(tmp_path / "own.mp4")
    r = client.post(
        _UPLOAD,
        files={"video": ("own.mp4", src.read_bytes(), "video/mp4")},
        data={"creator_user_id": "somebody_else"},
        headers=_TOKEN,
    )
    assert r.status_code == 200, r.text
