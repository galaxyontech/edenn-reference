"""Every top-level API response model carries a `version` field formatted
``dev-<IMAGE_TAG>``. This is asserted at the model level so we catch a missing
field even if no live request flows through the endpoint in CI.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class ApiResponseVersionFieldTests(unittest.TestCase):
    """Pin the version field on every public response model."""

    def _model_with_required_fields(self, model_cls):
        """Construct a minimal valid instance using only fields that must be set.

        Pydantic supplies defaults for everything else; we just need to satisfy
        ``...``-marked required fields.
        """

        from pydantic.fields import PydanticUndefined  # type: ignore

        kwargs: dict = {}
        for name, field in model_cls.model_fields.items():
            if field.is_required():
                annotation = field.annotation
                origin = getattr(annotation, "__origin__", None) or annotation
                if annotation is str:
                    kwargs[name] = "x"
                elif annotation is int:
                    kwargs[name] = 0
                elif annotation is bool:
                    kwargs[name] = False
                elif annotation is float:
                    kwargs[name] = 0.0
                elif origin in (dict, list):
                    kwargs[name] = origin()
                else:
                    # Best-effort: try to instantiate nested BaseModel with no args.
                    try:
                        kwargs[name] = annotation()
                    except Exception:
                        kwargs[name] = None
        return model_cls(**kwargs)

    def test_health_response_carries_version(self) -> None:
        from EdennCode.Deployment.api_common import HealthResponse
        with patch.dict(os.environ, {"IMAGE_TAG": "abc1234"}):
            r = HealthResponse(status="ok")
            self.assertEqual(r.version, "dev-abc1234")

    def test_health_response_falls_back_to_local(self) -> None:
        from EdennCode.Deployment.api_common import HealthResponse
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAGE_TAG", None)
            r = HealthResponse(status="ok")
            self.assertEqual(r.version, "dev-local")

    def test_every_top_level_response_has_version_field(self) -> None:
        """If you add a new public response model, add it here so we pin its version field."""

        from EdennCode.Deployment.api_audio_creative_edit import AudioCreativeEditResponse
        from EdennCode.Deployment.api_common import HealthResponse
        from EdennCode.Deployment.api_docs import ApiDocDetailResponse, ApiDocIndexResponse
        from EdennCode.Deployment.api_multi_image_generation import MultiImageJobResponse
        from EdennCode.Deployment.api_recommendations import RecommendationResponse
        from EdennCode.Deployment.api_video_alignment import VideoAudioAlignmentJobResponse
        from EdennCode.Deployment.api_video_generation import (
            AsyncVideoJobAcceptedResponse,
            AsyncVideoJobStatusResponse,
            VideoJobResponse,
        )
        from EdennCode.Deployment.api_vocal_clone import VocalCloneResponse

        models = [
            HealthResponse,
            VocalCloneResponse,
            AudioCreativeEditResponse,
            MultiImageJobResponse,
            VideoAudioAlignmentJobResponse,
            VideoJobResponse,
            AsyncVideoJobAcceptedResponse,
            AsyncVideoJobStatusResponse,
            ApiDocIndexResponse,
            ApiDocDetailResponse,
            RecommendationResponse,
        ]
        for cls in models:
            self.assertIn("version", cls.model_fields, f"{cls.__name__} missing 'version' field")

    def test_service_version_helper(self) -> None:
        from EdennCode.Deployment.api_common import service_version

        with patch.dict(os.environ, {"IMAGE_TAG": "deadbeef"}):
            self.assertEqual(service_version(), "dev-deadbeef")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAGE_TAG", None)
            self.assertEqual(service_version(), "dev-local")


if __name__ == "__main__":
    unittest.main()
