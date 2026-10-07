---
title: Video soundtrack
description: POST /api/v2/jobs/video-music — request format, fields, constraints, and examples.
---

```
POST /api/v2/jobs/video-music
```

Generates a soundtrack for a video and returns the video with that audio replacing or mixed with the original.

## Request format

Two formats, selected by `Content-Type`:

| `Content-Type` | Use | Source field |
|---|---|---|
| `multipart/form-data` | Upload the video file directly | `video` (file part) |
| `application/json` | Reference a downloadable URL | `video_url` (string) |

All other fields have the same names and meanings in both formats — form fields under multipart, object keys under JSON.

## Input constraints

| Constraint | Value | `error_code` |
|---|---|---|
| File size | 300 MB or less | `10005` |
| Maximum duration | 150 seconds or less | `10006` |
| Minimum duration | 15 seconds or more | `10007` |

Where the error surfaces depends on how you supplied the video:

| Source | Validated | Result |
|---|---|---|
| `video` (uploaded file) | At submission | `400` with the `error_code`, immediately |
| `video_url` | After the server downloads it, before processing | The job is queued, then fails; the `error_code` appears in `error` |

Both are permanent failures (`retryable: false`) and **are not billed**. Fix the input and submit again.

## Fields

### Source

Exactly one of these is required.

| Field | Type | Required | Description |
|---|---|---|---|
| `video` | file | One of the two | The video file. `multipart/form-data` only. Verified containers: `.mp4`, `.mov`, `.webm`, `.mkv`, `.avi`. Validation is by decoding, not by file extension. |
| `video_url` | string | One of the two | A publicly reachable URL. `application/json` only. **At submission the URL is only checked for valid syntax; it is not fetched.** The server downloads it when the job starts processing, so a well-formed but dead link is accepted, queued, and fails minutes later. |

### Generation

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_prompt` | string | Recommended | `""` | **The music you want** — style, mood, instrumentation, tempo. Whether the track has vocals, the singer's gender, and the lyric language are inferred from this text. To get vocals, say so: `female vocals, English, pop`. |
| `modelspec` | string | **Yes** | — | `edenn_enhanced` or `edenn_studio`. |
| `lyrics_prompt` | string | No | — | Direction for the lyrics. Supplying it turns vocals on. |

### Output

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `preserve_original_audio` | bool | No | `false` | When `true`, the original audio is kept and mixed with the generated music instead of being replaced. |
| `music_volume` | float | No | `1.0` | Gain applied to the generated music, `0.0`–`1.0`. |
| `audio_output_format` | string | No | — | Preferred audio container: `mp3` or `wav`. |

## Examples

### Multipart upload

```bash
curl -X POST "$BASE/api/v2/jobs/video-music" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video=@sample.mp4" \
  -F "modelspec=edenn_enhanced" \
  -F "user_prompt=female vocals, English, uplifting pop"
```

### JSON with a URL

```bash
curl -X POST "$BASE/api/v2/jobs/video-music" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url": "https://example.com/sample.mp4",
    "modelspec": "edenn_studio",
    "user_prompt": "male vocals, warm acoustic folk"
  }'
```

### Keeping the original audio underneath

```bash
curl -X POST "$BASE/api/v2/jobs/video-music" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video=@interview.mp4" \
  -F "modelspec=edenn_enhanced" \
  -F "user_prompt=soft instrumental bed, unobtrusive" \
  -F "preserve_original_audio=true" \
  -F "music_volume=0.35"
```

## Response

Submission returns immediately with `202` semantics over HTTP `200`:

```json
{
  "job_id": "job_40a5e8c426554fc8a793c9a47b113279",
  "task_id": "task_...",
  "status": "queued",
  "status_url": "/api/v2/jobs/job_40a5e8c426554fc8a793c9a47b113279"
}
```

Poll `GET /api/v2/jobs/{job_id}` for the result. Every field of the completed response is documented in the [Response reference](../response/).
