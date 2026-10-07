from __future__ import annotations

from typing import Iterable

from ..events import EventType
from ..models import AgenticAudioSessionSnapshot, AgenticAudioWebSocketEvent


def event_payload(event: AgenticAudioWebSocketEvent) -> dict:
    # The model's serializer scrubs upstream-vendor identity on every dump.
    return event.model_dump(mode="json")


def events_payload(events: Iterable[AgenticAudioWebSocketEvent]) -> list[dict]:
    return [event_payload(event) for event in events]


def snapshot_opened_event(snapshot: AgenticAudioSessionSnapshot) -> dict:
    return {
        "event_type": EventType.SESSION_OPENED.value,
        "session_id": snapshot.session_id,
        # Snapshot serializer scrubs vendor identity (see models.py).
        "payload": {"snapshot": snapshot.model_dump(mode="json")},
    }


__all__ = ["event_payload", "events_payload", "snapshot_opened_event"]
