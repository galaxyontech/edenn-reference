import json
import uuid
import pytest


@pytest.mark.asyncio
async def test_run_requires_existing_request(db_conn):
    """FK: pipeline_runs.request_id must reference a real requests row."""
    with pytest.raises(Exception) as exc_info:
        await db_conn.execute(
            """
            INSERT INTO pipeline_runs (run_id, request_id, workflow_type, workflow_version)
            VALUES ($1, $2, 'video_music', 'abc123')
            """,
            uuid.uuid4(), uuid.uuid4(),  # nonexistent request_id
        )
    assert "foreign key" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_token_totals_must_be_nonneg(db_conn):
    # Need a real request first
    rid = uuid.uuid4()
    await db_conn.execute(
        "INSERT INTO requests (request_id, endpoint, status, output_url) VALUES ($1, 'x', 'succeeded', 'http://x')",
        rid,
    )
    try:
        with pytest.raises(Exception) as exc_info:
            await db_conn.execute(
                """
                INSERT INTO pipeline_runs (run_id, request_id, workflow_type, workflow_version,
                                            total_input_tokens, total_output_tokens)
                VALUES ($1, $2, 'video_music', 'v', -5, 0)
                """,
                uuid.uuid4(), rid,
            )
        assert "token_totals_nonneg" in str(exc_info.value)
    finally:
        await db_conn.execute("DELETE FROM requests WHERE request_id=$1", rid)


@pytest.mark.asyncio
async def test_stage_index_unique_within_run(db_conn):
    rid = uuid.uuid4()
    run_id = uuid.uuid4()
    await db_conn.execute(
        "INSERT INTO requests (request_id, endpoint, status, output_url) VALUES ($1, 'x', 'succeeded', 'http://x')",
        rid,
    )
    await db_conn.execute(
        "INSERT INTO pipeline_runs (run_id, request_id, workflow_type, workflow_version) VALUES ($1, $2, 'video_music', 'v')",
        run_id, rid,
    )
    try:
        await db_conn.execute(
            "INSERT INTO pipeline_stages (run_id, stage_name, stage_index) VALUES ($1, 'a', 0)",
            run_id,
        )
        with pytest.raises(Exception) as exc_info:
            await db_conn.execute(
                "INSERT INTO pipeline_stages (run_id, stage_name, stage_index) VALUES ($1, 'b', 0)",
                run_id,
            )
        assert "unique" in str(exc_info.value).lower() or "duplicate" in str(exc_info.value).lower()
    finally:
        await db_conn.execute("DELETE FROM requests WHERE request_id=$1", rid)


@pytest.mark.asyncio
async def test_output_size_cap(db_conn):
    rid = uuid.uuid4()
    run_id = uuid.uuid4()
    await db_conn.execute(
        "INSERT INTO requests (request_id, endpoint, status, output_url) VALUES ($1, 'x', 'succeeded', 'http://x')",
        rid,
    )
    await db_conn.execute(
        "INSERT INTO pipeline_runs (run_id, request_id, workflow_type, workflow_version) VALUES ($1, $2, 'video_music', 'v')",
        run_id, rid,
    )
    try:
        big = {"data": "x" * 300_000}  # > 256 KB
        with pytest.raises(Exception) as exc_info:
            await db_conn.execute(
                "INSERT INTO pipeline_stages (run_id, stage_name, stage_index, output) VALUES ($1, 'a', 0, $2)",
                run_id, json.dumps(big),
            )
        assert "output_size_cap" in str(exc_info.value)
    finally:
        await db_conn.execute("DELETE FROM requests WHERE request_id=$1", rid)


@pytest.mark.asyncio
async def test_required_indexes_exist(db_conn):
    expected = {
        "pipeline_runs_pkey", "pipeline_runs_request_idx", "pipeline_runs_updated_at_idx",
        "pipeline_runs_workflow_idx", "pipeline_runs_modelspec_idx",
        "pipeline_runs_summary_tsv_idx", "pipeline_runs_video_summary_embedding_idx",
        "pipeline_runs_music_prompt_embedding_idx",
        "pipeline_stages_pkey", "pipeline_stages_run_index_unique",
        "pipeline_stages_name_status_idx", "pipeline_stages_provider_idx",
        "pipeline_stages_output_gin",
    }
    rows = await db_conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename IN ('pipeline_runs','pipeline_stages')"
    )
    actual = {r["indexname"] for r in rows}
    missing = expected - actual
    assert not missing, f"missing indexes: {missing}"
