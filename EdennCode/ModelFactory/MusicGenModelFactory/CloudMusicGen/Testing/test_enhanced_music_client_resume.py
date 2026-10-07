"""Regression test for the enhanced-tier music client's resume path.

``_generate_with_variants_detailed_in_cycle`` supports resuming an already-
submitted provider task (``resume_task_id``) so a retry does not pay for a
second generation. On that path only ``finished`` was assigned; the
``lyrics_meta`` / ``resolved_lyrics`` names that the default ``save_sidecar``
block references were never defined, so a resume raised ``NameError`` right
after the (already paid-for) wait + download — turning the money-saving resume
into a guaranteed dead-letter. See
``EdennCode/Deployment/PROD_READINESS_REMEDIATION_PLAN.md`` finding #20 (part A).

This drives the *real* client end to end over a real local HTTP server: the
real query poll (``GET /v1/song/query/{id}``), the real audio download, the real
provider-payload parsing, and the real sidecar write to a real temp dir. Only
the provider host is local (there are no enhanced-tier credentials locally).
Before the fix the call raises ``NameError``; after it, the resume completes and
writes its sidecar.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
)

_AUDIO_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-mp3-body"


def _task_payload(audio_url: str) -> dict:
    # Minimal provider response that passes the audio-url and timestamped-lyrics
    # checks so execution reaches the sidecar write.
    return {
        "id": "resume-123",
        "status": "succeeded",
        "trace_id": "trace-1",
        "audio_urls": [audio_url],
        "choices": [
            {
                "lyrics_sections": [
                    {
                        "lines": [
                            {
                                "text": "hello world",
                                "start": 0,
                                "end": 1500,
                                "words": [
                                    {"text": "hello", "start": 0, "end": 700},
                                    {"text": "world", "start": 700, "end": 1500},
                                ],
                            }
                        ]
                    }
                ]
            }
        ],
    }


class _ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:
        pass

    def _send_json(self, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        port = self.server.server_address[1]
        if self.path.startswith("/v1/account/billing"):
            # The client credit-checks a key before using it; keep it well above
            # the threshold so the key is considered healthy.
            self._send_json({"balance": 1_000_000})
            return
        if self.path.startswith("/v1/song/query/"):
            audio_url = f"http://127.0.0.1:{port}/audio/track.mp3"
            body = json.dumps(_task_payload(audio_url)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/audio/"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(_AUDIO_BYTES)))
            self.end_headers()
            self.wfile.write(_AUDIO_BYTES)
            return
        self.send_response(404)
        self.end_headers()


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_resume_path_completes_and_writes_sidecar(tmp_path: Path) -> None:
    server = _start_server()
    try:
        port = server.server_address[1]
        out = tmp_path / "track.mp3"

        async def _run():
            client = ProviderBMusicProvider(
                api_key="test-key",
                base_url=f"http://127.0.0.1:{port}",
                timeout=5,
                max_retries=0,
                default_output_dir=tmp_path,
            )
            # save_sidecar defaults to True — this is exactly the branch that
            # NameError'd on resume.
            return await client._generate_with_variants_detailed_in_cycle(
                "a prompt",
                n=1,
                timeout_s=10.0,
                poll_s=0.1,
                output_path=out,
                resume_task_id="resume-123",
            )

        final_path, secondary_path, primary_ts, secondary_ts = asyncio.run(
            asyncio.wait_for(_run(), timeout=15.0)
        )

        # Reaching a return value proves the sidecar block (which references the
        # previously-undefined names) executed without NameError.
        assert Path(final_path).exists()
        assert Path(final_path).read_bytes() == _AUDIO_BYTES
        assert secondary_path is None  # n=1 -> single track
        assert primary_ts.word_level  # timestamps parsed from the real payload
        # A sidecar JSON was written next to the track.
        sidecars = list(tmp_path.glob("*.json"))
        assert sidecars, "expected a sidecar file to be written on the resume path"
    finally:
        server.shutdown()
