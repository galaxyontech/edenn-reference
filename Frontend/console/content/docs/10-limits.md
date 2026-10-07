---
title: Limits
description: Every hard limit on inputs, parameters, keys, and result URLs.
---

Check this page before going to production. These are the values in force today; changes are announced in advance.

## Video soundtrack input

| Constraint | Limit | On violation |
|---|---|---|
| File size | 300 MB or less | `400` / `10005` |
| Maximum duration | 150 seconds or less | `400` / `10006` |
| Minimum duration | 15 seconds or more | `400` / `10007` |
| Containers | `.mp4`, `.mov`, `.webm`, `.mkv`, `.avi` | `400` |

Validation is by decoding, not by file extension. A file with the wrong extension but valid contents is accepted; a file with a correct extension that cannot be decoded is not.

## Image soundtrack input

| Constraint | Limit | On violation |
|---|---|---|
| Number of images | 3–10 | `400` |
| Image dimensions | Both sides 720 px or more | `400`, naming the image |
| Total video duration | 150 seconds or less | `400` |
| Per-image duration | 3–60 seconds | `400` |
| Formats | JPEG, PNG, WebP | `400` |

## Parameters

| Parameter | Range |
|---|---|
| `music_volume` | `0.0`–`1.0` |
| `transition_duration_s` | Each value `0.4`–`2.0`, and never more than half the shorter of the two adjacent image durations |
| `transition_types` length | 1, or N−1 where N is the number of images |

## API keys

| Constraint | Limit |
|---|---|
| Active keys per account | 20. A 21st returns `409 key_limit_reached` |
| Times the plaintext is shown | Once, at creation |
| Revocation propagation | Global within 60 seconds |
| Revoked keys | Do not count toward the limit |

## Results

| Item | Value |
|---|---|
| Signed URL validity | **120 minutes** |
| Job record and status | Stored durably |

An expired URL returns `403` and cannot be renewed. Query the job again with its `job_id` to get fresh signed URLs.

## Transport

| Item | Value |
|---|---|
| Transport | HTTPS required |
| Timestamps | UTC throughout |
| Idempotency key | Not offered. Resubmitting creates a new job and a new charge. |
