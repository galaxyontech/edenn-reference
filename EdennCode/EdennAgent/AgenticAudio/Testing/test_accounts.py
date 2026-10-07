"""Which account a principal belongs to — and what "we don't know" must mean.

The studio knows a uid. Money lives on a platform account. Nothing joined the
two, so "whose balance does this render come out of" had no answer at all.

This is the seam and only the seam: HOW the studio reads the platform's account
index (a direct read of the billing database, or an admin-guarded endpoint) is
an open decision, so the directory is injected the way the identity verifier is.
What these tests pin is the part that cannot wait for that decision — that an
absent answer is recorded as "not established" and never as "free", and that it
never overwrites a link already known.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from EdennCode.EdennAgent.AgenticAudio.api import accounts
from EdennCode.EdennAgent.AgenticAudio.persistence.collab import (
    InMemoryCollabRepository,
)


class _Directory:
    """An index that answers from a dict and counts how often it is asked."""

    def __init__(self, mapping: Optional[dict[str, str]] = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls = 0

    async def account_for(self, uid: str) -> Optional[str]:
        self.calls += 1
        return self.mapping.get(uid)


@pytest.fixture(autouse=True)
def _clean_directory():
    accounts.reset_for_tests()
    yield
    accounts.reset_for_tests()


def test_without_a_directory_nobody_has_an_account() -> None:
    """Not an error and not a refusal: the integration is not chosen yet, and
    a studio that invented an account id would be worse than one that says it
    does not know."""
    assert accounts.directory_configured() is False
    assert asyncio.run(accounts.account_for("uid_1")) is None


def test_a_configured_directory_answers() -> None:
    accounts.set_directory(_Directory({"uid_1": "acct_9"}))
    assert accounts.directory_configured() is True
    assert asyncio.run(accounts.account_for("uid_1")) == "acct_9"


def test_a_known_account_is_not_looked_up_on_every_request() -> None:
    """This runs inside authentication, which runs on every call. A uid's
    account changes about once in its life, so a round trip per request buys
    information that did not change."""
    directory = _Directory({"uid_1": "acct_9"})
    accounts.set_directory(directory)

    async def twice() -> None:
        assert await accounts.account_for("uid_1") == "acct_9"
        assert await accounts.account_for("uid_1") == "acct_9"

    asyncio.run(twice())
    assert directory.calls == 1


def test_no_account_yet_is_the_answer_that_gets_re_asked_soonest() -> None:
    """A hit is stable; a miss is the one that flips, the moment somebody
    finishes signing up. Caching both for the same long window would leave a
    new customer unable to spend for fifteen minutes after paying."""
    assert accounts.MISS_TTL_S < accounts.HIT_TTL_S / 10


def test_a_signup_can_be_picked_up_without_waiting() -> None:
    directory = _Directory({})
    accounts.set_directory(directory)
    assert asyncio.run(accounts.account_for("uid_1")) is None

    directory.mapping["uid_1"] = "acct_9"
    accounts.forget("uid_1")
    assert asyncio.run(accounts.account_for("uid_1")) == "acct_9"


def test_an_unanswered_lookup_never_erases_an_account_already_known() -> None:
    """`None` from the directory means "no answer" — nobody has signed up yet,
    or the index had a bad minute. Writing that over a stored link would
    disconnect a paying customer from their balance because a lookup timed
    out."""
    repo = InMemoryCollabRepository()
    repo.touch_user("uid_1", account_id="acct_9")
    repo.touch_user("uid_1", account_id=None)

    assert repo.get_user("uid_1")["account_id"] == "acct_9"


def test_authenticating_records_the_account_the_directory_gives(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authentication is the one place that already knows a caller is real, so
    it is where the link is established — and it is best-effort, because
    nothing charges yet and an index having a bad minute must not cost somebody
    their turn."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_collab import (
        _collab_client,
        _create_session,
    )

    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    accounts.set_directory(_Directory({"owner_1": "acct_9"}))

    client, collab_repo, _ = _collab_client(tmp_path)
    _create_session(client, headers={"Authorization": "Bearer owner_1"})

    assert collab_repo.get_user("owner_1")["account_id"] == "acct_9"


def test_a_uid_with_no_account_is_still_a_working_customer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is enforced yet, deliberately: refusing on a balance the system
    has never counted is how a customer gets told they are out of credit by a
    meter that has never run. The link is recorded first; the gate comes with
    the numbers."""
    from EdennCode.EdennAgent.AgenticAudio.Testing.test_agentic_audio_collab import (
        _collab_client,
        _create_session,
    )

    monkeypatch.setenv("AGENTIC_AUDIO_REQUIRE_AUTH", "1")
    monkeypatch.delenv("AGENTIC_AUDIO_API_KEYS", raising=False)
    monkeypatch.setenv("AGENTIC_AUDIO_TOKEN_IS_USER", "1")
    accounts.set_directory(_Directory({}))

    client, collab_repo, _ = _collab_client(tmp_path)
    session_id = _create_session(client, headers={"Authorization": "Bearer owner_1"})

    assert collab_repo.get_user("owner_1")["account_id"] is None
    snapshot = client.get(
        f"/api/v2/agentic/audio/sessions/{session_id}",
        headers={"Authorization": "Bearer owner_1"},
    )
    assert snapshot.status_code == 200
