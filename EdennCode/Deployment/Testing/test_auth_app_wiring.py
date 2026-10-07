"""App-level wiring tests: middleware registration order, CORS-on-401, inertness.

Builds a minimal app the same way api.py does (auth middleware registered
textually before CORS) rather than importing the production api.py module,
which requires live the model gateway env at import time.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext, JobStatus
from EdennCode.Deployment.api_security import resolve_cors_policy
from EdennCode.Deployment.api_video_generation import (
    _AsyncJobState,
    _async_job_store,
    _memory_async_video_job_store,
    _run_async_video_job_inner,
    create_video_generation_router,
)
from EdennCode.Deployment.auth.key_store import Principal
from EdennCode.Deployment.auth.middleware import create_auth_middleware
from EdennCode.Deployment.auth.usage_recorder import UsageRecorder
from EdennCode.Deployment.recommendation_persistence import RecommendationAssetIds
from EdennCode.Deployment.Testing.test_auth_middleware import StubKeyStore, PRINCIPAL
from EdennCode.Deployment.Testing.test_auth_usage_recorder import FakeUsageTable
from EdennCode.TestSuites.helpers.paths import SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH


def _wired_app(mode: str, origins: str = "https://app.example.com") -> FastAPI:
    app = FastAPI()
    # SAME ORDER AS api.py: auth first (inner), CORS second (outer).
    app.middleware("http")(create_auth_middleware(
        mode=mode, key_store=StubKeyStore(PRINCIPAL),
        logger=logging.getLogger("t"),
    ))
    policy = resolve_cors_policy(origins)
    if policy is not None:
        app.add_middleware(CORSMiddleware, **policy)

    @app.post("/api/v1/jobs/video")
    def job():
        return {"ok": True}

    @app.get("/healthz")
    def health():
        return {"ok": True}

    return app


def test_enforce_401_carries_cors_headers():
    client = TestClient(_wired_app("enforce"))
    resp = client.post(
        "/api/v1/jobs/video", headers={"Origin": "https://app.example.com"}
    )
    assert resp.status_code == 401
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_preflight_passes_without_key():
    client = TestClient(_wired_app("enforce"))
    resp = client.options(
        "/api/v1/jobs/video",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in resp.headers}


def test_off_mode_is_inert():
    client = TestClient(_wired_app("off", origins=""))
    assert client.post("/api/v1/jobs/video").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_apicontext_accepts_auth_fields():
    from unittest.mock import MagicMock
    from EdennCode.Deployment.api_common import ApiContext

    context = ApiContext(
        settings=MagicMock(), storage=MagicMock(), workflow=MagicMock(),
        alignment_workflow=MagicMock(), audio_creative_edit_workflow=MagicMock(),
        logger=logging.getLogger("t"),
        auth_key_store=object(), usage_recorder=object(),
    )
    assert context.auth_key_store is not None
    assert context.usage_recorder is not None


class _DisabledStorage:
    enabled = False


class _FakeVideoMetadata:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.duration = 1.0
        self.size_bytes = path.stat().st_size
        self.width = 320
        self.height = 240
        self.fps = 30.0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "audio_activity": [],
        }


def _post_sync_video(tmp_path):
    """Drive the real sync video endpoint against a stub workflow; return (response, table)."""
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_music_path = output_dir / "generated.mp3"
    remixed_video_path = output_dir / "remixed.mp4"
    generated_music_path.write_bytes(b"audio")
    remixed_video_path.write_bytes(b"video")

    async def fake_workflow_run(**kwargs):
        return SimpleNamespace(
            video_metadata=_FakeVideoMetadata(Path(kwargs["video_path"])),
            scenes=[],
            video_summary={"video_title": "Sync usage test"},
            music_prompt={"style_prompt": "bright pop"},
            generated_music_path=generated_music_path,
            complete_generated_music_path=None,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video_path,
            include_vocals=False,
            vocal_gender="female",
            lyrics_timestamps=[],
            word_level_lyrics_timestamps=[],
            user_requested_language="ENGLISH_US",
            token_usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            token_usage_breakdown=None,
            used_music_model_spec="edenn_basic",
            generation_api_call_count=1,
            job_received_timestamp=None,
            job_finished_timestamp=None,
            thumbnail_path=None,
        )

    settings = SimpleNamespace(
        workdir=tmp_path / "jobs",
        music_volume=1.0,
        preserve_original_audio=False,
        upload_container="uploads",
        audio_container_name="audio",
        output_container="videos",
    )

    table = FakeUsageTable()
    recorder = UsageRecorder(
        table, auth_mode="off", logger=logging.getLogger("t"), emit=lambda a: None,
    )

    context = ApiContext(
        settings=settings,
        storage=_DisabledStorage(),
        workflow=SimpleNamespace(run=AsyncMock(side_effect=fake_workflow_run)),
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=logging.getLogger("t"),
        usage_recorder=recorder,
    )

    app = FastAPI()
    app.include_router(create_video_generation_router(context))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs/video",
            data={"modelspec": "edenn_basic"},
            files={
                "video": (
                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.name,
                    SCENE_SEGMENTATION_REGRESSION_VIDEO_PATH.read_bytes(),
                    "video/mp4",
                )
            },
        )
    return response, table


def test_sync_video_endpoint_records_usage(tmp_path):
    """Router-level: the sync video handler writes exactly one ledger row."""
    response, table = _post_sync_video(tmp_path)
    assert response.status_code == 200, response.text
    assert len(table.rows) == 1
    row = table.rows[0]
    assert row["endpoint"] == "/api/v1/jobs/video"
    assert row["total_tokens"] == 15
    assert row["status"] == "completed"
    assert row["PartitionKey"] == "anonymous"  # off mode, no principal


def test_sync_video_endpoint_billing_log_mode_enriches_row(tmp_path):
    """End-to-end v1 slice: billed fields + video_duration_s land on the row."""
    import pytest as _pytest

    from EdennCode.Deployment.billing import set_billing_override
    from EdennCode.Deployment.billing.engine import BillingEngine
    from EdennCode.Deployment.billing.stores import AccountStore, PricingStore, WalletTxnStore
    from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

    pricing = PricingStore(FakeBillingTable(), logger=logging.getLogger("t"))
    asyncio.run(pricing.upsert(price_key="video_music",
                               billing_mode="per_request",
                               unit_price_micros=10_000_000))
    engine = BillingEngine(
        mode="log",
        account_store=AccountStore(FakeBillingTable(), logger=logging.getLogger("t")),
        txn_store=WalletTxnStore(FakeBillingTable(), logger=logging.getLogger("t")),
        pricing_store=pricing,
        logger=logging.getLogger("t"),
    )
    set_billing_override(engine)
    try:
        response, table = _post_sync_video(tmp_path)
    finally:
        set_billing_override(None)
    assert response.status_code == 200, response.text
    assert len(table.rows) == 1
    row = table.rows[0]
    assert row["billing_mode"] == "per_request"
    assert row["billed_units"] == 1
    assert row["billed_amount_usd"] == 10.0
    # _FakeVideoMetadata probes duration 1.0 — the duration pass-through works.
    assert row["video_duration_s"] == _pytest.approx(1.0)


def test_full_billing_wiring_gate_admin_and_account_endpoints():
    """api.py-shaped composition: middleware + real gate + engine + routers."""
    from EdennCode.Deployment.billing import set_billing_override
    from EdennCode.Deployment.billing.account_router import create_account_router
    from EdennCode.Deployment.billing.admin_router import create_billing_admin_router
    from EdennCode.Deployment.billing.engine import BillingEngine
    from EdennCode.Deployment.billing.gate import create_billing_gate
    from EdennCode.Deployment.billing.stores import AccountStore, PricingStore, WalletTxnStore
    from EdennCode.Deployment.Testing.test_billing_stores import FakeBillingTable

    logger = logging.getLogger("t")
    engine = BillingEngine(
        mode="enforce",
        account_store=AccountStore(FakeBillingTable(), logger=logger),
        txn_store=WalletTxnStore(FakeBillingTable(), logger=logger),
        pricing_store=PricingStore(FakeBillingTable(), logger=logger),
        logger=logger,
    )
    set_billing_override(engine)
    try:
        app = FastAPI()
        # SAME ORDER AS api.py: auth middleware with billing gate inside.
        app.middleware("http")(create_auth_middleware(
            mode="enforce", key_store=StubKeyStore(PRINCIPAL), logger=logger,
            billing_gate=create_billing_gate(logger),
        ))
        app.include_router(create_billing_admin_router(
            account_store=engine.account_store, txn_store=engine.txn_store,
            pricing_store=engine.pricing_store, admin_secret="adm-test",
            logger=logger))
        app.include_router(create_account_router(
            account_store=engine.account_store, usage_recorder=None,
            logger=logger))

        @app.post("/api/v1/jobs/video")
        def job():
            return {"ok": True}

        client = TestClient(app)
        auth = {"Authorization": "Bearer sk-good"}
        admin = {"x-admin-secret": "adm-test"}

        # No account yet: billable submit blocked, balance endpoint 404s.
        blocked = client.post("/api/v1/jobs/video", headers=auth)
        assert blocked.status_code == 402
        assert blocked.json()["code"] == "insufficient_balance"
        assert client.get("/api/v1/account/balance", headers=auth).status_code == 404
        # Account endpoints without a key are rejected by the middleware itself.
        assert client.get("/api/v1/account/balance").status_code == 401

        # Admin seeds account + recharge (admin paths bypass Bearer middleware
        # in prod via exemption; here the router guard alone is exercised).
        assert client.post("/api/v1/admin/accounts",
                           json={"account_id": "user-1",
                                 "registered_name": "User One"},
                           headers={**admin, **auth}).status_code == 200
        assert client.post("/api/v1/admin/accounts/user-1/recharge",
                           json={"amount_usd": 50},
                           headers={**admin, **auth}).status_code == 200

        # Funded: submit passes, balance endpoint reflects the wallet.
        assert client.post("/api/v1/jobs/video", headers=auth).status_code == 200
        balance = client.get("/api/v1/account/balance", headers=auth).json()
        assert balance["balance_usd"] == 50.0

        # Drain to zero via adjustment: gate closes again.
        client.post("/api/v1/admin/accounts/user-1/recharge",
                    json={"amount_usd": -50}, headers={**admin, **auth})
        assert client.post("/api/v1/jobs/video", headers=auth).status_code == 402
    finally:
        set_billing_override(None)


def _build_async_video_context(tmp_path: Path, workflow_run, recorder) -> ApiContext:
    settings = SimpleNamespace(
        workdir=tmp_path / "jobs",
        music_volume=1.0,
        preserve_original_audio=False,
        upload_container="uploads",
        audio_container_name="audio",
        output_container="videos",
    )
    return ApiContext(
        settings=settings,
        storage=_DisabledStorage(),
        workflow=SimpleNamespace(run=workflow_run),
        alignment_workflow=MagicMock(),
        audio_creative_edit_workflow=MagicMock(),
        logger=logging.getLogger("t"),
        async_video_job_store=_memory_async_video_job_store,
        usage_recorder=recorder,
    )


def test_async_video_runner_records_usage_with_principal(tmp_path):
    """_run_async_video_job_inner records a completed row carrying the principal."""
    job_id = "job_async_usage_success"
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    input_video = job_dir / "input.mp4"
    input_video.write_bytes(b"video")
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_music_path = output_dir / "generated.mp3"
    remixed_video_path = output_dir / "remixed.mp4"
    generated_music_path.write_bytes(b"audio")
    remixed_video_path.write_bytes(b"video")

    async def fake_workflow_run(**kwargs):
        return SimpleNamespace(
            video_metadata=_FakeVideoMetadata(Path(kwargs["video_path"])),
            scenes=[],
            video_summary={"video_title": "Async usage test"},
            music_prompt={"style_prompt": "bright pop"},
            generated_music_path=generated_music_path,
            complete_generated_music_path=None,
            secondary_complete_generated_music_path=None,
            remixed_video_path=remixed_video_path,
            include_vocals=False,
            vocal_gender="female",
            lyrics_timestamps=[],
            word_level_lyrics_timestamps=[],
            user_requested_language="ENGLISH_US",
            token_usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            token_usage_breakdown=None,
            used_music_model_spec="edenn_basic",
            job_received_timestamp=None,
            job_finished_timestamp=None,
            thumbnail_path=None,
        )

    table = FakeUsageTable()
    recorder = UsageRecorder(
        table, auth_mode="off", logger=logging.getLogger("t"), emit=lambda a: None,
    )
    context = _build_async_video_context(
        tmp_path, AsyncMock(side_effect=fake_workflow_run), recorder,
    )
    principal = Principal(user_id="user-7", key_prefix="sk-abc123def")
    _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

    async def run() -> None:
        await _run_async_video_job_inner(
            context=context,
            job_id=job_id,
            job_dir=job_dir,
            asset_ids=RecommendationAssetIds.create(job_id=job_id),
            effective_input_video_path=input_video,
            upload_content_type="video/mp4",
            compression_applied=False,
            preserve_original_audio=False,
            requested_volume=1.0,
            include_vocals=False,
            vocal_gender="female",
            user_prompt="bright pop",
            verbose_instruction=False,
            music_style_prompt=None,
            lyrics_prompt=None,
            requested_modelspec="edenn_basic",
            audio_output_format=None,
            vocal_id=None,
            vocal_sample_path=None,
            user_id=None,
            principal=principal,
            callback_url=None,
        )
        await asyncio.sleep(0.05)  # let the fire-and-forget ledger write land

    try:
        asyncio.run(run())
    finally:
        _async_job_store.pop(job_id, None)

    assert len(table.rows) == 1
    row = table.rows[0]
    assert row["PartitionKey"] == "user-7"
    assert row["endpoint"] == "/api/v1/jobs/async_video_music_gen"
    assert row["status"] == "completed"


def test_async_video_runner_records_failure_usage_with_principal(tmp_path):
    """Failure variant: the workflow raises; expect a 'failed' row with total_tokens 0."""
    job_id = "job_async_usage_failure"
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    input_video = job_dir / "input.mp4"
    input_video.write_bytes(b"video")

    async def fake_workflow_run(**kwargs):
        raise RuntimeError("provider failed")

    table = FakeUsageTable()
    recorder = UsageRecorder(
        table, auth_mode="off", logger=logging.getLogger("t"), emit=lambda a: None,
    )
    context = _build_async_video_context(
        tmp_path, AsyncMock(side_effect=fake_workflow_run), recorder,
    )
    principal = Principal(user_id="user-7", key_prefix="sk-abc123def")
    _async_job_store[job_id] = _AsyncJobState(status=JobStatus.PENDING)

    async def run() -> None:
        await _run_async_video_job_inner(
            context=context,
            job_id=job_id,
            job_dir=job_dir,
            asset_ids=RecommendationAssetIds.create(job_id=job_id),
            effective_input_video_path=input_video,
            upload_content_type="video/mp4",
            compression_applied=False,
            preserve_original_audio=False,
            requested_volume=1.0,
            include_vocals=False,
            vocal_gender="female",
            user_prompt="bright pop",
            verbose_instruction=False,
            music_style_prompt=None,
            lyrics_prompt=None,
            requested_modelspec="edenn_basic",
            audio_output_format=None,
            vocal_id=None,
            vocal_sample_path=None,
            user_id=None,
            principal=principal,
            callback_url=None,
        )
        await asyncio.sleep(0.05)  # let the fire-and-forget ledger write land

    try:
        asyncio.run(run())
    finally:
        _async_job_store.pop(job_id, None)

    assert len(table.rows) == 1
    row = table.rows[0]
    assert row["PartitionKey"] == "user-7"
    assert row["endpoint"] == "/api/v1/jobs/async_video_music_gen"
    assert row["status"] == "failed"
    assert row["total_tokens"] == 0
