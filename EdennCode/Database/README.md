# Telemetry MVP — operator guide

Throwaway MVP scripts that validate the schema and retrieval design from
`docs/superpowers/specs/2026-04-26-telemetry-schema-design.md` (§10/§11).

**Phase 2** (queue + consumer + middleware) replaces these scripts with the
real production write path. These MVP scripts are not for production use.

## Prereqs

1. `.env` populated with `TELEMETRY_DATABASE_URL`, `MODEL_GATEWAY_*` (see `.env.example`)
2. `vector` extension allowlisted on Azure Postgres (one-time, see Phase 1 plan Task 1 Step 4)
3. `.venv/bin/pip install -r requirements.txt`

## Run order

```bash
# 1. Schema
.venv/bin/python -m EdennCode.Database.migrations.apply

# 2. Seed corpus
.venv/bin/python -m scripts.seed_requests
.venv/bin/python -m scripts.seed_pipeline_runs

# 3. Backfill embeddings (the model gateway)
.venv/bin/python -m scripts.backfill_embeddings --table all

# 4. Search
.venv/bin/python -m scripts.search "any natural language query"
```

## Reset

```bash
PGPASSWORD=... psql ... -c "DROP TABLE IF EXISTS pipeline_stages, pipeline_runs, requests, schema_migrations CASCADE;"
```

## File ownership

- Track A (user intent): `001a_requests.sql`, `002a_requests_retrieval.sql`,
  `seed_requests.py`, `test_requests_schema.py`, `test_seed_requests.py`
- Track B (pipeline metadata): `001b_pipeline.sql`, `002b_pipeline_retrieval.sql`,
  `seed_pipeline_runs.py`, `test_pipeline_schema.py`, `test_seed_pipeline.py`
- Joint: everything else

## Tests

```bash
.venv/bin/pytest EdennCode/TestSuites/telemetry/ -v
```

All ~17 tests should pass after the run order above.
