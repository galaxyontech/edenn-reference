from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(slots=True)
class SceneSegment:
    """Simple timestamp window describing a raw visual scene."""

    start_sec: float
    end_sec: float

    def to_dict(self) -> dict:
        return {"start_sec": float(self.start_sec), "end_sec": float(self.end_sec)}


class VoiceOverMode(str, Enum):
    FULL = "full"
    SCENE = "scene"


class VoiceOverTTSProvider(str, Enum):
    PROVIDER_A = "provider_a"
    MODEL_GATEWAY = "model_gateway"


@dataclass(slots=True)
class VoiceSegmentPlan:
    """
    Planning metadata for a scene along with synthesized audio path (if generated).
    """

    scene_id: int
    start_sec: float
    end_sec: float
    need_voiceover: bool
    target_duration_sec: float
    voiceover_text: str
    audio_path: Optional[Path] = field(default=None)
    tts_provider: Optional[str] = field(default=None)
    tone_instruction: str = ""
    provider_a_style: str = ""
    provider_a_voice_settings: Optional[Dict[str, float | bool]] = field(default=None)
    model_gateway_tts_settings: Optional[Dict[str, str | float]] = field(default=None)
    emotion_keywords: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict) -> "VoiceSegmentPlan":
        return cls(
            scene_id=int(payload.get("scene_id", 0)),
            start_sec=float(payload.get("start_sec", 0.0)),
            end_sec=float(payload.get("end_sec", 0.0)),
            need_voiceover=bool(payload.get("need_voiceover", False)),
            target_duration_sec=float(
                payload.get("target_duration_sec", float(payload.get("end_sec", 0.0)) - float(payload.get("start_sec", 0.0)))
            ),
            voiceover_text=(payload.get("voiceover_text") or "").strip(),
            tts_provider=(payload.get("tts_provider") or "").strip() or None,
            tone_instruction=(payload.get("tone_instruction") or "").strip(),
            provider_a_style=(payload.get("provider_a_style") or "").strip(),
            provider_a_voice_settings=(
                payload.get("provider_a_voice_settings")
                if isinstance(payload.get("provider_a_voice_settings"), dict)
                else None
            ),
            model_gateway_tts_settings=(
                payload.get("model_gateway_tts_settings")
                if isinstance(payload.get("model_gateway_tts_settings"), dict)
                else None
            ),
            emotion_keywords=[str(item).strip() for item in payload.get("emotion_keywords", []) if str(item).strip()],
        )

    def to_dict(self) -> dict:
        data = {
            "scene_id": self.scene_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "need_voiceover": self.need_voiceover,
            "target_duration_sec": self.target_duration_sec,
            "voiceover_text": self.voiceover_text,
            "tts_provider": self.tts_provider,
            "tone_instruction": self.tone_instruction,
            "provider_a_style": self.provider_a_style,
            "provider_a_voice_settings": self.provider_a_voice_settings,
            "model_gateway_tts_settings": self.model_gateway_tts_settings,
            "emotion_keywords": self.emotion_keywords,
        }
        if self.audio_path:
            data["audio_path"] = str(self.audio_path)
        return data

