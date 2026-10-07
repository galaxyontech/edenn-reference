"""Unit tests for CacheService.get's cross-functionality ``kind`` guard.

The shared ``async_v2_cache`` table is used by multiple functionalities
(multi_image plans, video_music understanding/compression). ``get(expected_kind=...)``
must add an ``AND kind = %s`` filter so one functionality can never read back
another's row even if their ``cache_key`` strings ever coincide. These tests inject
a fake Postgres client (no DB) and assert the emitted SQL + params.
"""
import unittest
from contextlib import contextmanager

from EdennCode.Deployment.async_pipeline_v2.cache_service import CacheService


class _RecordingClient:
    def __init__(self, sink):
        self._sink = sink

    def run_sql(self, query, params=None):
        self._sink.append((query, list(params or [])))
        return []  # no rows -> get() returns None; we only inspect the query


class CacheServiceKindIsolationTests(unittest.TestCase):
    def _service_and_sink(self):
        sink = []

        @contextmanager
        def factory():
            yield _RecordingClient(sink)

        return CacheService(client_factory=factory, ensure_schema=False), sink

    def test_get_without_kind_omits_kind_filter(self):
        service, sink = self._service_and_sink()
        service.get("some-key")
        query, params = sink[-1]
        self.assertNotIn("kind = %s", query)
        self.assertEqual(params, ["some-key"])

    def test_get_with_expected_kind_filters_by_kind(self):
        service, sink = self._service_and_sink()
        service.get("multi_image_plan:abc123", expected_kind="multi_image_plan")
        query, params = sink[-1]
        self.assertIn("AND kind = %s", query)
        # cache_key binds first, expected_kind second — matching the %s order.
        self.assertEqual(params, ["multi_image_plan:abc123", "multi_image_plan"])

    def test_get_or_lease_read_probe_enforces_kind(self):
        # get_or_lease does a ready-probe via self.get(); it must pass the caller's
        # kind so a pending/ready row of a different functionality is never returned.
        service, sink = self._service_and_sink()
        service.get_or_lease("understanding:u1:xyz", kind="understanding")
        # The first recorded statement is the ready-probe get().
        probe_query, probe_params = sink[0]
        self.assertIn("AND kind = %s", probe_query)
        self.assertEqual(probe_params, ["understanding:u1:xyz", "understanding"])


if __name__ == "__main__":
    unittest.main()
