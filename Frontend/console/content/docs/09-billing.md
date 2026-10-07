---
title: Billing and usage
description: How jobs are priced, what happens when the balance runs out, and how to check it.
---

## Pricing model

Both products are billed on the duration of the **delivered video**, and both round up. What differs is the unit.

| Product | Unit | Formula |
|---|---|---|
| Video soundtrack | A 30-second block | `⌈duration ÷ 30⌉ × unit price` |
| Image soundtrack | One second, minimum 15 | `max(duration, 15) × price per second` |

Unit prices are set per account. The current values are on your account page in the console. To add credit, see [Adding credit](../recharge/).

### Video soundtrack

Every started 30 seconds counts as one unit. A 30-second video and a 1-second video both cost one unit; a 31-second video costs two.

| Delivered duration | Units | Example |
|---|---|---|
| 30s or less | 1 | 30s → 1 × unit price |
| 31–60s | 2 | 59s → 2 × unit price |
| 61–90s | 3 | 90s → 3 × unit price |
| and so on | `⌈duration ÷ 30⌉` | 118s → 4 × unit price |

Input video is capped at 150 seconds, so a single job is at most five units.

### Image soundtrack

Below 15 seconds, a job bills as 15 seconds. At or above 15 seconds it bills its actual length, rounded up to the second.

| Delivered duration | Billed as | Cost |
|---|---|---|
| Under 15s | 15s | 15 × price per second |
| 15s or more | Actual duration, rounded up | seconds × price per second |

The delivered duration follows your own parameters: `per_image_duration` multiplied by the number of images. Ten images at five seconds each is 50 seconds, billed as 50. The shortest job the endpoint accepts — three images at three seconds — delivers nine seconds and bills as 15.

### Reading a charge back

Every line in `GET /api/v1/account/usage` carries the numbers the charge was made from, so a bill can be checked without knowing the rules by heart:

| Field | Meaning |
|---|---|
| `video_duration_s` | Delivered duration the charge was computed from |
| `billed_units` | Units charged |
| `unit_seconds` | Seconds in one unit — 30 for video soundtrack, 1 for image soundtrack |
| `min_billable_seconds` | Minimum billable duration — 15 for image soundtrack, 0 where none applies |
| `unit_price_usd` | Price of one unit |
| `billed_amount_usd` | `billed_units × unit_price_usd` |

`unit_seconds` and `min_billable_seconds` record what a unit meant on the day of the charge, so historical lines stay verifiable if the rules change later. Lines billed before duration billing existed leave both `null`.

## Three rules

1. **Only `completed` jobs are billed.** A `failed` job costs nothing — including input rejections, generation timeouts, and internal errors.
2. **The balance is checked at submission.** A balance of zero or less returns **402 `insufficient_balance`**. Jobs already running are unaffected and complete normally.
3. **The balance belongs to the account, not the key.** Several keys on one account share one wallet. What separates per key is *consumption*, not balance.

## Checking the balance

```bash
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/v1/account/balance"
```

```json
{
  "account_id": "acct_...",
  "registered_name": "Your Company Ltd",
  "balance_usd": 749.5,
  "is_active": true,
  "updated_at": "2026-07-20T02:29:19+00:00",
  "balance_warning": null
}
```

| Field | Description |
|---|---|
| `balance_usd` | Current balance |
| `is_active` | `false` means submissions return `403 account_inactive` regardless of balance |
| `balance_warning` | Populated when the balance is low; `null` otherwise |
| `updated_at` | When the balance last changed. ISO-8601, UTC |

When the balance falls below a configured fraction of total credit added, `balance_warning` is populated and the response also carries an `X-Edenn-Balance-Warning: low` header.

**Monitor that header rather than computing a threshold yourself.** The threshold is configured server-side; a client-side copy stops being correct the moment it changes.

An account that has not been opened yet returns `404 account_not_found`.
