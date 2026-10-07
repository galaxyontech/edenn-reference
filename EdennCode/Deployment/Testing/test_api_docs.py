import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from EdennCode.Deployment.api_docs import ApiDocDef, ExampleDocDef, create_docs_router


class ApiDocsRouterTests(unittest.TestCase):
    def _build_app(self, *, registry=None) -> FastAPI:
        app = FastAPI()
        app.include_router(create_docs_router(registry=registry))
        return app

    def test_docs_index_lists_known_documents(self) -> None:
        app = self._build_app()
        with TestClient(app) as client:
            response = client.get("/api/v1/docs")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        slugs = {item["slug"] for item in payload["documents"]}
        self.assertIn("overview", slugs)
        self.assertIn("video-music-generation", slugs)
        self.assertIn("audio-creative-edit", slugs)
        self.assertIn("vocal-clone", slugs)

    def test_video_music_generation_doc_detail_returns_markdown(self) -> None:
        app = self._build_app()
        with TestClient(app) as client:
            response = client.get("/api/v1/docs/video-music-generation")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["slug"], "video-music-generation")
        self.assertIn("Video Music Generation API", payload["markdown"])

    def test_doc_detail_only_lists_examples_available_on_disk(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-docs-") as tmp:
            tmp_dir = Path(tmp)
            markdown_path = tmp_dir / "doc.md"
            markdown_path.write_text("# Example Doc\n", encoding="utf-8")
            available_json = tmp_dir / "example.json"
            available_json.write_text('{"status":"ok","modelspec":"edenn_enhanced"}', encoding="utf-8")
            missing_audio = tmp_dir / "missing.mp3"

            registry = {
                "custom": ApiDocDef(
                    slug="custom",
                    title="Custom Doc",
                    summary="Test registry",
                    markdown_path=markdown_path,
                    examples=(
                        ExampleDocDef(
                            id="available-json",
                            label="Available JSON",
                            path=available_json,
                        ),
                        ExampleDocDef(
                            id="missing-audio",
                            label="Missing audio",
                            path=missing_audio,
                        ),
                    ),
                )
            }
            app = self._build_app(registry=registry)

            with TestClient(app) as client:
                response = client.get("/api/v1/docs/custom")

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["slug"], "custom")
            self.assertIn("Example Doc", payload["markdown"])
            example_ids = {item["id"] for item in payload["examples"]}
            self.assertEqual(example_ids, {"available-json"})

    def test_docs_example_json_route_returns_saved_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-docs-") as tmp:
            tmp_dir = Path(tmp)
            markdown_path = tmp_dir / "doc.md"
            markdown_path.write_text("# Example Doc\n", encoding="utf-8")
            example_json = tmp_dir / "example.json"
            example_json.write_text(
                '{"status":"completed","modelspec":"edenn_enhanced"}',
                encoding="utf-8",
            )

            registry = {
                "custom": ApiDocDef(
                    slug="custom",
                    title="Custom Doc",
                    summary="Test registry",
                    markdown_path=markdown_path,
                    examples=(
                        ExampleDocDef(
                            id="json-example",
                            label="JSON Example",
                            path=example_json,
                        ),
                    ),
                )
            }
            app = self._build_app(registry=registry)

            with TestClient(app) as client:
                response = client.get("/api/v1/docs/custom/examples/json-example")

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["modelspec"], "edenn_enhanced")
            self.assertEqual(payload["status"], "completed")

    def test_docs_example_media_route_returns_audio_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="api-docs-") as tmp:
            tmp_dir = Path(tmp)
            markdown_path = tmp_dir / "doc.md"
            markdown_path.write_text("# Example Doc\n", encoding="utf-8")
            example_audio = tmp_dir / "example.mp3"
            example_audio.write_bytes(b"ID3-test-audio")

            registry = {
                "custom": ApiDocDef(
                    slug="custom",
                    title="Custom Doc",
                    summary="Test registry",
                    markdown_path=markdown_path,
                    examples=(
                        ExampleDocDef(
                            id="audio-example",
                            label="Audio Example",
                            path=example_audio,
                        ),
                    ),
                )
            }
            app = self._build_app(registry=registry)

            with TestClient(app) as client:
                response = client.get("/api/v1/docs/custom/examples/audio-example")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "audio/mpeg")
            self.assertEqual(response.content, b"ID3-test-audio")


if __name__ == "__main__":
    unittest.main()
