---
title: Overview
description: What the Edenn API does, how the asynchronous job model works, and how to integrate.
---

Edenn is a generative audio API. You send a video or a set of images; you get back a finished video with an AI-composed soundtrack. The music is generated from the visual content and your text prompt — it is not retrieved from a library.

## Products

| Product | Endpoint | Input | Output |
|---|---|---|---|
| Video soundtrack | `POST /api/v2/jobs/video-music` | One video | Video with the original audio replaced or mixed |
| Image soundtrack | `POST /api/v2/jobs/multi-image-music` | 3–10 images | Video assembled from the images, with transitions and a soundtrack |

Both share the same authentication, job model, error contract, and billing account.

## The job model

Every generation endpoint is **asynchronous and durable**. Submitting returns a `job_id` within 1–3 seconds. Generation continues on the server; you poll for the result.

```
                    ┌──────────────────────────────────────┐
   Your client ────▶│  1. Submit                           │
                    │  POST /api/v2/jobs/{product}         │
                    │  200 { job_id, status: "queued" }    │
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
   Your client ────▶│  2. Poll every 5–10 seconds          │
                    │  GET /api/v2/jobs/{job_id}           │
                    │  queued → processing → completed     │
                    └──────────────────────────────────────┘
                                     │
                                     ▼
                    ┌──────────────────────────────────────┐
   Your client ────▶│  3. Download                         │
                    │  result.video_metadata.video_url     │
                    │  Signed URL, valid for 120 minutes   │
                    └──────────────────────────────────────┘
```

Three properties of this model matter when you build against it:

- **Submission is decoupled from generation.** Submitting takes 1–3 seconds regardless of which model you choose. It does not hold your request thread open for the length of the job.
- **Results are durable.** A dropped connection, a client restart, or a crash does not lose the result. You can retrieve it later with the `job_id`.
- **The `job_id` is the only handle.** Persist it as soon as you receive it. If you lose it, the result is unreachable — there is no way to look a job up by any other property.

## Terminology

| Term | Meaning |
|---|---|
| **Job** | One generation request, identified by `job_id`. Status and result are stored durably. |
| **modelspec** | The generation model. See [Models](../quickstart/#models). |
| **API key** | Your credential, prefixed `sk-`. Usage and billing are attributed per key. |
| **Tracking ID** | The first 12 characters of an API key. It appears on every usage record and is what you quote in a support request. |
| **Account** | The billing entity. One account can hold several keys that draw on a single balance. |

## How to integrate

1. Create an API key in the console. See [Authentication](../authentication/).
2. Submit your first job: [Video soundtrack](../video-music/) or [Image soundtrack](../image-music/).
3. Poll for the result and read it. See [Job lifecycle](../jobs/) and [Response reference](../response/).
4. Handle failures per [Errors and retries](../errors/), and reconcile spend per [Billing and usage](../billing/).
5. Check [Limits](../limits/) before you go to production.

## Base URL

The API is served from the same host as this documentation. The console and the API are deployed together, so the domain you are reading this on is your base URL. There is nothing separate to configure.
