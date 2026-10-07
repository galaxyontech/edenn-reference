---
title: Job lifecycle
description: States, polling strategy, and the validity of result URLs.
---

## States

```
   queued ──────▶ processing ──────▶ completed
                       │
                       └───────────▶ failed
```

| State | Meaning | Terminal |
|---|---|---|
| `queued` | Accepted, not yet being processed | No |
| `processing` | Generating | No |
| `completed` | Succeeded. `result` is populated. **Billing happens here.** | Yes |
| `failed` | Failed. `error` is populated. **Not billed.** | Yes |

## Querying a job

```bash
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/v2/jobs/$JOB_ID"
```

| Field | Description |
|---|---|
| `status` | See the table above. This is the only field to branch on. |
| `modelspec` | The model that actually ran |
| `result` | Populated when `completed` — see [Response reference](../response/) |
| `error` | Populated when `failed` — `{ message, retryable, error_code }` |
| `created_at` / `updated_at` | ISO-8601, UTC |

## Polling strategy

- **Poll every 5–10 seconds.** Polling harder does not make the job finish sooner.
- **Set timeouts per model**, at least three times the [typical duration](../quickstart/#expected-duration).
- **Do not abandon the result when your client times out.** The job keeps running on the server and the result is stored. Query again with the `job_id`.
- Polling is not billed.

## Result URLs

Every `*_url` in `result` is a **signed URL valid for 120 minutes**.

- Download or copy the file to your own storage on receipt.
- **Do not persist the URL.** Once it expires it returns `403` and cannot be renewed.
- To get fresh URLs, query the job again. The job itself is stored durably.

## Idempotency

**There is no idempotency key. Repeat submissions are not deduplicated.**

Submitting the same input and parameters again produces a new `job_id`, a new generation, **a different piece of music** — generation is not deterministic — and a second charge.

Consequently:

- Persist the `job_id` at submission. Never "just submit it again to check".
- Client retry logic must retry the **query**, not the submission.
- Only resubmit when a job has reached `failed` with `retryable: true`. See [Errors and retries](../errors/).
