# Recommendation Data Schema Design

This document captures the MVP data model for a discovery/feed recommendation
agent.

The agent takes a `user_id`, fetches user preference and interaction signals
from the User DB, retrieves eligible creatives from the Video-Music DB, and ranks
final feed creatives.

The core product strength is **video/music alignment**. The MVP should make
alignment first-class without overcomplicating the schema.

Do not modify the existing `t_media_work` table. New recommendation structure
should be added through new tables.

## End-To-End Picture

Feed recommendation:

```text
User opens discovery/feed
        |
        v
Recommendation Agent receives user_id
        |
        v
User DB
  - user_profile
  - user_creation_summary
  - feed_interaction_event
  - creative_engagement_stats
        |
        v
Build user preference profile
  - visual taste
  - music taste
  - locale/language/platform
  - positive/negative engagement
        |
        v
Video-Music DB
  coarse retrieve eligible creatives
        |
        v
creative
  creative_id
  video_id
  selected_music_id
  alignment_id
  result_video_url
  thumbnail_url
        |
        v
creative_feature_snapshot
  visual features / visual_embedding
  music features / music_embedding
  alignment_score
  typed coarse filters
        |
        v
Rank
  alignment_score          core strength
  music_preference_score
  visual_preference_score
  engagement_quality_score
  freshness/diversity/safety
        |
        v
Return feed creatives
  creative_id
  result_video_url
  thumbnail_url
  title / description
```

Creation-side relationship:

```text
video_asset
  video_id
        |
        v
generation_job
  job_id
        |
        v
music_asset
  music_id primary
  music_id secondary optional
        |
        v
music_video_alignment
  alignment_id
  video_id + music_id
  alignment_score
        |
        v
creative
  creative_id
  selected_music_id
  alignment_id
  final rendered feed video
```

MVP principle:

```text
creative_id is what we recommend.
alignment_score is the strongest ranking signal.
video_id and music_id explain what made the creative.
user_id explains who we are ranking for.
```

## DB Boundaries

There are two DB sides:

| DB side | Owns |
| --- | --- |
| User DB | User profile, creation summary, feed interaction events, and engagement aggregates. |
| Video-Music DB | Creatives, videos, music variants, generation jobs, alignment, feature snapshots, annotations, enrichment, and embeddings. |

High-level rule:

```text
User DB:
  who is the user?
  what did the user do?

Video-Music DB:
  what creative exists?
  which video/music/alignment produced it?
  what features does it have?
```

## Core IDs

| ID | Meaning | DB owner |
| --- | --- | --- |
| `user_id` | Person using the product. | User DB |
| `creative_id` | Final rendered feed item shown to users. | Video-Music DB |
| `video_id` | Reusable source video or visual asset. | Video-Music DB |
| `music_id` | One generated music variant. | Video-Music DB |
| `alignment_id` | Quality record for one `video_id` + `music_id` pair. | Video-Music DB |
| `job_id` | Creation process/audit run. | Video-Music DB |

Relationship rules:

| Relationship | Meaning |
| --- | --- |
| One `video_id` to many `job_id`s | A source video can be reused for multiple generations. |
| One `job_id` to one `video_id` | A generation run starts from one source video. |
| One `job_id` to one or two `music_id`s | A job can produce primary and optional secondary music variants. |
| One `creative_id` to one selected `music_id` | The final feed item uses one selected music variant. |
| One `alignment_id` to one `video_id` + `music_id` | Alignment is pair-specific. |

## What Is `creative`?

`creative` is the final rendered feed item.

The feed does not display a raw source video or a raw generated audio track. It
displays the completed artifact:

```text
source video + selected generated music + remix/render result = creative
```

Entity meanings:

| Entity | Meaning |
| --- | --- |
| `video_asset` | Reusable source video / visual asset. |
| `music_asset` | Generated music variant. |
| `music_video_alignment` | Quality of a specific video/music pairing. |
| `creative` | Final remixed video shown in discovery/feed. |

Current compatibility:

```text
t_media_work.work_id ~= legacy creative_id
```

Because `t_media_work` should not be changed, the new `creative` table should
store an optional `legacy_work_id` to map existing media records into the new
feed-item model.

## User DB

This DB owns users and user behavior.

Existing tables:

| Table | Role |
| --- | --- |
| `user_profile` | Static user metadata: locale, language, platform, subscription tier. |
| `user_creation_summary` | Per-user aggregate creation preferences. |
| `user_interaction_event` | Experimental interaction table. Do not use as the clean long-term table. |
| `t_media_work` | Existing media/product table. Keep unchanged. `work_id` can map to `creative.legacy_work_id`. |

### `feed_interaction_event`

Clean long-term feed interaction table.

| Column | Purpose |
| --- | --- |
| `id` | Primary key. |
| `user_id` | User who performed the interaction. |
| `creative_id` | Feed item interacted with. External ID owned by Video-Music DB. |
| `event_type` | Impression, click, watch, audio_listen, like, save, share, hide, report. |
| `rank_position` | 1-based feed position where the creative was shown. Required for position-bias correction. |
| `watch_duration_ms` | Watch duration if applicable. |
| `watch_completion_pct` | Completion percentage if applicable. |
| `audio_listen_duration_ms` | Audio listening duration if applicable. |
| `session_id` | Session identifier. |
| `platform` | iOS, Android, web, etc. |
| `event_time` | Event timestamp. |
| `metadata` | JSON metadata for source, channel, experiment, etc. |

MVP backfill rule:

```text
feed_interaction_event.creative_id = user_interaction_event.work_id
```

Negative events:

`hide` and `report` are first-class negative signals. They should feed both
ranking penalties and content/feed safety filters.

### `creative_engagement_stats`

Pre-aggregated engagement features by creative. The recommender should not scan
raw `feed_interaction_event` rows per request.

| Column | Purpose |
| --- | --- |
| `creative_id` | Feed creative ID. External ID owned by Video-Music DB. |
| `impression_count` | Total impressions. |
| `click_count` | Total clicks. |
| `watch_count` | Total watch events. |
| `avg_watch_completion_pct` | Average watch completion. |
| `audio_listen_count` | Total audio listen events. |
| `like_count` | Total likes. |
| `save_count` | Total saves. |
| `share_count` | Total shares. |
| `hide_count` | Total hides. |
| `report_count` | Total reports. |
| `ctr` | Click-through rate. |
| `like_rate` | Likes per impression. |
| `save_rate` | Saves per impression. |
| `share_rate` | Shares per impression. |
| `hide_rate` | Hides per impression. |
| `report_rate` | Reports per impression. |
| `position_bias_corrected_score` | Optional precomputed engagement quality score corrected for rank position. |
| `updated_at` | Last aggregation timestamp. |

This table can be updated on a schedule, by streaming aggregation, or by trigger
depending on traffic volume and latency requirements.

## Video-Music DB

This DB owns creatives, source videos, generated music, alignment, feature
snapshots, annotations, enrichment, and embeddings.

Existing tables:

| Table | Role |
| --- | --- |
| `annotation_events` | Raw pipeline event log keyed by `job_id`. |
| `taxonomy_enrichments` | Music/text taxonomy currently keyed by `job_id`. |
| `visual_taxonomy` | Visual taxonomy currently keyed by `job_id`. |
| `music_audio_features` | Audio features currently keyed by `job_id`. |
| `requests` | API request observability. Not the primary recommendation item table. |
| `pipeline_runs` | Pipeline run observability. Not the primary recommendation item table. |

### `creative`

Feed candidate table and final rendered artifact table.

| Column | Purpose |
| --- | --- |
| `creative_id` | Primary key. Final feed item ID. |
| `legacy_work_id` | Optional mapping to `t_media_work.work_id`; no cross-DB FK. |
| `creator_user_id` | User who created this creative. External ID from User DB. |
| `job_id` | Generation run that produced it. |
| `video_id` | Source video asset used. |
| `selected_music_id` | Music variant selected for the final creative. |
| `alignment_id` | Alignment record for the selected video/music pair. |
| `result_video_url` | Final displayable video URL. |
| `result_video_blob` | Final displayable video blob path. |
| `thumbnail_url` | Feed thumbnail URL. |
| `thumbnail_blob` | Feed thumbnail blob path. |
| `title` | User-facing title. |
| `description` | User-facing description. |
| `visibility` | Feed eligibility, for example public/private/unlisted. |
| `status` | Draft, processing, published, hidden, deleted, failed, etc. |
| `created_at` | Creation timestamp. |
| `updated_at` | Last update timestamp. |

### `video_asset`

Source visual asset table.

| Column | Purpose |
| --- | --- |
| `video_id` | Primary key. |
| `owner_user_id` | Optional creator/uploader user ID. External ID from User DB. |
| `source_video_url` | Original source video URL. |
| `source_video_blob` | Original source video blob path. |
| `content_hash` | Optional dedupe key. |
| `duration_s` | Duration. |
| `width` | Width. |
| `height` | Height. |
| `fps` | Frames per second. |
| `visual_feature_json` | Denormalized visual features. |
| `visual_embedding` | Optional vector embedding for visual retrieval. |
| `created_at` | Creation timestamp. |

### `generation_job`

Creation process/audit table. Keep this simple for MVP; it is metadata, not a
ranking object.

| Column | Purpose |
| --- | --- |
| `job_id` | Primary key. |
| `creator_user_id` | User who initiated generation. External ID from User DB. |
| `video_id` | Source video asset. |
| `creative_id` | Optional final creative ID once created. |
| `prompt` | Original or sanitized prompt. |
| `model_spec` | Requested or resolved model spec. |
| `options` | JSON generation options. |
| `status` | Running, completed, failed, etc. |
| `created_at` | Start timestamp. |
| `finished_at` | Finish timestamp. |

Namespace rule:

`generation_job.job_id` must be the same ID namespace used by:

```text
annotation_events.job_id
taxonomy_enrichments.job_id
visual_taxonomy.job_id
music_audio_features.job_id
```

The API-created `job_id` should be passed into the workflow and annotation
pipeline. The workflow should not create a second unrelated annotation `job_id`.

### `music_asset`

Generated music variant table.

| Column | Purpose |
| --- | --- |
| `music_id` | Primary key. |
| `job_id` | Generation run that produced the music. |
| `video_id` | Source video context used during generation. |
| `variant_role` | `primary` or `secondary`. |
| `provider_name` | ProviderA, ProviderB, ProviderC, etc. |
| `provider_task_id` | Provider task ID if available. |
| `provider_audio_id` | Provider audio ID if available. |
| `full_audio_url` | Full generated track URL. |
| `full_audio_blob` | Full generated track blob path. |
| `trimmed_audio_url` | Trimmed or selected clip URL used in the final creative. |
| `trimmed_audio_blob` | Trimmed or selected clip blob path. |
| `duration_s` | Audio duration. |
| `lyrics` | Optional lyrics JSON/text. |
| `music_feature_json` | Denormalized music features. |
| `music_embedding` | Optional vector embedding for music retrieval. |
| `created_at` | Creation timestamp. |

### `music_video_alignment`

Alignment is the core MVP strength. This table should be first-class and easy
to debug.

Required MVP fields:

| Column | Purpose |
| --- | --- |
| `alignment_id` | Primary key. |
| `job_id` | Generation run. |
| `video_id` | Source video asset. |
| `music_id` | Music variant being aligned. |
| `creative_id` | Final creative that selected this alignment, when applicable. |
| `alignment_score` | Overall alignment quality. Primary alignment ranking signal. |
| `selected_clip_start_s` | Start offset selected for the final creative. |
| `selected_clip_duration_s` | Selected clip duration. |
| `matching_used_track` | Primary/secondary/etc. |
| `alignment_reason_json` | Debuggable explanation/details from matching. |
| `created_at` | Creation timestamp. |

Optional sub-scores if available without extra complexity:

| Column | Purpose |
| --- | --- |
| `beat_sync_score` | Beat/scene transition match. |
| `mood_match_score` | Music mood vs visual mood match. |
| `tempo_pacing_score` | Music tempo vs video pacing match. |
| `vocal_fit_score` | Vocal/lyrics fit, if applicable. |

Do not add `alignment_embedding` for MVP unless there is already a clear and
stable embedding source. Store detailed reasoning in `alignment_reason_json`
first.

### `creative_feature_snapshot`

Recommender-facing feature table. It denormalizes the most important video,
music, and alignment features behind a feed creative.

| Column | Purpose |
| --- | --- |
| `creative_id` | Primary key. Matches Video-Music DB `creative.creative_id`. |
| `job_id` | Generation run. |
| `video_id` | Source video asset. |
| `selected_music_id` | Selected music variant. |
| `alignment_id` | Selected video/music alignment row. |
| `alignment_score` | Denormalized core alignment score for fast ranking. |
| `genre_level1` | Coarse indexed music genre filter. |
| `genre_level2` | Secondary indexed music genre filter. |
| `language` | Music/lyrics or creative language filter. |
| `tempo_class` | Coarse tempo filter. |
| `energy_level` | Numeric music energy feature for filtering/scoring. |
| `vocal_style` | Vocal style filter, including instrumental/none. |
| `content_type` | Visual content type filter. |
| `platform_hint` | Visual/platform format filter, for example TikTok/YouTube/Instagram/generic. |
| `pacing_class` | Visual pacing filter. |
| `visibility` | Denormalized creative feed eligibility filter. |
| `status` | Denormalized creative publication/status filter. |
| `created_at` | Denormalized creative creation time for freshness filters. |
| `visual_feature_json` | Visual/feed features. |
| `music_feature_json` | Music features. |
| `alignment_feature_json` | Alignment scores and metadata. |
| `visual_embedding` | Optional visual vector copied from or derived from `video_asset`. |
| `music_embedding` | Optional music vector copied from or derived from `music_asset`. |
| `updated_at` | Snapshot update timestamp. |

Embedding rule:

Do not store only one `combined_embedding` as the primary retrieval vector.
Visual and music preferences must be independently weightable and debuggable.
Combine `visual_embedding`, `music_embedding`, and `alignment_score` at ranking
time with explicit or learnable weights.

Coarse-filter rule:

The most common retrieval filters should be typed columns on
`creative_feature_snapshot`, not only nested inside JSON blobs. At minimum,
index:

```text
visibility
status
created_at
alignment_score
genre_level1
genre_level2
language
tempo_class
energy_level
content_type
platform_hint
pacing_class
```

JSON feature blobs remain useful for inspection and long-tail features, but the
first retrieval pass should not depend on JSON scans.

## Cross-DB Contract

The DBs are separate, so do not depend on cross-DB foreign keys. Use stable IDs
as the contract.

| User DB | Video-Music DB |
| --- | --- |
| `feed_interaction_event.user_id` | External user ID used by `creative.creator_user_id`, `generation_job.creator_user_id`, and `video_asset.owner_user_id`. |
| `feed_interaction_event.creative_id` | `creative.creative_id` |
| `creative_engagement_stats.creative_id` | `creative.creative_id` |
| `t_media_work.work_id` | `creative.legacy_work_id` |
| `user_interaction_event.work_id` | Backfill source for `feed_interaction_event.creative_id` |

Within the Video-Music DB:

| Source | Target |
| --- | --- |
| `creative.job_id` | `generation_job.job_id` |
| `creative.video_id` | `video_asset.video_id` |
| `creative.selected_music_id` | `music_asset.music_id` |
| `creative.alignment_id` | `music_video_alignment.alignment_id` |
| `creative_feature_snapshot.creative_id` | `creative.creative_id` |

## Recommendation Agent Flow

Input:

```text
user_id
```

Steps:

1. Fetch user data from the User DB:
   `user_profile`, `user_creation_summary`, recent `feed_interaction_event`,
   and `creative_engagement_stats`.
2. Build user preference signals:
   visual interests, music interests, locale/language/platform, and interaction weights.
3. Coarse retrieve eligible `creative_feature_snapshot` candidates from the
   Video-Music DB using typed indexed filters.
4. Join candidate rows to `creative` for URLs, thumbnail, title, description,
   visibility, and status.
5. Rank candidates with alignment as the dominant signal.
6. Return feed items:
   `creative_id`, `result_video_url`, `thumbnail_url`, title, description, and debug scores.

## Ranking Dimensions

MVP ranking should make alignment dominant:

```text
feed_score =
  0.45 * alignment_score
  + 0.25 * music_preference_score
  + 0.20 * visual_preference_score
  + 0.10 * engagement_quality_score
  + freshness/diversity/safety adjustments
```

These weights are an MVP starting point, not a permanent rule.

Score definitions:

| Score | Meaning |
| --- | --- |
| `alignment_score` | How well the selected music works with the selected video. Core product strength. |
| `music_preference_score` | User tends to engage with similar genre, mood, tempo, vocal style, instruments, language, or provider/model tier. |
| `visual_preference_score` | User tends to engage with similar video style, content type, pacing, platform format, subjects, colors, or visual tags. |
| `engagement_quality_score` | Aggregate positive and negative interaction signals for the creative. |
| `freshness/diversity/safety adjustments` | Freshness, creator diversity, subscription/business rules, safety filters, and de-duplication. |

Debugging requirement:

The ranker should log or optionally return separate score components:

```text
alignment_score
music_preference_score
visual_preference_score
engagement_quality_score
freshness_score
diversity_adjustment
safety_adjustment
```

This keeps the feed explainable and makes it clear when alignment, music taste,
or visual taste drove a recommendation.

## MVP Priorities

1. Create `feed_interaction_event` in the User DB, including `rank_position`.
2. Create `creative_engagement_stats` in the User DB.
3. Create `creative` in the Video-Music DB without modifying `t_media_work`.
4. Create `video_asset`, `music_asset`, `music_video_alignment`, and
   `creative_feature_snapshot` in the Video-Music DB.
5. Keep `generation_job` simple and ensure the API-created `job_id` is passed
   into the workflow and all annotation events.
6. Generate stable `video_id`, `music_id`, `alignment_id`, and `creative_id`.
7. Persist `alignment_score` and selected clip metadata for every final creative.
8. Populate typed coarse-filter columns on `creative_feature_snapshot`.
9. Add separate `visual_embedding` and `music_embedding`; combine them at ranking time.

