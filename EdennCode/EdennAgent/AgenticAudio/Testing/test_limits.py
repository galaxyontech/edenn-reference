"""Rate limits and the daily spend ceiling.

Every turn on this surface costs money, and until now nothing in the repo
bounded it: one authenticated caller in a loop could drain the provider keys.

The unit tests pin the limiter's behaviour; the API tests prove it is actually
wired to the endpoints that spend — including that the check happens BEFORE the
work, since a limiter consulted afterwards has already paid for the thing it was
meant to prevent.
"""

from __future__ import annotations

import pytest

from EdennCode.EdennAgent.AgenticAudio.api.limits import (
    LimitExceeded,
    Limiter,
    limiter,
    set_limiter,
)


# ---------------------------------------------------------------------------#
# the limiter itself                                                          #
# ---------------------------------------------------------------------------#


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_turns_are_allowed_up_to_the_limit_then_refused() -> None:
    clock = _Clock()
    lim = Limiter(turn_limit=3, window_s=60, clock=clock)
    for _ in range(3):
        lim.check_turn("alice")
    with pytest.raises(LimitExceeded) as exc:
        lim.check_turn("alice")
    assert exc.value.status_code == 429
    assert exc.value.retry_after_s and exc.value.retry_after_s <= 60


def test_the_window_slides_rather_than_resetting_on_a_fixed_tick() -> None:
    """A fixed-bucket limiter lets a caller send 2x the limit across a boundary."""
    clock = _Clock()
    lim = Limiter(turn_limit=2, window_s=60, clock=clock)
    lim.check_turn("alice")
    clock.advance(30)
    lim.check_turn("alice")
    clock.advance(1)
    with pytest.raises(LimitExceeded):
        lim.check_turn("alice")
    # Once the first hit ages out of the window, one slot frees up — and only one.
    clock.advance(30)
    lim.check_turn("alice")
    with pytest.raises(LimitExceeded):
        lim.check_turn("alice")


def test_one_caller_cannot_exhaust_another_callers_budget() -> None:
    clock = _Clock()
    lim = Limiter(turn_limit=2, window_s=60, clock=clock)
    lim.check_turn("alice")
    lim.check_turn("alice")
    with pytest.raises(LimitExceeded):
        lim.check_turn("alice")
    lim.check_turn("bob")  # unaffected


def test_an_unauthenticated_surface_is_still_bounded() -> None:
    """No principal is not a licence to spend without limit."""
    clock = _Clock()
    lim = Limiter(turn_limit=1, window_s=60, clock=clock)
    lim.check_turn(None)
    with pytest.raises(LimitExceeded):
        lim.check_turn(None)


def test_the_daily_ceiling_counts_generations_and_then_refuses() -> None:
    day = {"v": "2026-08-25"}
    lim = Limiter(daily_generation_limit=2, day_key=lambda: day["v"])
    lim.check_generation("alice")
    lim.record_generation("alice")
    lim.check_generation("alice")
    lim.record_generation("alice")
    with pytest.raises(LimitExceeded) as exc:
        lim.check_generation("alice")
    assert "today's generation limit" in exc.value.detail
    assert lim.spent_today("alice") == 2


def test_the_ceiling_resets_on_a_new_day() -> None:
    day = {"v": "2026-08-25"}
    lim = Limiter(daily_generation_limit=1, day_key=lambda: day["v"])
    lim.record_generation("alice")
    with pytest.raises(LimitExceeded):
        lim.check_generation("alice")
    day["v"] = "2026-08-26"
    lim.check_generation("alice")  # a new day, a fresh budget
    assert lim.spent_today("alice") == 0


def test_the_rate_limit_and_the_ceiling_are_independent() -> None:
    """Slow, steady spending still hits the ceiling; fast browsing does not."""
    clock = _Clock()
    day = {"v": "2026-08-25"}
    lim = Limiter(turn_limit=100, window_s=60, daily_generation_limit=2,
                  clock=clock, day_key=lambda: day["v"])
    for _ in range(2):
        clock.advance(3600)
        lim.check_turn("alice")
        lim.check_generation("alice")
        lim.record_generation("alice")
    clock.advance(3600)
    lim.check_turn("alice")  # not rate limited
    with pytest.raises(LimitExceeded):
        lim.check_generation("alice")  # but out of budget


# ---------------------------------------------------------------------------#
# wired to the endpoints that spend                                           #
# ---------------------------------------------------------------------------#


@pytest.fixture(autouse=True)
def _isolated_limiter():
    """Each test gets its own counters, and the process limiter is restored."""
    previous = limiter()
    yield
    set_limiter(previous)
    previous.reset()


def test_a_turn_endpoint_refuses_once_the_rate_limit_is_hit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
        _bootstrap_decisions,
        _client_with_decisions,
        _create_session,
    )

    set_limiter(Limiter(turn_limit=2, window_s=60))
    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions() * 6)
    session = _create_session(client, source)
    sid = session["session_id"]

    seen = [
        client.post(
            f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": "again"}
        ).status_code
        for _ in range(4)
    ]
    assert 429 in seen, f"no turn was ever rate limited: {seen}"
    # The refusal tells the client when to come back rather than just failing.
    last = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": "again"}
    )
    assert last.status_code == 429
    assert "Retry-After" in last.headers


def test_the_daily_ceiling_stops_the_spend_before_the_provider_is_called(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the ceiling is that the generation does not happen."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
        _bootstrap_decisions,
        _client_with_decisions,
        _create_session,
    )

    # Two, not one: creating the session now spends as well (it runs the
    # analysis), so it takes the first of the day's budget.
    set_limiter(Limiter(turn_limit=1000, window_s=60, daily_generation_limit=2))
    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions() * 6)
    session = _create_session(client, source)
    sid = session["session_id"]

    first = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": "make music"}
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/v2/agentic/audio/sessions/{sid}/messages", json={"content": "make more"}
    )
    assert second.status_code == 429, second.text
    assert "generation limit" in second.json()["detail"]


def test_a_non_spending_choice_does_not_consume_the_daily_budget(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reassigning who owns a moment costs nothing and must not be rationed."""
    lim = Limiter(turn_limit=1000, window_s=60, daily_generation_limit=1)
    set_limiter(lim)

    from EdennCode.EdennAgent.AgenticAudio.api.router import _SPENDING_CHOICES

    assert "spotting" not in _SPENDING_CHOICES
    assert "clarification" not in _SPENDING_CHOICES
    # The ones that render must be rationed.
    for spending in ("proposal", "candidate", "variation", "voiceover", "sfx", "compose"):
        assert spending in _SPENDING_CHOICES


def test_the_free_verbs_are_not_rationed_like_a_render() -> None:
    """Re-cutting a window and comparing takes spend nothing. Counting them
    against the daily generation budget would ration the very moves a user
    makes to AVOID spending."""
    from EdennCode.EdennAgent.AgenticAudio.api.router import _SPENDING_CHOICES

    assert "sculpt" not in _SPENDING_CHOICES
    assert "compare" not in _SPENDING_CHOICES


def test_a_malformed_choice_frame_is_rejected_before_it_costs_a_render() -> None:
    """The message path learned this and the choice path never did: a frame was
    parsed AFTER the generation ceiling was charged, so an unknown choice_type
    spent a slot out of the caller's daily budget on a turn that could never
    run."""
    from pathlib import Path

    source = Path("EdennCode/EdennAgent/AgenticAudio/api/router.py").read_text()
    socket_loop = source.split("async def _send(obj: Any)")[1]

    parse_at = socket_loop.index("choice = AgenticAudioChoiceRequest(**payload)")
    charge_at = socket_loop.index("lim.check_generation(member_principal)")
    assert parse_at < charge_at, "the frame is still parsed after the budget is charged"


def test_a_failed_turn_leaves_an_account_of_itself() -> None:
    """The failure path was the one turn outcome rendered ephemerally. A glance
    away or a reconnect took the only explanation of why the turn did nothing,
    while every other outcome stayed re-readable in the transcript."""
    from pathlib import Path

    source = Path("EdennCode/EdennAgent/AgenticAudio/api/router.py").read_text()

    assert "def _record_turn_failure(" in source
    # Both turn-failure branches, not just the tidy one.
    assert source.count("_record_turn_failure(") >= 3
    assert '"turn_outcome": "failed"' in source


def test_creating_a_session_counts_against_the_ceiling_it_spends_from(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a session RUNS THE ANALYSIS — one vision call per scene, up to
    thirty, the most expensive single moment in a session. It was the one
    spending entrypoint with no limit on it at all, so a loop here spent
    without ever touching the thing meant to stop it."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_api import (
        _bootstrap_decisions,
        _client_with_decisions,
        _create_session,
    )

    set_limiter(Limiter(turn_limit=1000, window_s=60, daily_generation_limit=1))
    client, _, _, _, source = _client_with_decisions(tmp_path, _bootstrap_decisions() * 6)
    _create_session(client, source)

    refused = client.post(
        "/api/v2/agentic/audio/sessions",
        json={
            "source_video_artifact_id": source.artifact_id,
            "initial_message": "Score it.",
        },
    )
    assert refused.status_code == 429, refused.text
    assert "generation limit" in refused.json()["detail"]
