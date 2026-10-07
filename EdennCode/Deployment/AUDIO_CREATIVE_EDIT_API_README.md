# Audio Creative Edit API

This document is the detailed usage guide for `POST /api/v1/jobs/audio-creative-edit`.

It complements `EdennCode/Deployment/API_DOCUMENTATION.md` with:

- end-to-end workflow behavior
- provider-specific details
- practical request rules
- example requests
- example outputs
- reproducible example runs saved in this repo

This guide is also exposed by the service through:

- `GET /api/v1/docs/audio-creative-edit`

Saved example artifacts referenced by this guide can be fetched through:

- `GET /api/v1/docs/audio-creative-edit/examples/{example_id}`

Important:

- the markdown guide is always available through the docs route
- example artifacts are listed by the docs route only when those files are present on the server filesystem

## What This API Does

The audio creative edit API takes an existing source audio track and regenerates a new version of it based on:

- a user edit prompt
- an optional visual input
- a selected downstream music provider

This is not a waveform editor. It does not directly manipulate the uploaded audio in place. Instead, it:

1. understands the edit intent from the prompt
2. optionally analyzes images or video for creative direction
3. creates a structured edit prompt
4. uses the source audio as a melody or cover reference for downstream generation

Route:

- `POST /api/v1/jobs/audio-creative-edit`

Implementation:

- API route: `EdennCode/Deployment/api_audio_creative_edit.py`
- Orchestrator: `EdennCode/Deployment/audio_edit_workflows.py`
- Workflow: `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/audio_creative_edit_workflow.py`

## Current Behavior Summary

- Input audio is required.
- Visual input is optional.
- Exactly one visual mode may be used at a time:
  - no visuals
  - video or video URL
  - images
  - image URL list
- `edenn_enhanced` uses ProviderB melody-guided generation.
- `edenn_enhanced` vocal runs can optionally use a reusable `vocal_id` or an inline `vocal_sample` / `vocal_sample_url`.
- `edenn_studio` uses ProviderC upload-cover generation.
- `edenn_studio` now exposes `studio_mode`:
  - `simple` maps to ProviderC non-custom upload-cover
  - `custom` maps to ProviderC custom-mode upload-cover
- Vocal intent is inferred from the user prompt, not from the source audio file.
- Source lyrics are not preserved automatically.
- Generated lyric timestamps are returned only when the downstream provider produces them.

## High-Level Workflow

The request flows through four logical stages.

### 1. Source Resolution

The API accepts exactly one audio source:

- `audio`
- `audio_url`

If the source is uploaded, it is written into a per-job work directory. If Azure storage is enabled, the source audio is also staged to blob storage so downstream providers can consume it as a remote URL when needed.

Relevant code:

- `create_audio_creative_edit_job()` in `EdennCode/Deployment/api_audio_creative_edit.py`

### 2. Prompt Preprocessing

The workflow first runs the shared prompt preprocessor. It:

- sanitizes celebrity voice imitation requests for policy compliance
- translates the normalized prompt to English when needed
- infers whether vocals are requested
- infers vocal gender
- infers downstream language

Relevant code:

- `UserPromptPreprocessorAgent` in `EdennCode/WorkflowFactory/VideoMusicWorkflow/Stages/UserIntentUnderstandingStage/user_prompt_preprocessor.py`

### 3. Visual Conditioning

If visuals are provided:

- video input goes through preprocess, scene segmentation, and video understanding
- image input goes through batch visual analysis using the model gateway with temporary SAS-backed image URLs

This produces:

- `summary`
- `overall_mood`
- `visual_style`
- `creative_direction`
- optional `scenes`

If no visuals are provided, `visual_analysis.input_type = "none"`.

Relevant code:

- `VisualConditioningStage` in `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/VisualConditioningStage/visual_conditioning_stage.py`

### 4. Prompt Synthesis

The workflow converts:

- user prompt
- inferred vocal settings
- visual analysis

into a normalized creative edit payload:

- `title`
- `edit_intent_summary`
- `visual_style_summary`
- `edit_prompt`
- `style_prompt`
- `lyrics_prompt`

Relevant code:

- `CreativeEditPromptStage` in `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/CreativeEditPromptStage/creative_edit_prompt_stage.py`

### 5. Provider Generation

The final generation step depends on `modelspec`.

#### `edenn_enhanced`

Uses ProviderB melody-guided generation.

Behavior:

- the source audio is converted into a melody reference file
- the melody is the primary guide
- instrumental runs still send a lightweight prompt to ProviderB together with the melody guide
- vocal runs can optionally clone and reuse a ProviderB `vocal_id`
- two output variants are requested
- instrumental runs return no lyric timestamps
- vocal runs can return lyric timestamps if the provider supplies them

Important current nuance:

- the melody reference is the dominant guide
- vocal runs still use prompt understanding for vocal intent, language, gender, and generated lyrics, but the final ProviderB song-generation request currently sends `lyrics` plus `melody_id` without a style prompt because the provider rejects `prompt + melody_id` on this path
- when `vocal_id` or `vocal_sample` / `vocal_sample_url` is provided, the response returns `vocal_id_used`
- text guidance still influences the run overall, but this branch is still fundamentally melody-guided rather than fully text-directed
- although ProviderB exposes a dedicated remix API, this workflow is not wired to `/v1/song/remix` today
- for rough melody sketches such as humming, whistling, or a phone-recorded sung topline, this branch is usually the better fit because it is more likely to treat the upload as melody to extract and reuse

Relevant code:

- `AudioCreativeGenerationStage._run_provider_b()` in `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/AudioCreativeGenerationStage/audio_creative_generation_stage.py`

#### `edenn_studio`

Uses ProviderC upload-cover generation.

Behavior:

- the source audio must be available as a remote URL
- uploaded files are staged to blob storage automatically when storage is enabled
- `studio_mode=simple` sends only the synthesized `edit_prompt`
- `studio_mode=custom` maps synthesized fields into ProviderC custom mode:
  - `title`
  - `style_prompt`
  - `lyrics_prompt` for vocal runs
- optional Studio tuning fields are forwarded in custom mode:
  - `studio_style_weight`
  - `studio_audio_weight`
  - `studio_weirdness_constraint`
- the returned lyrics are for the generated output, not the source audio

Important current nuance:

- this is cover-style regeneration guided by the uploaded source
- it is not lyric preservation
- it is not source-audio continuation
- in custom vocal mode, the backend passes the synthesized `lyrics_prompt` to ProviderC as the lyric input
- that is still not source lyric extraction or lyric carry-over from the uploaded file
- for whistle-like or hummed inputs, this branch is more likely to treat the upload as source audio/performance material to reinterpret, not as a pure melody sketch to extract

Relevant code:

- `AudioCreativeGenerationStage._run_provider_c()` in `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/AudioCreativeGenerationStage/audio_creative_generation_stage.py`

## Request Contract

Content type:

- `multipart/form-data`

### Request Fields

| Field | Type | Required | Description |
|---|---|---:|---|
| `audio` | file | Conditionally | Uploaded source audio |
| `audio_url` | string | Conditionally | Publicly downloadable source audio URL |
| `user_prompt` | string | No | Creative edit instruction |
| `modelspec` | string | No | Provider branch to use |
| `studio_mode` | string | No | For `edenn_studio` only. `simple` uses ProviderC non-custom mode. `custom` uses ProviderC custom mode. Default: `simple` |
| `studio_style_weight` | number | No | For `edenn_studio` custom mode only. Style influence weight. Range `0.00` to `1.00` in `0.01` steps |
| `studio_audio_weight` | number | No | For `edenn_studio` custom mode only. Source-audio influence weight. Range `0.00` to `1.00` in `0.01` steps |
| `studio_weirdness_constraint` | number | No | For `edenn_studio` custom mode only. Creative deviation control. Range `0.00` to `1.00` in `0.01` steps |
| `video` | file | No | Optional visual conditioning video |
| `video_url` | string | No | Optional visual conditioning video URL |
| `images` | repeated file field | No | Optional visual conditioning image uploads |
| `image_urls_json` | string | No | Optional JSON array of image URLs |
| `vocal_id` | string | No | Optional reusable ProviderB vocal clone ID for vocal `edenn_enhanced` runs |
| `vocal_sample` | file | No | Optional vocal sample to clone inline for vocal `edenn_enhanced` runs |
| `vocal_sample_url` | string | No | Optional remote vocal sample URL to clone inline for vocal `edenn_enhanced` runs |

### Allowed `modelspec`

- `edenn_enhanced`
- `edenn_studio`

Legacy alias:

- `provider_c` maps to `edenn_studio`

### Validation Rules

Audio source:

- provide exactly one of `audio` or `audio_url`

Visual source:

- visuals are optional
- if provided, use exactly one mode:
  - `video`
  - `video_url`
  - `images`
  - `image_urls_json`

Invalid combinations return `400`.

Studio options:

- `studio_mode` defaults to `simple`
- allowed values are `simple` and `custom`
- Studio weight fields are only accepted when `modelspec=edenn_studio` and `studio_mode=custom`
- Studio weight fields must stay within `0.00` to `1.00`
- Studio weight fields must use `0.01` increments

Vocal clone options:

- provide either `vocal_id` or `vocal_sample` / `vocal_sample_url`, not both
- vocal clone inputs are only accepted when `modelspec=edenn_enhanced`
- the request must still resolve to a vocal run after prompt understanding

### Storage-Dependent Behavior

If Azure Blob Storage is enabled:

- uploaded source audio is staged to blob storage
- output media fields include blob names and SAS URLs
- image conditioning works

If Azure Blob Storage is disabled:

- output files may still be generated internally
- `edenn_studio` requires `audio_url`, because ProviderC needs a remote source audio URL
- image-based conditioning is unavailable, because the LLM image path relies on temporary SAS URLs

## Choosing The Right Model For Melody Sketches

For rough user-recorded melody inputs such as:

- whistling
- humming
- a phone-recorded sung topline
- a simple guide melody without a real arrangement

prefer `edenn_enhanced`.

Why:

- the current `edenn_enhanced` implementation uploads the source as a ProviderB `melody_id`
- in practice, that makes it more melody-guide-oriented
- it is more likely to preserve the contour of a whistle or hum while rebuilding the surrounding instrumentation

By contrast, `edenn_studio` is closer to cover-style audio-to-audio regeneration:

- it still uses the uploaded source
- but it is more likely to treat the upload as source audio style/performance material to reinterpret
- that makes it better for fuller demos, existing song ideas, or audio that already behaves like a produced clip

This is practical guidance, not a hard guarantee. Both model branches are influenced by the uploaded file, but their center of gravity is different:

- `edenn_enhanced` is more melody-extraction and melody-guidance oriented
- `edenn_studio` is more direct source-audio reinterpretation oriented

## Response Contract

Main response model:

- `AudioCreativeEditResponse` in `EdennCode/Deployment/api_audio_creative_edit.py`

### Top-Level Response Fields

| Field | Type | Description |
|---|---|---|
| `job_id` | string | Unique job identifier |
| `status` | string | Usually `completed` |
| `source_audio_blob` | string or null | Blob path for staged input audio |
| `source_audio_url` | string or null | Staged input audio URL |
| `edited_audio_blob` | string or null | Blob path for primary output |
| `edited_audio_url` | string or null | Primary output URL |
| `secondary_edited_audio_blob` | string or null | Blob path for optional second output |
| `secondary_edited_audio_url` | string or null | Optional second output URL |
| `thumbnail_blob` | string or null | Blob path for visual thumbnail |
| `thumbnail_url` | string or null | Thumbnail URL |
| `visual_analysis` | object | Result of visual conditioning |
| `creative_edit_prompt` | object | Structured prompt payload used to drive generation |
| `lyrics_timestamps` | array | Generated lyric timestamps when available |
| `include_vocals` | boolean | Inferred from prompt understanding |
| `vocal_gender` | string | Inferred from prompt understanding |
| `modelspec` | string | Actual provider branch used |
| `user_requested_language` | string | Inferred downstream language |
| `vocal_id_used` | string or null | The ProviderB vocal clone ID used for the run when vocal `edenn_enhanced` cloning applied |
| `token_usage` | integer or null | Total token count |
| `raw_token_usage` | object or null | Flat token breakdown |
| `token_usage_breakdown` | object or null | Stage-by-stage token breakdown |
| `job_received_timestamp` | integer or null | Unix timestamp |
| `job_finished_timestamp` | integer or null | Unix timestamp |

### `visual_analysis`

`visual_analysis.input_type` is one of:

- `none`
- `video`
- `images`

Fields:

- `summary`
- `overall_mood`
- `visual_style`
- `creative_direction`
- `key_elements`
- `scenes`

### `creative_edit_prompt`

Fields:

- `title`
- `edit_intent_summary`
- `visual_style_summary`
- `edit_prompt`
- `style_prompt`
- `lyrics_prompt`

## Example Requests

### Example 1: Uploaded Audio + Image Upload + ProviderB

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio=@/path/to/source.mp3" \
  -F "images=@/path/to/reference.jpg" \
  -F "user_prompt=Reimagine this as a spacious desert-drive instrumental with wider guitars, warm dust, calm forward motion, and cinematic openness." \
  -F "modelspec=edenn_enhanced"
```

Expected behavior:

- uploads the source audio
- analyzes the image
- creates a structured edit prompt
- sends the source melody plus prompt to ProviderB for the instrumental generation request
- returns two instrumental variants

### Example 2: Uploaded Audio + Image Upload + ProviderC Simple Mode

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio=@/path/to/source.mp3" \
  -F "images=@/path/to/reference.jpg" \
  -F "user_prompt=Turn this into a reflective female vocal road-song with clear lyrics, open-air warmth, and a hopeful lift while staying connected to the source melody." \
  -F "modelspec=edenn_enhanced" \
  -F "vocal_sample=@/path/to/voice_reference.wav"
```

Expected behavior:

- uploads and stages the source audio
- analyzes the image
- creates `edit_prompt`, `style_prompt`, and `lyrics_prompt`
- creates a ProviderB vocal clone from the supplied sample, then sends the melody-guided ProviderB vocal request
- returns one primary vocal output plus an optional secondary output
- returns lyric timestamps for the generated result when available
- returns `vocal_id_used`

### Example 3: Audio URL + Video URL + ProviderC Custom Mode

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio_url=https://example.com/source.wav" \
  -F "video_url=https://example.com/visual.mp4" \
  -F "user_prompt=Make this more cinematic, emotionally lifted, and better matched to the visual pacing." \
  -F "studio_mode=custom" \
  -F "studio_style_weight=0.65" \
  -F "studio_audio_weight=0.75" \
  -F "studio_weirdness_constraint=0.20" \
  -F "modelspec=edenn_studio"
```

Expected behavior:

- uploads or resolves the source audio URL
- analyzes the video
- synthesizes `title`, `style_prompt`, and `lyrics_prompt`
- sends a ProviderC custom-mode upload-cover request
- applies the optional Studio influence weights

### Example 4: Audio Upload Only

```bash
curl -X POST "http://localhost:8080/api/v1/jobs/audio-creative-edit" \
  -F "audio=@/path/to/source.wav" \
  -F "user_prompt=Create a cleaner, softer, more elegant instrumental version while preserving the melodic identity." \
  -F "modelspec=edenn_enhanced"
```

## Example Output Shapes

The exact URLs and IDs vary by run. The examples below are trimmed to stable fields.

### Example Output: `edenn_enhanced` Instrumental

```json
{
  "job_id": "<job_id>",
  "status": "completed",
  "modelspec": "edenn_enhanced",
  "include_vocals": false,
  "vocal_gender": "unknown",
  "user_requested_language": "ENGLISH_US",
  "visual_analysis": {
    "input_type": "images",
    "summary": "Urban cityscape scene depicting a person amidst traffic...",
    "overall_mood": "Calm yet thoughtful...",
    "visual_style": "Cinematic realism with desaturated tones...",
    "creative_direction": "Sound design should emphasize ambient city noises..."
  },
  "creative_edit_prompt": {
    "title": "Desert Reflection Drive",
    "edit_intent_summary": "Transform the original track into a spacious desert-drive instrumental...",
    "edit_prompt": "Rework the source into an instrumental evoking a slow-motion drive through open desert roads...",
    "style_prompt": "Instrumental with wide ambient guitars...",
    "lyrics_prompt": ""
  },
  "edited_audio_url": "<primary-output-url>",
  "secondary_edited_audio_url": "<secondary-output-url>",
  "lyrics_timestamps": []
}
```

### Example Output: `edenn_studio` Vocal

```json
{
  "job_id": "<job_id>",
  "status": "completed",
  "modelspec": "edenn_studio",
  "include_vocals": true,
  "vocal_gender": "female",
  "user_requested_language": "English",
  "visual_analysis": {
    "input_type": "images",
    "summary": "The visual portrays a bustling urban street scene...",
    "overall_mood": "Reflective and contemplative...",
    "visual_style": "Cinematic realism featuring muted tones...",
    "creative_direction": "The sound design should evoke a calm yet introspective energy..."
  },
  "creative_edit_prompt": {
    "title": "City Roads Reflections",
    "edit_prompt": "Rework the source audio to convey an open-road feeling within a dense urban setting...",
    "style_prompt": "Slow tempo, ambient-pop arrangement with acoustic guitar...",
    "lyrics_prompt": "English (US) female vocals expressing themes of inner journey..."
  },
  "edited_audio_url": "<primary-output-url>",
  "secondary_edited_audio_url": "<secondary-output-url>",
  "lyrics_timestamps": [
    {
      "text": "<generated lyric token or line>",
      "startS": 0.0,
      "endS": 1.2,
      "i": 0
    }
  ]
}
```

## Reproducible Example Runs In This Repo

The following artifacts were generated from live runs and are saved locally:

- Reference 15s clip:
  - `EdennCode/Deployment/example_runs/reference_15s.mp4`
- Still image used for image conditioning:
  - `EdennCode/Deployment/example_runs/reference_frame.jpg`
- Reference generated source audio downloaded locally:
  - `EdennCode/Deployment/example_runs/reference_source.mp3`
- Reference video-generation JSON:
  - `EdennCode/Deployment/example_runs/reference_edenn_basic.json`
- ProviderB creative edit JSON:
  - `EdennCode/Deployment/example_runs/audio_creative_edit_enhanced_instrumental.json`
- ProviderC creative edit JSON:
  - `EdennCode/Deployment/example_runs/audio_creative_edit_studio_vocal.json`
- Whistle-style source melody sketch:
  - `EdennCode/Deployment/example_runs/whistle_reference_source.mp3`
- Whistle-style ProviderB comparison JSON:
  - `EdennCode/Deployment/example_runs/whistle_audio_creative_edit_enhanced.json`
- Whistle-style Studio comparison JSON:
  - `EdennCode/Deployment/example_runs/whistle_audio_creative_edit_studio_custom.json`
- Whistle-style ProviderB output:
  - `EdennCode/Deployment/example_runs/whistle_enhanced_primary.mp3`
- Whistle-style Studio output:
  - `EdennCode/Deployment/example_runs/whistle_studio_primary.mp3`
- 30s previews for quick listening:
  - `EdennCode/Deployment/example_runs/previews/whistle_enhanced_primary_preview_30s.mp3`
  - `EdennCode/Deployment/example_runs/previews/whistle_studio_primary_preview_30s.mp3`

These files are useful for:

- frontend integration
- response-shape inspection
- provider behavior comparison
- regression checks after workflow changes
- model-selection guidance for melody sketch uploads

## Helper Script

For repeatable local runs, use:

- `EdennCode/Deployment/save_audio_creative_edit_response_example.py`

### Script Usage

Run with uploaded audio:

```bash
./.venv/bin/python EdennCode/Deployment/save_audio_creative_edit_response_example.py \
  --audio EdennCode/Deployment/example_runs/reference_source.mp3 \
  --image EdennCode/Deployment/example_runs/reference_frame.jpg \
  --user-prompt "Reimagine this as a spacious desert-drive instrumental with wider guitars, warm dust, calm forward motion, and cinematic openness." \
  --modelspec edenn_enhanced \
  --output-json EdennCode/Deployment/example_runs/audio_creative_edit_enhanced_instrumental.json
```

Run with uploaded audio for ProviderC:

```bash
./.venv/bin/python EdennCode/Deployment/save_audio_creative_edit_response_example.py \
  --audio EdennCode/Deployment/example_runs/reference_source.mp3 \
  --image EdennCode/Deployment/example_runs/reference_frame.jpg \
  --user-prompt "Turn this into a reflective female vocal road-song with clear lyrics, open-air warmth, and a hopeful lift while staying connected to the source melody." \
  --modelspec edenn_studio \
  --studio-mode simple \
  --output-json EdennCode/Deployment/example_runs/audio_creative_edit_studio_vocal.json
```

Run with ProviderC custom mode and explicit Studio weights:

```bash
./.venv/bin/python EdennCode/Deployment/save_audio_creative_edit_response_example.py \
  --audio EdennCode/Deployment/example_runs/reference_source.mp3 \
  --image EdennCode/Deployment/example_runs/reference_frame.jpg \
  --user-prompt "Make this more cinematic, controlled, and tightly anchored to the uploaded melody." \
  --modelspec edenn_studio \
  --studio-mode custom \
  --studio-style-weight 0.65 \
  --studio-audio-weight 0.75 \
  --studio-weirdness-constraint 0.20 \
  --output-json /tmp/audio_creative_edit_studio_custom.json
```

Run with a remote audio URL instead:

```bash
./.venv/bin/python EdennCode/Deployment/save_audio_creative_edit_response_example.py \
  --audio-url "https://example.com/source.wav" \
  --image /path/to/reference.jpg \
  --user-prompt "Make this warmer and more cinematic." \
  --modelspec edenn_studio \
  --output-json /tmp/audio_creative_edit_example.json
```

## Environment Requirements

Minimum shared requirements:

- `AZURE_ENDPOINT`
- `AZURE_API_KEY`
- `AZURE_MODEL`

For storage-backed behavior:

- `AZURE_STORAGE_CONNECTION_STRING`

For image-based conditioning:

- storage must be enabled
- `AZURE_STORAGE_LLM_IMAGE_CONTAINER` must be configured through deployment settings

For `edenn_enhanced`:

- one of:
  - `EDENN_ENHANCED_PROVIDER_B_API_KEY`
  - `PROVIDER_B_API_KEY`
  - `PROVIDER_B_API_KEY_1`
  - `PROVIDER_B_API_KEY_2`

For `edenn_studio`:

- one of:
  - `PROVIDER_C_API_KEY`
  - `PROVIDER_C_API_KEY_1` .. `PROVIDER_C_API_KEY_N` (numbered keys form a rotating pool)

## Operational Notes

- Job work directories are temporary and cleaned after request completion.
- Blob and SAS URL fields are only populated when storage is enabled.
- `lyrics_timestamps` describe generated output lyrics, not source-audio lyrics.
- `include_vocals` and `vocal_gender` are inferred from prompt understanding.
- The image-conditioning path uses temporary blob uploads and cleans them up after analysis.
- `studio_mode` defaults to `simple` when omitted.
- Studio influence weights are rejected unless `modelspec=edenn_studio` and `studio_mode=custom`.

## Known Limitations

These are current behaviors, not future plans:

- the API does not currently accept source lyric timestamps as input
- the API does not automatically extract lyrics from the uploaded source audio
- the API does not stem or separate the source audio automatically
- source lyrics are not preserved automatically
- ProviderC upload-cover behaves like guided cover regeneration, not lyric-preserving source editing
- `edenn_enhanced` does not yet expose separate remix influence weights because the workflow is not calling ProviderB's dedicated remix endpoint
- image conditioning depends on Azure Blob Storage

## Provider References

- ProviderC upload-cover docs:
  - https://docs.provider-c.example.invalid/provider_c-api/upload-and-cover-audio
- ProviderB remix docs:
  - https://platform.provider-b.example.invalid/docs/api/operations/post-v1-song-remix.html

## Troubleshooting

### `400` invalid visual source combination

Cause:

- more than one visual mode was provided

Fix:

- use exactly one of `video`, `video_url`, `images`, or `image_urls_json`

### `500` for `edenn_studio` with uploaded audio

Cause:

- storage is disabled, so the uploaded file cannot be staged to a remote URL for ProviderC

Fix:

- enable Azure Blob Storage
- or call the API with `audio_url`

### No lyric timestamps in response

Possible reasons:

- the run was instrumental
- the provider did not return timestamps
- the branch used was `edenn_enhanced` instrumental

### Image conditioning unavailable

Cause:

- storage is disabled or the LLM image container is not configured

Fix:

- configure Azure Blob Storage and the LLM image container

## Related Files

- `EdennCode/Deployment/api_audio_creative_edit.py`
- `EdennCode/Deployment/audio_edit_workflows.py`
- `EdennCode/Deployment/save_audio_creative_edit_response_example.py`
- `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/audio_creative_edit_workflow.py`
- `EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/AudioCreativeGenerationStage/audio_creative_generation_stage.py`
- `EdennCode/Deployment/API_DOCUMENTATION.md`
