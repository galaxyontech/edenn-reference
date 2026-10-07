---
title: Image soundtrack
description: POST /api/v2/jobs/multi-image-music — assemble images into a video with transitions and a soundtrack.
---

```
POST /api/v2/jobs/multi-image-music
Content-Type: multipart/form-data
```

Assembles 3–10 images into a video with transitions, and generates a soundtrack for it.

## Input constraints

| Constraint | Value |
|---|---|
| Number of images | 3–10 |
| Image dimensions | Both sides 720 px or more |
| Total video duration | 150 seconds or less |
| Formats | JPEG, PNG, WebP |

An image that is too small or cannot be decoded returns `400` at submission, naming which image failed.

## Fields

### Source

Exactly one of these is required.

| Field | Type | Required | Description |
|---|---|---|---|
| `images` | file[] | One of the two | Image files. Repeat the part: `-F "images=@a.jpg" -F "images=@b.jpg"` |
| `image_urls` | string | One of the two | A JSON array or comma-separated list of publicly reachable URLs. |

### Generation

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_prompt` | string | Recommended | `""` | **The music you want.** Whether the track has vocals, the singer's gender, and the lyric language are inferred from this text. |
| `modelspec` | string | **Yes** | — | `edenn_enhanced` or `edenn_studio`. |
| `user_lyrics_prompt` | string | No | — | Direction for the lyrics. Supplying it turns vocals on. |

Voice cloning is not available on this endpoint.

### Timing and order

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image_order` | string | No | `auto` | `auto` lets the model reorder images into a narrative sequence. `fixed` keeps your submission order. |
| `per_image_duration` | float | No | `5.0` | Seconds each image is shown, `3`–`60`. Applies to every image. |
| `per_image_durations` | string | No | — | Per-image durations, one per image. **Requires `image_order=fixed`.** Mutually exclusive with `total_duration_s`. |
| `total_duration_s` | float | No | — | Total length, divided evenly across the images. **Requires `image_order=fixed`.** Mutually exclusive with `per_image_durations`. |
| `align_to_beats` | bool | No | `true` | Aligns image changes to the beat of the generated music. Turns off automatically when you specify durations explicitly. |

### Transitions

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `transition_mode` | string | No | `auto` | Applies only when `transition_types` is absent. `auto` and `random` pick a weighted-random effect at each change. `none` cuts hard. |
| `transition_types` | string | No | — | Per-change list of effects. Supplying it overrides `transition_mode`. |
| `transition_duration_s` | string | No | `1` | Per-change duration in seconds. Each value `0.4`–`2.0`. |

### Output

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `music_volume` | float | No | — | Gain applied to the generated music, `0.0`–`1.0`. |
| `audio_output_format` | string | No | — | Preferred audio container: `mp3` or `wav`. |

## How transitions work

**N images have N−1 changes.** Both `transition_types` and `transition_duration_s` are per-change lists. Each must have either **one** value, applied to every change, or exactly **N−1** values, applied in order. Any other length returns `400`.

| Goal | Value |
|---|---|
| Hard cuts throughout | `transition_mode=none` |
| Random per change (default) | Omit, or `transition_mode=auto` |
| One effect everywhere | `transition_types=fade` |
| A specific effect per change | `transition_types=fade,auto,dissolve` |
| A specific duration per change | `transition_duration_s=0.6,0.5,1.0` |

Each entry is an effect name or `auto`, which randomises that one change. Effect names are case-insensitive. There are 58 of them — see the [Transitions reference](../transitions/). An unrecognised name returns `400` with the full list of valid values.

Two runtime rules affect the result:

- The actual blend at each change is capped at **half the shorter of the two adjacent image durations**. A `transition_duration_s` of `2.0` between two 3-second images becomes 1.5 seconds.
- If a change is too short to blend across at least two frames, **the entire video falls back to hard cuts**.

Transitions do not change the total video length.

## Examples

Three images, so two changes. A specific effect at each, one duration applied to both:

```bash
curl -X POST "$BASE/api/v2/jobs/multi-image-music" \
  -H "Authorization: Bearer $API_KEY" \
  -F "images=@photo1.jpg" -F "images=@photo2.jpg" -F "images=@photo3.jpg" \
  -F "modelspec=edenn_enhanced" \
  -F "user_prompt=female vocals, English, gentle and melodic" \
  -F "per_image_duration=5" \
  -F "transition_types=fade,circleopen" \
  -F "transition_duration_s=1"
```

Fixed order with per-image durations:

```bash
curl -X POST "$BASE/api/v2/jobs/multi-image-music" \
  -H "Authorization: Bearer $API_KEY" \
  -F 'image_urls=["https://cdn.example.com/1.webp","https://cdn.example.com/2.webp","https://cdn.example.com/3.webp"]' \
  -F "image_order=fixed" \
  -F "per_image_durations=4,6,5" \
  -F "user_prompt=warm acoustic guitar, instrumental" \
  -F "transition_types=fade" \
  -F "transition_duration_s=0.6"
```

## Response

Identical in shape to the video endpoint. Poll `GET /api/v2/jobs/{job_id}`; every field is documented in the [Response reference](../response/).

**Image jobs are billed by the duration of the delivered video**, rounded up to the second, with a **15-second minimum**. `per_image_duration` and the number of images therefore determine the cost of the job directly: ten images at five seconds each is a 50-second billing base. Below the minimum the floor applies — three images at three seconds delivers nine seconds and bills as 15. See [Billing and usage](../billing/).
