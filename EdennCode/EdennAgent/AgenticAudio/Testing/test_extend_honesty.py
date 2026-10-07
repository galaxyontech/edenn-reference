"""Asking for more length, and what the product actually does about it.

`edit_audio(edit_kind="extend")` has always written an `agentic_extend_mode`
into the job payload, and that string appears exactly once in the codebase:
where it is written. Nothing reads it. So "make it 20 seconds longer" ran the
ordinary generation path and returned a DIFFERENT piece of music — the user
asked for more of this track and got another track, with nothing anywhere
saying so.

The pieces for a real extension are further along than that suggests: the
provider clients can extend a track, one of them handling instrumental
material explicitly, and a take now carries the handle an extension needs. The
missing piece is the one in the middle — a job consumer that performs the
extension.

Which makes the capture dangerous on its own, and that is what these tests
pin. With a handle present the capability test would pass, every extend would
be labelled native, and it would still be regenerating: a worse failure than
the silent one, because the label reads as a guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

from EdennCode.EdennAgent.AgenticAudio.agent.prompts import SYSTEM_PROMPT
from EdennCode.EdennAgent.AgenticAudio.models import (
    NATIVE_EXTEND_CONSUMER_AVAILABLE,
    NATIVE_EXTEND_PROVIDERS,
)


def test_nothing_claims_to_extend_natively_while_no_consumer_exists() -> None:
    """The flag and the reality have to move together."""
    assert NATIVE_EXTEND_CONSUMER_AVAILABLE is False

    # The flag is not decoration: it is in the capability test itself.
    media = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py").read_text()
    assert "NATIVE_EXTEND_CONSUMER_AVAILABLE" in media
    native_test = media.split("native_capable = (")[1].split(")")[0]
    assert "NATIVE_EXTEND_CONSUMER_AVAILABLE" in native_test


def test_the_flag_flips_only_when_something_reads_the_mode() -> None:
    """The guard against flipping it early: if a consumer really exists, the
    payload key it would read is read SOMEWHERE other than where it is
    written. Today it is written once and read nowhere."""
    import subprocess

    hits = subprocess.run(
        ["grep", "-rn", "--include=*.py", "agentic_extend_mode", "EdennCode/"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    writes = [h for h in hits if "extra_payload[" in h]
    reads = [
        h for h in hits
        if "extra_payload[" not in h
        and "/Testing/" not in h
        and "__pycache__" not in h
    ]

    assert writes, "the mode is still recorded on the job"
    if reads:
        assert NATIVE_EXTEND_CONSUMER_AVAILABLE, (
            "something now reads the extend mode — wire it and flip the flag "
            f"in the same change: {reads}"
        )
    else:
        assert not NATIVE_EXTEND_CONSUMER_AVAILABLE, (
            "the flag claims a consumer that does not exist"
        )


def test_a_take_keeps_the_handle_an_extension_would_need() -> None:
    """Captured now so the consumer has something to work with, and because the
    deployment that ships was dropping it: the orchestrator carried the id the
    whole time and the result threw it away."""
    devserver = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    music_result = devserver.split("async def _real_music_result")[1].split("\n    def ")[0]

    assert '"provider_audio_id"' in music_result
    assert '"provider_task_id"' in music_result


def test_the_handle_never_reaches_the_client() -> None:
    """It is a provider-side identifier. It belongs in session state and
    nowhere a client can read it."""
    from EdennCode.Deployment.error_codes import redact_client_keys

    cleaned = redact_client_keys(
        {"candidate_id": "c1", "provider_audio_id": "abc123", "provider_task_id": "t9"}
    )
    assert "provider_audio_id" not in cleaned
    assert "provider_task_id" not in cleaned
    assert cleaned.get("candidate_id") == "c1"


def test_the_agent_is_told_to_say_a_longer_take_is_a_new_take() -> None:
    """The data was always on the candidate; nothing ever asked the agent to
    mention it. Until a consumer exists, the words are the only place this can
    be made true."""
    assert "NOT YET AN EXTENSION" in SYSTEM_PROMPT
    assert "regenerate_fallback" in SYSTEM_PROMPT
    assert "a new take rather than this one continued" in SYSTEM_PROMPT


def test_no_clause_tells_the_model_the_take_is_lengthened_in_place() -> None:
    """The honest paragraph was not enough on its own: the edit_audio entry
    still said "extend" was done in place on the two paid tiers, which is the
    opposite promise and sits ~200 lines closer to the call the model makes.
    While no consumer performs an extension, NOTHING may describe one as
    continuing the take the user already has."""
    if NATIVE_EXTEND_CONSUMER_AVAILABLE:
        return  # the claim becomes true, and this stops being a lie to catch

    flat = re.sub(r"\s+", " ", SYSTEM_PROMPT)
    for phrase in ("in place", "in-place"):
        for match in re.finditer(re.escape(phrase), flat, re.I):
            window = flat[max(0, match.start() - 220): match.end() + 220]
            assert "extend" not in window.lower(), (
                f"the prompt claims an in-place extend: ...{window}..."
            )


def test_the_providers_that_could_extend_are_still_named() -> None:
    """Flipping the consumer flag should not also require rediscovering which
    tiers can do this at all."""
    assert len(NATIVE_EXTEND_PROVIDERS) == 2


def test_the_length_control_says_it_will_be_a_new_take() -> None:
    """The control exists now, which makes the disclosure load-bearing: a user
    who asks for four more seconds of THIS music and is handed different music
    has been told something untrue by the product. The prompt already carries
    it for the chat path; this is the same sentence where a click starts it."""
    app = Path("EdennCode/EdennAgent/AgenticAudio/frontend/js/app.js").read_text()

    longer = app.split("function requestLonger(")[1].split("function ")[0]
    assert "NEW take" in longer
    assert "not this one continued" in longer
    assert "confirmSpend" in longer, "a paid action with no spend confirmation"


def test_restyle_is_reachable_without_typing_the_words() -> None:
    app = Path("EdennCode/EdennAgent/AgenticAudio/frontend/js/app.js").read_text()

    assert 'edit_kind: "creative_edit"' in app
    assert 'label: "Restyle…"' in app
