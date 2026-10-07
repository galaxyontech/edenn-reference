---
title: Quickstart
description: From creating a key to downloading your first generated video.
---

A complete integration, end to end. It takes about ten minutes.

## 1. Set your environment

Every request in this guide goes to the API host below.

```bash
export BASE="https://api.example.invalid"
export API_KEY="sk-..."          # created in step 2
```

## 2. Create an API key

Open the **API key** page in the console and create one. The plaintext key is returned once, at creation. The server stores only a hash of it and cannot show it to you again.

Full key handling rules are in [Authentication](../authentication/).

## 3. Verify the credential

```bash
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/v1/account/balance"
```

A `200` with your account balance means the key works. A `401` means it does not — see [Errors and retries](../errors/).

## 4. Submit a job

```bash
curl -X POST "$BASE/api/v2/jobs/video-music" \
  -H "Authorization: Bearer $API_KEY" \
  -F "video=@sample.mp4" \
  -F "modelspec=edenn_enhanced" \
  -F "user_prompt=an upbeat electronic track, female vocals, English"
```

The response arrives immediately:

```json
{
  "job_id": "job_40a5e8c426554fc8a793c9a47b113279",
  "task_id": "task_...",
  "status": "queued",
  "status_url": "/api/v2/jobs/job_40a5e8c426554fc8a793c9a47b113279"
}
```

**Persist the `job_id` now.** It is the only way to retrieve this result.

## 5. Poll for the result

```bash
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/v2/jobs/$JOB_ID"
```

Poll every 5–10 seconds. When `status` reaches `completed`, the `result` object is populated:

```json
{
  "job_id": "job_40a5e8c426554fc8a793c9a47b113279",
  "status": "completed",
  "modelspec": "edenn_enhanced",
  "result": {
    "video_metadata": {
      "video_url": "https://.../remixed_video.mp4?...",
      "thumbnail_url": "https://.../thumbnail.webp?...",
      "geometry": { "width": 1280, "height": 720, "duration": 9.0, "fps": 30.0 }
    },
    "audio_metadata": {
      "audio_url": "https://.../matched_audio.mp3?...",
      "music_title": "Neon Morning"
    }
  },
  "created_at": "2026-07-21T02:28:53+00:00",
  "finished_at": "2026-07-21T02:29:19+00:00"
}
```

Every field is documented in the [Response reference](../response/).

## 6. Download the output

`video_url` and `audio_url` are **signed URLs valid for 120 minutes**. Download or copy the file to your own storage as soon as you receive it. Do not store the URL itself — it expires.

## Complete example

The same integration in one file. Both handle the two things that trip people up: polling until a terminal state rather than a fixed number of tries, and downloading before the signed URL expires.

### Python

```python
import os, time, requests

BASE = os.environ["EDENN_BASE"]
KEY = os.environ["EDENN_API_KEY"]
HEADERS = {"Authorization": f"Bearer {KEY}"}

# 1. Submit
with open("sample.mp4", "rb") as f:
    r = requests.post(
        f"{BASE}/api/v2/jobs/video-music",
        headers=HEADERS,
        files={"video": f},
        data={
            "modelspec": "edenn_enhanced",
            "user_prompt": "female vocals, English, uplifting pop",
        },
        timeout=120,
    )
r.raise_for_status()
job_id = r.json()["job_id"]
print("submitted", job_id)          # persist this before doing anything else

# 2. Poll until terminal. No attempt cap — the job outlives any client timeout,
#    and giving up locally does not stop it or refund it.
deadline = time.time() + 20 * 60
while True:
    if time.time() > deadline:
        raise TimeoutError(f"still running after 20 minutes: {job_id}")
    job = requests.get(f"{BASE}/api/v2/jobs/{job_id}",
                       headers=HEADERS, timeout=30).json()
    status = job["status"]
    if status == "completed":
        break
    if status == "failed":
        err = job.get("error") or {}
        raise RuntimeError(
            f"{err.get('error_code')}: {err.get('message')} "
            f"(retryable={err.get('retryable')})"
        )
    time.sleep(8)

# 3. Download immediately — the URL is signed and expires in 120 minutes.
video_url = job["result"]["video_metadata"]["video_url"]
with requests.get(video_url, stream=True, timeout=300) as stream:
    stream.raise_for_status()
    with open("output.mp4", "wb") as out:
        for chunk in stream.iter_content(1 << 16):
            out.write(chunk)

print("model that ran:", job["modelspec"])
print("saved output.mp4")
```

### Node

```javascript
import fs from 'node:fs';
import { Readable } from 'node:stream';

const BASE = process.env.EDENN_BASE;
const HEADERS = { Authorization: `Bearer ${process.env.EDENN_API_KEY}` };

// 1. Submit
const form = new FormData();
form.set('video', new Blob([fs.readFileSync('sample.mp4')]), 'sample.mp4');
form.set('modelspec', 'edenn_enhanced');
form.set('user_prompt', 'female vocals, English, uplifting pop');

const submit = await fetch(`${BASE}/api/v2/jobs/video-music`, {
  method: 'POST', headers: HEADERS, body: form,
});
if (!submit.ok) throw new Error(`submit failed: ${submit.status}`);
const { job_id } = await submit.json();
console.log('submitted', job_id);   // persist this before doing anything else

// 2. Poll until terminal
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const deadline = Date.now() + 20 * 60 * 1000;
let job;
for (;;) {
  if (Date.now() > deadline) throw new Error(`still running after 20 minutes: ${job_id}`);
  job = await (await fetch(`${BASE}/api/v2/jobs/${job_id}`, { headers: HEADERS })).json();
  if (job.status === 'completed') break;
  if (job.status === 'failed') {
    const e = job.error ?? {};
    throw new Error(`${e.error_code}: ${e.message} (retryable=${e.retryable})`);
  }
  await sleep(8000);
}

// 3. Download immediately — the URL is signed and expires in 120 minutes.
const file = await fetch(job.result.video_metadata.video_url);
await new Promise((resolve, reject) => {
  const out = fs.createWriteStream('output.mp4');
  Readable.fromWeb(file.body).pipe(out).on('finish', resolve).on('error', reject);
});

console.log('model that ran:', job.modelspec);
console.log('saved output.mp4');
```

### Working with lyrics

If you asked for vocals and want subtitles, remember the units:

```python
# startS / endS are MILLISECONDS despite the names.
for line in job["result"]["audio_metadata"]["lyrics_timestamps"]:
    start_s = line["startS"] / 1000
    end_s = line["endS"] / 1000
    print(f"{start_s:7.2f} -> {end_s:7.2f}  {line['text']}")
```

Use `lyrics_timestamps` for subtitles on the returned video. `full_lyrics_timestamps` is aligned to `complete_audio_url`, which is a different and usually longer timeline.

## Models

`modelspec` selects how the music is generated. **Specify it explicitly.**

| Model | Output | Notes |
|---|---|---|
| `edenn_enhanced` | Song with vocals | Generates two alternatives. Supports voice cloning on the video endpoint. |
| `edenn_studio` | Fully structured song | Verse and chorus structure. Highest quality, slowest. |

### Vocals come from the prompt, not a field

Whether the track has vocals, the singer's gender, and the language of the lyrics are all inferred from `user_prompt`. There is no explicit field for any of them.

To get vocals, say so in the prompt — for example `female vocals, English, pop`. Alternatively, supply the lyrics-direction field (`lyrics_prompt` on the video endpoint, `user_lyrics_prompt` on the image endpoint); providing it turns vocals on.

If your prompt describes instrumental music — say, "a gentle piano background" — the output may contain no vocals even though you selected a vocal model. **Vocals are a result of the prompt, not a guarantee of the model.**

## Expected duration

Typical single-job times under light load. These are for setting client timeouts. **They are not a service level commitment**, and real timings vary with concurrency and input length.

| Model | Video soundtrack | Image soundtrack |
|---|---|---|
| `edenn_enhanced` | ~2–3 minutes | ~3 minutes |
| `edenn_studio` | ~3–4 minutes | ~5 minutes |

Nearly all of this is music generation. `edenn_enhanced` writes lyrics, composes, synthesises a vocal performance, and produces two alternatives. `edenn_studio` builds a full song structure and takes longest. Visual analysis scales with input length but is a small fraction of the total.

Set client timeouts to at least three times the typical value for your model, and rely on polling rather than on any single request completing.
