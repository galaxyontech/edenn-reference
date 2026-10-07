"""E2E driver: runs whole conversations through the real agentic router over
in-memory fakes, with a pluggable LLM client (scripted for CI, or the real model
for runtime "watch the session" runs). Only the agent's reasoning (and, in real
mode, the judge) hit a model — no Postgres or providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.EdennAgent.AgenticAudio.api import create_agentic_audio_router
from EdennCode.EdennAgent.AgenticAudio.tools import AgenticAudioTools

from .fakes import (
    MemoryAgenticRepository,
    MemoryAsyncRepository,
    MemoryQueue,
    RecordingCompose,
    build_context,
    fake_analyze,
    fake_remix,
    seed_source_video,
)
from .scenarios import Scenario


@dataclass
class TurnResult:
    status: int
    intent: Optional[str]
    tools: list[str]
    jobs_enqueued: int
    events: list[str]
    clarified: bool


@dataclass
class Trajectory:
    scenario: str
    turns: list[TurnResult]
    transcript: list[dict[str, str]]
    final_state: dict[str, Any]


class E2EDriver:
    """Drives the agentic API over in-memory fakes with a supplied llm_client."""

    def __init__(self, tmp_path: Path, llm_client: Any) -> None:
        self.async_repo = MemoryAsyncRepository()
        self.queue = MemoryQueue()
        self.agent_repo = MemoryAgenticRepository()
        self.source = seed_source_video(self.async_repo)
        # In-memory remix/compose fakes so cost-free editing flows (adjust_remix,
        # compose_mix) stay hermetic — without these the tools fall through to real
        # ffmpeg + network downloads of the seeded (fake) source URL.
        self.compose = RecordingCompose()
        context = build_context(tmp_path)
        tools = AgenticAudioTools(
            async_repository=self.async_repo,
            queue=self.queue,
            settings=context.settings,
            analyze_fn=fake_analyze,
            remix_fn=fake_remix,
            compose_fn=self.compose,
        )
        app = FastAPI()
        app.include_router(
            create_agentic_audio_router(
                context,  # type: ignore[arg-type]
                repository=self.agent_repo,
                async_repository=self.async_repo,
                queue=self.queue,
                tools=tools,
                llm_client=llm_client,
            )
        )
        self.client = TestClient(app)
        self.session_id: Optional[str] = None

    def _base(self) -> str:
        return f"/api/v2/agentic/audio/sessions/{self.session_id}"

    def create(self, initial_message: Optional[str]) -> Any:
        response = self.client.post(
            "/api/v2/agentic/audio/sessions",
            json={
                "source_video_artifact_id": self.source.artifact_id,
                "creator_user_id": "eval",
                "initial_message": initial_message,
            },
        )
        if response.status_code == 200:
            self.session_id = response.json()["session_id"]
        return response

    def snapshot(self) -> dict[str, Any]:
        return self.client.get(self._base()).json()

    def message(self, content: str) -> Any:
        return self.client.post(f"{self._base()}/messages", json={"content": content})

    def choice(self, body: dict[str, Any]) -> Any:
        return self.client.post(f"{self._base()}/choices", json=body)


def run_scenario(driver: E2EDriver, scenario: Scenario) -> Trajectory:
    """Drive a scenario's user turns and collect a per-turn trajectory."""

    turns: list[TurnResult] = []
    jobs_before = 0
    for index, turn in enumerate(scenario.turns):
        if index == 0 and turn.message is not None and turn.choice is None:
            response = driver.create(turn.message)
            snapshot = driver.snapshot() if response.status_code == 200 else {}
        elif turn.choice is not None:
            response = driver.choice(turn.choice)
            snapshot = (
                response.json().get("snapshot", {})
                if response.status_code == 200
                else driver.snapshot()
            )
        else:
            response = driver.message(turn.message or "")
            snapshot = (
                response.json().get("snapshot", {})
                if response.status_code == 200
                else driver.snapshot()
            )

        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        events = [e.get("event_type") for e in (body.get("events") or [])]
        jobs_now = len(driver.queue.envelopes)
        state = snapshot.get("state", {}) if snapshot else {}
        recorded = state.get("turns") or []
        last = recorded[-1] if recorded else {}
        turns.append(
            TurnResult(
                status=response.status_code,
                intent=last.get("intent"),
                tools=list(last.get("tools") or []),
                jobs_enqueued=jobs_now - jobs_before,
                events=events,
                clarified=("clarify.cards" in events) or bool(state.get("pending_clarification")),
            )
        )
        jobs_before = jobs_now

    final_state = driver.snapshot().get("state", {}) if driver.session_id else {}
    transcript = [
        {"role": m["role"], "content": m["content"]}
        for m in (driver.snapshot().get("messages", []) if driver.session_id else [])
    ]
    return Trajectory(
        scenario=scenario.name, turns=turns, transcript=transcript, final_state=final_state
    )


# Back-compat alias for the previous harness name.
MemoryEvalDriver = E2EDriver


__all__ = ["E2EDriver", "MemoryEvalDriver", "Trajectory", "TurnResult", "run_scenario"]
