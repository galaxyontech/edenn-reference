"""Multi-image is billed by delivered video length: $0.05 per output second.

Guards the pricing so a change to the rate or the duration source is a loud test
failure rather than a silent mischarge.
"""
import unittest

from EdennCode.Deployment.api_multi_image_generation import (
    MULTI_IMAGE_COST_PER_SECOND_USD,
    _multi_image_total_cost,
    _multi_image_video_seconds,
)
from EdennCode.Deployment.api_video_generation import (
    VideoGeometry,
    _video_generation_cost_response,
)


class _Result:
    """Minimal stand-in for MultiImageGenerationResult's cost inputs."""

    token_usage = {"prompt_tokens": 687, "completion_tokens": 118, "total_tokens": 805}


class MultiImageCostTests(unittest.TestCase):
    def test_rate_is_five_cents_per_second(self) -> None:
        self.assertEqual(MULTI_IMAGE_COST_PER_SECOND_USD, 0.05)

    def test_cost_is_video_seconds_times_rate(self) -> None:
        for seconds in (5.0, 13.5, 30.0, 150.0):
            geo = VideoGeometry(duration=seconds, width=720, height=1024, fps=30.0)
            self.assertEqual(
                _multi_image_total_cost(geo, audio_duration_s=None),
                round(seconds * 0.05, 6),
            )

    def test_falls_back_to_audio_duration_when_geometry_has_none(self) -> None:
        geo = VideoGeometry(width=720, height=1024, fps=30.0)  # no duration
        self.assertEqual(_multi_image_total_cost(geo, audio_duration_s=12.0), 0.6)

    def test_zero_when_no_duration_available(self) -> None:
        geo = VideoGeometry(width=720, height=1024, fps=30.0)
        self.assertEqual(_multi_image_total_cost(geo, audio_duration_s=None), 0.0)

    def test_override_sets_total_cost_but_keeps_breakdown(self) -> None:
        # The duration-based total is authoritative; the generation/token breakdown
        # stays populated for internal accounting.
        resp = _video_generation_cost_response(
            _Result(),
            raw_token_usage=_Result.token_usage,
            total_cost_override=13.5 * 0.05,
            creative_duration=13.5,
        )
        self.assertEqual(resp.total_cost, 0.675)
        self.assertEqual(resp.creative_duration, 13.5)
        self.assertIsNotNone(resp.token_num)
        self.assertGreater(resp.creation_times, 0)

    def test_creative_duration_is_the_video_length(self) -> None:
        geo = VideoGeometry(duration=42.0, width=720, height=1024, fps=30.0)
        self.assertEqual(_multi_image_video_seconds(geo, audio_duration_s=None), 42.0)
        # cost is that duration * rate, and the two agree
        self.assertEqual(_multi_image_total_cost(geo, None), round(42.0 * 0.05, 6))

    def test_video_path_unchanged_without_override(self) -> None:
        # No override -> total is generation cost + token cost (video pricing).
        resp = _video_generation_cost_response(
            _Result(), raw_token_usage=_Result.token_usage
        )
        self.assertAlmostEqual(
            resp.total_cost, resp.creation_cost + (resp.token_cost or 0.0), places=9
        )


if __name__ == "__main__":
    unittest.main()
