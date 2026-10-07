# Vocal Clone API

This document describes `POST /api/v1/jobs/vocal-clone`.

## Purpose

Use this route when you want a reusable ProviderB `vocal_id` that can later be supplied to vocal `edenn_enhanced` generation requests.

Supported downstream request fields:

- `vocal_id`
- `vocal_sample`
- `vocal_sample_url`

Relevant generation routes:

- `POST /api/v1/jobs/video`
- `POST /api/v1/jobs/audio-creative-edit`
- `POST /api/v1/jobs/multi-image`

## Request

Content type:

- `multipart/form-data`

Fields:

| Field | Type | Required | Description |
|---|---|---:|---|
| `vocal_sample` | file | Conditionally | Uploaded vocal sample audio |
| `vocal_sample_url` | string | Conditionally | Publicly downloadable `http/https` vocal sample URL |

Validation rules:

- provide exactly one of `vocal_sample` or `vocal_sample_url`
- the backend normalizes the resolved sample into an M4A file and caps the prepared clip at 30 seconds before calling ProviderB

## Response

```json
{
  "job_id": "7f4ac8f7e2bc45c798bf9fdf28d9c56d",
  "status": "completed",
  "vocal_sample_blob": "jobs/<job_id>/vocal-clone/vocal_sample_provider_b_vocal_clone.m4a",
  "vocal_sample_url": "https://<storage>/user-uploads/jobs/<job_id>/vocal-clone/vocal_sample_provider_b_vocal_clone.m4a?<sas>",
  "vocal_id": "vocal_123456"
}
```

`vocal_sample_blob` and `vocal_sample_url` are only populated when Azure Blob Storage is enabled.

## Example

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/vocal-clone" \
  -F "vocal_sample=@/path/to/voice_reference.wav"
```
