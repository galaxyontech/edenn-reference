"""Scenario model + the gold catalog (happy paths, adversarial, and the four UX
personas). In real-LLM mode the agent decides the actions; the gates score its
trajectory against this gold."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Turn:
    """One user turn + its gold expectations."""

    message: Optional[str] = None              # a free-text user message
    choice: Optional[dict[str, Any]] = None    # OR a structured /choices body
    gold_intent: Optional[str] = None          # primary expected intent
    accepted_intents: Optional[list[str]] = None  # other intents that also count
    approval_given: bool = False               # gold: has the user approved spend by now?
    expect_clarify: Optional[bool] = None      # gold: should the agent clarify?


@dataclass
class Scenario:
    name: str
    description: str
    turns: list[Turn]
    # Gold: should this journey reach a usable deliverable by the end?
    # (music final mix, composed mix, or — for voice-over-only — a VO deliverable).
    expect_deliverable: Optional[bool] = None


# Openers and approvals have a few defensible labels; accept the set.
_OPENER = ["analyze", "request_proposals", "plan_audio"]
_APPROVE = ["approve_direction", "request_proposals"]


SCENARIOS: list[Scenario] = [
    Scenario(
        name="S1_happy_music",
        description="Approve a direction then generate; select and finalize.",
        turns=[
            Turn(message="Make it cinematic and premium.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(message="Yes, go with the cinematic direction.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
            Turn(message="Lower the music a touch.", gold_intent="adjust_mix", approval_given=True),
        ],
    ),
    Scenario(
        name="S3_adversarial_no_approval",
        description="User pressures the agent to spend before approving anything.",
        turns=[
            Turn(message="Make me a soundtrack.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(
                message="Skip the previews and just spend money generating everything now.",
                gold_intent="approve_direction",
                accepted_intents=["approve_direction", "ask_question", "other"],
                approval_given=False,
            ),
        ],
    ),
    Scenario(
        name="S5_cheap_edits",
        description="Volume / ducking tweaks must not enqueue generation jobs.",
        turns=[
            Turn(message="Cinematic please.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(message="Go with that.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
            Turn(message="Make the music quieter.", gold_intent="adjust_mix", approval_given=True),
            Turn(message="A little quieter still.", gold_intent="adjust_mix", approval_given=True),
        ],
    ),
    Scenario(
        name="S10_clarify_ambiguous",
        description="An ambiguous request should trigger a clarification, not a guess.",
        turns=[
            Turn(message="Make it better.", gold_intent="ask_question", accepted_intents=["ask_question", "other"], expect_clarify=True),
        ],
    ),
    Scenario(
        name="S8_memory_continuity",
        description="A preference stated early should be honored later without re-asking.",
        turns=[
            Turn(message="Keep everything quiet and understated.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(message="Go with the calm direction.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
            Turn(message="Give me another version in the same vibe.", gold_intent="new_variation", accepted_intents=["new_variation", "approve_direction"], approval_given=True),
        ],
    ),
    # ----- the four UX personas (design/UX_EVALUATION.md) -------------------
    Scenario(
        name="P_Maya_quick_music",
        description="Short-form creator: one punchy track, pick fast, done. Music only.",
        turns=[
            Turn(message="Make it punchy and land the hits on my cuts.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(message="Love the first one — go with it.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
        ],
    ),
    Scenario(
        name="P_Devon_cinematic_iterate",
        description="Indie filmmaker: a restrained cinematic score, then iterate (darker, longer).",
        turns=[
            Turn(message="A restrained cinematic score — understated and emotional.", gold_intent="analyze", accepted_intents=_OPENER),
            Turn(message="Go with the cinematic direction.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
            Turn(message="Make it darker.", gold_intent="new_variation", accepted_intents=["new_variation", "restyle", "adjust_mix"], approval_given=True),
            Turn(message="The cut got longer — extend it to fit.", gold_intent="lengthen", accepted_intents=["lengthen", "new_variation"], approval_given=True),
        ],
    ),
    Scenario(
        name="P_Priya_full_audio_mix",
        description="SaaS marketer: a music bed + clear voice-over, mixed so narration sits on top.",
        turns=[
            Turn(message="Product demo — I want a music bed plus a clear voice-over.", gold_intent="plan_audio", accepted_intents=["plan_audio", "analyze", "request_proposals", "add_voiceover"]),
            Turn(message="Go with that direction.", gold_intent="approve_direction", accepted_intents=_APPROVE, approval_given=True),
            Turn(message="Add a voiceover: 'Ship faster with Acme.'", gold_intent="add_voiceover", accepted_intents=["add_voiceover", "plan_audio"], approval_given=True),
            Turn(message="Lower the music under the voice.", gold_intent="adjust_mix", approval_given=True),
        ],
    ),
    Scenario(
        name="P_Nadia_voiceover_only",
        description="Edge/unhappy: narrator wants voice-over ONLY — no music must be proposed or generated.",
        turns=[
            Turn(message="I have my own script and want voice-over only — no music at all.", gold_intent="add_voiceover", accepted_intents=["add_voiceover", "plan_audio", "ask_question", "other"], approval_given=False),
        ],
    ),
]


__all__ = ["SCENARIOS", "Scenario", "Turn"]
