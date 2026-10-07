"""
JSON schema definitions for video → sound-effect event detection and user prompt understanding.

All fields are required; no heuristic defaults are allowed. These schemas are used with
the model gateway Video models via the `response_format` JSON Schema interface.
"""

from __future__ import annotations

from typing import Any, Dict


VIDEO_SFX_EVENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "event_type": {
            "type": "string",
            "enum": ["TRANSITION", "CONTACT", "IMPACT", "REVEAL", "EMPHASIS"],
        },
        "event_description": {"type": "string"},
        "start_timestamp": {"type": "number", "minimum": 0},
        "end_timestamp": {"type": "number", "minimum": 0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": [
        "event_id",
        "event_type",
        "event_description",
        "start_timestamp",
        "end_timestamp",
        "confidence",
    ],
    "additionalProperties": False,
}


USER_PROMPT_UNDERSTANDING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "extract_sfx_description": {
            "type": "string",
            "description": "User-provided description of desired SFX characteristics or palette.",
        },
        "extract_intent_where_to_add_this_event": {
            "type": "string",
            "description": "User intent about where to place the SFX within the video timeline or narrative.",
        },
    },
    "required": [
        "extract_sfx_description",
        "extract_intent_where_to_add_this_event",
    ],
    "additionalProperties": False,
}


VIDEO_SFX_EVENTS_ONLY_RESPONSE_SCHEMA: Dict[str, Any] = {
    "name": "video_to_sound_effect_events_only",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": VIDEO_SFX_EVENT_SCHEMA,
                "minItems": 0,
                "maxItems": 24,
            },
        },
        "required": ["events"],
        "additionalProperties": False,
    },
}


USER_PROMPT_UNDERSTANDING_RESPONSE_SCHEMA: Dict[str, Any] = {
    "name": "video_to_sound_effect_user_prompt_understanding",
    "strict": True,
    "schema": USER_PROMPT_UNDERSTANDING_SCHEMA,
}


VIDEO_SFX_RESPONSE_WITH_PROMPT_UNDERSTANDING_SCHEMA: Dict[str, Any] = {
    "name": "video_to_sound_effect_events",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": VIDEO_SFX_EVENT_SCHEMA,
                "minItems": 1,
                "maxItems": 24,
            },
            "user_prompt_understanding": USER_PROMPT_UNDERSTANDING_SCHEMA,
        },
        "required": ["events", "user_prompt_understanding"],
        "additionalProperties": False,
    },
}


VIDEO_SFX_EVENT_SCHEMA_V2: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "event_type": {
            "type": "string",
            "enum": [
                "TRANSITION",
                "CONTACT",
                "IMPACT",
                "REVEAL",
                "EMPHASIS",
                "ACTION",
                "AMBIENCE",
            ],
        },
        "event_description": {
            "type": "string",
            "description": "What is visually happening (subject + action + object).",
        },
        "sound_prompt": {
            "type": "string",
            "description": (
                "Text-to-sound generation prompt for this event: concrete sound "
                "identity, material, and character. No timing words."
            ),
        },
        "start_timestamp": {"type": "number", "minimum": 0},
        "end_timestamp": {"type": "number", "minimum": 0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": [
        "event_id",
        "event_type",
        "event_description",
        "sound_prompt",
        "start_timestamp",
        "end_timestamp",
        "confidence",
    ],
    "additionalProperties": False,
}


VIDEO_SFX_ANALYSIS_V2_RESPONSE_SCHEMA: Dict[str, Any] = {
    "name": "video_to_sound_effect_analysis_v2",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "scene_summary": {
                "type": "string",
                "description": "One-sentence description of the overall scene and setting.",
            },
            "ambience_description": {
                "type": "string",
                "description": (
                    "Text-to-sound prompt for one continuous, loopable background "
                    "ambience bed matching the setting. Empty string if no ambience fits."
                ),
            },
            "events": {
                "type": "array",
                "items": VIDEO_SFX_EVENT_SCHEMA_V2,
                "minItems": 0,
                "maxItems": 24,
            },
        },
        "required": ["scene_summary", "ambience_description", "events"],
        "additionalProperties": False,
    },
}


class VideoToSoundEffectSchemas:
    """Accessor for video→SFX structured response schemas."""

    @staticmethod
    def event_response() -> Dict[str, Any]:
        return VIDEO_SFX_EVENTS_ONLY_RESPONSE_SCHEMA

    @staticmethod
    def analysis_v2_response() -> Dict[str, Any]:
        return VIDEO_SFX_ANALYSIS_V2_RESPONSE_SCHEMA

    @staticmethod
    def user_prompt_understanding() -> Dict[str, Any]:
        return USER_PROMPT_UNDERSTANDING_RESPONSE_SCHEMA

    @staticmethod
    def event_response_with_prompt_understanding() -> Dict[str, Any]:
        return VIDEO_SFX_RESPONSE_WITH_PROMPT_UNDERSTANDING_SCHEMA
