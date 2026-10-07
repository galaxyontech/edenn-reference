from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from EdennCode.MusicGenerationCore.models import MusicSection, NarrativeCue, SectionPlan
from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.exceptions import EdennProviderResponseError, EdennValidationError
from EdennCode.ModelFactory.PromptFactory.prompts import PromptBuilder, ResponseSchemas


@dataclass
class ImageSequencePlanningStageInput:
    image_paths: Sequence[Path]
    default_image_duration_s: float = 3.0
    user_prompt: str = ""
    preferred_output_language: str = "English"
    include_vocals: bool = False
    preferred_lyric_language: str = ""
    user_lyrics_prompt: str = ""
    # When True the user's upload order is authoritative: the planner is told
    # the order is fixed, and whatever it returns is overridden with the
    # identity order — the narrative is planned FOR the sequence, never a
    # reordering OF it.
    fixed_image_order: bool = False


@dataclass
class ImageSequencePlanningStageOutput:
    ordered_images: List[Path]
    plan: Dict[str, object]
    section_plan: SectionPlan
    # Token usage from the planning model call; empty on a cache hit (no call made).
    token_usage: Optional[Dict[str, Any]] = None


class ImageSequencePlanningStage:
    """Summarize images, decide a narrative order, and build a global music plan."""

    TITLE_MAX_WORDS = 8
    TITLE_MAX_CHARS = 60

    # Bumped when the plan prompt/schema changes so stale cached plans invalidate.
    PLAN_CACHE_VERSION = "miplan_v1"

    def __init__(
        self,
        *,
        llm_client: AzureMultimodalClient,
        plan_cache: Optional[Any] = None,
    ) -> None:
        self.llm_client = llm_client
        # Optional duck-typed cache: ``.get(key) -> Optional[dict]`` / ``.put(key, plan)``.
        # Stores the raw plan JSON inline (no SAS URLs), so it is safe to reuse — the
        # deterministic normalization below re-runs on every hit. Pure optimization:
        # any cache error degrades to a live Azure call.
        self.plan_cache = plan_cache

    @classmethod
    def _plan_cache_key(cls, image_payloads, stage_input: "ImageSequencePlanningStageInput") -> str:
        digest = hashlib.sha256()
        digest.update(f"{cls.PLAN_CACHE_VERSION}:".encode("utf-8"))
        for mime_type, image_b64 in image_payloads:
            digest.update(str(mime_type).encode("utf-8"))
            digest.update(b":")
            digest.update(hashlib.sha256(str(image_b64).encode("utf-8")).digest())
        for part in (
            stage_input.user_prompt or "",
            stage_input.preferred_output_language or "",
            str(bool(stage_input.include_vocals)),
            stage_input.preferred_lyric_language or "",
            stage_input.user_lyrics_prompt or "",
            str(bool(stage_input.fixed_image_order)),
        ):
            digest.update(b"|")
            digest.update(part.encode("utf-8"))
        # Literal functionality prefix keeps this key structurally disjoint from
        # other functionalities' cache keys in the shared async_v2_cache table
        # (mirrors video_music's "compress:"/"understanding:" prefixes).
        return f"multi_image_plan:{digest.hexdigest()}"

    async def run(self, stage_input: ImageSequencePlanningStageInput) -> ImageSequencePlanningStageOutput:
        normalized = self._normalize_paths(stage_input.image_paths)
        image_payloads = [self._encode_image(path) for path in normalized]
        messages = PromptBuilder.build_multi_image_music_messages(
            image_payloads,
            user_prompt=stage_input.user_prompt,
            preferred_output_language=stage_input.preferred_output_language,
            include_vocals=stage_input.include_vocals,
            preferred_lyric_language=stage_input.preferred_lyric_language,
            user_lyrics_prompt=stage_input.user_lyrics_prompt,
            fixed_image_order=stage_input.fixed_image_order,
        )
        schema = ResponseSchemas.image_sequence_music()

        plan = None
        # Cache hits make no model call, so their token usage is empty (0 cost).
        token_usage: Dict[str, Any] = {}
        cache_key: Optional[str] = None
        if self.plan_cache is not None:
            try:
                cache_key = self._plan_cache_key(image_payloads, stage_input)
                cached = self.plan_cache.get(cache_key)
                if isinstance(cached, dict) and cached:
                    plan = cached
            except Exception:
                plan = None  # cache is a pure optimization — never fail the plan on it
        if plan is None:
            result = await self.llm_client.complete_messages(messages, json_schema=schema)
            if isinstance(result, tuple):
                plan = result[0]
                token_usage = result[1] if len(result) > 1 and isinstance(result[1], dict) else {}
            else:
                plan = result
            if not plan:
                raise EdennProviderResponseError(
                    "The planning model did not return a plan for the provided image batch.",
                    component="image_sequence_planning",
                    operation="run",
                )
            if self.plan_cache is not None and cache_key is not None:
                try:
                    self.plan_cache.put(cache_key, dict(plan))
                except Exception:
                    pass
        if stage_input.fixed_image_order:
            # The user's upload order is authoritative — deterministic override,
            # regardless of what the planner returned.
            resolved_order = list(range(1, len(normalized) + 1))
            ordered_images = normalized
        else:
            resolved_order = self._resolve_image_order(plan.get("image_order"), len(normalized))
            if resolved_order:
                ordered_images = [normalized[idx - 1] for idx in resolved_order]
            else:
                resolved_order = list(range(1, len(normalized) + 1))
                ordered_images = normalized

        section_plan = self._build_section_plan(
            plan,
            resolved_order=resolved_order,
            default_image_duration_s=stage_input.default_image_duration_s,
        )

        normalized_plan = dict(plan)
        normalized_plan["video_title"] = self._normalize_title(
            plan.get("video_title"),
            default="Generated Slideshow",
        )
        # Song-style name for the generated track; distinct from the video title.
        # Falls back to the video title when the model omits it.
        normalized_plan["music_title"] = self._normalize_title(
            plan.get("music_title"),
            default=normalized_plan["video_title"],
        )
        normalized_plan["video_description"] = self._read_text(
            plan.get("video_description"),
            default=section_plan.summary,
        )
        normalized_plan["image_order"] = resolved_order
        normalized_plan.update(section_plan.legacy_metadata())
        normalized_plan["image_beats"] = [
            {
                "image_index": int(cue.source_refs[0].split(":")[-1]) if cue.source_refs else idx + 1,
                "label": cue.label,
                "role": cue.role,
                "emotion": cue.emotion,
                "description": cue.description,
                "transition_hint": cue.transition_hint,
            }
            for idx, cue in enumerate(section_plan.cues)
        ]
        normalized_plan["music_sections"] = [
            {
                "section_id": section.section_id,
                "label": section.label,
                "image_indices": list(section.image_indices),
                "objective": section.objective,
                "energy_start": section.energy_start,
                "energy_end": section.energy_end,
                "instrumentation_focus": list(section.instrumentation_focus),
                "lyric_lines": list(section.lyric_lines),
            }
            for section in section_plan.sections
        ]
        return ImageSequencePlanningStageOutput(
            ordered_images=ordered_images,
            plan=normalized_plan,
            section_plan=section_plan,
            token_usage=token_usage,
        )

    @staticmethod
    def _normalize_paths(image_paths: Sequence[Path]) -> List[Path]:
        if not image_paths:
            raise EdennValidationError("image_paths must contain at least one path", component="image_sequence_planning", operation="validate_paths")

        normalized: List[Path] = []
        for path in image_paths:
            resolved = Path(path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(resolved)
            normalized.append(resolved)
        return normalized

    @staticmethod
    def _encode_image(image_path: Path) -> Tuple[str, str]:
        data = image_path.read_bytes()
        image_b64 = base64.b64encode(data).decode("utf-8")
        mime_type, _ = mimetypes.guess_type(str(image_path))
        return (mime_type or "image/png", image_b64)

    @staticmethod
    def _resolve_image_order(raw_order: object, image_count: int) -> Optional[List[int]]:
        if image_count <= 0:
            return None
        if not isinstance(raw_order, list):
            return None

        parsed: List[int] = []
        for item in raw_order:
            if isinstance(item, bool):
                return None
            if isinstance(item, int):
                parsed.append(item)
            elif isinstance(item, str) and item.strip().isdigit():
                parsed.append(int(item.strip()))
            else:
                return None

        if len(parsed) != image_count:
            return None
        if len(set(parsed)) != image_count:
            return None
        if any(idx < 1 or idx > image_count for idx in parsed):
            return None
        return parsed

    @staticmethod
    def _read_text(value: object, *, default: str = "") -> str:
        if value is None:
            return default
        return str(value).strip() or default

    @classmethod
    def _normalize_title(cls, value: object, *, default: str) -> str:
        raw = cls._read_text(value, default=default)
        collapsed = " ".join(raw.split())
        if not collapsed:
            collapsed = default
        words = collapsed.split(" ")
        if len(words) > cls.TITLE_MAX_WORDS:
            collapsed = " ".join(words[: cls.TITLE_MAX_WORDS]).strip()
        if len(collapsed) > cls.TITLE_MAX_CHARS:
            collapsed = collapsed[: cls.TITLE_MAX_CHARS].rstrip()
        return collapsed or default

    @staticmethod
    def _read_float(value: object) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _read_string_list(value: object) -> List[str]:
        if not isinstance(value, list):
            return []
        results: List[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned:
                results.append(cleaned)
        return results

    @classmethod
    def _build_section_plan(
        cls,
        plan: Dict[str, object],
        *,
        resolved_order: List[int],
        default_image_duration_s: float,
    ) -> SectionPlan:
        if default_image_duration_s <= 0:
            raise ValueError("default_image_duration_s must be positive")

        beat_map: Dict[int, Dict[str, object]] = {}
        raw_beats = plan.get("image_beats")
        if isinstance(raw_beats, list):
            for raw in raw_beats:
                if not isinstance(raw, dict):
                    continue
                image_index = raw.get("image_index")
                if isinstance(image_index, bool):
                    continue
                if isinstance(image_index, int) and image_index > 0:
                    beat_map[image_index] = raw

        cues: List[NarrativeCue] = []
        cue_lookup: Dict[int, str] = {}
        ordered_positions = {image_index: idx for idx, image_index in enumerate(resolved_order, start=1)}
        for position, image_index in enumerate(resolved_order, start=1):
            beat = beat_map.get(image_index, {})
            cue_id = f"image_{image_index}"
            cue_lookup[image_index] = cue_id
            cues.append(
                NarrativeCue(
                    cue_id=cue_id,
                    label=cls._read_text(beat.get("label"), default=f"Frame {position}"),
                    role=cls._read_text(beat.get("role"), default="progression"),
                    target_duration_s=default_image_duration_s,
                    emotion=cls._read_text(beat.get("emotion")),
                    description=cls._read_text(beat.get("description")),
                    transition_hint=cls._read_text(beat.get("transition_hint")),
                    source_refs=[f"image:{image_index}", f"order:{position}"],
                )
            )

        raw_sections = plan.get("music_sections")
        assigned: set[int] = set()
        sections: List[MusicSection] = []
        if isinstance(raw_sections, list):
            for idx, raw_section in enumerate(raw_sections, start=1):
                if not isinstance(raw_section, dict):
                    continue
                raw_indices = cls._read_string_list(raw_section.get("image_indices"))
                normalized_indices: List[int] = []
                for candidate in raw_section.get("image_indices") or []:
                    if isinstance(candidate, bool):
                        continue
                    if isinstance(candidate, int) and candidate in ordered_positions and candidate not in normalized_indices:
                        normalized_indices.append(candidate)
                normalized_indices.sort(key=lambda item: ordered_positions[item])
                normalized_indices = [item for item in normalized_indices if item not in assigned]
                if not normalized_indices:
                    continue
                assigned.update(normalized_indices)
                section_id = cls._read_text(raw_section.get("section_id"), default=f"section_{idx}")
                label = cls._read_text(raw_section.get("label"), default=f"Section {idx}")
                objective = cls._read_text(raw_section.get("objective"), default=label)
                energy_start = cls._read_float(raw_section.get("energy_start"))
                energy_end = cls._read_float(raw_section.get("energy_end"))
                sections.append(
                    MusicSection(
                        section_id=section_id,
                        label=label,
                        target_duration_s=len(normalized_indices) * default_image_duration_s,
                        objective=objective,
                        energy_start=min(1.0, max(0.0, energy_start if energy_start is not None else 0.35)),
                        energy_end=min(1.0, max(0.0, energy_end if energy_end is not None else 0.65)),
                        image_indices=normalized_indices,
                        cue_ids=[cue_lookup[item] for item in normalized_indices],
                        instrumentation_focus=cls._read_string_list(raw_section.get("instrumentation_focus")),
                        lyric_lines=cls._read_string_list(raw_section.get("lyric_lines")),
                    )
                )

        missing_indices = [image_index for image_index in resolved_order if image_index not in assigned]
        if missing_indices:
            if sections:
                last = sections[-1]
                merged_indices = list(last.image_indices) + missing_indices
                sections[-1] = MusicSection(
                    section_id=last.section_id,
                    label=last.label,
                    target_duration_s=len(merged_indices) * default_image_duration_s,
                    objective=last.objective,
                    energy_start=last.energy_start,
                    energy_end=last.energy_end,
                    image_indices=merged_indices,
                    cue_ids=[cue_lookup[item] for item in merged_indices],
                    instrumentation_focus=list(last.instrumentation_focus),
                    lyric_lines=list(last.lyric_lines),
                )
            else:
                sections = [
                    MusicSection(
                        section_id="section_1",
                        label="Main Arc",
                        target_duration_s=len(missing_indices) * default_image_duration_s,
                        objective=cls._read_text(plan.get("storyline_summary"), default="Support the visual narrative."),
                        energy_start=0.3,
                        energy_end=0.8,
                        image_indices=missing_indices,
                        cue_ids=[cue_lookup[item] for item in missing_indices],
                        instrumentation_focus=cls._read_string_list(
                            plan.get("primary_instruments") or plan.get("instruments")
                        ),
                        lyric_lines=[],
                    )
                ]

        return SectionPlan(
            summary=cls._read_text(plan.get("storyline_summary"), default="Visual story progression"),
            total_duration_s=len(cues) * default_image_duration_s,
            overall_mood=cls._read_text(
                plan.get("overall_mood") or plan.get("recommended_mood"),
                default="engaging",
            ),
            target_bpm=cls._read_float(plan.get("target_bpm") or plan.get("tempo_bpm")),
            primary_instruments=cls._read_string_list(
                plan.get("primary_instruments") or plan.get("instruments")
            ),
            cues=cues,
            sections=sections,
            music_prompt_summary=cls._read_text(
                plan.get("music_prompt_summary") or plan.get("music_prompt"),
                default=cls._read_text(plan.get("storyline_summary"), default="Create a cohesive music arc."),
            ),
        )
