---
title: Authentication
description: Credential format, key lifecycle, rotation, and revocation.
---

## Credential format

Every generation and account endpoint takes a bearer token:

```
Authorization: Bearer REDACTED_API_KEY
```

A missing or invalid credential returns **401**. HTTPS is required.

## Properties of a key

| Property | Value |
|---|---|
| Times the plaintext is shown | **Once**, in the response that creates it. The server stores only a SHA-256 hash. |
| Recoverable | No. A lost key can only be replaced. |
| Billing attribution | Usage is recorded per key. Several keys on one account draw on a single balance. |
| Identifier | The **tracking ID** — the first 12 characters. It appears on every usage record and is what you quote in a support request. |
| Limit | **20 active keys** per account. Creating a 21st returns 409. |

## Lifecycle

### Creating

Create a key on the **API key** page in the console. The name is for your own reference, can be changed at any time, and has no effect on requests.

**Use a separate key per purpose** — production, staging, and each downstream system. Usage records are grouped by tracking ID, so separate keys are what let you see which integration is consuming your balance, and let you revoke one without affecting the others.

### Rotating

There is no forced rotation period. Rotate by creating before revoking:

1. Create the new key.
2. Distribute it and confirm traffic has moved — in the usage records, the new tracking ID starts appearing and the old one drops to zero.
3. Revoke the old key.

### Revoking

Revoke a key in the console.

**Revocation takes effect globally within 60 seconds.** During that window, jobs already running continue to completion and a new request may still be accepted. For a planned rotation, allow for those 60 seconds in your cutover. If you are responding to a leaked credential, revoke first and then check the usage records to confirm no calls followed.

Revocation cannot be undone. Revoked keys do not count toward the limit of 20.

## Endpoints

### List keys

```bash
curl "$BASE/api/v1/account/keys" -H "Authorization: Bearer $ID_TOKEN"
```

```json
{
  "keys": [
    {
      "key_prefix": "sk-XbgqH8R8r",
      "key_suffix": "RLYA",
      "name": "production",
      "created_at": "2026-08-02T09:14:00+00:00",
      "last_used_at": "2026-08-03T02:41:00+00:00",
      "is_active": true,
      "revoked_at": null
    }
  ]
}
```

Add `?include_revoked=true` to include revoked keys.

The response never contains key material. `key_prefix` is the tracking ID. `key_suffix` is the last four characters of the plaintext, so you can match a key in your environment against a row in this list — every key starts with the same characters, so the tail is what distinguishes them. Keys created before this field existed have an empty `key_suffix` and always will; their plaintext no longer exists anywhere.

`last_used_at` is written with a delay of up to a few minutes. Use it to answer "is anything still calling this key", not as a precise call timestamp.

### Creating and revoking

Keys are created, renamed, and revoked **only from the console UI**. An API key cannot manage API keys.

## Security requirements

- **Never put a key in client-side code, a mobile bundle, or a public repository.** A key is a payment credential for your account.
- Call the API from your server. Do not let an end user's browser hold a key.
- If a key leaks: revoke it, create a replacement, then review the usage records for calls you did not make.
- Edenn will never ask you for the plaintext of a key through any channel.
