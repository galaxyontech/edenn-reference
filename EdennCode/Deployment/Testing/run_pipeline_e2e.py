"""
Live end-to-end pipeline run with annotation + taxonomy enrichment.

Runs the full VideoMusicWorkflowE2E on a real smoke video, persists all
seven annotation events to PostgreSQL, then runs the LLM taxonomy enrichment
pass and retrieves the results from the DB.

Usage:
    python -m EdennCode.Deployment.Testing.run_pipeline_e2e

Environment variables required (same as production API):
    AZURE_ENDPOINT, AZURE_MODEL, AZURE_API_KEY
    PROVIDER_A_API_KEY  (or PROVIDER_A_API_KEY)
    DATABASE_URL  (or PG_HOST / PG_USER / PG_PASSWORD / PG_DBNAME)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.env import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e_runner")

# ── Internal imports (after env is loaded) ────────────────────────────────────
from EdennCode.Annotation.core.annotation_dispatcher import AnnotationDispatcher
from EdennCode.Annotation.core.annotation_event import AnnotationEvent
from EdennCode.Annotation.core.annotation_store import AnnotationStore
from EdennCode.Annotation.enrichment.enrichment_processor import EnrichmentProcessor
from EdennCode.Annotation.enrichment.taxonomy_extractor import TaxonomyExtractor
from EdennCode.Annotation.store.postgres_store import PostgresAnnotationStore
from EdennCode.Deployment.postgres_wrapper import PostgresClient
from EdennCode.Deployment.settings import DeploymentSettings
from EdennCode.Deployment.storage import create_storage_service
from EdennCode.Util.MediaUtils.pipeline_util import build_azure_client
from EdennCode.WorkflowFactory.VideoMusicWorkflow.Stages.VideoMusicGenerationE2E.video_music_generation_workflow import (
    VideoMusicWorkflowE2E,
    VideoMusicWorkflowE2EInput,
)
from EdennCode.TestSuites.helpers.paths import SMOKE_VIDEO_PATH


# ── Tiny sidecar store that captures the first job_id it sees ─────────────────

class _JobIdCapture(AnnotationStore):
    """Records the job_id from the first event emitted; delegates nothing."""

    def __init__(self) -> None:
        self.job_id: Optional[str] = None

    async def write(self, event: AnnotationEvent) -> None:
        if self.job_id is None:
            self.job_id = event.job_id

    async def query(self, event_type: str, limit: int = 100) -> List[AnnotationEvent]:
        return []

    async def get_by_job(self, job_id: str) -> List[AnnotationEvent]:
        return []

    async def get_by_job_and_type(self, job_id: str, event_type: str) -> List[AnnotationEvent]:
        return []

    async def all_events(self) -> List[AnnotationEvent]:
        return []


# ── Pretty-print helpers ──────────────────────────────────────────────────────

def _section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def _kv(key: str, value: Any, indent: int = 2) -> None:
    pad = " " * indent
    if isinstance(value, list):
        print(f"{pad}{key}:")
        for item in value:
            print(f"{pad}  • {item}")
    elif isinstance(value, dict):
        print(f"{pad}{key}:")
        for k, v in value.items():
            print(f"{pad}  {k}: {v}")
    else:
        print(f"{pad}{key}: {value}")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:

    # ── 1. Connect to Postgres and ensure tables exist ────────────────────────
    _section("Step 1 — Connecting to PostgreSQL and creating tables")
    pg_client = PostgresClient.from_env()
    pg_store = PostgresAnnotationStore(pg_client)
    pg_store.create_tables()
    logger.info("Tables ready.")

    # ── 2. Build dispatcher: Postgres store + job-id capture sidecar ──────────
    job_id_capture = _JobIdCapture()
    dispatcher = AnnotationDispatcher(stores=[pg_store, job_id_capture])

    # ── 3. Run the full pipeline ──────────────────────────────────────────────
    _section("Step 2 — Running VideoMusicWorkflowE2E")
    print(f"  Video : {SMOKE_VIDEO_PATH.name}")
    print(f"  Prompt: 'cinematic, uplifting, no vocals'")
    print(f"  Model : edenn_basic")

    settings = DeploymentSettings.from_env()
    storage = create_storage_service(settings)
    workflow = VideoMusicWorkflowE2E(
        storage_service=storage,
        llm_image_container=settings.llm_image_container,
        llm_image_sas_ttl_minutes=settings.llm_image_sas_ttl_minutes,
        llm_image_cleanup_delay_seconds=settings.llm_image_cleanup_delay_seconds,
    )
    workflow_input = VideoMusicWorkflowE2EInput(
        video_path=str(SMOKE_VIDEO_PATH),
        user_prompt="cinematic, uplifting, no vocals",
        music_model_spec="edenn_basic",
        annotation_dispatcher=dispatcher,
    )

    output = await workflow.generate(workflow_input)
    logger.info("Pipeline finished. Waiting 1s for background annotation tasks to flush.")
    await asyncio.sleep(1.0)

    job_id = job_id_capture.job_id
    if not job_id:
        logger.error("No job_id captured — annotation dispatcher may not have been called.")
        return

    _section("Step 3 — Pipeline output summary")
    _kv("job_id", job_id)
    _kv("model_used", output.UsedMusicModelSpecs)
    _kv("include_vocals", output.include_vocals)
    _kv("duration (video)", f"{output.video_metadata.duration:.1f}s")
    _kv("scenes", len(output.scenes))
    _kv("music_file", Path(output.generated_music_path).name if output.generated_music_path else "—")
    _kv("token_usage", output.token_usage)

    # ── 4. Run taxonomy enrichment (LLM call) ─────────────────────────────────
    _section("Step 4 — Running taxonomy enrichment (LLM)")
    llm_client = build_azure_client()
    extractor = TaxonomyExtractor(llm_client)
    processor = EnrichmentProcessor(
        store=pg_store,
        extractor=extractor,
        extraction_prompt_version="v1",
    )

    result = await processor.process_job(job_id)
    logger.info("Taxonomy enrichment result: %s", result)

    # ── 5. Retrieve annotation events from DB and display ─────────────────────
    _section("Step 5 — Retrieving annotation events from PostgreSQL")
    events = await pg_store.get_by_job(job_id)
    print(f"\n  Found {len(events)} events for job_id={job_id}")
    for ev in events:
        etype = getattr(ev, "event_type", "?")
        eid   = getattr(ev, "event_id", "?")[:8]
        print(f"  • {etype:<35} event_id={eid}…")

    # ── 6. Retrieve and display taxonomy enrichment ───────────────────────────
    _section("Step 6 — Taxonomy enrichment stored in PostgreSQL")
    enrichment_events = await pg_store.get_by_job_and_type(job_id, "taxonomy_enrichment")

    if not enrichment_events:
        print("  No taxonomy_enrichment event found.")
        return

    ev = enrichment_events[0]
    taxonomy = getattr(ev, "taxonomy", {})
    if hasattr(taxonomy, "__dict__"):
        # SimpleNamespace from _DBEvent
        t: Dict[str, Any] = taxonomy.__dict__
    elif isinstance(taxonomy, dict):
        t = taxonomy
    else:
        t = {}

    print()
    _kv("provider_name",     getattr(ev, "provider_name", "—"))
    _kv("model_spec",        getattr(ev, "model_spec", "—"))
    _kv("music_filename",    getattr(ev, "music_filename", "—"))
    _kv("extraction_model",  getattr(ev, "extraction_model", "—"))
    _kv("latency",           f"{getattr(ev, 'extraction_latency_s', 0):.2f}s")
    _kv("failed",            getattr(ev, "failed", False))

    print("\n  ── Genre Hierarchy ──────────────────────────────────")
    _kv("genre_level1",      t.get("genre_level1", "—"))
    _kv("genre_level2",      t.get("genre_level2", "—"))
    _kv("genre_tags",        t.get("genre_tags", []))

    print("\n  ── Mood & Affect ─────────────────────────────────────")
    _kv("mood_tags",         t.get("mood_tags", []))
    _kv("sentiment",         t.get("sentiment", "—"))
    _kv("energy_level",      t.get("energy_level", "—"))

    print("\n  ── Music Character ───────────────────────────────────")
    _kv("instrument_tags",   t.get("instrument_tags", []))
    _kv("tempo_class",       t.get("tempo_class", "—"))
    _kv("vocal_style",       t.get("vocal_style", "—"))

    print("\n  ── Themes & Activity ─────────────────────────────────")
    _kv("theme_tags",        t.get("theme_tags", []))
    _kv("activity_tags",     t.get("activity_tags", []))

    print("\n  ── Visual Context ────────────────────────────────────")
    _kv("location_types",    t.get("location_types", []))
    _kv("subject_types",     t.get("subject_types", []))
    _kv("motion_class",      t.get("motion_class", "—"))

    # ── 7. Show direct DB row from taxonomy_enrichments ──────────────────────
    _section("Step 7 — Raw row from taxonomy_enrichments table")
    rows = pg_client.fetch_rows(
        "taxonomy_enrichments",
        where_clause="job_id = %s ORDER BY id DESC",
        where_params=[job_id],
        limit=1,
    )
    if rows:
        row = rows[0]
        for col in [
            "job_id", "provider_name", "model_spec", "music_filename",
            "genre_level1", "genre_level2", "genre_tags",
            "mood_tags", "sentiment", "energy_level",
            "instrument_tags", "tempo_class", "vocal_style",
            "theme_tags", "activity_tags",
            "failed", "extraction_latency_s", "extraction_model",
        ]:
            val = row.get(col)
            if isinstance(val, list):
                print(f"  {col:<28}: {val}")
            else:
                print(f"  {col:<28}: {val}")
    else:
        print("  (no row found)")

    print(f"\n{'═' * 60}")
    print("  Done. All data persisted to PostgreSQL.")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
