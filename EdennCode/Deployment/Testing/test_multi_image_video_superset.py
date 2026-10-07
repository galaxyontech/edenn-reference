"""Golden guard for the shared job-response shape.

Both job responses are an envelope (job_id / status / version / modelspec) plus
exactly four metadata blocks. Multi-image's response now SUBCLASSES the video
response with no additions, so the two are structurally IDENTICAL rather than a
superset that could drift. `test_removed_fields_are_gone` stops trimmed fields
from creeping back in.
"""
import unittest
from typing import Optional

from EdennCode.Deployment.api_multi_image_generation import MultiImageJobResponse
from EdennCode.Deployment.api_video_generation import (
    AudioMetadataBlock,
    ResponseMetadata,
    VideoJobResponse,
    VideoMetadataBlock,
)

ENVELOPE = {"job_id", "status", "version", "modelspec"}
BLOCKS = {
    "response_metadata",
    "cost_metadata",
    "video_metadata",
    "audio_metadata",
}

# Deliberately removed from the client contract.
REMOVED_FIELDS = {
    # Trimmed in the alignment pass.
    "request_metadata", "events_url", "scenes", "style_prompt", "lyrics_prompt",
    # Storage internals, dead provider handles, per-asset ids, token accounting
    # superseded by cost_metadata.token_num, and single-track primary/secondary.
    "audio_blob", "complete_audio_blob", "video_blob", "thumbnail_blob", "upload_blob",
    "upload_url",
    "token_usage", "raw_token_usage", "token_usage_breakdown",
    "matching", "critical_warning",
    "video_id", "creative_id", "primary_music_id", "selected_music_id", "alignment_id",
    "vocal_id_used", "provider_task_id", "provider_audio_id",
    "full_tracks", "section_timeline", "planning_metadata", "audio_window_start_s",
    "secondary_music_id", "secondary_full_lyrics", "secondary_full_lyrics_timestamps",
    "secondary_full_word_level_lyrics_timestamps", "secondary_complete_audio_blob",
    "secondary_complete_audio_url", "secondary_complete_audio_duration_s",
    "secondary_complete_audio_size_bytes",
    "primary_full_lyrics", "primary_full_lyrics_timestamps",
    "primary_full_word_level_lyrics_timestamps",
    # Renamed: the brief the pipeline handed the generator is a description now.
    "music_prompt",
    # music_title is exposed once (audio_metadata); it must not sit at the block top.
}


def _all_field_names(model) -> set[str]:
    """Every field name in a response, top level and inside each block."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        annotation = field.annotation
        if hasattr(annotation, "model_fields"):
            names |= set(annotation.model_fields)
    return names


class JobResponseShapeTests(unittest.TestCase):
    def test_top_level_is_envelope_plus_four_blocks(self) -> None:
        for model in (VideoJobResponse, MultiImageJobResponse):
            with self.subTest(model=model.__name__):
                self.assertEqual(set(model.model_fields), ENVELOPE | BLOCKS)

    def test_image_music_and_video_music_are_identical(self) -> None:
        # Structural: image-music subclasses video-music with no additions, so a
        # video field cannot go missing (or an image field diverge) undetected.
        self.assertTrue(issubclass(MultiImageJobResponse, VideoJobResponse))
        self.assertEqual(
            list(MultiImageJobResponse.model_fields), list(VideoJobResponse.model_fields)
        )
        self.assertEqual(_all_field_names(MultiImageJobResponse), _all_field_names(VideoJobResponse))

    def test_removed_fields_are_gone(self) -> None:
        for model in (VideoJobResponse, MultiImageJobResponse):
            with self.subTest(model=model.__name__):
                leaked = _all_field_names(model) & REMOVED_FIELDS
                self.assertEqual(leaked, set(), f"removed fields reappeared: {leaked}")

    def test_modelspec_is_top_level(self) -> None:
        for model in (VideoJobResponse, MultiImageJobResponse):
            with self.subTest(model=model.__name__):
                self.assertIn("modelspec", model.model_fields)
                # not nested inside any block
                for block in ("response_metadata", "cost_metadata", "video_metadata", "audio_metadata"):
                    ann = model.model_fields[block].annotation
                    self.assertNotIn("modelspec", getattr(ann, "model_fields", {}))

    def test_music_description_is_a_plain_string_on_both(self) -> None:
        # music_description is the music brief as a plain string on both paths;
        # the nested tempo/mood/instruments block was removed.
        self.assertEqual(
            AudioMetadataBlock.model_fields["music_description"].annotation,
            Optional[str],
        )
        import EdennCode.Deployment.api_video_generation as vg
        self.assertFalse(hasattr(vg, "MusicDescriptionResponse"))

    def test_no_blob_or_primary_secondary_naming_anywhere(self) -> None:
        for model in (VideoJobResponse, MultiImageJobResponse):
            for name in _all_field_names(model):
                with self.subTest(model=model.__name__, field=name):
                    self.assertNotIn("blob", name.lower())
                    self.assertFalse(name.startswith(("primary_", "secondary_")))

    def test_scenes_removed_from_video_metadata(self) -> None:
        self.assertNotIn("scenes", VideoMetadataBlock.model_fields)

    def test_compression_applied_is_shared(self) -> None:
        self.assertIn("compression_applied", ResponseMetadata.model_fields)

    def test_geometry_carries_the_shared_keys(self) -> None:
        for model in (VideoJobResponse, MultiImageJobResponse):
            with self.subTest(model=model.__name__):
                geometry = model(job_id="j").model_dump(mode="json")["video_metadata"][
                    "geometry"
                ]
                self.assertEqual(set(geometry), {"width", "height", "duration", "fps"})
                # duration_s is input-only (mirrored into duration), never surfaced.
                self.assertNotIn("duration_s", geometry)

    def test_music_title_is_never_empty_on_a_populated_response(self) -> None:
        from EdennCode.Deployment.api_video_generation import (
            MUSIC_TITLE_FALLBACK,
            _music_title_from_summary,
        )

        self.assertEqual(_music_title_from_summary({"music_title": "Neon Drift"}), "Neon Drift")
        self.assertEqual(_music_title_from_summary({"video_title": "A Day Out"}), "A Day Out")
        self.assertEqual(_music_title_from_summary({}), MUSIC_TITLE_FALLBACK)
        self.assertEqual(_music_title_from_summary(None), MUSIC_TITLE_FALLBACK)
        self.assertEqual(_music_title_from_summary({"music_title": "  "}), MUSIC_TITLE_FALLBACK)


if __name__ == "__main__":
    unittest.main()
