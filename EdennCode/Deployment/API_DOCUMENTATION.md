# Edenn Media Service API

This document describes the FastAPI app implemented in `EdennCode/Deployment/api.py`
and the routers under `EdennCode/Deployment/api_*.py`.

It is intentionally scoped to the API layer. Where a field is passed through from an
underlying workflow or provider without a strict Pydantic schema, this document calls
that out instead of pretending the nested payload is more stable than the code
guarantees.

## Service Summary

| Item | Value |
|---|---|
| Service name | `Edenn Media Service` |
| App version | `0.1.0` |
| Protocol | synchronous HTTP |
| Primary POST content type | `multipart/form-data` |
| OpenAPI schema | `GET /openapi.json` |
| Interactive docs | `GET /docs`, `GET /redoc` |
| Custom docs index | `GET /api/v1/docs` |
| Health check | `GET /healthz` |

## Route Inventory

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness check |
| `GET` | `/api/v1/docs` | List custom markdown-backed API docs |
| `GET` | `/api/v1/docs/{slug}` | Return a docs page and available example artifact references |
| `GET` | `/api/v1/docs/{slug}/examples/{example_id}` | Return a saved example artifact directly |
| `POST` | `/api/v1/jobs/vocal-clone` | Prepare a vocal sample and create a reusable `vocal_id` |
| `POST` | `/api/v1/jobs/video` | Generate music for a video and return the remixed video plus metadata |
| `POST` | `/api/v1/jobs/video-align-audio` | Align an existing audio source to a video and return ranked matching segments |
| `POST` | `/api/v1/jobs/audio-creative-edit` | Regenerate a source audio track, optionally conditioned by visuals |
| `POST` | `/api/v1/jobs/multi-image` | Build a slideshow video from uploaded images and generated music (synchronous) |
| `POST` | `/api/v1/jobs/async_multi-image` | Submit a multi-image slideshow job and return a `job_id` immediately |
| `GET` | `/api/v1/jobs/async_multi-image/{job_id}` | Poll the status and final result of an async multi-image job |

## Base URL

Use your deployed host or local host, for example:

- `https://<your-host>`
- `http://localhost:8080`

## Authentication

The current FastAPI app does not enforce application-layer authentication.

If you need auth in production, add it at the gateway, reverse proxy, or middleware
layer.

## CORS

The app configures `CORSMiddleware` from `API_ALLOWED_ORIGINS`.

Current behavior:

- `allow_methods=["*"]`
- `allow_headers=["*"]`
- `allow_credentials=True`

If you serve browser clients with credentials, do not leave `API_ALLOWED_ORIGINS=*`
in production.

## Common Contract Notes

### POST request format

All job routes use `multipart/form-data`.

File uploads are regular multipart file fields. Optional structured list inputs such as
`lyrics_timestamps_json` and `image_urls_json` are sent as JSON strings inside form
fields.

### Remote media URLs

All remote media URL fields are expected to be public `http` or `https` URLs.

The API validates the URL scheme before download. Download failures are surfaced as
`400`.

### Storage-dependent fields

Blob path and signed URL fields are only populated when the configured storage backend
is enabled and the upload succeeds.

General rules:

- if storage is disabled, most `*_url` response fields are `null`
- temporary work directories are still used internally and are cleaned after the request
- the video-music and multi-image job responses expose signed URLs only; storage blob
  paths are never returned
- `audio-creative-edit` with `modelspec=edenn_studio` needs a remotely accessible
  source audio URL; if storage is enabled the API can stage an uploaded file and use
  that staged URL automatically, otherwise the caller must provide `audio_url`

### Timing fields and units

The API does not normalize all timing fields to one shared unit.

Important implications:

- `LyricsTimestamp.startS` and `LyricsTimestamp.endS` are numeric offsets passed through
  as provided or produced by the underlying workflow/provider
- `job_received_timestamp` and `job_finished_timestamp` are not normalized across all
  routes
- `POST /api/v1/jobs/video-align-audio` currently uses Unix seconds
- `POST /api/v1/jobs/multi-image` currently uses Unix milliseconds
- `POST /api/v1/jobs/video` and `POST /api/v1/jobs/audio-creative-edit` return
  workflow-provided integers without extra normalization at the API layer

Consumers should treat these values as route-specific implementation details unless
their downstream usage is tightly controlled.

### Loose nested dictionaries

Some response fields are intentionally loose dictionaries rather than rigid API models.
The API forwards them from the workflow layer.

Current examples include:

- `video_metadata.geometry`
- `video_metadata.video_summary`
- `AlignmentSegmentResponse.details`

The examples in this file show common shapes, but the API layer does not enforce an
exhaustive nested schema for those dictionaries.

### Error mapping

Error responses use FastAPI's standard shape:

```json
{
  "detail": "Human-readable error message"
}
```

The API maps internal typed errors to HTTP status codes as follows:

| Status | Meaning |
|---|---|
| `400` | Validation failure, malformed JSON form field, invalid field combination, unsupported URL scheme, or content-policy-style request rejection |
| `422` | FastAPI form validation failure, such as a missing required multipart field |
| `502` | Upstream provider, storage, or provider-authentication failure |
| `503` | Upstream rate limit |
| `504` | Upstream timeout |
| `500` | Unexpected internal or configuration failure |

## Shared Schemas

### `LyricsTimestamp`

Used by multiple routes for timestamped lyric or word data.

```json
{
  "text": "line or word",
  "startS": 0.0,
  "endS": 0.6,
  "i": 0
}
```

| Field | Type | Notes |
|---|---|---|
| `text` | string | Lyric line or word text |
| `startS` | number | Numeric timing offset, passed through without API-level unit normalization |
| `endS` | number | Numeric timing offset, passed through without API-level unit normalization |
| `i` | integer or `null` | Optional original index |

### `TokenUsageCounts`

```json
{
  "prompt_tokens": 812,
  "completion_tokens": 416,
  "total_tokens": 1228
}
```

### `TokenUsageBreakdown`

Used by `POST /api/v1/jobs/video`.

```json
{
  "user_prompt_preprocessor": {
    "prompt_tokens": 92,
    "completion_tokens": 38,
    "total_tokens": 130
  },
  "scene_understanding": {
    "prompt_tokens": 480,
    "completion_tokens": 210,
    "total_tokens": 690
  },
  "video_summary": {
    "prompt_tokens": 140,
    "completion_tokens": 96,
    "total_tokens": 236
  },
  "music_prompt_orchestration": {
    "prompt_tokens": 100,
    "completion_tokens": 72,
    "total_tokens": 172
  }
}
```

### `CreativeEditTokenBreakdown`

Used by `POST /api/v1/jobs/audio-creative-edit`.

```json
{
  "user_prompt_preprocessor": {
    "prompt_tokens": 706,
    "completion_tokens": 128,
    "total_tokens": 834
  },
  "visual_analysis": {
    "prompt_tokens": 9447,
    "completion_tokens": 497,
    "total_tokens": 9944
  },
  "creative_edit_prompt": {
    "prompt_tokens": 953,
    "completion_tokens": 155,
    "total_tokens": 1108
  }
}
```

## `GET /healthz`

Simple liveness check.

### Response

```json
{
  "status": "ok"
}
```

## Documentation Routes

The service exposes markdown-backed documentation and saved example artifacts over API
routes.

### `GET /api/v1/docs`

Returns the list of documentation pages registered by the service.

#### Response shape

```json
{
  "documents": [
    {
      "slug": "overview",
      "title": "Service API Overview",
      "summary": "Top-level API contract for the Edenn Media Service.",
      "endpoint": "/api/v1/docs/overview"
    }
  ]
}
```

Current registered slugs in code:

- `overview`
- `audio-creative-edit`
- `vocal-clone`

### `GET /api/v1/docs/{slug}`

Returns a specific documentation page with its raw markdown content and any example
artifacts that currently exist on disk.

#### Response shape

```json
{
  "slug": "overview",
  "title": "Service API Overview",
  "summary": "Top-level API contract for the Edenn Media Service.",
  "markdown_path": "EdennCode/Deployment/API_DOCUMENTATION.md",
  "markdown": "# Edenn Media Service API\n...",
  "examples": [
    {
      "id": "reference-edenn-basic-response",
      "label": "Reference edenn_basic response",
      "description": "Saved reference response from the video generation API using edenn_basic.",
      "file_path": "EdennCode/Deployment/example_runs/reference_edenn_basic.json",
      "media_type": "application/json",
      "endpoint": "/api/v1/docs/overview/examples/reference-edenn-basic-response"
    }
  ]
}
```

Notes:

- `markdown_path` is repo-relative when possible
- `examples` only includes files that currently exist on the service filesystem
- unknown slugs return `404`

### `GET /api/v1/docs/{slug}/examples/{example_id}`

Returns the referenced example artifact directly.

Behavior depends on the file type:

- `.json` files are returned as JSON
- text files such as `.md` are returned as text with the matching media type
- other files are returned as file responses

Failure cases:

- unknown `slug` returns `404`
- unknown `example_id` for a valid slug returns `404`
- known example metadata whose file is missing on disk returns `404`

## `POST /api/v1/jobs/vocal-clone`

Creates a reusable ProviderB `vocal_id` from an uploaded or remotely hosted vocal sample.

### Request

Content type:

- `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---:|---|
| `vocal_sample` | file | Conditionally | Uploaded vocal sample audio |
| `vocal_sample_url` | string | Conditionally | Public `http` or `https` vocal sample URL |

### Validation and behavior

- provide exactly one of `vocal_sample` or `vocal_sample_url`
- the resolved source audio is prepared for cloning with `ffmpeg`
- the prepared clone input is converted to `.m4a`
- the prepared clone input is capped at 30 seconds
- the prepared clone input is encoded as AAC at `160k`

### Response

```json
{
  "job_id": "7f4ac8f7e2bc45c798bf9fdf28d9c56d",
  "status": "completed",
  "vocal_sample_blob": "jobs/<job_id>/vocal-clone/vocal_sample_provider_b_vocal_clone.m4a",
  "vocal_sample_url": "https://<storage>/user-uploads/jobs/<job_id>/vocal-clone/vocal_sample_provider_b_vocal_clone.m4a?<sas>",
  "vocal_id": "vocal_123456"
}
```

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Generated request/job identifier |
| `status` | string | Always `"completed"` on success |
| `vocal_sample_blob` | string or `null` | Populated only when storage is enabled |
| `vocal_sample_url` | string or `null` | Populated only when storage is enabled |
| `vocal_id` | string | Reusable vocal clone identifier |

### Example

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/vocal-clone" \
  -F "vocal_sample=@/path/to/voice_reference.wav"
```

## `POST /api/v1/jobs/video`

Uploads a video, or downloads one from a URL, runs the video-to-music workflow, and
returns the remixed video plus generated audio metadata.

### Request

Content type:

- `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---:|---|---|
| `video` | file | Conditionally | n/a | Input video upload |
| `video_url` | string | No | `null` | Public `http` or `https` video URL |
| `preserve_original_audio` | boolean | No | `false` | Request to preserve original audio in the remix; effective value is `request OR API_PRESERVE_ORIGINAL_AUDIO` |
| `compression_flag` | boolean | No | `false` | If true, compress the resolved input video to max height `1280` before workflow execution |
| `music_volume` | number | No | `API_GENERATED_MUSIC_VOLUME` | Generated music mix volume passed to the workflow |
| `water_mark` | boolean | No | `false` | Append the Edenn spoken watermark to complete/full-track audio outputs |
| `include_vocals` | boolean | No | `false` | Vocal generation hint sent to the workflow |
| `vocal_gender` | string | No | `"female"` | Vocal gender hint sent to the workflow |
| `user_prompt` | string | No | `""` | Free-text generation instruction |
| `verbose_instruction` | boolean | No | `false` | Opt into structured prompt routing with `music_style_prompt` and optional `lyrics_prompt` |
| `music_style_prompt` | string | No | `null` | Required when `verbose_instruction=true`; explicit style/music direction adapted with video understanding |
| `lyrics_prompt` | string | No | `null` | Optional when `verbose_instruction=true`; explicit lyric-generation guidance |
| `modelspec` | string | No | `"edenn_basic"` | Requested music model |
| `audio_output_format` | string | No | `null` | Optional downstream output format hint; not API-validated |
| `vocal_id` | string | No | `null` | Reusable vocal clone ID for `edenn_enhanced` requests |
| `vocal_sample` | file | No | n/a | Inline vocal sample for `edenn_enhanced` vocal cloning |
| `vocal_sample_url` | string | No | `null` | Remote inline vocal sample URL for `edenn_enhanced` vocal cloning |

### Accepted `modelspec` values

Normalized accepted values:

| Request value | Normalized value |
|---|---|
| `edenn_basic` | `edenn_basic` |
| `edenn_enhanced` | `edenn_enhanced` |
| `edenn_studio` | `edenn_studio` |
| `provider_a` | `edenn_basic` |
| `provider_a` | `edenn_basic` |
| `provider_a` | `edenn_basic` |
| `provider_c` | `edenn_studio` |

Any other value returns `400`.

### Validation and behavior

- provide at least one of `video` or `video_url`
- if both `video` and `video_url` are provided, `video_url` wins and the uploaded file
  is ignored
- `compression_flag=true` compresses the resolved input file before workflow execution
  only when the helper produces a different output file
- the staged input actually sent into the workflow may be the compressed file rather
  than the original upload; it is not returned in the response
- provide either `vocal_id` or `vocal_sample` / `vocal_sample_url`, not both
- inline vocal clone inputs are only accepted when `modelspec` resolves to
  `edenn_enhanced`
- `verbose_instruction=true` requires an empty `user_prompt`, requires
  `music_style_prompt`, accepts optional `lyrics_prompt`, and is only accepted
  for requested `modelspec=edenn_enhanced` or `modelspec=edenn_studio`
- `music_style_prompt` and `lyrics_prompt` are rejected unless
  `verbose_instruction=true`
- when `vocal_sample` or `vocal_sample_url` is used inline, the API prepares that
  sample with the same 30-second `.m4a` normalization used by
  `POST /api/v1/jobs/vocal-clone`

### Response

Successful responses use the following top-level schema:

```json
{
  "job_id": "13d35b2d8fc8457fa90fbe214ff2d304",
  "status": "completed",
  "version": "1.4.0",
  "request_metadata": {
    "modelspec": "edenn_enhanced",
    "include_vocals": true,
    "vocal_gender": "female",
    "user_requested_language": "ENGLISH_US"
  },
  "response_metadata": {
    "job_received_timestamp": 1751270400000,
    "job_finished_timestamp": 1751270512000
  },
  "cost_metadata": {
    "model_spec_name": "edenn-perceptron-1.1",
    "creation_cost": 0.065,
    "creation_times": 1,
    "token_num": 1228,
    "token_cost": 0.019848
  },
  "video_metadata": {
    "video_url": "https://<storage>/generated-media/jobs/<job_id>/video/remixed.mp4?<sas>",
    "thumbnail_url": "https://<storage>/generated-media/jobs/<job_id>/thumbnail/thumb.webp?<sas>",
    "geometry": {
      "width": 1080,
      "height": 1920,
      "duration": 15.31,
      "duration_s": 15.31,
      "fps": 30.0,
      "has_audio": true
    },
    "scenes": [
      {
        "scene_index": 0,
        "start_timestamp": 0.0,
        "end_timestamp": 4.9,
        "visual_summary": "string",
        "key_actions": "string",
        "mood": "string"
      }
    ],
    "video_summary": {
      "video_title": "string",
      "video_description": "string",
      "music_title": "string"
    }
  },
  "audio_metadata": {
    "audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/generated.mp3?<sas>",
    "audio_duration_s": 15.31,
    "audio_size_bytes": 245760,
    "complete_audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/complete/complete_audio.mp3?<sas>",
    "complete_audio_duration_s": 203.1,
    "complete_audio_size_bytes": 3251072,
    "music_title": "Neon Drift",
    "music_description": {
      "style_prompt": "string",
      "lyrics_prompt": "string",
      "global_music_prompt": null,
      "global_mood": null,
      "tempo_bpm": null,
      "instruments": []
    },
    "full_lyrics": "string",
    "full_lyrics_timestamps": [],
    "full_word_level_lyrics_timestamps": [],
    "lyrics_timestamps": [],
    "word_level_lyrics_timestamps": []
  }
}
```

The response is an envelope (`job_id`, `status`, `version`) plus five metadata blocks.
Only signed URLs are returned — storage blob paths are server-side. Exactly one track is
generated per job, so no field is qualified as primary or secondary.

The multi-image response (`POST /api/v1/jobs/multi-image`) uses this same shape, with a
few additions: `request_metadata.lyrics_language`, `response_metadata.compression_applied`,
and `video_metadata.video_title` / `video_metadata.video_description`. Its
`audio_metadata.music_description` is a plain string rather than an object.

### Response fields

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Generated request/job identifier |
| `status` | string | Always `"completed"` on success |
| `version` | string | Service version that produced the response |
| **`request_metadata`** | object | What the caller asked for, as the pipeline resolved it |
| `request_metadata.modelspec` | string | Model spec actually used, falling back to the normalized request value |
| `request_metadata.include_vocals` | boolean | Final vocal flag |
| `request_metadata.vocal_gender` | string | Final vocal gender |
| `request_metadata.user_requested_language` | string | Language identifier |
| `request_metadata.lyrics_language` | string | Multi-image only |
| **`response_metadata`** | object | Job lifecycle facts |
| `response_metadata.job_received_timestamp` | integer or `null` | Epoch ms when the job was accepted |
| `response_metadata.job_finished_timestamp` | integer or `null` | Epoch ms when the job completed |
| `response_metadata.compression_applied` | boolean | Multi-image only |
| **`cost_metadata`** | object | Cost summary |
| `cost_metadata.model_spec_name` | string | Billing model-spec name |
| `cost_metadata.creation_cost` | number | USD generation cost |
| `cost_metadata.creation_times` | integer | Generation/extension provider call count |
| `cost_metadata.token_num` | integer or `null` | Total tokens consumed by the LLM stages |
| `cost_metadata.token_cost` | number or `null` | USD token cost at baseline rates × 1.2 |
| **`video_metadata`** | object | The delivered video and what the pipeline understood about it |
| `video_metadata.video_url` | string or `null` | Signed URL for the output video |
| `video_metadata.thumbnail_url` | string or `null` | Signed URL for the generated thumbnail |
| `video_metadata.geometry` | object | Probed geometry: `width`, `height`, `duration`, `duration_s`, `fps`, plus any further probed keys (codecs, bitrates, `audio_activity`) |
| `video_metadata.scenes` | array | Scene list with `scene_index`, `start_timestamp`, `end_timestamp`, `visual_summary`, `key_actions`, `mood`. Empty for multi-image |
| `video_metadata.video_summary` | object or string | Understanding summary; keys such as `video_title`, `video_description`, `music_title`, `summary`, `overall_mood` |
| `video_metadata.video_title` | string | Multi-image only |
| `video_metadata.video_description` | string | Multi-image only |
| **`audio_metadata`** | object | The generated music |
| `audio_metadata.audio_url` | string or `null` | Signed URL for the clip muxed into the video |
| `audio_metadata.audio_duration_s` | number or `null` | Duration of that clip |
| `audio_metadata.audio_size_bytes` | integer or `null` | Byte size of that clip |
| `audio_metadata.complete_audio_url` | string or `null` | Signed URL for the full-length track the clip was cut from |
| `audio_metadata.complete_audio_duration_s` | number or `null` | Duration of the full track |
| `audio_metadata.complete_audio_size_bytes` | integer or `null` | Byte size of the full track |
| `audio_metadata.music_title` | string | Song-style track name. Never empty |
| `audio_metadata.music_description` | object | `style_prompt`, `lyrics_prompt`, `global_music_prompt`, `global_mood`, `tempo_bpm`, `instruments`. A plain string on multi-image |
| `audio_metadata.full_lyrics` | string or `null` | Lyrics for the full track |
| `audio_metadata.full_lyrics_timestamps` | array of `LyricsTimestamp` | Line-level timestamps over the full track |
| `audio_metadata.full_word_level_lyrics_timestamps` | array of `LyricsTimestamp` | Word-level timestamps over the full track |
| `audio_metadata.lyrics_timestamps` | array of `LyricsTimestamp` | Line-level timestamps aligned to the delivered video |
| `audio_metadata.word_level_lyrics_timestamps` | array of `LyricsTimestamp` | Word-level timestamps aligned to the delivered video |

### Example

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/video" \
  -F "video=@/path/to/input.mp4" \
  -F "user_prompt=Energetic cinematic soundtrack." \
  -F "modelspec=edenn_basic"
```

Remote video input:

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/video" \
  -F "video_url=https://example.com/input.mov" \
  -F "compression_flag=true" \
  -F "modelspec=edenn_studio"
```

## Async v2 Video Music Frontend Flow

Use async v2 for full video-music generation when the frontend wants an async
job id immediately and a final remixed video URL later. The frontend calls the
API container with the same input pattern as v1: provide a source `video` upload
or a `video_url`. The API stages the source internally, creates a durable job,
enqueues the first task, and returns `job_id`, `status_url`, and `events_url`.

Frontend should send:

```json
{
  "mode": "split"
}
```

`mode=split` is the deployed production path for async v2. `mode=monolith` is
still accepted for fallback/dev parity, but the normal frontend path should use
`split`.

### Optional: Stage the source video for reuse

```text
POST /api/v2/assets/video
```

Content type:

- `multipart/form-data`

Provide exactly one of:

| Field | Type | Required | Description |
|---|---|---:|---|
| `video` | file | Conditionally | Uploaded source video |
| `video_url` | string | Conditionally | Public or signed source video URL |
| `creator_user_id` | string | No | Optional frontend/user identifier |
| `session_id` | string | No | Optional frontend session identifier |

Frontend example using a video URL:

```ts
async function stageVideoAsset(baseUrl: string, videoUrl: string) {
  const form = new FormData();
  form.append("video_url", videoUrl);

  const response = await fetch(`${baseUrl}/api/v2/assets/video`, {
    method: "POST",
    body: form,
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return response.json();
}
```

Response example:

```json
{
  "artifact_id": "artifact_abc123",
  "job_id": "asset_job_abc123",
  "artifact_type": "source_video",
  "status": "completed",
  "version": "dev-cloudshell-v2-compress-20260521220125",
  "container": "user-uploads",
  "blob_name": "jobs/asset_job_abc123/input/source_video.mp4",
  "url": "https://<storage>/jobs/asset_job_abc123/input/source_video.mp4?<sas>",
  "content_type": "video/mp4",
  "metadata": {
    "duration": 60.0,
    "width": 2560,
    "height": 1440,
    "size_bytes": 91877680
  },
  "status_url": "/api/v2/jobs/asset_job_abc123"
}
```

This endpoint is optional. Use it only when a caller intentionally wants to
stage a reusable source video artifact. The normal frontend video URL flow can
skip this step and call `POST /api/v2/jobs/video-music` directly.

### Submit the video-music job

```text
POST /api/v2/jobs/video-music
```

Content type:

- `multipart/form-data`

Minimum valid request:

```bash
BASE_URL="https://staging-app.worker.example.invalid"

curl -sS -X POST "$BASE_URL/api/v2/jobs/video-music" \
  -F "video_url=https://cdn.example.com/videos/input.mp4"
```

Recommended frontend request:

```ts
async function submitAsyncV2VideoMusicJob(
  baseUrl: string,
  input: {
    videoFile?: File;
    videoUrl?: string;
    prompt?: string;
    modelspec?: "edenn_basic" | "edenn_enhanced" | "edenn_studio";
  },
) {
  const form = new FormData();

  if (input.videoUrl) {
    form.append("video_url", input.videoUrl);
  } else if (input.videoFile) {
    form.append("video", input.videoFile);
  } else {
    throw new Error("Provide videoFile or videoUrl");
  }

  form.append("mode", "split");
  form.append("modelspec", input.modelspec ?? "edenn_basic");
  form.append("user_prompt", input.prompt ?? "");
  form.append("compression_flag", "true");
  form.append("compression_max_height", "720");
  form.append("music_volume", "0.8");

  const response = await fetch(`${baseUrl}/api/v2/jobs/video-music`, {
    method: "POST",
    body: form,
  });

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return response.json();
}
```

Request fields:

| Field | Type | Required | Default | Frontend guidance |
|---|---|---:|---|---|
| `video` | file | Conditionally | n/a | Same as v1; uploaded source video |
| `video_url` | string | Conditionally | n/a | Same as v1; public or signed source video URL |
| `mode` | string | No | `"monolith"` | Send `"split"` for normal frontend async v2 jobs |
| `modelspec` | string | No | `"edenn_basic"` | Use `edenn_basic`, `edenn_enhanced`, or `edenn_studio` |
| `user_prompt` | string | No | `""` | User music direction. Vocal intent and gender are inferred from this prompt |
| `compression_flag` | boolean | No | `false` | Recommend `true` for frontend video URL flow |
| `compression_max_height` | integer | No | `1280` | Recommend `720` for faster frontend jobs; allowed range is `1..4320` |
| `music_volume` | number | No | `1.0` | Optional generated music volume |
| `water_mark` | boolean | No | `false` | Append the Edenn spoken watermark to complete/full-track audio outputs |
| `preserve_original_audio` | boolean | No | `false` | Optional original-audio preservation |
| `verbose_instruction` | boolean | No | `false` | Deprecated no-op on v2 — accepted and ignored |
| `music_style_prompt` | string | No | `null` | Deprecated alias for `user_prompt` (used only when `user_prompt` is empty) |
| `lyrics_prompt` | string | No | `null` | Lyric direction; supplying it turns vocals on. Requires `modelspec=edenn_enhanced` or `edenn_studio` — otherwise 400, error `10009` |
| `audio_output_format` | string | No | `null` | Optional output audio format |
| `vocal_id` | string | No | `null` | Optional reusable vocal clone ID for `edenn_enhanced` |
| `vocal_sample` | file | No | n/a | Optional inline vocal sample for `edenn_enhanced` |
| `vocal_sample_url` | string | No | `null` | Optional remote vocal sample for `edenn_enhanced` |
| `user_id` | string | No | `null` | Optional caller/user identifier for tracking |
| `session_id` | string | No | `null` | Optional frontend session identifier for tracking |
| `creator_user_id` | string | No | `null` | Optional creator identifier; defaults to `user_id` when omitted |
| `callback_url` | string | No | `null` | Reserved for callback-compatible async clients |
| `priority` | integer | No | `0` | Higher values lease first |
| `max_attempts` | integer | No | `3` | Recommend `1` for early testing |

`source_video_artifact_id` is still accepted for internal/debug reuse flows, but
frontend should not use it for normal v1/v2-compatible submission.

V2 does not accept separate `include_vocals` or `vocal_gender` request fields.
The workflow infers final vocal intent and gender from `user_prompt` or, in
verbose mode, `lyrics_prompt`.

Send exactly one of `video` or `video_url`. If both are supplied, the current
implementation prefers `video_url` and ignores the upload.

Accepted `modelspec` values are:

| Value | Notes |
|---|---|
| `edenn_basic` | Instrumental/basic generation path |
| `edenn_enhanced` | Enhanced provider path; ask for vocals in the prompt or `lyrics_prompt` to test vocals |
| `edenn_studio` | Studio provider path; ask for vocals in the prompt or `lyrics_prompt` to test vocals |

Job accepted response:

```json
{
  "job_id": "job_abc123",
  "task_id": "job_abc123:video-preprocess",
  "status": "queued",
  "version": "dev-asyncv2-v1form-split-20260616t055328z-f2f8e98",
  "status_url": "/api/v2/jobs/job_abc123",
  "events_url": "/api/v2/jobs/job_abc123/events"
}
```

The frontend should store `job_id` and poll the `status_url`.

### Poll job status

```text
GET /api/v2/jobs/{job_id}
```

Frontend polling example:

```ts
async function pollVideoMusicJob(baseUrl: string, jobId: string) {
  const response = await fetch(`${baseUrl}/api/v2/jobs/${jobId}`);

  if (!response.ok) {
    throw new Error(await response.text());
  }

  return response.json();
}
```

Typical queued/processing response shape:

```json
{
  "job_id": "job_abc123",
  "job_type": "video_music",
  "status": "processing",
  "current_stage": "analysis_and_planning",
  "progress_percent": 20,
  "request": {
    "video_url": "https://example.com/input.mp4",
    "requested_source_video_url": "https://example.com/input.mp4",
    "modelspec": "edenn_basic",
    "compression_flag": true,
    "compression_max_height": 1280
  },
  "result": null,
  "error": null
}
```

`request` echoes only the fields the caller actually submitted. Server-injected
internals — `mode`, `priority`, `max_attempts`, `job_received_timestamp`, the
resolved artifact handles (`source_video_artifact_id`, `image_artifact_ids`,
`vocal_artifact_id`, `vocal_sample_path`) and the recommendation asset ids
(`video_id`, `creative_id`, `primary_music_id`, `secondary_music_id`,
`selected_music_id`, `alignment_id`) — are stripped from the response. What the
caller supplied is still echoed, including `requested_source_video_artifact_id`
and `requested_source_video_url`.

Completed response shape:

```json
{
  "job_id": "job_abc123",
  "status": "completed",
  "current_stage": "selection_ranking_remix_finalize",
  "progress_percent": 100,
  "result": {
    "job_id": "job_abc123",
    "status": "completed",
    "upload_url": "https://<storage>/jobs/job_abc123/input/compressed/source_video.mp4?<sas>",
    "audio_url": "https://<storage>/jobs/job_abc123/audio/matched_audio.mp3?<sas>",
    "complete_audio_url": null,
    "video_url": "https://<storage>/jobs/job_abc123/video/remixed_video.mp4?<sas>",
    "thumbnail_url": "https://<storage>/jobs/job_abc123/thumbnail/thumbnail.webp?<sas>",
    "video_metadata": {
      "duration": 60.0,
      "width": 2276,
      "height": 1280
    },
    "scenes": [],
    "video_summary": {},
    "music_description": {},
    "include_vocals": false,
    "modelspec": "edenn_basic"
  },
  "error": null
}
```

Frontend should use:

| Status | Frontend behavior |
|---|---|
| `queued` | Show queued/waiting state and keep polling |
| `processing` | Show processing state and keep polling |
| `completed` | Read `result.video_url` and show/download the final video |
| `failed` | Show `error.message` if present |
| `canceled` | Show canceled state |

### Step 4: Optional events/debug endpoint

```text
GET /api/v2/jobs/{job_id}/events
```

This is useful for internal debugging and showing detailed stage history. For
frontend product UI, `GET /api/v2/jobs/{job_id}` is usually enough.

### Step 5: Optional cancel endpoint

```text
POST /api/v2/jobs/{job_id}/cancel
```

This cancels queued tasks for the job and marks the job canceled when possible.
It does not directly kill already-running provider calls.

## `POST /api/v1/jobs/video-align-audio`

Aligns an existing audio source to a video and returns ranked matching audio segments.

### Request

Content type:

- `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---:|---|---|
| `video` | file | Conditionally | n/a | Uploaded video input |
| `video_url` | string | Conditionally | n/a | Public `http` or `https` video URL |
| `audio` | file | Conditionally | n/a | Uploaded audio input |
| `audio_url` | string | Conditionally | n/a | Public `http` or `https` audio URL |
| `lyrics_timestamps_json` | string | No | `null` | JSON array of `LyricsTimestamp` objects |
| `top_k` | integer | No | `3` | Number of ranked segments to return; must be between `1` and `5` |

### Validation and behavior

- provide exactly one of `video` or `video_url`
- provide exactly one of `audio` or `audio_url`
- `lyrics_timestamps_json`, if present, must be a valid JSON array
- each `lyrics_timestamps_json` item must validate as `LyricsTimestamp`
- `top_k` must be between `1` and `5`
- if `lyrics_timestamps_json` is omitted, the request still succeeds and
  `lyrics_provided` is `false`

### Response

```json
{
  "job_id": "9c52c3f29d654ced8c3f5a4bce1f3b4f",
  "status": "completed",
  "video_metadata": {
    "path": "/abs/path/to/input.mp4",
    "duration": 15.31,
    "width": 1080,
    "height": 1920
  },
  "lyrics_provided": true,
  "best_segment": {
    "rank": 1,
    "music_start_s": 12.4,
    "music_end_s": 27.71,
    "score": 9.38,
    "matched_audio_blob": "jobs/<job_id>/alignment/segments/rank_1/alignment_rank_1.wav",
    "matched_audio_url": "https://<storage>/generated-media/jobs/<job_id>/alignment/segments/rank_1/alignment_rank_1.wav?<sas>",
    "aligned_lyrics": [
      {
        "text": "line",
        "startS": 0.1,
        "endS": 0.9,
        "i": 0
      }
    ],
    "details": {
      "use_lyrics": true
    }
  },
  "segments": []
}
```

### Response fields

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Generated request/job identifier |
| `status` | string | Always `"completed"` on success |
| `video_metadata` | object | Workflow-owned metadata dictionary for the input video |
| `lyrics_provided` | boolean | Indicates whether lyric timestamps were supplied to the workflow |
| `best_segment` | object | Same structure as a segment item; always the first-ranked segment |
| `segments` | array | Ranked segment list up to `top_k` items |
| `job_received_timestamp` | integer or `null` | Current route uses Unix seconds |
| `job_finished_timestamp` | integer or `null` | Current route uses Unix seconds |

Segment object fields:

| Field | Type | Notes |
|---|---|---|
| `rank` | integer | Rank position starting at `1` |
| `music_start_s` | number | Start offset of the matched audio segment |
| `music_end_s` | number | End offset of the matched audio segment |
| `score` | number | Workflow-provided segment score |
| `matched_audio_blob` | string or `null` | Populated only when storage is enabled |
| `matched_audio_url` | string or `null` | Populated only when storage is enabled |
| `aligned_lyrics` | array of `LyricsTimestamp` | Aligned lyric offsets for this segment when available |
| `details` | object | Workflow-owned segment metadata |

### Example

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/video-align-audio" \
  -F "video=@/path/to/input.mp4" \
  -F "audio=@/path/to/song.wav" \
  -F "top_k=3"
```

With lyric timestamps:

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/video-align-audio" \
  -F "video=@/path/to/input.mp4" \
  -F "audio=@/path/to/song.wav" \
  -F 'lyrics_timestamps_json=[{"text":"line","startS":0.1,"endS":0.9,"i":0}]' \
  -F "top_k=2"
```

## `POST /api/v1/jobs/audio-creative-edit`

Uploads or resolves a source audio track, optionally conditions generation on visuals,
and returns a creatively regenerated audio output.

For provider-specific notes and saved example runs, see
`EdennCode/Deployment/AUDIO_CREATIVE_EDIT_API_README.md`.

### Request

Content type:

- `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---:|---|---|
| `audio` | file | Conditionally | n/a | Uploaded source audio |
| `audio_url` | string | Conditionally | n/a | Public `http` or `https` source audio URL |
| `user_prompt` | string | No | `""` | Creative edit instruction |
| `modelspec` | string | No | `edenn_enhanced` | Requested creative edit model |
| `studio_mode` | string | No | `simple` | Studio mode; allowed values are `simple` and `custom` |
| `studio_style_weight` | number | No | `null` | Studio custom style weight |
| `studio_audio_weight` | number | No | `null` | Studio custom source-audio weight |
| `studio_weirdness_constraint` | number | No | `null` | Studio custom weirdness constraint |
| `video` | file | No | n/a | Optional visual conditioning video |
| `video_url` | string | No | `null` | Optional visual conditioning video URL |
| `images` | file[] | No | n/a | Optional repeated image upload field |
| `image_urls_json` | string | No | `null` | Optional JSON array of image URLs |
| `vocal_id` | string | No | `null` | Reusable vocal clone ID for `edenn_enhanced` requests |
| `vocal_sample` | file | No | n/a | Inline vocal sample for `edenn_enhanced` vocal cloning |
| `vocal_sample_url` | string | No | `null` | Remote inline vocal sample URL for `edenn_enhanced` vocal cloning |

### Accepted `modelspec` values

| Request value | Normalized value |
|---|---|
| `edenn_enhanced` | `edenn_enhanced` |
| `edenn_studio` | `edenn_studio` |
| `provider_c` | `edenn_studio` |

Any other value returns `400`.

### Validation and behavior

Audio source:

- provide exactly one of `audio` or `audio_url`

Visual source:

- visual conditioning is optional
- if visuals are supplied, use exactly one mode:
  - `video` or `video_url`
  - repeated `images`
  - `image_urls_json`
- `image_urls_json` must be a JSON array of non-empty public `http` or `https` URLs
- providing uploaded images and `image_urls_json` together returns `400`
- providing a video source together with image sources returns `400`

Studio options:

- `studio_mode` must be `simple` or `custom`
- `studio_style_weight`, `studio_audio_weight`, and `studio_weirdness_constraint`
  must each be between `0.00` and `1.00`
- those weights must use `0.01` increments
- Studio-specific options are only allowed when `modelspec` resolves to
  `edenn_studio`
- Studio weights are only allowed when `studio_mode=custom`

Remote-accessible source audio requirement for `edenn_studio`:

- the Studio branch needs a remotely accessible source audio URL
- if storage is enabled, an uploaded `audio` file is staged automatically and that
  staged signed URL is used
- if storage is not enabled, callers must provide `audio_url`

Vocal clone options:

- provide either `vocal_id` or `vocal_sample` / `vocal_sample_url`, not both
- inline vocal clone inputs are only accepted when `modelspec` resolves to
  `edenn_enhanced`
- when `vocal_sample` or `vocal_sample_url` is used inline, the API prepares that
  sample with the same 30-second `.m4a` normalization used by
  `POST /api/v1/jobs/vocal-clone`

### Response

```json
{
  "job_id": "eeba202192f74ffca1e2b0e4e1747a1d",
  "status": "completed",
  "source_audio_url": "https://<storage>/user-uploads/jobs/<job_id>/source-audio/source.wav?<sas>",
  "edited_audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio-edit/primary.mp3?<sas>",
  "secondary_edited_audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio-edit/secondary/alt.mp3?<sas>",
  "thumbnail_url": "https://<storage>/generated-media/jobs/<job_id>/creative-edit/thumbnail/thumb.jpg?<sas>",
  "visual_analysis": {
    "input_type": "video",
    "summary": "string",
    "overall_mood": "string",
    "visual_style": "string",
    "creative_direction": "string",
    "key_elements": [],
    "scenes": []
  },
  "creative_edit_prompt": {
    "title": "string",
    "edit_intent_summary": "string",
    "visual_style_summary": "string",
    "edit_prompt": "string",
    "style_prompt": "string",
    "lyrics_prompt": "string"
  },
  "lyrics_timestamps": [],
  "include_vocals": false,
  "vocal_gender": "unknown",
  "modelspec": "edenn_enhanced",
  "user_requested_language": "ENGLISH_US",
  "vocal_id_used": null,
  "token_usage": 11886,
  "raw_token_usage": {
    "prompt_tokens": 11106,
    "completion_tokens": 780,
    "total_tokens": 11886
  },
  "token_usage_breakdown": {
    "user_prompt_preprocessor": {
      "prompt_tokens": 706,
      "completion_tokens": 128,
      "total_tokens": 834
    },
    "visual_analysis": {
      "prompt_tokens": 9447,
      "completion_tokens": 497,
      "total_tokens": 9944
    },
    "creative_edit_prompt": {
      "prompt_tokens": 953,
      "completion_tokens": 155,
      "total_tokens": 1108
    }
  }
}
```

### Response fields

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Generated request/job identifier |
| `status` | string | Always `"completed"` on success |
| `source_audio_blob` | string or `null` | Blob path for the staged source audio |
| `source_audio_url` | string or `null` | Signed URL for the staged source audio |
| `edited_audio_blob` | string or `null` | Blob path for the primary creative-edit output |
| `edited_audio_url` | string or `null` | Signed URL for the primary creative-edit output |
| `secondary_edited_audio_blob` | string or `null` | Blob path for the optional secondary creative-edit output |
| `secondary_edited_audio_url` | string or `null` | Signed URL for the optional secondary creative-edit output |
| `thumbnail_blob` | string or `null` | Blob path for the optional visual thumbnail |
| `thumbnail_url` | string or `null` | Signed URL for the optional visual thumbnail |
| `visual_analysis` | object | Normalized visual analysis object |
| `creative_edit_prompt` | object | Normalized creative-edit prompt object |
| `lyrics_timestamps` | array of `LyricsTimestamp` | Generated-output lyric timestamps when available |
| `include_vocals` | boolean | Final workflow-returned vocal flag |
| `vocal_gender` | string | Final workflow-returned vocal gender |
| `modelspec` | string | Final workflow-returned model spec |
| `user_requested_language` | string | Workflow-returned language identifier |
| `vocal_id_used` | string or `null` | Vocal clone ID actually used for the run when applicable |
| `token_usage` | integer or `null` | Equal to `raw_token_usage.total_tokens` when present |
| `raw_token_usage` | `TokenUsageCounts` or `null` | Raw total token counts |
| `token_usage_breakdown` | `CreativeEditTokenBreakdown` or `null` | Stage-level token usage |
| `job_received_timestamp` | integer or `null` | Workflow-provided timestamp |
| `job_finished_timestamp` | integer or `null` | Workflow-provided timestamp |

`visual_analysis` fields:

| Field | Type | Notes |
|---|---|---|
| `input_type` | string | Workflow-returned visual input type; current values include `none`, `video`, and `images` |
| `summary` | string | Overall visual summary |
| `overall_mood` | string | Workflow-returned mood summary |
| `visual_style` | string | Workflow-returned visual-style summary |
| `creative_direction` | string | Workflow-returned creative direction |
| `key_elements` | array of strings | Workflow-returned key visual elements |
| `scenes` | array | Scene list with `scene_index`, `start_timestamp`, `end_timestamp`, `visual_summary`, `key_actions`, and `mood` |

`creative_edit_prompt` fields:

| Field | Type |
|---|---|
| `title` | string or `null` |
| `edit_intent_summary` | string or `null` |
| `visual_style_summary` | string or `null` |
| `edit_prompt` | string or `null` |
| `style_prompt` | string or `null` |
| `lyrics_prompt` | string or `null` |

### Example

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio=@/path/to/source.wav" \
  -F "video=@/path/to/input.mp4" \
  -F "user_prompt=Make this brighter and more cinematic." \
  -F "modelspec=edenn_enhanced"
```

Studio custom example:

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio_url=https://example.com/source.wav" \
  -F "user_prompt=Turn this into a darker theatrical cover." \
  -F "modelspec=edenn_studio" \
  -F "studio_mode=custom" \
  -F "studio_style_weight=0.65" \
  -F "studio_audio_weight=0.75" \
  -F "studio_weirdness_constraint=0.20"
```

## `POST /api/v1/jobs/multi-image`

Uploads one or more images, builds a slideshow plan, generates music, and returns the
final slideshow assets plus planning metadata.

The route name suggests multi-image usage, but the current API contract only enforces
that at least one image is uploaded. For the intended slideshow workflow, send multiple
images.

### Request

Content type:

- `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---:|---|---|
| `images` | file[] | Yes | n/a | Repeated image upload field; current route accepts one or more files |
| `user_prompt` | string | No | `""` | Planning and music-generation instruction |
| `modelspec` | string | No | `"edenn_basic"` | Requested music model |
| `align_to_beats` | boolean | No | `true` | Beat-alignment hint for slideshow timing |
| `per_image_duration` | number | No | `3.0` | Requested per-image duration in seconds; must be `3`–`60`. Values below `3` are rejected with `400` error code `10008`. Totals (`image_count × per_image_duration`) under `15` seconds are auto-bumped (+1 s per image, round-robin from the first image) up to the 15-second billing floor |
| `music_volume` | number | No | `API_GENERATED_MUSIC_VOLUME` | Generated music mix volume passed to the workflow |
| `water_mark` | boolean | No | `false` | Append the Edenn spoken watermark to full-track audio outputs |
| `vocal_id` | string | No | `null` | Reusable vocal clone ID for `edenn_enhanced` requests |
| `vocal_sample` | file | No | n/a | Inline vocal sample for `edenn_enhanced` vocal cloning |
| `vocal_sample_url` | string | No | `null` | Remote inline vocal sample URL for `edenn_enhanced` vocal cloning |

### Accepted `modelspec` values

| Request value | Normalized value |
|---|---|
| `edenn_basic` | `edenn_basic` |
| `edenn_enhanced` | `edenn_enhanced` |
| `edenn_studio` | `edenn_studio` |
| `provider_a` | `edenn_basic` |
| `provider_a` | `edenn_basic` |
| `provider_a` | `edenn_basic` |
| `provider_b` | `edenn_enhanced` |
| `provider_c` | `edenn_studio` |

Any other value returns `400`.

### Validation and behavior

- at least one `images` upload is required
- `per_image_duration` must be between `3` and `60` seconds; a value below `3`
  returns `400` with error code `10008` (`"Each image must be displayed for at
  least 3 seconds."`, `retryable: false`), and the slideshow total must not
  exceed `150` seconds
- if the slideshow total (`image_count × per_image_duration`) is under `15`
  seconds, the request is still accepted: the service lengthens the plan by
  adding `+1` second to one image at a time, round-robin starting from the
  first image, until the total reaches `15` seconds. Multi-image is billed per
  delivered video second, so the minimum billed length is 15 s. When the bump
  lands equally on every image the plan stays uniform and beat alignment is
  preserved; otherwise the plan becomes an explicit per-image duration list,
  which disables beat alignment (same semantics as caller-supplied fixed
  timing)
- provide either `vocal_id` or `vocal_sample` / `vocal_sample_url`, not both
- inline vocal clone inputs are only accepted when `modelspec` resolves to
  `edenn_enhanced`
- when `vocal_sample` or `vocal_sample_url` is used inline, the API prepares that
  sample with the same 30-second `.m4a` normalization used by
  `POST /api/v1/jobs/vocal-clone`

### Response

The multi-image response uses the same envelope + five metadata blocks as
`POST /api/v1/jobs/video` (see that endpoint for the shared field table). The
differences are noted after the example.

```json
{
  "job_id": "4f1bc7a7c11a4dc6af0d6d8307f1c5d0",
  "status": "completed",
  "version": "1.4.0",
  "request_metadata": {
    "modelspec": "edenn_enhanced",
    "include_vocals": true,
    "vocal_gender": "female",
    "user_requested_language": "EN",
    "lyrics_language": "EN"
  },
  "response_metadata": {
    "job_received_timestamp": 1751270400000,
    "job_finished_timestamp": 1751270498000,
    "compression_applied": false
  },
  "cost_metadata": {
    "model_spec_name": "edenn-perceptron-1.1",
    "creation_cost": 0.065,
    "creation_times": 1,
    "token_num": 843,
    "token_cost": 0.013704
  },
  "video_metadata": {
    "video_url": "https://<storage>/generated-media/jobs/<job_id>/video/slideshow.mp4?<sas>",
    "thumbnail_url": "https://<storage>/generated-media/jobs/<job_id>/thumbnail/thumbnail.webp?<sas>",
    "geometry": {
      "width": 1080,
      "height": 1920,
      "duration": 12.0,
      "duration_s": 12.0,
      "fps": 30.0
    },
    "scenes": [],
    "video_summary": {
      "video_title": "Glow Frames",
      "music_title": "Glow",
      "video_description": "A rising lifestyle reveal.",
      "summary": "string",
      "overall_mood": "string"
    },
    "video_title": "Glow Frames",
    "video_description": "A rising lifestyle reveal."
  },
  "audio_metadata": {
    "audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/window/complete_audio.mp3?<sas>",
    "audio_duration_s": 12.0,
    "audio_size_bytes": 192512,
    "complete_audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/complete_audio.mp3?<sas>",
    "complete_audio_duration_s": 187.4,
    "complete_audio_size_bytes": 2998272,
    "music_title": "Glow",
    "music_description": "Bright vocal pop",
    "full_lyrics": "string",
    "full_lyrics_timestamps": [],
    "full_word_level_lyrics_timestamps": [],
    "lyrics_timestamps": [],
    "word_level_lyrics_timestamps": []
  }
}
```

### Differences from the video-music response

| Field | Type | Notes |
|---|---|---|
| `request_metadata.lyrics_language` | string | Multi-image only. Lyric language returned by the workflow |
| `response_metadata.compression_applied` | boolean | Multi-image only. Whether the input images were compressed |
| `video_metadata.video_title` | string | Multi-image only. Descriptive title for the slideshow |
| `video_metadata.video_description` | string | Multi-image only |
| `video_metadata.scenes` | array | Always empty — multi-image has no per-scene visual breakdown |
| `audio_metadata.music_description` | string | A plain string here, where the video response returns an object |

Notes:

- `audio_metadata.audio_url` is the clip muxed into the slideshow;
  `audio_metadata.complete_audio_url` is the full track it was cut from. They are the
  same asset when the whole track fits the video.
- Exactly one track is generated and uploaded per job. Alternate takes are not returned.
- When storage is disabled, every `*_url` field is `null`; durations and byte sizes are
  still populated from the local files.


## Async Multi-Image Music Generation

The same slideshow pipeline as `POST /api/v1/jobs/multi-image`, run asynchronously.
Submission validates and stages all inputs synchronously, then returns a `job_id`
immediately while the pipeline runs in the background. Poll the status route (or
supply a `callback_url`) to retrieve the final result.

Prefer this over the synchronous route for production clients: a full slideshow run
(image planning, music generation, beat alignment, and FFmpeg assembly) routinely
exceeds typical HTTP gateway timeouts.

### Job lifecycle

`pending` → `processing` → `completed` | `failed`

- the submit route registers the job as `pending` and returns `202 Accepted`
- the background worker transitions it to `processing`, then to a terminal
  `completed` (with `result`) or `failed` (with `error`)
- terminal jobs are retained for `ASYNC_MULTI_IMAGE_JOB_TTL_SECONDS` (default 6h),
  after which polling returns `404`

### `POST /api/v1/jobs/async_multi-image`

Content type:

- `multipart/form-data`

Accepts every field from `POST /api/v1/jobs/multi-image` (identical names,
defaults, validation, and `modelspec` normalization), plus:

| Field | Type | Required | Default | Description |
|---|---|---:|---|---|
| `callback_url` | string | No | `null` | Absolute `http(s)` URL that receives a `POST` with the final status payload when the job reaches a terminal state |

`callback_url` validation:

- must be an absolute `http` or `https` URL
- must not target `localhost`, loopback, private, or link-local addresses

All `4xx` validation (missing images, invalid `modelspec`, `per_image_duration`,
vocal-clone rules, and `callback_url`) happens synchronously during submission, so a
rejected request never registers a job.

#### Response

```json
{
  "job_id": "4f1bc7a7c11a4dc6af0d6d8307f1c5d0",
  "status": "pending",
  "version": "dev-local"
}
```

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Identifier used to poll status and correlate the callback |
| `status` | string | Always `"pending"` on acceptance |
| `version` | string | Service version tag |

### `GET /api/v1/jobs/async_multi-image/{job_id}`

#### Response

```json
{
  "job_id": "4f1bc7a7c11a4dc6af0d6d8307f1c5d0",
  "status": "completed",
  "version": "dev-local",
  "result": {
    "job_id": "4f1bc7a7c11a4dc6af0d6d8307f1c5d0",
    "status": "completed",
    "version": "1.4.0",
    "request_metadata": { "modelspec": "edenn_enhanced", "lyrics_language": "EN" },
    "response_metadata": { "compression_applied": false },
    "cost_metadata": { "model_spec_name": "edenn-perceptron-1.1", "token_num": 843 },
    "video_metadata": {
      "video_url": "https://<storage>/generated-media/jobs/<job_id>/video/slideshow.mp4?<sas>",
      "video_title": "Glow Frames",
      "video_description": "A rising lifestyle reveal."
    },
    "audio_metadata": {
      "audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/window/complete_audio.mp3?<sas>",
      "complete_audio_url": "https://<storage>/generated-media/jobs/<job_id>/audio/complete_audio.mp3?<sas>",
      "music_title": "Glow",
      "music_description": "Bright vocal pop"
    }
  },
  "error": null,
  "created_at": 1718668800
}
```

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Echoes the requested job ID |
| `status` | string | `pending`, `processing`, `completed`, or `failed` |
| `version` | string | Service version tag |
| `result` | object or `null` | Full `POST /api/v1/jobs/multi-image` response shape; populated only when `status` is `completed` |
| `error` | object or `null` | `{ status, error_code, message, retryable }`; populated only when `status` is `failed` |
| `created_at` | integer | Job creation time in Unix seconds |

- unknown or expired `job_id` returns `404`
- when `result` is present it matches the synchronous multi-image response field for
  field (abridged in the example above)

### Callback payload

When `callback_url` is supplied, the worker sends a best-effort `POST` once the job is
terminal. Delivery failures are logged but never change the stored job state, so the
poll route remains the source of truth. The body matches the poll response:

```json
{
  "job_id": "4f1bc7a7c11a4dc6af0d6d8307f1c5d0",
  "status": "completed",
  "result": { "...": "same shape as the poll result field" },
  "error": null,
  "created_at": 1718668800
}
```

### Example

```bash
# 1. Submit and capture the job_id
JOB_ID=$(curl -sS -X POST "http://localhost:8080/api/v1/jobs/async_multi-image" \
  -F "images=@/path/to/frame1.png" \
  -F "images=@/path/to/frame2.png" \
  -F "user_prompt=Make this a bright vocal pop slideshow." \
  -F "modelspec=edenn_enhanced" \
  -F "callback_url=https://your-service.example.com/webhooks/edenn" \
  | python -c "import sys, json; print(json.load(sys.stdin)['job_id'])")

# 2. Poll until the status is completed or failed
curl -sS "http://localhost:8080/api/v1/jobs/async_multi-image/$JOB_ID"
```

### Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `ASYNC_MULTI_IMAGE_JOB_STORE` | `auto` | Job store backend: `auto` (Postgres when DB env is present, else in-memory), `postgres`, or `memory` |
| `ASYNC_MULTI_IMAGE_JOB_TTL_SECONDS` | `21600` | Retention for terminal jobs before polling returns `404` |
| `ASYNC_MULTI_IMAGE_MAX_IN_FLIGHT_PER_REPLICA` | `1` | Max concurrently running async multi-image jobs per API replica |

The Postgres-backed store uses a dedicated `async_multi_image_jobs` table and is
recommended for multi-replica deployments so job state is shared across replicas; the
in-memory store is per-process and intended for single-replica or local use.

## Operational Notes

- request work directories are cleaned after each request completes
- local filesystem paths returned inside workflow-owned metadata such as `video_metadata.path`
  are implementation details and should not be treated as stable public paths
- the machine-readable contract at `GET /openapi.json` remains the authoritative schema
  for request parsing and top-level response models
