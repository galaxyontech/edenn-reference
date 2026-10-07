from __future__ import annotations

import logging
import os

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from EdennCode.Annotation.core.annotation_dispatcher import AnnotationDispatcher
from EdennCode.Annotation.store.postgres_store import PostgresAnnotationStore
from EdennCode.Deployment.audio_edit_workflows import AudioCreativeEditOrchestrator
from EdennCode.Deployment.alignment_workflows import AudioAlignmentOrchestrator
from EdennCode.Deployment.api_common import (
    ApiContext,
    HealthResponse,
    sanitized_request_validation_handler,
)
from EdennCode.Deployment.api_audio_creative_edit import create_audio_creative_edit_router
from EdennCode.Deployment.api_docs import create_docs_router
from EdennCode.Deployment.api_security import error_tracker_send_pii, resolve_cors_policy
from EdennCode.Deployment.shared_pg_pool import api_pg_client
from EdennCode.Deployment.api_multi_image_generation import create_multi_image_generation_router
from EdennCode.Deployment.api_provider_callbacks import create_provider_callbacks_router
from EdennCode.Deployment.provider_music_callbacks import (
    resolve_provider_music_callback_store,
)
from EdennCode.Deployment.api_recommendations import create_recommendations_router
from EdennCode.Deployment.api_vocal_clone import create_vocal_clone_router
from EdennCode.Deployment.api_video_alignment import create_video_alignment_router
from EdennCode.Deployment.api_video_generation import create_video_generation_router
from EdennCode.Deployment.async_pipeline_v2.api import create_async_pipeline_v2_router
from EdennCode.Deployment.async_video_job_store import (
    build_async_multi_image_job_store_from_env,
)
from EdennCode.Deployment.auth.admin_router import create_admin_router
from EdennCode.Deployment.auth.key_store import ApiKeyStore
from EdennCode.Deployment.auth.middleware import create_auth_middleware, resolve_auth_mode
from EdennCode.Deployment.auth.telemetry import setup_telemetry
from EdennCode.Deployment.auth.usage_recorder import resolve_usage_recorder
from EdennCode.Deployment.auth.console_session import ConsoleSessionResolver
from EdennCode.Deployment.auth.firebase_verifier import FirebaseTokenVerifier
from EdennCode.Deployment.console_static import (
    create_console_config_router,
    mount_console,
)
from EdennCode.Deployment.auth.signup_router import create_signup_router
from EdennCode.Deployment.billing import get_billing_stores, resolve_billing
from EdennCode.Deployment.billing.account_router import create_account_router
from EdennCode.Deployment.billing.admin_router import create_billing_admin_router
from EdennCode.Deployment.billing.gate import create_billing_gate
from EdennCode.Deployment.multi_image_workflows import MultiImageGenerationOrchestrator
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.recommendation_persistence import RecommendationPersistenceService
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Deployment.vocal_clone_workflows import VocalCloneOrchestrator
from EdennCode.Deployment.workflows import VideoGenerationOrchestrator
from EdennCode.env import load_env

load_env()

logger = logging.getLogger("edenn.api")
logging.basicConfig(level=os.getenv("API_LOG_LEVEL", "INFO"))

settings = DeploymentSettings.from_env()

setup_telemetry(
    connection_string=settings.appinsights_connection_string,
    role="api",
    logger=logging.getLogger("EdennCode.Deployment.telemetry"),
)

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        # PII (user IPs, request data, prompt/lyric text) is not shipped to the
        # error tracker unless the deployment explicitly opts in.
        send_default_pii=error_tracker_send_pii(os.getenv("SENTRY_SEND_DEFAULT_PII", "")),
        traces_sample_rate=settings.sentry_traces_sample_rate,
        environment=settings.sentry_environment,
        integrations=[
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
    )

def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


storage = create_storage_service(settings)

annotation_dispatcher = None
if _env_truthy("ANNOTATION_DISPATCHER_ENABLED"):
    try:
        annotation_dispatcher = AnnotationDispatcher(
            stores=[PostgresAnnotationStore(client=PostgresClient.from_env())],
        )
        logger.info("AnnotationDispatcher enabled with PostgresAnnotationStore.")
    except Exception as exc:
        logger.warning(
            "AnnotationDispatcher disabled because initialization failed: %s",
            exc,
            exc_info=True,
        )

workflow = VideoGenerationOrchestrator(
    storage=storage,
    llm_image_container=settings.llm_image_container,
    llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
    llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    annotation_dispatcher=annotation_dispatcher,
)
alignment_workflow = AudioAlignmentOrchestrator()
audio_creative_edit_workflow = AudioCreativeEditOrchestrator(
    storage=storage,
    llm_image_container=settings.llm_image_container,
    llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
    llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
)
multi_image_workflow = MultiImageGenerationOrchestrator()
multi_image_async_job_store = build_async_multi_image_job_store_from_env(
    client_factory=api_pg_client,
)
# Callback store: pool the receiver's write path and fail closed (refuse to boot)
# if the callback wait path is enabled but the store resolves to in-memory.
resolve_provider_music_callback_store(client_factory=api_pg_client, logger=logger)
vocal_clone_workflow = VocalCloneOrchestrator()
recommendation_persistence = None
if _env_truthy("RECOMMENDATION_PERSISTENCE_ENABLED"):
    try:
        recommendation_persistence = RecommendationPersistenceService.from_video_music_db()
    except Exception as exc:
        logger.warning(
            "Recommendation persistence is disabled because initialization failed: %s",
            exc,
            exc_info=True,
        )
auth_mode = resolve_auth_mode(settings.auth_mode)
usage_recorder = resolve_usage_recorder(settings, logger)
billing_engine = resolve_billing(settings, logger)
# The key store comes from the billing store factory, not from a second
# from_settings call: accounts, keys and the wallet must all live in the same
# store, or a customer can authenticate against one and be billed against
# another. Falls back to Table Storage when billing is unconfigured.
_billing_stores = get_billing_stores()
auth_key_store = (getattr(_billing_stores, "key_store", None)
                  or ApiKeyStore.from_settings(settings, logger))
if usage_recorder is not None:
    # Dual-write of usage rows into Postgres, and — once BILLING_PG_PRIMARY is
    # set — the 详单 reads move with them. Writes always stay on this recorder,
    # so both ledgers remain complete and the cutover stays reversible.
    usage_recorder.attach_billing_stores(_billing_stores)
if auth_mode == "enforce" and auth_key_store is None:
    logger.error(
        "AUTH_MODE=enforce with no key store configured — every non-exempt "
        "request will receive 503 until Azure Table Storage is reachable."
    )
context = ApiContext(
    settings=settings,
    storage=storage,
    workflow=workflow,
    alignment_workflow=alignment_workflow,
    audio_creative_edit_workflow=audio_creative_edit_workflow,
    logger=logger,
    multi_image_workflow=multi_image_workflow,
    multi_image_async_job_store=multi_image_async_job_store,
    vocal_clone_workflow=vocal_clone_workflow,
    recommendation_persistence=recommendation_persistence,
    annotation_dispatcher=annotation_dispatcher,
    auth_key_store=auth_key_store,
    usage_recorder=usage_recorder,
)

# Console identity (subproject A/B). Both are None unless FIREBASE_PROJECT_ID is
# set, in which case verified signup and console sessions simply are not served.
firebase_verifier = FirebaseTokenVerifier.from_settings(settings, logger)
console_session_resolver = ConsoleSessionResolver.from_settings(
    settings,
    verifier=firebase_verifier,
    index_store=billing_engine.index_store if billing_engine else None,
    logger=logger,
)

app = FastAPI(
    title="Edenn Media Service",
    version="0.1.0",
    description="Upload an advertisement video and receive a remixed version with generated music.",
)

app.add_exception_handler(
    RequestValidationError, sanitized_request_validation_handler
)

# API-key auth (AUTH_MODE = off | log | enforce). Registered before CORS so the
# CORS layer wraps it and 401/503 responses still carry CORS headers. The
# billing gate rides inside it for billable POST submissions (402/403 when
# BILLING_MODE=enforce and the account is broke/inactive).
app.middleware("http")(
    create_auth_middleware(
        mode=auth_mode,
        key_store=auth_key_store,
        logger=logger,
        billing_gate=create_billing_gate(logger),
        console_session_resolver=console_session_resolver,
    )
)

# CORS is closed by default (same-origin only). Set API_ALLOWED_ORIGINS to an
# explicit comma-separated origin list to allow a browser frontend; a wildcard
# ("*") is accepted but never combined with credentials.
cors_policy = resolve_cors_policy(os.getenv("API_ALLOWED_ORIGINS", ""))
if cors_policy is not None:
    if cors_policy["allow_origins"] == ["*"]:
        logger.warning(
            "CORS: wildcard origin enabled (credentials disabled); set "
            "API_ALLOWED_ORIGINS to explicit origins in production.",
        )
    app.add_middleware(CORSMiddleware, **cors_policy)


@app.get("/healthz", response_model=HealthResponse)
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")


app.include_router(create_video_alignment_router(context))
app.include_router(create_docs_router())
app.include_router(create_vocal_clone_router(context))
app.include_router(create_audio_creative_edit_router(context))
app.include_router(create_multi_image_generation_router(context))
app.include_router(create_provider_callbacks_router(context))
app.include_router(create_video_generation_router(context))
app.include_router(create_recommendations_router(context))
app.include_router(
    create_admin_router(
        key_store=auth_key_store,
        usage_recorder=usage_recorder,
        admin_secret=settings.api_admin_secret,
        logger=logger,
    )
)
app.include_router(
    create_billing_admin_router(
        account_store=billing_engine.account_store if billing_engine else None,
        txn_store=billing_engine.txn_store if billing_engine else None,
        pricing_store=billing_engine.pricing_store if billing_engine else None,
        index_store=billing_engine.index_store if billing_engine else None,
        admin_secret=settings.api_admin_secret,
        logger=logger,
        rmb_per_usd=settings.billing_rmb_per_usd,
        usage_recorder=usage_recorder,
    )
)
# User-facing balance/详单. Deliberately NOT auth-exempt: identity comes from
# the Bearer key middleware (which resolves principals in every AUTH_MODE).
app.include_router(
    create_account_router(
        account_store=billing_engine.account_store if billing_engine else None,
        usage_recorder=usage_recorder,
        key_store=auth_key_store,
        logger=logger,
    )
)
# Public self-serve signup (auth-exempt; see middleware._EXEMPT_EXACT). Serves
# only while BILLING_MODE=enforce, because a zero-balance wallet is what keeps
# a self-minted key from spending.
app.include_router(
    create_signup_router(
        key_store=auth_key_store,
        billing_engine=billing_engine,
        logger=logger,
        firebase_verifier=firebase_verifier,
        admin_secret=settings.api_admin_secret,
    )
)
if _env_truthy("ASYNC_PIPELINE_V2_ENABLED"):
    app.include_router(create_async_pipeline_v2_router(context))
# Agentic Audio is deliberately NOT mounted here. It is a standalone product
# with its own deployment cycle, its own infrastructure and its own database
# (owner decision, 2026-08-25); running its code inside this process would put
# it back on this app's config and release cadence. It ships from
# EdennCode/EdennAgent/AgenticAudio/design/devserver.py as its own container.
# Firebase's public client config, read by the console at load time so those
# values live in exactly one place (this env block).
app.include_router(create_console_config_router(settings=settings, logger=logger))
# Same-origin console (V2). No-op when the static export has not been built;
# the Firebase Hosting deployment (V1) serves the same artifact independently.
mount_console(app, logger)


__all__ = ["app"]
