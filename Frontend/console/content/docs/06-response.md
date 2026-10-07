---
title: Response reference
description: Every field of a completed job, level by level, for both endpoints.
---

`GET /api/v2/jobs/{job_id}` returns an envelope. When `status` is `completed`, the envelope carries a `result` object.

**The video and image endpoints return the same shape.** Everything below applies to both unless noted.

## Structure

```
job_id                                    string
status                                    string
modelspec                                 string
created_at                                string
updated_at                                string
version                                   string
error                                     object | null
result                                    object
├─ video_metadata                         object
│  ├─ video_url                           string
│  ├─ thumbnail_url                       string
│  ├─ video_size_bytes                    integer
│  ├─ geometry                            object
│  │  ├─ width                            integer
│  │  ├─ height                           integer
│  │  ├─ duration                         number
│  │  └─ fps                              number
│  └─ video_summary                       object
│     ├─ video_title                      string
│     ├─ video_description                string
│     ├─ summary                          string
│     └─ overall_mood                     string
├─ audio_metadata                         object
│  ├─ audio_url                           string
│  ├─ audio_duration_s                    number
│  ├─ audio_size_bytes                    integer
│  ├─ complete_audio_url                  string
│  ├─ complete_audio_duration_s           number
│  ├─ complete_audio_size_bytes           integer
│  ├─ music_title                         string
│  ├─ music_description                   string
│  ├─ full_lyrics                         string | null
│  ├─ full_lyrics_timestamps              array of timestamp
│  ├─ full_word_level_lyrics_timestamps   array of timestamp
│  ├─ lyrics_timestamps                   array of timestamp
│  └─ word_level_lyrics_timestamps        array of timestamp
└─ response_metadata                      object
   ├─ job_received_timestamp              integer
   ├─ job_finished_timestamp              integer
   └─ compression_applied                 boolean
```

## Level 1 — envelope

| Field | Type | Description |
|---|---|---|
| `job_id` | string | The identifier returned at submission. |
| `status` | string | `queued`, `processing`, `completed`, or `failed`. The only field to branch on. |
| `modelspec` | string | The model that **actually ran**. This can differ from what you requested; check it rather than assuming. |
| `result` | object \| null | Populated when `status` is `completed`. Level 2 below. |
| `error` | object \| null | Populated when `status` is `failed`. See [Errors and retries](../errors/). |
| `created_at` | string | When the job was accepted. ISO-8601, UTC. |
| `updated_at` | string | When the job last changed state. ISO-8601, UTC. |
| `version` | string | The service build that produced the response. Quote it in a support request. |

## Level 2 — `result`

| Field | Type | Description |
|---|---|---|
| `video_metadata` | object | The delivered video and its dimensions. |
| `audio_metadata` | object | The generated music, its lyrics, and their timings. |
| `response_metadata` | object | Job lifecycle timestamps. |

## Level 3 — `result.video_metadata`

| Field | Type | Description |
|---|---|---|
| `video_url` | string | **The finished video.** Signed URL, valid 120 minutes. |
| `thumbnail_url` | string | A still frame from the video, WebP. Signed URL, same validity. |
| `video_size_bytes` | integer | Size of the video file, in bytes. |
| `geometry` | object | Dimensions of the delivered file. Level 4 below. |
| `video_summary` | object | What the model understood the content to be. Level 4 below. |

### Level 4 — `result.video_metadata.geometry`

Probed from the delivered file, not copied from your input. If the video was resized during processing, these are the output dimensions.

| Field | Type | Description |
|---|---|---|
| `width` | integer | Pixels. |
| `height` | integer | Pixels. |
| `duration` | number | Seconds. **This is the value image jobs are billed on**, rounded up. |
| `fps` | number | Frames per second. |

### Level 4 — `result.video_metadata.video_summary`

| Field | Type | Description |
|---|---|---|
| `video_title` | string | A short generated title. |
| `video_description` | string | A one-paragraph description of the content. |
| `summary` | string | The narrative summary the model composed against. Image jobs only; empty for video jobs. |
| `overall_mood` | string | The mood the model detected. Image jobs only. |

These are generated, not extracted from metadata you supplied. Treat them as a starting point for a caption, not as ground truth.

## Level 3 — `result.audio_metadata`

Two versions of the track are returned. `audio_url` is the segment muxed into the video. `complete_audio_url` is the full-length track that segment was cut from, usually longer than the video.

| Field | Type | Description |
|---|---|---|
| `audio_url` | string | The audio as used in the video. Signed URL, valid 120 minutes. |
| `audio_duration_s` | number | Length of that segment, in seconds. |
| `audio_size_bytes` | integer | Size in bytes. |
| `complete_audio_url` | string | The full generated track. Signed URL, same validity. |
| `complete_audio_duration_s` | number | Length of the full track, in seconds. |
| `complete_audio_size_bytes` | integer | Size in bytes. |
| `music_title` | string | A song-style name for the track. Distinct from `video_title`. |
| `music_description` | string | The brief the generator was given, as plain text. Useful for reproducing a similar result. |
| `full_lyrics` | string \| null | Lyrics of the complete track. `null` for instrumental output. |
| `full_lyrics_timestamps` | array | Line-level timings against the **complete track**. |
| `full_word_level_lyrics_timestamps` | array | Word-level timings against the **complete track**. |
| `lyrics_timestamps` | array | Line-level timings against the **delivered video**. |
| `word_level_lyrics_timestamps` | array | Word-level timings against the **delivered video**. |

**There are two timelines, and they are not interchangeable.** Use the `lyrics_timestamps` pair for subtitles on the returned video. Use the `full_` pair only when working with `complete_audio_url`. Mixing them puts captions in the wrong place.

All four arrays are empty for instrumental output.

### Level 4 — timestamp object

| Field | Type | Description |
|---|---|---|
| `text` | string | The line, or the word. |
| `startS` | number | Start offset. **In milliseconds**, despite the field name. |
| `endS` | number | End offset. **In milliseconds**, despite the field name. |
| `i` | integer \| null | Index within the sequence. |

**`startS` and `endS` are milliseconds.** The names are misleading and cannot be corrected without breaking existing integrations. Divide by 1000 for seconds.

## Level 3 — `result.response_metadata`

| Field | Type | Description |
|---|---|---|
| `job_received_timestamp` | integer | Unix epoch seconds, when the job was accepted. |
| `job_finished_timestamp` | integer | Unix epoch seconds, when generation finished. |
| `compression_applied` | boolean | Whether the source media was compressed before processing. Image jobs may set this; video jobs report `false`. |

These are epoch integers, unlike the envelope's `created_at` and `updated_at`, which are ISO-8601 strings.

## Example

```json
{
  "job_id": "job_40a5e8c426554fc8a793c9a47b113279",
  "status": "completed",
  "modelspec": "edenn_enhanced",
  "version": "dev-a1b2c3d",
  "created_at": "2026-07-21T02:28:53+00:00",
  "updated_at": "2026-07-21T02:29:19+00:00",
  "error": null,
  "result": {
    "video_metadata": {
      "video_url": "https://.../remixed_video.mp4?sig=...",
      "thumbnail_url": "https://.../thumbnail.webp?sig=...",
      "video_size_bytes": 4181234,
      "geometry": { "width": 1280, "height": 720, "duration": 28.0, "fps": 30.0 },
      "video_summary": {
        "video_title": "Morning in the Studio",
        "video_description": "A ceramicist shapes a bowl at the wheel as light moves across the room.",
        "summary": "",
        "overall_mood": ""
      }
    },
    "audio_metadata": {
      "audio_url": "https://.../matched_audio.mp3?sig=...",
      "audio_duration_s": 28.0,
      "audio_size_bytes": 448512,
      "complete_audio_url": "https://.../complete_track.mp3?sig=...",
      "complete_audio_duration_s": 132.4,
      "complete_audio_size_bytes": 2119680,
      "music_title": "Neon Morning",
      "music_description": "Uplifting pop with female vocals, English lyrics, moderate tempo.",
      "full_lyrics": "Light comes in slow...",
      "full_lyrics_timestamps": [
        { "text": "Light comes in slow", "startS": 0, "endS": 3120, "i": 0 }
      ],
      "full_word_level_lyrics_timestamps": [
        { "text": "Light", "startS": 0, "endS": 410, "i": 0 }
      ],
      "lyrics_timestamps": [
        { "text": "Light comes in slow", "startS": 0, "endS": 3120, "i": 0 }
      ],
      "word_level_lyrics_timestamps": [
        { "text": "Light", "startS": 0, "endS": 410, "i": 0 }
      ]
    },
    "response_metadata": {
      "job_received_timestamp": 1784780933,
      "job_finished_timestamp": 1784780959,
      "compression_applied": false
    }
  }
}
```

## Notes for integrators

- **Every `*_url` is signed and expires after 120 minutes.** Download or copy to your own storage on receipt; do not persist the URL. Query the job again for fresh ones.
- **Optional fields can be `null`.** Everything except `job_id` and `status` can be absent or null on some path — instrumental output has no lyrics, a failed probe leaves `geometry` empty. Read defensively.
- **Check `modelspec` on the envelope** rather than assuming the model you requested is the one that ran.
