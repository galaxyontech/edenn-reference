import uuid
import pytest


@pytest.mark.asyncio
async def test_succeeded_requires_output_url(db_conn):
    """CHECK constraint: status='succeeded' must have non-NULL output_url."""
    with pytest.raises(Exception) as exc_info:
        await db_conn.execute(
            """
            INSERT INTO requests (request_id, endpoint, status, output_url)
            VALUES ($1, 'video_generation', 'succeeded', NULL)
            """,
            uuid.uuid4(),
        )
    assert "requests_succeeded_has_url" in str(exc_info.value)


@pytest.mark.asyncio
async def test_responded_after_received(db_conn):
    """CHECK constraint: responded_at >= received_at."""
    rid = uuid.uuid4()
    with pytest.raises(Exception) as exc_info:
        await db_conn.execute(
            """
            INSERT INTO requests (request_id, endpoint, received_at, responded_at)
            VALUES ($1, 'video_generation', '2026-04-26 10:00:00+00', '2026-04-26 09:00:00+00')
            """,
            rid,
        )
    assert "requests_responded_after_received" in str(exc_info.value)


@pytest.mark.asyncio
async def test_status_check_rejects_unknown_value(db_conn):
    with pytest.raises(Exception) as exc_info:
        await db_conn.execute(
            "INSERT INTO requests (request_id, endpoint, status) VALUES ($1, 'x', 'bogus')",
            uuid.uuid4(),
        )
    msg = str(exc_info.value).lower()
    assert "status" in msg or "check" in msg


@pytest.mark.asyncio
async def test_latency_ms_is_generated(db_conn):
    rid = uuid.uuid4()
    await db_conn.execute(
        """
        INSERT INTO requests (request_id, endpoint, received_at, responded_at, status, output_url)
        VALUES ($1, 'video_generation', '2026-04-26 10:00:00+00', '2026-04-26 10:00:02.500+00',
                'succeeded', 'https://x/y')
        """,
        rid,
    )
    latency = await db_conn.fetchval("SELECT latency_ms FROM requests WHERE request_id=$1", rid)
    assert latency == 2500, f"expected 2500ms, got {latency}"
    # cleanup so this row doesn't pollute subsequent tests
    await db_conn.execute("DELETE FROM requests WHERE request_id=$1", rid)


@pytest.mark.asyncio
async def test_required_indexes_exist(db_conn):
    expected = {
        "requests_pkey",
        "requests_received_at_idx",
        "requests_updated_at_idx",
        "requests_endpoint_status_idx",
        "requests_options_modelspec_idx",
        "requests_user_prompt_tsv_idx",
        "requests_extracted_intent_gin",
        "requests_user_prompt_embedding_idx",
    }
    rows = await db_conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'requests'"
    )
    actual = {r["indexname"] for r in rows}
    missing = expected - actual
    assert not missing, f"missing indexes: {missing}"
