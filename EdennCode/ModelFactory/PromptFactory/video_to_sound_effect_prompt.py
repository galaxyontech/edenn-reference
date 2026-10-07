"""
Prompt construction utilities for the model gateway Video → Sound Effect event detection.

These prompts are used with the model gateway Video models via the Responses API.
They always rely on a publicly accessible Azure Blob URL (never raw bytes).
"""

from __future__ import annotations

from typing import Dict, List, Optional


class VideoToSoundEffectPrompt:
    """
    Build inputs for the the model gateway Video model to extract SFX-worthy events.
    """

    @staticmethod
    def _system_text(duration: float) -> str:
        return (
            "You are a precise sound-effect event detector for video. "
            "Return ONLY JSON that strictly matches the provided schema. "
            f"Timestamps must be within 0 and {duration:.2f} seconds, and end_timestamp must be greater than start_timestamp. "
            "Do not hallucinate events that are not visually/aurally supported."
        )

    @staticmethod
    def _user_notes(user_prompt: Optional[str]) -> str:
        prompt = (user_prompt or "").strip()
        if not prompt:
            return "User SFX notes: (none provided)."
        return f"User SFX notes: {prompt}"

    @staticmethod
    def build_input(
        *,
        video_url: str,
        duration: float,
        user_prompt: Optional[str] = None,
    ) -> List[Dict]:
        """
        Construct the Responses API `input` payload with a video URL and guidance text.
        """
        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": VideoToSoundEffectPrompt._system_text(duration),
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Detect all SFX-worthy events with concise event_description, "
                            "start_timestamp, end_timestamp, and confidence. "
                            "Respond with JSON only."
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": VideoToSoundEffectPrompt._user_notes(user_prompt),
                    },
                    {"type": "input_video", "input": {"url": video_url}},
                ],
            },
        ]

    @staticmethod
    def _system_text_v2(duration: float) -> str:
        return (
            "You are a professional sound designer spotting a video for sound effects. "
            "Return ONLY JSON that strictly matches the provided schema. "
            f"The video is {duration:.2f} seconds long; every timestamp must lie within "
            "[0, that duration] and end_timestamp must be greater than start_timestamp.\n\n"
            "Work in two scans:\n"
            "1. COARSE SCAN — watch the whole video and note the setting, pacing, and every "
            "moment where a sound-producing action happens on screen.\n"
            "2. PRECISION SCAN — revisit each candidate moment and pin start_timestamp to the "
            "exact instant of visual contact or motion onset (the frame where the impact lands, "
            "the cut happens, the object starts moving) — not when the action begins to wind up.\n\n"
            "Rules:\n"
            "- Discrete events (IMPACT, CONTACT, TRANSITION, REVEAL, EMPHASIS) should be short: "
            "0.3–2.0 seconds. Sustained on-screen actions (ACTION) may run longer.\n"
            "- Do not emit an AMBIENCE event; describe the background bed only in "
            "ambience_description.\n"
            "- event_description says what is VISUALLY happening; sound_prompt describes the "
            "SOUND to synthesize (identity, material, texture, character) with no timing words.\n"
            "- Only include events visually supported by the footage; confidence reflects how "
            "certain you are the event is real and correctly timed.\n"
            "- Prefer fewer, well-timed, high-salience events over exhaustive coverage."
        )

    @staticmethod
    def build_analysis_v2_input(
        *,
        video_url: str,
        duration: float,
        user_prompt: Optional[str] = None,
    ) -> List[Dict]:
        """
        v2 analysis payload: two-scan spotting instructions, per-event sound
        prompts, and a separate ambience-bed description.
        """
        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": VideoToSoundEffectPrompt._system_text_v2(duration),
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Spot this video for sound design. Return scene_summary, "
                            "ambience_description, and the event list. JSON only."
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": VideoToSoundEffectPrompt._user_notes(user_prompt),
                    },
                    {"type": "input_video", "input": {"url": video_url}},
                ],
            },
        ]
