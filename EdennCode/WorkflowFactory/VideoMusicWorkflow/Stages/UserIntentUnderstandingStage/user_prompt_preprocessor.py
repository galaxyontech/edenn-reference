"""
User Prompt Preprocessor Agent for VideoMusicWorkflow.

Combines TOS compliance checking and user intent extraction (language, video category)
in a single LLM call. This agent should run at the start of the workflow before
any other processing stages.

Features:
- TOS Compliance: Transforms celebrity voice references into voice characteristics
- Language Detection: Detects if user wants Chinese or English content
- Category Detection: Identifies video category (advertisement, vlog, etc.)
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClient,
)
from EdennCode.WorkflowFactory.VideoMusicWorkflow.PipelineDataModel.pipeline_datamodel import (
    Language,
    VideoCategory,
)
from EdennCode.exceptions import EdennValidationError
from EdennCode.Annotation.core.annotation_dispatcher import (
    AnnotationDispatcher,
    safe_emit_annotation,
)
from EdennCode.Annotation.events.request_context_event import RequestContextEvent

logger = logging.getLogger(__name__)

DEFAULT_PREPROCESSOR_MODEL = None

_VALID_VERBOSE_MODELSPECS = {"edenn_enhanced", "edenn_studio"}
_CHINESE_LANGUAGE_CODES = {"CN", "ZH", "ZH_CN", "CHINESE", "CHINESE_MAINLAND", "MANDARIN"}

# High-precision, deterministic vocal-intent signals. A stated VOCAL GENDER only
# makes sense when the user wants vocals, so it is treated as an unambiguous
# vocal request. This is the safety net for the tag-style prompts the frontend
# emits (e.g. "[vocal_gender: female][language: english]"): the LLM sometimes
# extracts the gender yet still returns include_vocals=false, silently
# downgrading a clear vocal request to instrumental. Deliberately narrow so it
# never fires on negations ("no vocals", "instrumental") — those, and generic
# "sing/lyrics/vocals" wording, are left to the LLM (which handles negation).
_EXPLICIT_VOCAL_GENDER_RE = re.compile(
    r"(?i)(?:vocal[_\s-]?gender\s*[:=]\s*(?:male|female)"
    r"|(?<![a-z])(?:male|female)\s+(?:lead\s+)?(?:vocal|vocals|voice|singer|vocalist))"
)


def prompt_declares_vocal_gender(prompt: Optional[str]) -> bool:
    """True when the prompt explicitly declares a vocal gender.

    A vocal gender is only meaningful with vocals, so its presence is an
    unambiguous vocal request — used to keep such requests from being
    downgraded to instrumental by an uncertain LLM inference.
    """
    return bool(_EXPLICIT_VOCAL_GENDER_RE.search(prompt or ""))


# Fallback extractors for the frontend tag convention, used ONLY to fill a
# gender/language the LLM left blank on a request we forced to vocals — so a
# corrected vocal job is never left without a vocalist gender or lyrics language.
_TAG_VOCAL_GENDER_RE = re.compile(r"(?i)vocal[_\s-]?gender\s*[:=]\s*(male|female)")
_TAG_LANGUAGE_RE = re.compile(r"(?i)(?:vocal[_\s-]?)?language\s*[:=]\s*([A-Za-z][A-Za-z _\-]{1,29})")


def parse_prompt_vocal_gender(prompt: Optional[str]) -> Optional[str]:
    match = _TAG_VOCAL_GENDER_RE.search(prompt or "")
    return match.group(1).lower() if match else None


def parse_prompt_language(prompt: Optional[str]) -> Optional[str]:
    match = _TAG_LANGUAGE_RE.search(prompt or "")
    return match.group(1).strip() if match else None


def resolve_effective_include_vocals(
    *,
    detected_include_vocals: bool,
    user_prompt: Optional[str],
) -> bool:
    """Final vocal decision: the LLM inference, corrected when the PROMPT
    explicitly declares a vocal gender (a gender presupposes a vocalist).

    Only the raw-prompt declaration is used as an override — NOT the model's
    ``detected_vocal_gender`` field, which defaults to "Female" and would
    otherwise force vocals onto genuinely instrumental jobs. The regex is
    high-precision and never matches negations ("no vocals", "instrumental").
    Never turns vocals OFF."""
    if detected_include_vocals:
        return True
    if prompt_declares_vocal_gender(user_prompt):
        return True
    return False


# =============================================================================
# System Prompt - Combined TOS + Intent Extraction
# =============================================================================

PREPROCESSOR_SYSTEM_PROMPT = """You preprocess user prompts for a video-to-music system.
Return ONLY JSON matching the provided schema.

Hard rules:
- transformed_prompt MUST ALWAYS be English. If input is not English, translate intent to English.
- TOS compliance: if the user requests a real person's voice/vocals/narration (any language),
  remove the person's name and replace it with neutral voice characteristics (pitch/timbre/tone/pacing/energy).
  If ambiguous whether it's a voice imitation request, sanitize it.
  If a person is mentioned ONLY as non-voice music/style inspiration (e.g., composer/director vibe), keep it.
- was_transformed = true if you sanitized any real-person voice request.
- detected_references = list of the person names you sanitized (empty if none).
- detected_include_vocals: infer whether the user wants vocals/lyrics (true/false) base on the user prompt.
  A stated vocal gender (e.g. "vocal_gender: female", "male vocal") or a requested vocal/lyrics language
  MEANS the user wants vocals — detected_include_vocals MUST be true in that case. Only return false when the
  prompt clearly wants instrumental/no vocals or gives no vocal signal at all.
- detected_vocal_gender: infer from the user prompt; return "male", "female", or "unknown".
- detected_vocal_language: infer requested vocal/lyrics language; leave empty if no vocal intent. Return in that language.
- detected_language: infer requested general language for titles/description; return directly (no hardcoding).
- detected_category: choose one of ADVERTISEMENT, VLOG, CREATOR_CONTENT, VIDEO (default VIDEO).
- reasoning <= 2 sentences.

Examples:
1) Input: 我想要中文歌曲，像周杰伦那样的声音
   transformed_prompt: I want Chinese songs with a smooth, melodic male vocal tone and light R&B styling.
   detected_language: CHINESE_MAINLAND, was_transformed: true, detected_references: [周杰伦]
2) Input: Make an ad with Morgan Freeman voice
   transformed_prompt: Make a brand advertisement with a deep, resonant, warm baritone narration and calm pacing.
   detected_language: ENGLISH_US, was_transformed: true, detected_references: [Morgan Freeman]
3) Input: [vocal_gender: female]\n[language: english]
   transformed_prompt: Create a song with clear female English vocals that fit the video.
   detected_include_vocals: true, detected_vocal_gender: female, detected_vocal_language: ENGLISH_US, detected_language: ENGLISH_US
"""


LYRICS_PREPROCESSOR_SYSTEM_PROMPT = """You preprocess lyrics guidance for a video-to-music system.
Return ONLY JSON matching the provided schema.

Hard rules:
- transformed_prompt MUST preserve the user's original language. Do not translate lyrics guidance.
- Preserve exact requested words, phrases, lyric lines, rhyme ideas, and line-order constraints.
- TOS compliance: if the user requests a real person's voice/vocals/narration (any language),
  remove the person's name and replace only that unsafe voice-imitation portion with neutral voice
  characteristics in the same language as the input. Preserve all other lyric content.
- was_transformed = true if you sanitized any real-person voice request.
- detected_references = list of the person names you sanitized (empty if none).
- detected_include_vocals MUST be true for any non-empty lyrics guidance.
- detected_vocal_language: infer the lyrics/vocal language from the lyrics guidance.
- detected_vocal_gender: infer from the lyrics guidance; return "male", "female", or "unknown".
- detected_language: infer requested general language for generated user-facing content.
- detected_category: choose one of ADVERTISEMENT, VLOG, CREATOR_CONTENT, VIDEO (default VIDEO).
- reasoning <= 2 sentences.

Examples:
1) Input: 副歌必须包含：海风吹过我们身旁
   transformed_prompt: 副歌必须包含：海风吹过我们身旁
   detected_vocal_language: CHINESE_MAINLAND
2) Input: El coro debe decir: brilla mi corazón
   transformed_prompt: El coro debe decir: brilla mi corazón
   detected_vocal_language: SPANISH
"""


# =============================================================================
# JSON Schema for LLM Structured Output
# =============================================================================

PREPROCESSOR_JSON_SCHEMA = {
    "name": "user_prompt_preprocessor_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "was_transformed": {
                "type": "boolean",
                "description": "Whether any celebrity voice references were detected and transformed",
            },
            "transformed_prompt": {
                "type": "string",
                "description": "The sanitized user prompt (original if no transformation needed)",
            },
            "detected_references": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of detected celebrity/public figure voice references",
            },
            "detected_language": {
                "type": "string",
                "description": "Detected language preference for generated content",
            },
            "detected_vocal_language": {
                "type": "string",
                "description": "Detected language preference for vocals/lyrics ( empty if no vocal intent)",
            },
            "detected_category": {
                "type": "string",
                "enum": [
                    VideoCategory.ADVERTISEMENT,
                    VideoCategory.VLOG,
                    VideoCategory.CREATOR_CONTENT,
                    VideoCategory.DEFAULT,
                ],
                "description": "Detected video category type",
            },
            "detected_vocal_gender": {
                "type": "string",
                "enum": ["male", "female", "unknown"],
                "description": "Vocal gender preference inferred from the prompt (male/female/unknown).",
            },
            "detected_include_vocals": {
                "type": "boolean",
                "description": "Whether the user wants vocals/lyrics.",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of analysis (<= 2 sentences)",
            },
        },
        "required": [
            "was_transformed",
            "transformed_prompt",
            "detected_references",
            "detected_language",
            "detected_vocal_language",
            "detected_category",
            "detected_vocal_gender",
            "detected_include_vocals",
            "reasoning",
        ],
        "additionalProperties": False,
    },
}


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class UserPromptPreprocessorResult:
    """Combined TOS + Intent extraction result from a single LLM call."""

    was_transformed: bool
    detected_include_vocals: bool
    transformed_prompt: str = ""
    detected_references: List[str] = field(default_factory=list)

    detected_language: str = Language.EN
    detected_category: str = VideoCategory.DEFAULT
    detected_vocal_gender: str = "Female"
    detected_vocal_language: str = ""

    reasoning: str = ""
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class UserPromptPreprocessingStageInput:
    """Input contract for the full preprocessing stage (Stage 0)."""
    user_prompt: Optional[str]
    verbose_instruction: bool
    music_style_prompt: Optional[str]
    lyrics_prompt: Optional[str]
    music_model_spec: str
    job_id: str = ""
    annotation_dispatcher: Optional[AnnotationDispatcher] = None


@dataclass
class UserPromptPreprocessingStageOutput:
    """
    Output of Stage 0.  Everything the downstream workflow needs from preprocessing.

    sanitized_prompt:
        Unified prompt forwarded to VideoUnderstanding and prompt orchestration.
        In verbose mode this is the formatted "Music style prompt / Lyrics prompt" block.
    sanitized_style_prompt / sanitized_lyrics_prompt:
        Non-None only in verbose mode; used by PromptOrchestration to pass the
        prompts separately rather than as a combined string.
    original_prompt_for_event:
        Raw caller input before sanitisation; used for annotation only.
    effective_modelspec:
        Canonically normalised modelspec string, possibly overridden for Chinese vocals.
    analysis_language:
        Always Language.EN — internal scene/video analysis is English-only.
    """
    sanitized_prompt: str
    sanitized_style_prompt: Optional[str]
    sanitized_lyrics_prompt: Optional[str]
    original_prompt_for_event: str
    effective_include_vocals: bool
    effective_vocal_gender: str
    effective_language: str
    analysis_language: str
    effective_modelspec: str
    detected_language: str
    detected_vocal_language: str
    detected_category: str
    was_transformed: bool
    detected_references: List[str]
    prompt_tokens: int
    completion_tokens: int
    tokens_used: int


# =============================================================================
# Module-level helpers (used only within this module)
# =============================================================================

def _first_text(*values: Optional[str]) -> str:
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


def _first_known_gender(*values: Optional[str]) -> str:
    for value in values:
        normalized = (value or "").strip().lower()
        if normalized and normalized != "unknown":
            return normalized
    return "unknown"


def _is_chinese_language(value: Optional[str]) -> bool:
    return (value or "").strip().upper().replace("-", "_") in _CHINESE_LANGUAGE_CODES


def _verbose_instruction_prompt(
    music_style_prompt: str,
    lyrics_prompt: Optional[str],
) -> str:
    sections = []
    style_text = (music_style_prompt or "").strip()
    if style_text:
        sections.append(f"Music style prompt:\n{style_text}")
    lyrics_text = (lyrics_prompt or "").strip()
    if lyrics_text:
        sections.append(f"Lyrics prompt:\n{lyrics_text}")
    return "\n\n".join(sections)


def _merge_verbose_preprocessor_results(
    *,
    style_result: Optional[UserPromptPreprocessorResult],
    lyrics_result: Optional[UserPromptPreprocessorResult],
    sanitized_style_prompt: str,
    sanitized_lyrics_prompt: str,
) -> UserPromptPreprocessorResult:
    """Merge the style- and lyric-pass results into one detection result.

    Either pass may be absent: lyric-only requests run no style pass (a stub
    style result would otherwise mask the lyric pass's detections — vocal
    gender, language, category — with its dataclass defaults).
    """
    detected_references: list[str] = []
    for result in (style_result, lyrics_result):
        if result is None:
            continue
        for reference in result.detected_references:
            if reference not in detected_references:
                detected_references.append(reference)

    lyrics_requested = bool(sanitized_lyrics_prompt.strip())
    detected_include_vocals = (
        bool(style_result and style_result.detected_include_vocals)
        or lyrics_requested
        or bool(lyrics_result and lyrics_result.detected_include_vocals)
    )
    detected_vocal_language = _first_text(
        getattr(lyrics_result, "detected_vocal_language", None),
        getattr(style_result, "detected_vocal_language", None),
        getattr(lyrics_result, "detected_language", None),
    )
    detected_language = _first_text(
        getattr(style_result, "detected_language", None),
        getattr(lyrics_result, "detected_language", None),
        Language.EN,
    )
    detected_vocal_gender = _first_known_gender(
        getattr(style_result, "detected_vocal_gender", None),
        getattr(lyrics_result, "detected_vocal_gender", None),
    )
    detected_category = (
        style_result.detected_category
        if style_result is not None
        else (
            lyrics_result.detected_category
            if lyrics_result is not None
            else VideoCategory.DEFAULT
        )
    )

    return UserPromptPreprocessorResult(
        was_transformed=(
            bool(style_result and style_result.was_transformed)
            or bool(lyrics_result and lyrics_result.was_transformed)
        ),
        detected_include_vocals=detected_include_vocals,
        transformed_prompt=_verbose_instruction_prompt(
            sanitized_style_prompt,
            sanitized_lyrics_prompt,
        ),
        detected_references=detected_references,
        detected_language=detected_language,
        detected_category=detected_category,
        detected_vocal_gender=detected_vocal_gender,
        detected_vocal_language=detected_vocal_language,
        reasoning=_first_text(
            getattr(style_result, "reasoning", None),
            getattr(lyrics_result, "reasoning", None),
        ),
        tokens_used=(
            int(getattr(style_result, "tokens_used", 0) or 0)
            + int(getattr(lyrics_result, "tokens_used", 0) or 0)
        ),
        prompt_tokens=(
            int(getattr(style_result, "prompt_tokens", 0) or 0)
            + int(getattr(lyrics_result, "prompt_tokens", 0) or 0)
        ),
        completion_tokens=(
            int(getattr(style_result, "completion_tokens", 0) or 0)
            + int(getattr(lyrics_result, "completion_tokens", 0) or 0)
        ),
    )


def _resolve_effective_modelspec(
    *,
    music_model_spec: str,
    include_vocals: bool,
    effective_language: str,
) -> str:
    """Return the canonical modelspec, overriding to edenn_enhanced for Chinese vocals on edenn_basic."""
    if (
        include_vocals
        and _is_chinese_language(effective_language)
        and music_model_spec == "edenn_basic"
    ):
        return "edenn_enhanced"
    return music_model_spec


# =============================================================================
# Agent Class
# =============================================================================

class UserPromptPreprocessorAgent:
    """
    Combined TOS compliance and user intent extraction agent.

    Exposes two interfaces:
    - ``preprocess(user_prompt)``: low-level single LLM call, used internally.
    - ``run(stage_input)``: high-level Stage 0 entry point consumed by the workflow.
      Handles verbose mode branching, merging, language routing, and modelspec
      overrides — so the workflow itself stays at stage-orchestration level.
    """

    def __init__(
        self,
        llm_client: Optional[AzureMultimodalClient] = None,
        model: Optional[str] = None,
        max_retries: int = 2,
    ):
        self._llm_client = llm_client
        self.model = model or os.getenv(
            "PREPROCESSOR_MODEL") or DEFAULT_PREPROCESSOR_MODEL
        self.max_retries = max_retries
        self.total_tokens_used = 0

    @property
    def llm_client(self) -> AzureMultimodalClient:
        if self._llm_client is None:
            from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
            self._llm_client = build_azure_client()
        return self._llm_client

    async def run(
        self, stage_input: UserPromptPreprocessingStageInput
    ) -> UserPromptPreprocessingStageOutput:
        """
        Stage 0 entry point.

        Validates inputs, runs one or two LLM preprocessor calls (verbose mode
        runs style and lyrics separately), merges results, resolves language and
        modelspec overrides, and returns a fully-resolved output ready for the
        workflow to pass into downstream stages.
        """
        _stage_start = time.time()
        if stage_input.verbose_instruction:
            raw_style = (stage_input.music_style_prompt or "").strip()
            raw_lyrics = (stage_input.lyrics_prompt or "").strip()

            if (stage_input.user_prompt or "").strip():
                raise EdennValidationError(
                    "user_prompt must be empty when verbose_instruction is enabled.",
                    public_message=(
                        "When verbose_instruction=true, omit user_prompt and "
                        "provide music_style_prompt."
                    ),
                    component="video_music",
                    operation="generate",
                )
            # A lyric direction alone is a complete verbose request: the dual
            # template derives style from the video when the style slot is
            # empty. Only a fully empty verbose request is invalid.
            if not raw_style and not raw_lyrics:
                raise EdennValidationError(
                    "music_style_prompt or lyrics_prompt is required when verbose_instruction is enabled.",
                    public_message=(
                        "Provide music direction: music_style_prompt or "
                        "lyrics_prompt is required for an explicit-direction request."
                    ),
                    component="video_music",
                    operation="generate",
                )
            if stage_input.music_model_spec not in _VALID_VERBOSE_MODELSPECS:
                raise EdennValidationError(
                    "explicit style/lyric direction is only supported for edenn_enhanced or edenn_studio requests.",
                    public_message=(
                        "Explicit music direction (music_style_prompt / "
                        "lyrics_prompt) is not supported by this model "
                        "specification. Set modelspec=edenn_enhanced or "
                        "modelspec=edenn_studio."
                    ),
                    component="video_music",
                    operation="generate",
                )

            # Lyric-only requests skip the style pass entirely: running the
            # preprocessor on an empty string would return a stub whose
            # dataclass defaults (gender, language, category) mask the lyric
            # pass's real detections in the merge.
            style_result = await self.preprocess(raw_style) if raw_style else None
            lyrics_result = (
                await self.preprocess_lyrics_prompt(raw_lyrics)
                if raw_lyrics
                else None
            )

            sanitized_style = style_result.transformed_prompt if style_result else ""
            # An empty LLM transform must not drop a paid lyric direction:
            # fall back to the raw text (parity with the multi-image path).
            sanitized_lyrics = (
                ((lyrics_result.transformed_prompt or "").strip() or raw_lyrics)
                if lyrics_result
                else ""
            )

            merged = _merge_verbose_preprocessor_results(
                style_result=style_result,
                lyrics_result=lyrics_result,
                sanitized_style_prompt=sanitized_style,
                sanitized_lyrics_prompt=sanitized_lyrics,
            )

            original_prompt_for_event = _verbose_instruction_prompt(raw_style, raw_lyrics)
            sanitized_style_prompt: Optional[str] = sanitized_style
            sanitized_lyrics_prompt: Optional[str] = sanitized_lyrics
            preprocessor_result = merged

        else:
            preprocessor_result = await self.preprocess(stage_input.user_prompt)
            original_prompt_for_event = stage_input.user_prompt
            sanitized_style_prompt = None
            sanitized_lyrics_prompt = None

        # A stated vocal gender (extracted by the model, or explicit in the
        # prompt) presupposes vocals; correct a false inference so an explicit
        # vocal request is never silently rendered instrumental. The verbose
        # path already forces vocals from lyrics guidance, so this mainly guards
        # the plain user_prompt path.
        effective_include_vocals = resolve_effective_include_vocals(
            detected_include_vocals=preprocessor_result.detected_include_vocals,
            user_prompt=stage_input.user_prompt,
        )
        # True only when the raw-prompt override flipped the model's instrumental
        # inference — the case where the model's own gender/language fields are
        # untrustworthy and must be backfilled from the prompt's explicit tags.
        forced_vocals = (
            effective_include_vocals and not preprocessor_result.detected_include_vocals
        )
        effective_vocal_gender = preprocessor_result.detected_vocal_gender
        effective_language = (
            preprocessor_result.detected_vocal_language
            if effective_include_vocals
            else preprocessor_result.detected_language
        )
        # On a corrected job, the prompt's explicit tags are authoritative over
        # the model's defaulted/blank fields, so a corrected vocal job is never
        # left with the wrong or a missing gender/language.
        if forced_vocals:
            prompt_gender = parse_prompt_vocal_gender(stage_input.user_prompt)
            if prompt_gender:
                effective_vocal_gender = prompt_gender
            if not (effective_language or "").strip():
                effective_language = (
                    parse_prompt_language(stage_input.user_prompt)
                    or preprocessor_result.detected_language
                )

        effective_modelspec = _resolve_effective_modelspec(
            music_model_spec=stage_input.music_model_spec,
            include_vocals=effective_include_vocals,
            effective_language=effective_language,
        )

        output = UserPromptPreprocessingStageOutput(
            sanitized_prompt=preprocessor_result.transformed_prompt,
            sanitized_style_prompt=sanitized_style_prompt,
            sanitized_lyrics_prompt=sanitized_lyrics_prompt,
            original_prompt_for_event=original_prompt_for_event,
            effective_include_vocals=effective_include_vocals,
            effective_vocal_gender=effective_vocal_gender,
            effective_language=effective_language,
            analysis_language=Language.EN,
            effective_modelspec=effective_modelspec,
            detected_language=preprocessor_result.detected_language,
            detected_vocal_language=preprocessor_result.detected_vocal_language,
            detected_category=preprocessor_result.detected_category,
            was_transformed=preprocessor_result.was_transformed,
            detected_references=list(preprocessor_result.detected_references),
            prompt_tokens=int(preprocessor_result.prompt_tokens or 0),
            completion_tokens=int(preprocessor_result.completion_tokens or 0),
            tokens_used=int(preprocessor_result.tokens_used or 0),
        )
        safe_emit_annotation(
            stage_input.annotation_dispatcher,
            lambda: RequestContextEvent(
                job_id=stage_input.job_id,
                original_prompt=original_prompt_for_event,
                sanitized_prompt=output.sanitized_prompt,
                was_prompt_transformed=output.was_transformed,
                detected_references=output.detected_references,
                detected_language=output.detected_language,
                detected_vocal_language=output.detected_vocal_language,
                detected_category=output.detected_category,
                include_vocals=output.effective_include_vocals,
                vocal_gender=output.effective_vocal_gender,
                verbose_instruction=stage_input.verbose_instruction,
                stage_latency_s=time.time() - _stage_start,
            ),
        )
        return output

    async def preprocess(self, user_prompt: str) -> UserPromptPreprocessorResult:
        """Single LLM call for TOS compliance + intent extraction."""
        if not user_prompt or not user_prompt.strip():
            return UserPromptPreprocessorResult(
                was_transformed=False,
                transformed_prompt=user_prompt or "",
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                reasoning="Empty prompt - using defaults",
                detected_include_vocals=False,
            )

        logger.info(
            "[UserPromptPreprocessor] Processing prompt (%s): %s...",
            self.model,
            user_prompt[:50],
        )

        try:
            result = await self._call_llm(user_prompt)
            self.total_tokens_used += result.tokens_used
            if result.was_transformed:
                logger.info(
                    "[UserPromptPreprocessor] TOS transformed: %s → %s...",
                    result.detected_references,
                    result.transformed_prompt[:50],
                )
            logger.info(
                "[UserPromptPreprocessor] Detected: language=%s, category=%s",
                result.detected_language,
                result.detected_category,
            )
            return result
        except Exception as e:
            logger.error("[UserPromptPreprocessor] LLM call failed: %s", e)
            return UserPromptPreprocessorResult(
                was_transformed=False,
                transformed_prompt=user_prompt,
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                detected_include_vocals=False,
                reasoning=f"LLM call failed: {e}. Using defaults.",
            )

    async def _call_llm(self, user_prompt: str) -> UserPromptPreprocessorResult:
        return await self._call_llm_with_system_prompt(
            user_prompt=user_prompt,
            system_prompt=PREPROCESSOR_SYSTEM_PROMPT,
        )

    async def preprocess_lyrics_prompt(self, lyrics_prompt: str) -> UserPromptPreprocessorResult:
        """Sanitize verbose lyrics guidance without translating it."""
        if not lyrics_prompt or not lyrics_prompt.strip():
            return UserPromptPreprocessorResult(
                was_transformed=False,
                transformed_prompt=lyrics_prompt or "",
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                reasoning="Empty lyrics prompt - using defaults",
                detected_include_vocals=False,
            )

        logger.info(
            "[UserPromptPreprocessor] Processing lyrics prompt (%s): %s...",
            self.model,
            lyrics_prompt[:50],
        )

        try:
            result = await self._call_llm_with_system_prompt(
                user_prompt=lyrics_prompt,
                system_prompt=LYRICS_PREPROCESSOR_SYSTEM_PROMPT,
            )
            self.total_tokens_used += result.tokens_used
            result.detected_include_vocals = True
            if not result.detected_vocal_language:
                result.detected_vocal_language = result.detected_language
            return result
        except Exception as e:
            logger.error("[UserPromptPreprocessor] Lyrics LLM call failed: %s", e)
            return UserPromptPreprocessorResult(
                was_transformed=False,
                transformed_prompt=lyrics_prompt,
                detected_references=[],
                detected_language=Language.EN,
                detected_category=VideoCategory.DEFAULT,
                detected_include_vocals=True,
                reasoning=f"LLM call failed: {e}. Preserving original lyrics prompt.",
            )

    async def _call_llm_with_system_prompt(
        self,
        *,
        user_prompt: str,
        system_prompt: str,
    ) -> UserPromptPreprocessorResult:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User prompt to analyze:\n{user_prompt}"},
        ]

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response_data, usage = await self.llm_client.complete_messages(
                    messages=messages,
                    json_schema=PREPROCESSOR_JSON_SCHEMA,
                    max_tokens=600,
                )
                return UserPromptPreprocessorResult(
                    was_transformed=response_data.get("was_transformed", False),
                    transformed_prompt=response_data.get("transformed_prompt", user_prompt),
                    detected_references=response_data.get("detected_references", []),
                    detected_language=response_data.get("detected_language", Language.EN),
                    detected_category=response_data.get("detected_category", VideoCategory.DEFAULT),
                    detected_vocal_gender=response_data.get("detected_vocal_gender", "unknown"),
                    detected_vocal_language=response_data.get("detected_vocal_language", ""),
                    detected_include_vocals=bool(response_data.get("detected_include_vocals", False)),
                    reasoning=response_data.get("reasoning", ""),
                    tokens_used=usage.get("total_tokens", 0),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "[UserPromptPreprocessor] LLM call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    e,
                )

        raise last_error or Exception("LLM call failed after retries")


__all__ = [
    "UserPromptPreprocessorAgent",
    "UserPromptPreprocessorResult",
    "UserPromptPreprocessingStageInput",
    "UserPromptPreprocessingStageOutput",
    "PREPROCESSOR_SYSTEM_PROMPT",
    "LYRICS_PREPROCESSOR_SYSTEM_PROMPT",
    "PREPROCESSOR_JSON_SCHEMA",
]
