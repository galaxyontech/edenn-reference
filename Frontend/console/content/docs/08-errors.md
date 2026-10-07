---
title: Errors and retries
description: The error contract, the error code catalogue, and when to retry.
---

## Error contract

Every 4xx response body and every failed job's `error` object uses the same structure:

```json
{ "message": "...", "retryable": true, "error_code": 30200 }
```

| Field | Description |
|---|---|
| `message` | **Wording can change. Do not branch on it.** |
| `retryable` | Whether resubmitting with the same parameters is worth doing |
| `error_code` | Stable machine-readable identifier. **Branch on this.** |

## HTTP status codes

| Status | Meaning | What to do |
|---|---|---|
| **400** | Invalid parameters — unknown transition name, image count out of range, mutually exclusive fields, input over a limit | Correct per `error_code` and resubmit |
| **401** | Missing or invalid credential | Check the `Authorization: Bearer` header; confirm the key is not revoked |
| **402** | Insufficient balance (`insufficient_balance`) | Add credit and resubmit. See [Billing and usage](../billing/) |
| **403** | Account deactivated (`account_inactive`), or an API key used against a key-management endpoint (`session_required`) | Contact Edenn; manage keys from the console |
| **404** | No such job | Check the `job_id` |
| **422** | Field type error in the request body | Correct per the field path in the response |

## Error codes

| `error_code` | Meaning | `retryable` | What to do |
|---|---|---|---|
| `10005` | Input video over 300 MB | `false` | Compress or trim, then resubmit |
| `10006` | Input video over 150 seconds | `false` | Trim, then resubmit |
| `10007` | Input video under 15 seconds | `false` | Use a longer video |
| `30200` | Generation timed out | `true` | Resubmit with the same parameters |
| `90001` | Internal service error | `true` | Retry later. If it persists, contact Edenn with the `job_id` |

## When to retry

**Branch on `retryable`, not on the HTTP status and not on `message`.**

| `retryable` | Meaning | Action |
|---|---|---|
| `true` | Transient | Submit a **new job** with the same parameters. Back off exponentially, starting no sooner than 30 seconds. |
| `false` | Input or account problem | Retrying will not help. Fix the input or the account. |

Two rules that are easy to get wrong:

- **A retry is a new job, not a continuation.** It gets a new `job_id` and is billed separately.
- **Never use resubmission to deduplicate or to verify.** There is no idempotency key; the same input submitted twice produces different music and two charges. See [Idempotency](../jobs/#idempotency).

## Where input errors surface

The three input codes (`10005`, `10006`, `10007`) appear in different places depending on how you supplied the video:

| Source | Behaviour |
|---|---|
| Uploaded file | `400` at submission, synchronously |
| `video_url` | Job is queued, then reaches `failed` with the code in `error` |

Neither is billed. If you use `video_url`, handle these three codes in your polling path as well as at submission — checking only at submission will miss them.

## Reporting a problem

| Item | Where to find it |
|---|---|
| `job_id` | The submission response, or the usage records |
| `error_code` and `message` | The `error` object on the job |
| Tracking ID | The `key_prefix` column in the usage records |
| Time in UTC | `created_at` on the job |
| `version` | The envelope of any response |

The `job_id` is the essential one. Without it a specific generation cannot be located.
