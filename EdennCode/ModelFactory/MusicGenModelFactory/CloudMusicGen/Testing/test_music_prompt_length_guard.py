"""Regression tests for the outbound music-prompt length guard.

Background
----------
The enhanced-tier music API rejects a generation prompt longer than 1024
characters outright (HTTP 400) — the generation never starts. The prompt that
reaches the provider is not the direction the user approved: it is assembled
downstream, with scene enrichment and per-take variation appended, so it can
overshoot the limit while the stored direction sits well under it.

That is exactly how a live session degraded. One take of a two-take proposal
was rejected on length, fell back to a synthesized placeholder tone, and the
job still reported ``completed`` — the only signal that the user's "take" was a
sine wave was ``placeholder: true``. There was no length guard anywhere.

These tests lock in the fix:

* the prompt is clamped where the provider payload is built, not on the
  user-facing direction;
* the clamp sheds the trailing enrichment rather than cutting mid-word, so the
  musical direction survives;
* a length rejection is a non-retryable INPUT error — retrying it against the
  key pool cannot help and only burns keys.

Test strategy (real client, no provider credits)
------------------------------------------------
The real async client runs against a real local HTTP server that records the
bodies it receives, so the assertions are made against the payload that
actually went over the wire — not against a mock's recorded arguments.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from EdennCode.exceptions import EdennProviderResponseError
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_music import (
    ProviderBMusicProvider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_b_payload import (
    MAX_PROMPT_CHARS,
    clamp_prompt,
)

# The creative direction a user approved — the part that must survive a clamp.
DIRECTION = (
    "Warm cinematic indie-folk built on brushed drums, upright bass and a "
    "close-mic'd acoustic guitar. Keep the arrangement patient and unhurried, "
    "letting a single guitar motif carry the opening before the rhythm section "
    "arrives. Avoid synthetic textures and keep the low end soft."
)

# The trailing scene enrichment appended downstream — the least load-bearing end.
ENRICHMENT_TAIL_MARKER = "Total length"


def _oversized_prompt() -> str:
    """The shape that broke in production: direction, then grounding, then a
    long ";"-joined per-scene arc, then a total-length note."""
    arc = "; ".join(f"{i}-{i + 4}s handheld shot {i // 4}" for i in range(0, 160, 4))
    return (
        f"{DIRECTION} "
        "Ground the score in the video — footage mood: sunlit coastal drive; "
        "tempo around 96 BPM; instrumentation drawn from the footage: acoustic "
        "guitar, brushed kit, upright bass. "
        f"Follow the video's arc: {arc}. {ENRICHMENT_TAIL_MARKER} is about 160s."
    )


def _normalized_tokens(text: str) -> set[str]:
    """Whitespace tokens with sentence punctuation stripped, so a token that is
    present here but absent from the source proves a word was split."""
    return {token.rstrip(".;,") for token in text.split() if token.rstrip(".;,")}


class _ServerState:
    def __init__(self, *, reject_on_length: bool = False) -> None:
        self.reject_on_length = reject_on_length
        self.generate_payloads: list[dict] = []
        self._lock = threading.Lock()

    def record(self, payload: dict) -> None:
        with self._lock:
            self.generate_payloads.append(payload)

    @property
    def generate_calls(self) -> int:
        with self._lock:
            return len(self.generate_payloads)

    @property
    def last_payload(self) -> dict | None:
        with self._lock:
            return dict(self.generate_payloads[-1]) if self.generate_payloads else None


class _ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # keep test output clean
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path.startswith("/v1/account/billing"):
            # The client credit-checks a key before using it; stay above the gate.
            self._send_json({"balance": 1_000_000})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        state: _ServerState = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.startswith("/v1/instrumental/generate"):
            self.send_response(404)
            self.end_headers()
            return
        state.record(payload)
        if state.reject_on_length:
            # The upstream envelope for an over-long prompt.
            self._send_json(
                {"error": {"message": "Invalid Request, The prompt exceeds 1024 characters."}},
                status=400,
            )
            return
        self._send_json({"id": "task-1", "status": "queued", "trace_id": "trace-1"})


def _start_server(state: _ServerState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    server.state = state  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _submit(prompt: str, *, port: int, output_dir: Path, max_retries: int = 0):
    async def _run():
        client = ProviderBMusicProvider(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}",
            timeout=5,
            max_retries=max_retries,
            default_output_dir=output_dir,
        )
        return await client.generate_instrumental_task(prompt=prompt, n=1)

    return asyncio.run(asyncio.wait_for(_run(), timeout=15.0))


# --- the headline regression -------------------------------------------------

def test_over_limit_prompt_is_clamped_before_the_request_is_built(tmp_path: Path) -> None:
    prompt = _oversized_prompt()
    assert len(prompt) > MAX_PROMPT_CHARS, "fixture must exceed the provider limit"

    state = _ServerState()
    server = _start_server(state)
    try:
        task = _submit(prompt, port=server.server_address[1], output_dir=tmp_path)
    finally:
        server.shutdown()

    # The request was accepted rather than rejected on length.
    assert task.task_id == "task-1"

    sent = state.last_payload
    assert sent is not None, "the provider never received a generate request"
    sent_prompt = sent["prompt"]

    # 1. The payload that went over the wire is inside the provider's limit.
    assert len(sent_prompt) <= MAX_PROMPT_CHARS
    # 2. The musical direction survived the clamp intact.
    assert sent_prompt.startswith(DIRECTION)
    # 3. The trailing scene enrichment is what got dropped.
    assert ENRICHMENT_TAIL_MARKER not in sent_prompt
    # 4. Nothing was cut mid-word: every token also exists in the source prompt.
    assert _normalized_tokens(sent_prompt) <= _normalized_tokens(prompt)


def test_prompt_within_the_limit_is_sent_unchanged(tmp_path: Path) -> None:
    state = _ServerState()
    server = _start_server(state)
    try:
        _submit(DIRECTION, port=server.server_address[1], output_dir=tmp_path)
    finally:
        server.shutdown()

    sent = state.last_payload
    assert sent is not None
    assert sent["prompt"] == DIRECTION


# --- a length rejection is an input error, not a transient fault -------------

def test_length_rejection_is_not_retryable_and_burns_a_single_key(tmp_path: Path) -> None:
    state = _ServerState(reject_on_length=True)
    server = _start_server(state)
    try:
        with pytest.raises(EdennProviderResponseError) as caught:
            # max_retries=3: a fault classified as transient would be retried.
            _submit(
                DIRECTION,
                port=server.server_address[1],
                output_dir=tmp_path,
                max_retries=3,
            )
    finally:
        server.shutdown()

    error = caught.value
    assert error.status_code == 400
    assert error.retryable is False
    assert error.error_code == "prompt_too_long"
    # The identical payload fails on every key, so the request must be made once.
    assert state.generate_calls == 1


# --- clamp behaviour ---------------------------------------------------------

def test_clamp_keeps_whole_words_when_there_is_no_sentence_boundary() -> None:
    run_on = ("lush ambient pad " * 100).strip()
    clamped = clamp_prompt(run_on)

    assert len(clamped) <= MAX_PROMPT_CHARS
    # A word-boundary cut leaves a clean prefix of the original.
    assert run_on.startswith(clamped)
    assert not clamped.endswith("pa")


def test_clamp_bounds_a_single_unbroken_token() -> None:
    clamped = clamp_prompt("x" * (MAX_PROMPT_CHARS * 3))

    # No boundary exists to cut on, so the hard limit still has to hold.
    assert len(clamped) == MAX_PROMPT_CHARS


def test_clamp_leaves_short_prompts_untouched() -> None:
    assert clamp_prompt(DIRECTION) == DIRECTION
    assert clamp_prompt("") == ""
