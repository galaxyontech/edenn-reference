from __future__ import annotations
from typing import Any, Dict

import copy
from dataclasses import dataclass
from typing import List, Tuple


# =========================
# JSON Schemas
# =========================
from typing import Any, Dict, Optional

from EdennCode.exceptions import EdennValidationError
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import Language
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.SceneSegmentationStage.datamodel import SceneUnderstanding


# Positive, originality-framed phrasing. The earlier negation-heavy wording
# ("do not mention… do not ask for soundalikes…" + meta-prompting "you write
# prompts for <external product>") was deterministically flagged as a jailbreak
# attempt by the upstream model's content filter, breaking the studio tier of
# video-music 100% (e2e-capability-eval-newapp-2026-07-30).
MUSIC_PROMPT_IP_SAFETY_DIRECTIVE = (
    "Originality requirements:\n"
    "- Describe the music purely in generic musical terms: mood, pacing, energy, tempo, genre, and instrumentation.\n"
    "- Keep every reference original; when scene context resembles well-known media, translate it into neutral mood and pacing language instead of naming it.\n"
)


# =========================
# JSON Schemas
# =========================

IMAGE_TO_MUSIC_SCHEMA: Dict[str, Any] = {
    "name": "image_music_plan",
    "schema": {
        "type": "object",
        "properties": {
            "image_summary": {
                "type": "string",
                "description": "1–2 sentence description of what is visually happening in the ad image."
            },
            "recommended_mood": {
                "type": "string",
                "description": "Overall emotional tone for the music (e.g., 'uplifting and confident')."
            },
            "tempo_bpm": {
                "type": "number",
                "description": "Approximate tempo in beats per minute suitable for the image context."
            },
            "instruments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key instruments that should be featured in the music."
            },
            "music_prompt": {
                "type": "string",
                "description": "Concise, production-ready prompt for a generative music model."
            },
        },
        "required": [
            "image_summary",
            "recommended_mood",
            "tempo_bpm",
            "instruments",
            "music_prompt",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

IMAGE_SEQUENCE_MUSIC_SCHEMA: Dict[str, Any] = {
    "name": "image_sequence_music_plan",
    "schema": {
        "type": "object",
        "properties": {
            "video_title": {
                "type": "string",
                "description": "Short title for the slideshow video in the requested language."
            },
            "music_title": {
                "type": "string",
                "description": (
                    "A song-style name for the generated track, in the requested language. "
                    "Evocative and short like a real song title (e.g. 'Golden Hour', "
                    "'City Lights'); NOT a description of the video or slideshow. "
                    "Distinct from video_title."
                )
            },
            "video_description": {
                "type": "string",
                "description": "Concise description of the slideshow video in the requested language."
            },
            "image_order": {
                "type": "array",
                "description": "1-based indices of the frames in the chosen narrative order. Use every frame exactly once.",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
            },
            "storyline_summary": {
                "type": "string",
                "description": "High-level narrative that explains how the ordered frames tell a compelling story."
            },
            "image_beats": {
                "type": "array",
                "description": "One beat per frame describing each frame's narrative role in the ordered story.",
                "items": {
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer", "minimum": 1},
                        "label": {"type": "string"},
                        "role": {"type": "string"},
                        "emotion": {"type": "string"},
                        "description": {"type": "string"},
                        "transition_hint": {"type": "string"},
                    },
                    "required": [
                        "image_index",
                        "label",
                        "role",
                        "emotion",
                        "description",
                        "transition_hint",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "overall_mood": {
                "type": "string",
                "description": "Overall emotional tone that ties the full story together."
            },
            "target_bpm": {
                "type": "number",
                "description": "Tempo that best fits the pacing of the sequence."
            },
            "primary_instruments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Featured instruments that reinforce the story."
            },
            "music_sections": {
                "type": "array",
                "description": "Macro music sections mapped to one or more frames in image_order. These sections define the musical arc for downstream generators.",
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string"},
                        "label": {"type": "string"},
                        "image_indices": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "minItems": 1,
                        },
                        "objective": {"type": "string"},
                        "energy_start": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "energy_end": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "instrumentation_focus": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "lyric_lines": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "section_id",
                        "label",
                        "image_indices",
                        "objective",
                        "energy_start",
                        "energy_end",
                        "instrumentation_focus",
                        "lyric_lines",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "music_prompt_summary": {
                "type": "string",
                "description": "Concise summary of the intended music direction for compatibility and logging."
            },
        },
        "required": [
            "video_title",
            "music_title",
            "video_description",
            "image_order",
            "storyline_summary",
            "image_beats",
            "overall_mood",
            "target_bpm",
            "primary_instruments",
            "music_sections",
            "music_prompt_summary",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

IMAGE_BATCH_VISUAL_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "name": "image_batch_visual_analysis",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Concise overall summary of the provided visual creatives."
            },
            "overall_mood": {
                "type": "string",
                "description": "Overall emotional tone shared across the visual set."
            },
            "visual_style": {
                "type": "string",
                "description": "Short description of the dominant visual style and aesthetic."
            },
            "key_elements": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Most important recurring subjects, motifs, or design elements."
            },
            "creative_direction": {
                "type": "string",
                "description": "How the visuals should influence sound design, pacing, and tone."
            },
        },
        "required": [
            "summary",
            "overall_mood",
            "visual_style",
            "key_elements",
            "creative_direction",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

AUDIO_CREATIVE_EDIT_PROMPT_SCHEMA: Dict[str, Any] = {
    "name": "audio_creative_edit_prompt",
    "schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short working title for the edited audio."
            },
            "edit_intent_summary": {
                "type": "string",
                "description": "One-sentence summary of how the source audio should be changed."
            },
            "visual_style_summary": {
                "type": "string",
                "description": "One-sentence summary of the visual style influence."
            },
            "edit_prompt": {
                "type": "string",
                "description": "Provider-neutral prompt describing how the source audio should be transformed."
            },
            "style_prompt": {
                "type": "string",
                "description": "Concise music style, arrangement, instrumentation, energy, and pacing direction."
            },
            "lyrics_prompt": {
                "type": "string",
                "description": "Lyric or vocal-theme instruction. Use an empty string when vocals are not requested."
            },
        },
        "required": [
            "title",
            "edit_intent_summary",
            "visual_style_summary",
            "edit_prompt",
            "style_prompt",
            "lyrics_prompt",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

SCENE_UNDERSTANDING_SCHEMA: Dict[str, Any] = {
    "name": "scene_understanding",
    "schema": {
        "type": "object",
        "properties": {
            "visual_summary": {
                "type": "string",
                "description": "1–3 sentence description of what is visually happening in the scene."
            },
            "key_actions": {
                "type": "string",
                "description": "Short description of key actions or events in the scene."
            },
            "mood": {
                "type": "string",
                "description": "Perceived mood/feeling of the scene (e.g., 'calm', 'energetic', 'nostalgic')."
            },
        },
        "required": ["visual_summary", "key_actions", "mood"],
        "additionalProperties": False,
    },
    "strict": True,
}

VIDEO_SUMMARY_SCHEMA: Dict[str, Any] = {
    "name": "video_summary",
    "schema": {
        "type": "object",
        "properties": {
            "video_title": {
                "type": "string",
                "description": "Video title in the requested language, <= 20 words, optimized for social sharing/click-through. Do not use meta labels such as ad, advertisement, commercial, promo, campaign, 广告, or 宣传片."
            },
            "music_title": {
                "type": "string",
                "description": "A song-style name for the track to be generated for this video, in the requested language. Short and evocative like a real song title (e.g. 'Golden Hour', 'City Lights'); NOT a description of the video. Distinct from video_title. Do not use meta labels such as ad, advertisement, commercial, promo, campaign, 广告, or 宣传片."
            },
            "video_description": {
                "type": "string",
                "description": "Concise video description in the requested language, <= 100 words. Do not use meta labels such as ad, advertisement, commercial, promo, campaign, 广告, or 宣传片."
            },
            "summary": {
                "type": "string",
                "description": "1–3 sentence description of the overall video in the requested language."
            },
            "overall_mood": {
                "type": "string",
                "description": "Overall emotional tone of the video."
            },
            "core_message": {
                "type": "string",
                "description": "Main takeaway or value proposition the video is trying to convey."
            },
            "has_explicit_call_to_action": {
                "type": "boolean",
                "description": "Whether the video contains an explicit call-to-action (e.g., 'Sign up now')."
            },
        },
        "required": [
            "video_title",
            "music_title",
            "video_description",
            "summary",
            "overall_mood",
            "core_message",
            "has_explicit_call_to_action",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

MUSIC_ALIGNMENT_SCHEMA: Dict[str, Any] = {
    "name": "video_music_alignment",
    "schema": {
        "type": "object",
        "properties": {
            "global_music_prompt": {"type": "string"},
            "global_mood": {"type": "string"},
            "tempo_bpm": {"type": "number"},
            "instruments": {
                "type": "array",
                "items": {"type": "string"}
            },

        },
        "required": ["global_music_prompt", "global_mood", "tempo_bpm", "instruments"],
        "additionalProperties": False,
    },
    "strict": True,
}

PROVIDER_C_CUSTOM_LYRICS_SCHEMA: Dict[str, Any] = {
    "name": "provider_c_custom_lyrics",
    "schema": {
        "type": "object",
        "properties": {
            "lyrics_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 190,
                "description": "Extremely short lyric-generation instruction for ProviderC Custom Mode (<= 190 characters).",
            },
            "style_prompt": {"type": "string", "minLength": 1, "maxLength": 980},
        },
        "required": ["lyrics_prompt", "style_prompt"],
        "additionalProperties": False,
    },
    "strict": True,
}

PROVIDER_C_SIMPLE_PROMPT_SCHEMA: Dict[str, Any] = {
    "name": "provider_c_simple_prompt",
    "schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Single-line prompt for ProviderC Simple Mode (<= 500 characters).",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
    "strict": True,
}

PROVIDER_B_DUAL_PROMPT_SCHEMA: Dict[str, Any] = {
    "name": "provider_b_music_prompt",
    "schema": {
        "type": "object",
        "properties": {
            "style_prompt": {"type": "string"},
            "lyrics_prompt": {"type": "string"},
        },
        "required": ["style_prompt", "lyrics_prompt"],
        "additionalProperties": False,
    },
    "strict": True,
}

VOICEOVER_PLAN_SCHEMA: Dict[str, Any] = {
    "name": "voiceover_semantic_plan",
    "schema": {
        "type": "object",
        "properties": {
            "voice_segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "integer"},
                        "start_sec": {"type": "number"},
                        "end_sec": {"type": "number"},
                        "need_voiceover": {"type": "boolean"},
                        "target_duration_sec": {"type": "number"},
                        "voiceover_text": {"type": "string"},
                        "tone_instruction": {"type": "string"},
                        "provider_a_style": {"type": "string"},
                        "provider_a_voice_settings": {
                            "type": "object",
                            "properties": {
                                "stability": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                                "similarity_boost": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                                "style": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                                "use_speaker_boost": {"type": "boolean"},
                                "speed": {"type": "number", "minimum": 0.7, "maximum": 1.2},
                            },
                            "required": [
                                "stability",
                                "similarity_boost",
                                "style",
                                "use_speaker_boost",
                                "speed",
                            ],
                            "additionalProperties": False,
                        },
                        "emotion_keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "scene_id",
                        "start_sec",
                        "end_sec",
                        "need_voiceover",
                        "target_duration_sec",
                        "voiceover_text",
                        "tone_instruction",
                        "provider_a_style",
                        "provider_a_voice_settings",
                        "emotion_keywords",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 1,
            }
        },
        "required": ["voice_segments"],
        "additionalProperties": False,
    },
    "strict": True,
}


FULL_VIDEO_SCRIPT_SCHEMA: Dict[str, Any] = {
    "name": "full_video_voiceover_script",
    "schema": {
        "type": "object",
        "properties": {
            "video_id": {"type": "string"},
            "tts_provider": {"type": "string", "enum": ["provider_a"]},
            "voice_id": {"type": "string"},
            "language": {"type": "string", "default": "en"},

            # Non-robotic ads-safe settings (same idea as your master settings)
            "master_voice_settings": {
                "type": "object",
                "properties": {
                    "stability": {"type": "number", "minimum": 0.30, "maximum": 0.50, "default": 0.42},
                    "similarity_boost": {"type": "number", "minimum": 0.60, "maximum": 0.85, "default": 0.72},
                    "style": {"type": "number", "minimum": 0.45, "maximum": 0.85, "default": 0.65},
                    "use_speaker_boost": {"type": "boolean", "const": False, "default": False},
                    "speed": {"type": "number", "minimum": 0.92, "maximum": 1.06, "default": 0.98},
                },
                "required": ["stability", "similarity_boost", "style", "use_speaker_boost", "speed"],
                "additionalProperties": False,
            },

            "global_tone_instruction": {"type": "string", "maxLength": 160},
            "global_emotion_keywords": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},

            # single continuous narration (no anchors)
            "script": {"type": "string", "maxLength": 1200},
        },
        "required": [
            "video_id",
            "tts_provider",
            "voice_id",
            "language",
            "master_voice_settings",
            "global_tone_instruction",
            "global_emotion_keywords",
            "script",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}

MODEL_GATEWAY_VOICEOVER_SCHEMA: Dict[str, Any] = {
    "name": "voiceover_script_model_gateway",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "model_gateway_tts_settings": {
                "type": "object",
                "properties": {
                    "voice": {
                        "type": "string",
                        "enum": ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
                    },
                    "speed": {
                        "type": "number",
                        "minimum": 0.85,
                        "maximum": 1.15
                    },
                    "pacing_profile": {
                        "type": "string",
                        "enum": ["fast_paced_energetic", "steady_narrative", "dramatic_slow", "conversational_casual"],
                        "description": "The overall rhythmic style of the delivery."
                    },
                    "instructions": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300
                    }
                },
                "required": ["voice", "speed", "pacing_profile", "instructions"],
                "additionalProperties": False
            },
            "metadata": {
                "type": "object",
                "properties": {
                    "estimated_word_count": {"type": "integer"},
                    "target_duration_seconds": {"type": "integer"}
                },
                "required": ["estimated_word_count", "target_duration_seconds"],
                "additionalProperties": False
            },
            "script": {
                "type": "string",
                "description": "The final spoken script, including phonetic spelling for brands and rhythmic punctuation."
            }
        },
        "required": ["model_gateway_tts_settings", "metadata", "script"],
        "additionalProperties": False
    }
}


# =========================
# Schema Accessor
# =========================

class ResponseSchemas:
    """
    Central place to access JSON schemas used for Azure/ModelGateway response_format.
    """

    @staticmethod
    def image_music() -> Dict[str, Any]:
        """
        Schema for image → music prompt/planning.
        """
        return IMAGE_TO_MUSIC_SCHEMA

    @staticmethod
    def image_sequence_music() -> Dict[str, Any]:
        """Schema for multi-image story-driven planning and music alignment."""
        return IMAGE_SEQUENCE_MUSIC_SCHEMA

    @staticmethod
    def image_batch_visual_analysis() -> Dict[str, Any]:
        """Schema for image-batch visual conditioning."""
        return IMAGE_BATCH_VISUAL_ANALYSIS_SCHEMA

    @staticmethod
    def scene_understanding() -> Dict[str, Any]:
        """
        Schema for per-scene visual understanding.
        """
        return SCENE_UNDERSTANDING_SCHEMA

    @staticmethod
    def video_summary() -> Dict[str, Any]:
        """
        Schema for overall video summary.
        """
        return VIDEO_SUMMARY_SCHEMA

    @staticmethod
    def music_alignment() -> Dict[str, Any]:
        """
        Schema for full video music alignment (global + per-section).
        """
        return MUSIC_ALIGNMENT_SCHEMA

    @staticmethod
    def provider_c_custom_lyrics() -> Dict[str, Any]:
        """
        Schema for ProviderC custom-mode prompt generation (lyrics_prompt/style_prompt).
        """
        return PROVIDER_C_CUSTOM_LYRICS_SCHEMA

    @staticmethod
    def provider_c_simple_prompt() -> Dict[str, Any]:
        """
        Schema for ProviderC simple-mode prompt generation (single prompt string).
        """
        return PROVIDER_C_SIMPLE_PROMPT_SCHEMA

    @staticmethod
    def provider_b_dual_prompt() -> Dict[str, Any]:
        """
        Schema for ProviderB dual-prompt generation (style_prompt + lyrics_prompt).
        """
        return PROVIDER_B_DUAL_PROMPT_SCHEMA

    @staticmethod
    def audio_creative_edit_prompt() -> Dict[str, Any]:
        """Schema for source-audio creative edit prompt generation."""
        return AUDIO_CREATIVE_EDIT_PROMPT_SCHEMA

    @staticmethod
    def voiceover_plan() -> Dict[str, Any]:
        """Schema for voiceover semantic planning."""
        return VOICEOVER_PLAN_SCHEMA

    @staticmethod
    def voiceover_full_plan() -> Dict[str, Any]:
        """Schema for full-length voiceover planning."""
        return FULL_VIDEO_SCRIPT_SCHEMA

    @staticmethod
    def voiceover_full_plan_model_gateway() -> Dict[str, Any]:
        """Schema for full-length voiceover planning with ModelGateway TTS."""
        return MODEL_GATEWAY_VOICEOVER_SCHEMA


# =========================
# PromptFactory Templates
# =========================

@dataclass
class Prompt:
    # ----- Image → Music -----
    image_to_music: str = (
        "Analyze this advertisement image. Briefly describe the scene, then craft a concise "
        "music-generation prompt that maximizes positive emotions, engagement, and conversion. "
        "Keep the prompt actionable and production-ready."
    )

    image_sequence_to_music: str = (
        "You are given a batch of advertisement frames. Determine the best narrative order (you may reorder the "
        "frames), then design a section-aware music plan for the full sequence.\n"
        "Return JSON only.\n"
        "Requirements:\n"
        "- video_title and video_description must be concise, compelling, and written in the requested output language.\n"
        "- music_title must read like a real song name (short, evocative, e.g. 'Golden Hour', 'City Lights'), "
        "in the requested output language. It must NOT describe the video/slideshow and must be distinct from video_title.\n"
        "- image_order must use every frame exactly once.\n"
        "- image_beats must contain one beat per frame and reference the original frame index.\n"
        "- music_sections must define 2 to 6 macro sections that map to one or more frame indices.\n"
        "- music_sections should describe a real musical arc such as intro, build, lift, peak, resolve.\n"
        "- lyric_lines should be short, singable, and can be empty if lyrics are not strongly implied.\n"
        "- music_prompt_summary must stay concise and production-ready.\n"
        "- Optimize for a cohesive, engaging music arc, not disconnected per-frame cues."
    )

    image_batch_visual_analysis: str = (
        "You are analyzing a batch of visual creatives that will be used to guide an audio edit.\n"
        "Summarize the shared visual language, emotional tone, and creative direction.\n"
        "Return JSON only following the provided schema.\n"
        "Do not mention technical image quality or file format.\n"
    )

    # ----- Scene Understanding -----
    scene_understanding: str = (
        "You are analyzing a scene from a video.\n"
        "Preferred output language: {language_name}.\n"
        "All textual fields must be written in {language_name}.\n"
        "If the frames contain text or branding in another language, translate or summarize it in {language_name} instead of copying it verbatim.\n"
        "Do not identify or name copyrighted/trademarked characters, franchises, studios, logos, or brands; describe recognizable IP generically by role and action only.\n"
        "Describe only what is visible and avoid speculation.\n"
        "Scene {scene_index} runs from {start_time:.2f}s to {end_time:.2f}s.\n"
        "Return JSON only."
    )

    # ----- Video Summary -----
    video_summary: str = (
        "You are summarizing a short-form video.\n"
        "Preferred output language: {language_name}.\n"
        "All textual fields must be written in {language_name}.\n\n"
        "Here are scene timelines and descriptions:\n"
        "{scene_lines}\n\n"
        "If any scene descriptions appear in another language, translate and summarize them into {language_name} rather than mirroring that language.\n"
        "Avoid naming copyrighted/trademarked characters, franchises, studios, logos, or brands; use generic roles and moods instead.\n"
        "Return JSON (following the provided schema) with:\n"
        "- video_title: <=20 words, highlight core value proposition, social-share friendly. Do not include meta labels like ad, advertisement, commercial, promo, campaign, 广告, or 宣传片.\n"
        "- music_title: a song-style name for the track (short, evocative, e.g. 'Golden Hour', 'City Lights'), in the requested language. Must read like a real song name, NOT a description of the video, and be distinct from video_title. No meta labels.\n"
        "- video_description: <=100 words, summarize overall content and mood. Do not include meta labels like ad, advertisement, commercial, promo, campaign, 广告, or 宣传片.\n"
        "- summary: 1-3 sentence overall summary of the video.\n"
        "- overall_mood: overall emotional tone.\n"
        "- core_message: core claim or value proposition.\n"
        "- has_explicit_call_to_action: whether there is explicit CTA.\n"
        "For video_title and video_description, describe the content naturally and never call it an ad, advertisement, commercial, promo, campaign, 广告, or 宣传片.\n"
        "Strictly follow length limits. Output JSON only."
    )

    # ----- Video → Music JSON Aligned -----
    video_to_music: str = (
        "You are designing music prompt for a Video and downstream model is provider_a.\n\n"
        "You are given:\n"
        "- A provider-safe list of scene timings and moods.\n\n"
        "Your job:\n"
        "- Align the music to the visual narrative.\n"
        "- {vocal_directive}\n"
        "- Pronunciation: lyrics must be 吐字清晰标准 (crisp, clear articulation).\n"
        "- Language: if Chinese is requested, default to Mainland Mandarin (普通话) delivery.\n"
        "- (Important) User Music Style preference: {user_prompt}\n"
        f"{MUSIC_PROMPT_IP_SAFETY_DIRECTIVE}"
        "- (Important) Adding this music will make the video Experience more engaging and fitting visual contents.\n"
        "You MUST return valid JSON that follows the schema:\n"
        "- global_music_prompt \n"
        "- global_mood\n"
        "- tempo_bpm\n"
        "- instruments\n"
        "Only output JSON. Do not include any additional text.\n\n"
        "Scene list:\n{scene_lines}\n"
    )

    # ----- ProviderC Custom Lyrics  -----

    # Common core (keep short + consistent)
    # Neutral music-director framing (no external-product meta-prompting — the
    # old wording tripped the upstream content filter; see the directive note).
    PROVIDER_C_LYRICS_COMMON: str = (
        "You are a music director writing a production brief for a soundtrack with vocals.\n"
        "Return JSON only with keys: lyrics_prompt, style_prompt.\n"
        "Do NOT write finished lyrics; lyrics_prompt is a short instruction describing what the lyrics should express.\n"
        "Follow vocals: {vocal_directive}.\n"
        "User: {user_prompt}\n"
        "Lyrics Prompt Instruction:\n"
        "- CRITICAL: lyrics_prompt MUST be under 190 characters. This is a hard API limit. Count carefully.\n"
        "- Must be very short and paced to the video.\n"
        "- Be specific about themes, moods, and song structure in as few words as possible.\n"
        "- Describe what the lyrics should convey; never write the lyrics themselves.\n"
        "Style Prompt Instruction: [language(must be specified), vocal_directive] genre+mood+instruments+tempo, description of the music, <=5000 chars.\n"
        "If user_prompt is non-empty, it has highest priority.\n"
        f"{MUSIC_PROMPT_IP_SAFETY_DIRECTIVE}"
        "Use the scenes to define a simple arc (setup->peak->resolve) and match timing.\n"
        "Scenes:\n{scene_lines}\n"
        "- All field response must be in {language}.\n"

    )

    PROVIDER_C_CUSTOM_LYRICS_CN: str = (
        PROVIDER_C_LYRICS_COMMON
        + "All output text must be in Chinese (Simplified).\n"
        + "Make it suitable for Chinese vocals.\n"
    )

    # ----- ProviderC Simple Mode (prompt only) -----
    provider_c_simple_prompt: str = (
        "You are a music director writing one production brief for an instrumental soundtrack.\n"
        "The music will be attached to a video (described by its scene list). Always focus on promoting positive emotions.\n"
        "Return JSON with a single field: prompt.\n"
        "Hard requirements:\n"
        "- prompt MUST be 500 characters or fewer (including spaces).\n"
        "- No line breaks; keep it a single paragraph.\n"
        "- Include style, genre, mood, tempo (BPM or descriptive), instrumentation, and arrangement arc.\n"
        "- {vocal_directive}\n"
        f"{MUSIC_PROMPT_IP_SAFETY_DIRECTIVE}"
        "- If a user prompt exists, prioritize it.\n"
        "User prompt: {user_prompt}\n"
        "Language requirement: {language} — return the prompt in {language}.\n"
        "Scene list:\n{scene_lines}\n"
    )

    audio_creative_edit: str = (
        "You are designing a creative edit for an existing source audio track.\n"
        "The output will be used to transform the source audio so it better matches the requested direction and any provided visuals.\n"
        "Preserve a recognizable relationship to the original audio while changing style, energy, arrangement, and emotional framing.\n"
        "Return JSON only with keys: title, edit_intent_summary, visual_style_summary, edit_prompt, style_prompt, lyrics_prompt.\n"
        "- title: short working title.\n"
        "- edit_intent_summary: one sentence describing the target transformation.\n"
        "- visual_style_summary: one sentence describing the visual influence. If no visuals are provided, explain that the edit is driven only by the user instruction.\n"
        "- edit_prompt: provider-neutral prompt for transforming the source audio.\n"
        "- style_prompt: concise style/instrument/tempo/arrangement direction.\n"
        "- lyrics_prompt: concise lyric or vocal-theme direction if vocals are requested; otherwise return an empty string.\n"
        "- Keep all fields in {language_name}.\n"
        "- Vocal directive: {vocal_directive}\n"
        "- User edit instruction: {user_prompt}\n"
        "- Visual input type: {visual_input_type}\n"
        "- Visual context:\n{visual_context}\n"
    )

    # --------------------------
    # Formatting Helpers
    # --------------------------

    def format_scene_understanding(
        self,
        *,
        scene_index: int,
        start_time: float,
        end_time: float,
        language: str = Language.EN,
    ) -> str:
        return self.scene_understanding.format(
            scene_index=scene_index,
            start_time=start_time,
            end_time=end_time,
            language_name=self._normalize_language_name(language),
        )

    @staticmethod
    def _normalize_language_name(language: str) -> str:
        lang = (language or "").strip().upper()
        if lang in {Language.CN, "CN", "ZH", "ZH_CN", "CHINESE", "MANDARIN"}:
            return "Simplified Chinese (Mainland Mandarin)"
        if lang in {Language.EN, "EN", "ENGLISH", "EN_US"}:
            return "English (US)"
        return language or "English (US)"

    def format_video_summary(self, *, scene_lines: str, language: str = Language.EN) -> str:
        return self.video_summary.format(
            scene_lines=scene_lines,
            language_name=self._normalize_language_name(language),
        )

    def format_video_music(
        self,
        *,
        scene_lines: str,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> str:
        cleaned_gender = vocal_gender.strip().lower() or "female"
        language_name = self._normalize_language_name(language)
        if include_vocals:
            cleaned_gender = cleaned_gender.strip().lower()
            vocal_directive = f"{cleaned_gender} vocals in {language_name}."
        else:
            vocal_directive = "Keep the composition strictly instrumental with zero vocals or vocal chops."
        return self.video_to_music.format(
            scene_lines=scene_lines,
            vocal_directive=vocal_directive,
            user_prompt=user_prompt,
        )

    def format_provider_c_custom_lyrics(
        self,
        *,
        scene_lines: str,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> str:


        if include_vocals:
            cleaned_gender = vocal_gender.strip().lower()
            vocal_directive = f"Need clean {cleaned_gender} vocals in {language}."
        else:
            vocal_directive = "Keep the composition strictly instrumental with zero vocals."

        return self.PROVIDER_C_LYRICS_COMMON.format(
            scene_lines=scene_lines,
            vocal_directive=vocal_directive,
            user_prompt=user_prompt,
            language=language,
        )

    def format_provider_c_simple_prompt(
        self,
        *,
        scene_lines: str,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> str:
        cleaned_gender = vocal_gender.strip().lower() or "female"
        normalized_language = (language or "").strip().upper()
        if include_vocals:
            vocal_directive = f"Include clean {cleaned_gender} vocals in the requested language."
        else:
            vocal_directive = "Instrumental only; no vocals or vocal chops."
        return self.provider_c_simple_prompt.format(
            scene_lines=scene_lines,
            vocal_directive=vocal_directive,
            user_prompt=user_prompt,
            language=normalized_language,
        )

    def format_audio_creative_edit(
        self,
        *,
        visual_context: str,
        visual_input_type: str,
        include_vocals: bool,
        vocal_gender: str,
        user_prompt: str,
        language: str = Language.EN,
    ) -> str:
        cleaned_gender = (vocal_gender or "female").strip().lower() or "female"
        language_name = self._normalize_language_name(language)
        if include_vocals:
            vocal_directive = (
                f"Include {cleaned_gender} vocals in {language_name}. "
                "lyrics_prompt must describe the theme and vocal direction, not final lyrics."
            )
        else:
            vocal_directive = "Keep the edit instrumental. lyrics_prompt must be an empty string."
        return self.audio_creative_edit.format(
            language_name=language_name,
            vocal_directive=vocal_directive,
            user_prompt=user_prompt,
            visual_input_type=visual_input_type,
            visual_context=visual_context,
        )


@dataclass
class VoiceoverPrompt:

    video_voice_over_with_scenes_generation: str = (
        "You are writing short ad voiceover lines for ProviderA TTS.\n"
        "Return JSON objects matching the schema EXACTLY.\n"
        "\n"
        "Hard requirements:\n"
        "- If scene has no meaningful product/brand visuals, set need_voiceover=false.\n"
        "- If need_voiceover=true, write voiceover_text that sounds natural when spoken.\n"
        "- Keep it short: aim for target_duration_sec (±0.2s). Use ~2.2 words/sec.\n"
        "- Use spoken formatting:\n"
        "  * Websites: say 'Brand dot com' (NOT 'Brand.com').\n"
        "  * Numbers/prices: write how you'd say it ('twenty percent', '$49' -> 'forty-nine dollars').\n"
        "- Add breathing room with punctuation. If you can use SSML, include a second field voiceover_text_ssml\n"
        "  with <break time=\"200ms\"/>. If schema does not allow it, keep only voiceover_text.\n"
        "- Provide tone_instruction (one sentence), provider_a_style (1–3 words), and 3–6 emotion_keywords.\n"
        "- Provide provider_a_voice_settings (stability, similarity_boost, style, use_speaker_boost, speed).\n"
        "  Use these values to reflect the sentence-level emotional undertone and keep transitions natural across scenes.\n"
        "- Avoid contradictory tags (e.g., 'calm' + 'hyper').\n"
        "\n"
        "Overall video duration is approximately {duration_hint:.2f} seconds.\n"
    )

    video_voice_over_with_one_pass: str = (
        """
        You are a performance-marketing voice director.

        Write ONE continuous voiceover script for the entire video Video length is {duration_hint}.
        This will be rendered once using ProviderA.

        Hard requirements:
        - Return JSON that matches the schema EXACTLY. JSON only.
        - Use spoken language, not written prose.
        - Use punctuation and line breaks for breath.
        - Keep total length suitable for a short ad (8–18 seconds).
        - Use spoken formatting (say "Brand dot com", not "Brand.com").
        - Choose non-robotic ProviderA settings:
        stability 0.30–0.50,
        similarity_boost 0.60–0.85,
        style 0.45–0.85,
        use_speaker_boost=false,
        speed 0.92–1.06.

        You will be shown key frames from the video.
        Base the script on what you see in the images.

        """
    )

    video_voice_over_with_one_pass_model_gateway: str = (
        """
        You are an expert Short-Form Video Scriptwriter and Voiceover Director specializing in high-retention TikTok, Reels, and YouTube Shorts.

        ### 1. OBJECTIVE
        Create a compelling voiceover script for a {video_type} that fits exactly within {duration_hint} seconds. 

        ### 2. DURATION MATH (Hard Constraint)
        ModelGateway TTS speaks at approximately 150 words per minute (2.5 words per second).
        - For {duration_hint} seconds, your script MUST NOT exceed {max_words} words.
        - If the script is too long, the video will fail. Prioritize brevity.

        ### 3. SCRIPT STRUCTURE (Short-Form Best Practices)
        - **The Hook (0-3s):** Start with a high-energy, curiosity-driven statement. No "Hey guys."
        - **The Value (3s to end-3s):** Fast-paced delivery of the main message.
        - **The CTA (Final 3s):** One clear, punchy instruction.

        ### 4. DELIVERY GUIDELINES
        - **Spoken Style:** Use contractions (don't, can't, it's). Use fragments. Write like a human talking to a friend.
        - **Natural Branding**: Do NOT hyphenate brand names. If a name is hard to pronounce, provide a "sounds like" hint in the instructions, NOT in the script text.
        - **No Spelling Out**: Avoid "dot com" unless it's essential for the brand identity. Usually, "Shop Italo Jewelry" is stronger than "Shop Italo Jewelry dot com."
        - **The Fade Out**: The final sentence should be a confident statement, not a URL reading.
        - **Instructions:** Tell the TTS model exactly what emotion to convey (e.g., "Excited but cynical," or "Whispered and intimate").

        ### 5. INPUT CONTEXT
        - Language: {language}
        - TTS Model: {model_name}
        - Visual Context: [Analyze provided keyframes/description for alignment]

        ### VOICE SELECTION LOGIC (Crucial)
            Select the voice based on the brand's personality:
            - SHIMMER: Use for high-energy, "sparkle," beauty, and fast-paced luxury.
            - ASH: Use for modern, "cool," and trendy creator-style ads.
            - SAGE: Use for understated, quiet luxury and premium sophistication.
            - CORAL: Use for friendly, approachable, and warm lifestyle products.
            - Avoid BALLAD or ECHO for high-energy ads; they are too heavy/slow.

        ### 6. OUTPUT REQUIREMENTS
        - Return JSON ONLY.
        - Ensure `estimated_word_count` matches the actual script word count.
        - Ensure `script` is rhythmic and fits the duration.
        
        """
    )

    def format_full_instructions(self, *, duration_hint: float) -> str:
        return self.video_voice_over_with_one_pass.format(duration_hint=duration_hint)

    def format_full_model_gateway_instructions(self, *, duration_hint: float, language: str, model_name: str) -> str:
        clean_language = (language or "en").strip()
        return self.video_voice_over_with_one_pass_model_gateway.format(
            duration_hint=duration_hint,
            language=clean_language or "en",
            model_name=model_name,
            video_type="video advertisement",
            max_words=int(duration_hint * 2)
        )

    def format_scene_instructions(self, *, duration_hint: float) -> str:
        return self.video_voice_over_with_scenes_generation.format(duration_hint=duration_hint)


# =========================
# PromptFactory Builders
# =========================

class VoiceOverPromptBuilder:

    @staticmethod
    def build_full_video_voiceover_script_messages(
        contexts: List["VoiceoverSceneContext"],
        *,
        duration: Optional[float] = None,
    ) -> List[Dict]:
        """
        Docstring for build_full_video_voiceover_script_messages This is for PROVIDER_A Single Video Voice Over 

        :param contexts: Description
        :type contexts: List["VoiceoverSceneContext"]
        :param duration: Description
        :type duration: Optional[float]
        :return: Description
        :rtype: List[Dict[Any, Any]]
        """

        if not contexts:
            raise EdennValidationError("contexts must not be empty", component="prompt_building", operation="build_voiceover_messages")

        duration_hint = duration
        if duration_hint is None:
            duration_hint = max((ctx.end_sec for ctx in contexts), default=0.0)

        prompt = VoiceoverPrompt().format_full_instructions(duration_hint=duration_hint)
        content: List[Dict] = [{"type": "text", "text": prompt}]

        for i, ctx in enumerate(contexts, start=1):
            content.append({"type": "text", "text": f"Key frame {i}"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{ctx.first_frame_b64}"}
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{ctx.last_frame_b64}"}
            })

        return [{"role": "user", "content": content}]

    @staticmethod
    def build_full_video_voiceover_script_messages_model_gateway(
        contexts: List[VoiceoverSceneContext],
        *,
        duration: float,
        language: str = "en",
        model_name: str
    ) -> List[Dict]:

        # Construct Prompt Formatting with Language, model name and video durations
        prompt = VoiceoverPrompt().format_full_model_gateway_instructions(
            duration_hint=duration,
            language=language,
            model_name=model_name

        )
        content: List[Dict] = [{"type": "text", "text": prompt}]

        for i, ctx in enumerate(contexts, start=1):
            content.append({"type": "text", "text": f"Key frame {i}"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{ctx.first_frame_b64}"},
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{ctx.last_frame_b64}"},
                }
            )

        return [{"role": "user", "content": content}]


class PromptBuilder:
    """
    Builds ModelGateway/Azure message payloads by reusing prompt templates from the PromptFactory class.
    """

    @staticmethod
    def build_image_music_messages(image_b64: str, *, mime_type: str = "image/jpeg") -> List[Dict]:
        prompt = Prompt().image_to_music
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime_type};base64,{image_b64}"}},
                ],
            }
        ]

    @staticmethod
    def build_multi_image_music_messages(
        image_payloads: List[Tuple[str, str]],
        *,
        user_prompt: str = "",
        preferred_output_language: str = "English",
        include_vocals: bool = False,
        preferred_lyric_language: str = "",
        user_lyrics_prompt: str = "",
        fixed_image_order: bool = False,
    ) -> List[Dict]:
        if not image_payloads:
            raise EdennValidationError("image_payloads must contain at least one image.", component="prompt_building", operation="build_multi_image_messages")

        prompt = Prompt().image_sequence_to_music
        prompt += f"\nPreferred output language for title/description/story text: {preferred_output_language}."
        if fixed_image_order:
            prompt += (
                "\nThe image order is FIXED by the user and must not be changed:"
                " plan the narrative, sections, and music arc following the images"
                " exactly in the order given, and return image_order as the"
                " identity sequence [1, 2, ...]."
            )
        if user_prompt.strip():
            prompt += f"\nUser prompt guidance: {user_prompt.strip()}"
        if include_vocals:
            lyric_language = preferred_lyric_language.strip() or preferred_output_language
            prompt += (
                "\nVocals are requested."
                f"\nWrite lyric_lines in {lyric_language}."
            )
            if user_lyrics_prompt.strip():
                prompt += (
                    "\nThe user provided explicit lyric direction; honor its theme,"
                    " tone, and any requested phrases when writing lyric_lines:"
                    f"\n{user_lyrics_prompt.strip()}"
                )
        else:
            prompt += "\nVocals are not required unless clearly justified by the user guidance."
        content: List[Dict] = [{"type": "text", "text": prompt}]
        for idx, (mime_type, image_b64) in enumerate(image_payloads, start=1):
            safe_mime = mime_type or "image/png"
            data_url = f"data:{safe_mime};base64,{image_b64}"
            content.append({"type": "text", "text": f"Frame {idx}"})
            content.append(
                {"type": "image_url", "image_url": {"url": data_url}})

        return [
            {
                "role": "user",
                "content": content,
            }
        ]

    @staticmethod
    def build_image_batch_visual_analysis_messages(image_urls: List[str]) -> List[Dict]:
        if not image_urls:
            raise EdennValidationError("image_urls must contain at least one image URL.", component="prompt_building", operation="build_image_analysis_messages")

        prompt = Prompt().image_batch_visual_analysis
        content: List[Dict] = [{"type": "text", "text": prompt}]
        for idx, image_url in enumerate(image_urls, start=1):
            content.append({"type": "text", "text": f"Creative {idx}"})
            content.append({"type": "image_url", "image_url": {"url": image_url}})

        return [{"role": "user", "content": content}]

    @staticmethod
    def build_scene_understanding_messages(
        *,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        scene_index: int,
        start_time: float,
        end_time: float,
        language: str = Language.EN,
    ) -> List[Dict]:
        if not first_frame_url and not last_frame_url:
            raise ValueError("At least one scene frame URL is required.")

        prompt = Prompt().format_scene_understanding(
            scene_index=scene_index,
            start_time=start_time,
            end_time=end_time,
            language=language,
        )
        language_name = Prompt()._normalize_language_name(language)

        content = [{"type": "text", "text": prompt}]
        if first_frame_url:
            content.append({"type": "image_url", "image_url": {
                "url": first_frame_url}})
        if last_frame_url:
            content.append({"type": "image_url", "image_url": {
                "url": last_frame_url}})

        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Write every JSON string field in {language_name}. "
                            "If visual text appears in another language, translate or summarize it into the requested language. "
                            "Do not name copyrighted/trademarked characters, franchises, studios, logos, or brands. "
                            "Return JSON only."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": content,
            }
        ]

    @staticmethod
    def build_video_summary_messages(
        scenes: List[SceneUnderstanding],
        *,
        language: str = Language.EN,
    ) -> List[Dict]:
        scene_lines = format_scene_lines(scenes)
        prompt = Prompt().format_video_summary(
            scene_lines=scene_lines, language=language)
        language_name = Prompt()._normalize_language_name(language)

        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Write every JSON string field in {language_name}. "
                            "Do not mirror multilingual input; normalize it into the requested language. "
                            "Return JSON only."
                        ),
                    }
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ]

    @staticmethod
    def build_provider_a_music_prompt_messages(
        scenes: List[SceneUnderstanding],
        *,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str,
    ) -> List[Dict]:
        scene_lines = format_music_safe_scene_lines(scenes)
        prompt = Prompt().format_video_music(
            scene_lines=scene_lines,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )
        return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    @staticmethod
    def build_provider_d_music_prompt_messages(
        scenes: List[SceneUnderstanding],
        *,
        include_vocals: bool = True,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str,
    ) -> List[Dict]:
        return PromptBuilder._build_dual_prompt_messages(
            provider_label="ProviderD",
            scenes=scenes,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )

    @staticmethod
    def build_provider_b_music_prompt_messages(
        scenes: List[SceneUnderstanding],
        *,
        include_vocals: bool = True,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> List[Dict]:
        return PromptBuilder._build_dual_prompt_messages(
            provider_label="ProviderB",
            scenes=scenes,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )

    @staticmethod
    def build_verbose_dual_prompt_messages(
        scenes: List[SceneUnderstanding],
        *,
        provider_label: str,
        include_vocals: bool,
        vocal_gender: str,
        music_style_prompt: Optional[str] = None,
        lyrics_prompt: Optional[str] = None,
        language: str = Language.EN,
        video_summary: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        scene_lines = format_music_safe_scene_lines(scenes)
        video_summary_lines = PromptBuilder._format_music_safe_video_summary(
            video_summary
        )
        style_text = (music_style_prompt or "").strip()
        lyrics_text = lyrics_prompt.strip() if lyrics_prompt is not None else None
        if include_vocals and lyrics_text:
            lyrics_directive = (
                "- lyrics_prompt: preserve the user-provided lyric guidance as "
                "the primary instruction. Keep requested words, themes, and "
                "line ideas; do not contradict it.\n"
            )
        elif include_vocals:
            lyrics_directive = (
                "- lyrics_prompt: create a concise lyric-generation instruction "
                "from the video arc, detected vocal language, and music style. "
                "Do not write final lyrics.\n"
            )
        else:
            lyrics_directive = (
                "- lyrics_prompt: return an empty string because the request is "
                "instrumental/no-lyrics.\n"
            )

        if style_text:
            style_directive = (
                "- The caller's music request below is the highest-priority "
                "guidance for style, mood, instrumentation, and vocals.\n"
            )
        else:
            style_directive = (
                "- No caller style guidance was provided: derive the full music "
                "style from the video summary and scene list.\n"
            )

        prompt_text = (
            f"You are writing {provider_label} music inputs from structured caller guidance.\n"
            "Return JSON only with keys: style_prompt, lyrics_prompt.\n"
            f"- All fields must be in {language}.\n"
            f"{style_directive}"
            "- Use the video summary and scene list to adapt the style_prompt to visual mood, pacing, duration, and narrative arc.\n"
            "- Do not copy scene descriptions verbatim; turn them into production-ready music direction.\n"
            f"{MUSIC_PROMPT_IP_SAFETY_DIRECTIVE}"
            f"- Vocal requirement: {'vocals requested' if include_vocals else 'instrumental/no lyrics'}; vocal gender: {vocal_gender}.\n"
            "- style_prompt: concise genre, mood, instrumentation, tempo, arrangement, language/vocal requirements, and video pacing guidance.\n"
            f"{lyrics_directive}"
            f"Caller music request (style guidance):\n{style_text or 'Not provided.'}\n\n"
            f"Caller lyrics_prompt:\n{lyrics_text if lyrics_text is not None else 'Not provided.'}\n\n"
            f"Video summary:\n{video_summary_lines}\n\n"
            f"Scene list:\n{scene_lines}\n"
        )
        return [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}]

    @staticmethod
    def _format_video_summary(video_summary: Optional[Dict[str, Any]]) -> str:
        if not video_summary:
            return "No separate video summary provided."

        lines: list[str] = []
        for key, value in video_summary.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                text = str(value).strip()
                if text:
                    lines.append(f"{key}: {text}")
        return "\n".join(lines) or "No separate video summary provided."

    @staticmethod
    def _format_music_safe_video_summary(video_summary: Optional[Dict[str, Any]]) -> str:
        if not video_summary:
            return "No separate video summary provided."

        lines: list[str] = []
        for key in ("overall_mood", "core_message", "has_explicit_call_to_action"):
            value = video_summary.get(key)
            if value is None:
                continue
            text = _compact_music_context_text(value)
            if text:
                lines.append(f"{key}: {text}")
        return "\n".join(lines) or "No separate video summary provided."

    @staticmethod
    def _build_dual_prompt_messages(
        *,
        provider_label: str,
        scenes: List[SceneUnderstanding],
        include_vocals: bool,
        vocal_gender: str,
        user_prompt: str,
        language: str,
    ) -> List[Dict]:
        """
        Dual-prompt providers (style_prompt + lyrics_prompt) with explicit language control.
        """
        scene_lines = format_music_safe_scene_lines(scenes)
        if include_vocals:
            vocal_directive = f"{language} {vocal_gender} vocals, crisp diction."
            lyrics_directive = "concise theme instruction for the provider's lyrics_generation API."
        else:
            vocal_directive = "instrumental/no lyrics; do not include vocals or vocal chops."
            lyrics_directive = "empty string because this is an instrumental/no-lyrics request."
        prompt_text = (
            f"You are writing {provider_label} music inputs (style_prompt + lyrics_prompt) from the scene context.\n"
            "Return JSON only with keys: style_prompt, lyrics_prompt.\n"
            f"- Both Fields must output in {language}.\n"
            f"- Vocal requirement: {vocal_directive}\n"
            "- style_prompt: [Vocal directives/Requirements] concise music style/mood/instrument/tempo description. must less than 1024 characters\n"
            f"- lyrics_prompt: {lyrics_directive}\n"
            "- Do NOT write final lyrics.\n"
            f"{MUSIC_PROMPT_IP_SAFETY_DIRECTIVE}"
            f"- User style hint: {user_prompt}\n"
            "Use the scene list for mood/arc context:\n"
            f"{scene_lines}\n"
        )

        return [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}]

    @staticmethod
    def build_provider_c_custom_lyrics_messages(
        scenes: List[SceneUnderstanding],
        *,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> List[Dict]:
        scene_lines = format_music_safe_scene_lines(scenes)
        prompt = Prompt().format_provider_c_custom_lyrics(
            scene_lines=scene_lines,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )
        return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    @staticmethod
    def build_provider_c_simple_prompt_messages(
        scenes: List[SceneUnderstanding],
        *,
        include_vocals: bool = False,
        vocal_gender: str = "female",
        user_prompt: str = "",
        language: str = Language.EN,
    ) -> List[Dict]:
        scene_lines = format_music_safe_scene_lines(scenes)
        prompt = Prompt().format_provider_c_simple_prompt(
            scene_lines=scene_lines,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )
        return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    @staticmethod
    def build_audio_creative_edit_messages(
        *,
        visual_context: str,
        visual_input_type: str,
        include_vocals: bool,
        vocal_gender: str,
        user_prompt: str,
        language: str = Language.EN,
    ) -> List[Dict]:
        prompt = Prompt().format_audio_creative_edit(
            visual_context=visual_context,
            visual_input_type=visual_input_type,
            include_vocals=include_vocals,
            vocal_gender=vocal_gender,
            user_prompt=user_prompt,
            language=language,
        )
        return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]


# =========================
# Helpers
# =========================


def format_scene_lines(scenes: List[SceneUnderstanding]) -> str:
    """
    Formats SceneUnderstanding objects into readable lines for downstream prompts.

    Example output:
    - Scene 0 (0.00s–2.50s): A woman pours coffee at a kitchen counter. | Actions: pouring, smiling | Mood: warm
    """
    lines = []
    for s in scenes:
        line = (
            f"- Scene {s.scene_index} "
            f"({s.start_timestamp:.2f}s–{s.end_timestamp:.2f}s): "
            f"{s.visual_summary} "
            f"| Actions: {s.key_actions} "
            f"| Mood: {s.mood}"
        )
        lines.append(line)
    return "\n".join(lines)


def _compact_music_context_text(value: Any, *, max_chars: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(".,;:") or text[:max_chars]


def format_music_safe_scene_lines(scenes: List[SceneUnderstanding]) -> str:
    """
    Format scene context for music prompt generation without forwarding visual
    subject details that can trigger provider IP/copyright filters.
    """
    lines = []
    for scene in scenes:
        start = float(getattr(scene, "start_timestamp", 0.0) or 0.0)
        end = float(getattr(scene, "end_timestamp", start) or start)
        duration = max(0.0, end - start)
        mood = _compact_music_context_text(getattr(scene, "mood", ""))
        if not mood:
            mood = "neutral"
        line = (
            f"- Scene {getattr(scene, 'scene_index', len(lines))} "
            f"({start:.2f}s-{end:.2f}s, {duration:.1f}s): "
            f"music mood={mood}"
        )
        lines.append(line)
    return "\n".join(lines)


@dataclass
class VoiceoverSceneContext:
    scene_index: int
    start_sec: float
    end_sec: float
    first_frame_b64: str
    last_frame_b64: str
