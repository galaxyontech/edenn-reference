"""The lyric sheet sent to the provider must cover a full-length song.

The provider aligns only the submitted sheet; a slideshow-sized sheet (≈10
lines) leaves the generated song's looped repeats unstamped, so full_lyrics and
its timestamps stop mid-track (verified against the provider's raw task payload,
2026-07-17). Vocal multi-image jobs therefore expand the sheet with explicit
repeat sections before generation.
"""
from EdennCode.MusicGenerationCore.models import MusicSection, SectionPlan
from EdennCode.WorkflowFactory.MultiImageWorkflow.Stages.MusicGenerationStage.music_generation_stage import (
    FULL_SONG_TARGET_LINES,
    expand_plan_for_full_song,
)


def _section(section_id: str, label: str, lines: list[str]) -> MusicSection:
    return MusicSection(
        section_id=section_id,
        label=label,
        target_duration_s=3.0,
        objective="test objective",
        energy_start=0.4,
        energy_end=0.8,
        lyric_lines=lines,
    )


def _plan(sections: list[MusicSection]) -> SectionPlan:
    return SectionPlan(
        summary="test",
        total_duration_s=9.0,
        overall_mood="upbeat",
        target_bpm=120,
        primary_instruments=["piano"],
        cues=[],
        sections=sections,
        music_prompt_summary="test prompt",
    )


def test_short_sheet_is_expanded_to_full_song_length():
    plan = _plan([
        _section("s1", "Intro", ["line one", "line two"]),
        _section("s2", "Build", ["line three", "line four"]),
        _section("s3", "Final Chorus", ["chorus a", "chorus b", "chorus c"]),
    ])
    expanded = expand_plan_for_full_song(plan)
    total_lines = sum(len(s.lyric_lines) for s in expanded.sections)
    original_lines = sum(len(s.lyric_lines) for s in plan.sections)
    # Grows toward the full-song target; a short sheet at least doubles, capped
    # by the fixed repeat templates (chorus/reprise x4).
    assert total_lines >= 2 * original_lines
    assert total_lines == 18  # 7 planned + chorus(3) + reprise(2) + chorus(3) + chorus(3)
    # Repeats reuse the PLANNED lines (the closing section loops, like the song does).
    appended = expanded.sections[len(plan.sections):]
    assert appended, "expected repeat sections to be appended"
    assert appended[0].lyric_lines == ["chorus a", "chorus b", "chorus c"]
    # Original plan untouched (frozen dataclasses, new object returned).
    assert len(plan.sections) == 3
    # Slideshow-facing fields unchanged.
    assert expanded.total_duration_s == plan.total_duration_s


def test_full_length_sheet_is_left_alone():
    sections = [
        _section(f"s{i}", f"Verse {i}", [f"line {i}a", f"line {i}b", f"line {i}c"])
        for i in range(10)  # 30 lines >= target
    ]
    plan = _plan(sections)
    assert expand_plan_for_full_song(plan) is plan


def test_instrumental_plan_without_lyrics_is_left_alone():
    plan = _plan([_section("s1", "Intro", []), _section("s2", "Build", [])])
    assert expand_plan_for_full_song(plan) is plan
