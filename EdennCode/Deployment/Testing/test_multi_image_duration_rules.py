"""Duration rules for multi-image jobs: the 3s per-image minimum (error 10008)
and the 15s billing floor (+1s per image round-robin from the first image).

Covers the pure helpers shared by every surface, and the v1 endpoints' wiring
of the bumped timing into the workflow call. The v2 submit surface is covered
in async_pipeline_v2/Testing/test_multi_image_field_behavior.py.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_common import ApiContext
from EdennCode.Deployment.api_multi_image_generation import (
    MULTI_IMAGE_MIN_TOTAL_DURATION_S,
    MULTI_IMAGE_PER_IMAGE_MIN_S,
    bump_durations_to_billing_floor,
    create_multi_image_generation_router,
    resolve_fixed_image_durations,
    resolve_uniform_image_timing,
    validate_multi_image_request,
)
from EdennCode.exceptions import EdennValidationError


class BillingFloorBumpTests(unittest.TestCase):
    def test_totals_at_or_above_floor_unchanged(self) -> None:
        for durations in ([5.0, 5.0, 5.0], [3.0, 3.0, 3.0, 3.0, 3.0], [60.0, 60.0, 30.0]):
            self.assertEqual(bump_durations_to_billing_floor(durations), durations)

    def test_round_robin_starts_from_first_image(self) -> None:
        # [3,4,3] = 10s -> [4,4,3] [4,5,3] [4,5,4] [5,5,4] [5,6,4] = 15s.
        self.assertEqual(
            bump_durations_to_billing_floor([3.0, 4.0, 3.0]), [5.0, 6.0, 4.0]
        )

    def test_uniform_start_lands_uniform_when_divisible(self) -> None:
        # 3 x 3s = 9s needs +6s: two full rounds -> 5s each.
        self.assertEqual(
            bump_durations_to_billing_floor([3.0, 3.0, 3.0]), [5.0, 5.0, 5.0]
        )

    def test_partial_round_stops_at_floor(self) -> None:
        # 4 x 3s = 12s needs +3s: first three images only.
        self.assertEqual(
            bump_durations_to_billing_floor([3.0] * 4), [4.0, 4.0, 4.0, 3.0]
        )

    def test_float_accumulation_does_not_over_bump(self) -> None:
        # 14s split into thirds sums to 13.999999999999998; the bump must stop
        # once the total is within epsilon of 15, not add a spurious extra 1s.
        third = 14.0 / 3.0
        bumped = bump_durations_to_billing_floor([third, third, third])
        self.assertAlmostEqual(sum(bumped), MULTI_IMAGE_MIN_TOTAL_DURATION_S, places=6)
        self.assertEqual(bumped, [third + 1.0, third, third])

    def test_empty_list_passthrough(self) -> None:
        self.assertEqual(bump_durations_to_billing_floor([]), [])


class UniformTimingResolutionTests(unittest.TestCase):
    def test_no_bump_needed_stays_uniform(self) -> None:
        self.assertEqual(resolve_uniform_image_timing(3, 5.0), (5.0, None))
        self.assertEqual(resolve_uniform_image_timing(5, 3.0), (3.0, None))

    def test_uniform_bump_returns_adjusted_uniform_value(self) -> None:
        self.assertEqual(resolve_uniform_image_timing(3, 3.0), (5.0, None))

    def test_uneven_bump_returns_explicit_list(self) -> None:
        self.assertEqual(
            resolve_uniform_image_timing(4, 3.0), (3.0, [4.0, 4.0, 4.0, 3.0])
        )


def _error_code(exc: HTTPException) -> int:
    detail = exc.detail
    assert isinstance(detail, dict), detail
    return detail["error_code"]


class UniformValidationTests(unittest.TestCase):
    def test_below_minimum_rejected_with_code_10008(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            validate_multi_image_request(3, MULTI_IMAGE_PER_IMAGE_MIN_S - 0.1)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_error_code(ctx.exception), 10008)

    def test_minimum_is_inclusive(self) -> None:
        validate_multi_image_request(5, MULTI_IMAGE_PER_IMAGE_MIN_S)

    def test_uniform_bounds_skipped_when_explicit_timing_wins(self) -> None:
        validate_multi_image_request(3, 0.5, enforce_uniform_timing=False)

    def test_non_finite_uniform_duration_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(HTTPException) as ctx:
                validate_multi_image_request(3, value)
            self.assertEqual(ctx.exception.status_code, 400)


class FixedDurationResolutionTests(unittest.TestCase):
    def test_explicit_value_below_minimum_rejected_with_code_10008(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            resolve_fixed_image_durations(
                image_count=3,
                per_image_durations=[4.0, 2.5, 6.0],
                total_duration_s=None,
                image_order="fixed",
            )
        self.assertEqual(_error_code(ctx.exception), 10008)

    def test_total_share_below_minimum_rejected_with_code_10008(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            resolve_fixed_image_durations(
                image_count=3,
                per_image_durations=None,
                total_duration_s=8.0,
                image_order="fixed",
            )
        self.assertEqual(_error_code(ctx.exception), 10008)

    def test_short_explicit_total_bumped_to_floor(self) -> None:
        resolved = resolve_fixed_image_durations(
            image_count=3,
            per_image_durations=[3.0, 4.0, 3.0],
            total_duration_s=None,
            image_order="fixed",
        )
        self.assertEqual(resolved, [5.0, 6.0, 4.0])

    def test_total_at_floor_untouched(self) -> None:
        resolved = resolve_fixed_image_durations(
            image_count=3,
            per_image_durations=[4.0, 5.0, 6.0],
            total_duration_s=None,
            image_order="fixed",
        )
        self.assertEqual(resolved, [4.0, 5.0, 6.0])

    def test_non_finite_explicit_values_rejected(self) -> None:
        # NaN passes one-sided range comparisons; the explicit finiteness check
        # must 400 it (and infinities) before it can reach a billed job.
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(HTTPException) as ctx:
                resolve_fixed_image_durations(
                    image_count=3,
                    per_image_durations=[4.0, bad, 4.0],
                    total_duration_s=None,
                    image_order="fixed",
                )
            self.assertEqual(ctx.exception.status_code, 400)

    def test_non_finite_total_duration_rejected(self) -> None:
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(HTTPException) as ctx:
                resolve_fixed_image_durations(
                    image_count=3,
                    per_image_durations=None,
                    total_duration_s=bad,
                    image_order="fixed",
                )
            self.assertEqual(ctx.exception.status_code, 400)


class V1EndpointTimingTests(unittest.TestCase):
    """The v1 endpoints must reject sub-3s timing with 10008 and hand the
    billing-floor bump to the workflow."""

    def _build_client(self, tmp_dir: Path, workflow_run) -> TestClient:
        context = ApiContext(
            settings=SimpleNamespace(
                workdir=tmp_dir / "jobs",
                music_volume=1.0,
                audio_container_name="audio",
                output_container="videos",
            ),
            storage=SimpleNamespace(enabled=False),
            workflow=MagicMock(),
            alignment_workflow=MagicMock(),
            audio_creative_edit_workflow=MagicMock(),
            logger=MagicMock(),
            multi_image_workflow=SimpleNamespace(run=workflow_run),
        )
        app = FastAPI()
        app.include_router(create_multi_image_generation_router(context))
        return TestClient(app)

    @staticmethod
    def _image_files(count: int):
        return [
            ("images", (f"img_{i}.png", b"fake-png-bytes", "image/png"))
            for i in range(count)
        ]

    def test_sub_3s_uniform_duration_is_coded_400_and_never_runs(self) -> None:
        # Sync and async submit share _prepare_multi_image_job, so both must
        # reject with the same coded 400 before any work starts.
        for endpoint in ("/api/v1/jobs/multi-image", "/api/v1/jobs/async_multi-image"):
            calls: list[dict] = []

            async def workflow_run(**kwargs):
                calls.append(kwargs)
                raise AssertionError("workflow must not run for a rejected request")

            with tempfile.TemporaryDirectory(prefix="multi-image-min-") as tmp:
                client = self._build_client(Path(tmp), workflow_run)
                response = client.post(
                    endpoint,
                    files=self._image_files(3),
                    data={"per_image_duration": "2"},
                )
            self.assertEqual(response.status_code, 400, f"{endpoint}: {response.text}")
            detail = response.json()["detail"]
            self.assertEqual(detail["error_code"], 10008)
            self.assertFalse(detail["retryable"])
            self.assertEqual(calls, [])

    def _capture_workflow_timing(self, image_count: int, per_image_duration: str) -> dict:
        calls: list[dict] = []

        async def workflow_run(**kwargs):
            calls.append(kwargs)
            # Abort after capturing the timing so the test does not need the
            # full response-assembly fixture; the endpoint maps this to a 400.
            raise EdennValidationError("stop after capture")

        with tempfile.TemporaryDirectory(prefix="multi-image-bump-") as tmp:
            client = self._build_client(Path(tmp), workflow_run)
            client.post(
                "/api/v1/jobs/multi-image",
                files=self._image_files(image_count),
                data={"per_image_duration": per_image_duration},
            )
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_uniform_bump_keeps_uniform_timing(self) -> None:
        # 3 x 3s = 9s bumps evenly to 5s each: the workflow still receives
        # uniform timing (beat alignment preserved), no explicit list.
        kwargs = self._capture_workflow_timing(3, "3")
        self.assertEqual(kwargs["per_image_duration"], 5.0)
        self.assertIsNone(kwargs["per_image_durations"])

    def test_uneven_bump_passes_explicit_durations(self) -> None:
        # 4 x 3s = 12s bumps to [4,4,4,3]: the workflow receives the explicit
        # per-image list (exact timing, beats off downstream).
        kwargs = self._capture_workflow_timing(4, "3")
        self.assertEqual(kwargs["per_image_durations"], [4.0, 4.0, 4.0, 3.0])
        self.assertEqual(kwargs["per_image_duration"], 3.0)

    def test_timing_at_floor_passes_through(self) -> None:
        kwargs = self._capture_workflow_timing(3, "5")
        self.assertEqual(kwargs["per_image_duration"], 5.0)
        self.assertIsNone(kwargs["per_image_durations"])

    def test_bump_counts_only_images_the_pipeline_renders(self) -> None:
        # The preprocess stage silently skips unsupported extensions. Four
        # uploads where one is a .tiff render as three slides, so the bump must
        # target 3 x 3s = 9s (uniform 5s), not 4 slides — an explicit 4-entry
        # list would fail the job mid-flight on the count check.
        calls: list[dict] = []

        async def workflow_run(**kwargs):
            calls.append(kwargs)
            raise EdennValidationError("stop after capture")

        files = self._image_files(3) + [
            ("images", ("scan.tiff", b"fake-tiff-bytes", "image/tiff"))
        ]
        with tempfile.TemporaryDirectory(prefix="multi-image-drop-") as tmp:
            client = self._build_client(Path(tmp), workflow_run)
            client.post(
                "/api/v1/jobs/multi-image",
                files=files,
                data={"per_image_duration": "3"},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["per_image_duration"], 5.0)
        self.assertIsNone(calls[0]["per_image_durations"])


if __name__ == "__main__":
    unittest.main()
