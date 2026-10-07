# Edenn Test Coverage

This repo treats real user-style examples as golden truth. Mocked tests protect
validation, failure handling, and fast local feedback, but production confidence
comes from remote integration tests that run the API against real media assets,
real prompts, real storage, and the intended upstream provider for each case.

## Test Layers

### Local Unit And Contract Tests

Local tests run without remote provider credentials. They cover request parsing,
input validation, response serialization, storage wrappers, retry helpers, and
workflow contracts with providers mocked.

Representative commands:

```bash
./.venv/bin/python -m pytest -m "not remote_integration"
./.venv/bin/python -m pytest EdennCode/Deployment/Testing/test_api_video_generation_compression.py -vv
```

### Remote API Integration Tests

Remote API tests use FastAPI `TestClient`, real deployment settings, real media
storage, and selected real upstream providers. Provider branches that are not
under test in a specific case are patched out so each test has one clear owner.

The main video generation remote suite covers model/provider API contracts for:

- `edenn_basic` instrumental and vocal requests.
- `edenn_enhanced` instrumental and vocal requests.
- `edenn_studio` instrumental and vocal requests.
- verbose split prompts with `music_style_prompt` and `lyrics_prompt`.
- enhanced vocal clone through a real sample upload or URL.

### Golden Video Generation Tests

Golden cases live in `EdennCode/TestSuites/golden/video_generation_cases.json` and are executed
by `EdennCode/Deployment/Testing/test_api_video_generation_golden_remote_integration.py`.

Current golden coverage includes:

- real uploaded smoke video with `edenn_basic` instrumental prompt.
- real uploaded smoke video with `edenn_enhanced` vocal prompt.
- real uploaded smoke video with `edenn_studio` verbose Japanese lyric guidance.
- real production video staged through storage and submitted as `video_url`.
- real uploaded smoke video with `edenn_enhanced` vocal clone sample.

Golden assertions intentionally focus on stable truth rather than exact generated
bytes. They verify:

- successful completion and required asset IDs.
- selected `modelspec` and `include_vocals` behavior.
- video metadata bounds, width, and height.
- generated `audio_url`, `video_url`, and `complete_audio_url` where expected.
- lyrics and word-level timestamp fields for vocal-capable providers.
- `matching.used_track` for enhanced/studio primary track selection.
- explicit lyric guidance preservation for verbose lyric prompts.
- `vocal_id_used` for vocal clone cases.

### Negative And Failure Tests

Local API tests reject malformed or conflicting inputs before the workflow runs.
Examples include missing video input, invalid `modelspec`, split prompts without
`verbose_instruction=true`, verbose prompts with `user_prompt`, invalid vocal
clone model combinations, multiple vocal sample sources, and provider preparation
failures.

Retry helper tests cover remote HTTP and provider failures so CI behavior stays
predictable when providers are temporarily unavailable.

## Remote Retry Policy

Remote integration tests that call providers should wrap their operation in
`run_with_remote_rate_limit_retry`. The helper retries transient failures,
including provider rate limits, timeouts, and HTTP `429`, `500`, `502`, `503`,
and `504` responses.

GitHub CI also runs remote API and workflow matrices with `max-parallel: 1` to
reduce provider rate-limit pressure. Remote video API jobs save response payloads
under `test-results/*-payloads` for artifact review.

## GitHub CI

`.github/workflows/ci.yml` separates tests into:

- `local-core`: broad local tests excluding slow API suites.
- `api-local`: local API endpoint contract suites.
- `api-remote`: remote API suites, including `video-golden`.
- `remote-workflows`: direct workflow remote integration suites.

Remote jobs run with `STRICT_REMOTE_INTEGRATION=1`, so missing integration
environment variables fail CI instead of silently skipping.

## Adding A Golden Case

1. Add or reuse a committed media asset under `EdennCode/TestSuites/assets`.
2. Add a new entry to `EdennCode/TestSuites/golden/video_generation_cases.json`.
3. Set `required_remote_provider` to the provider that should be real.
4. Patch all unrelated providers in `patch_providers`.
5. Encode only stable expected behavior in `expected`.
6. Run local collection first:

```bash
./.venv/bin/python -m json.tool EdennCode/TestSuites/golden/video_generation_cases.json
./.venv/bin/python -m pytest EdennCode/Deployment/Testing/test_api_video_generation_golden_remote_integration.py -m remote_integration -q
```

Without remote env, the suite should skip cleanly. With CI integration secrets,
it should run strictly.
