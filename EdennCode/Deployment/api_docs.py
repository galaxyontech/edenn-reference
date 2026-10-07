from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from EdennCode.Deployment.api_common import service_version


DEPLOYMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEPLOYMENT_DIR.parents[1]


@dataclass(frozen=True)
class ExampleDocDef:
    id: str
    label: str
    path: Path
    description: str = ""


@dataclass(frozen=True)
class ApiDocDef:
    slug: str
    title: str
    summary: str
    markdown_path: Path
    examples: tuple[ExampleDocDef, ...] = ()


class ApiDocIndexEntry(BaseModel):
    slug: str
    title: str
    summary: str
    endpoint: str


class ApiDocExampleEntry(BaseModel):
    id: str
    label: str
    description: str = ""
    file_path: str
    media_type: str
    endpoint: str


class ApiDocDetailResponse(BaseModel):
    slug: str
    title: str
    summary: str
    markdown_path: str
    markdown: str
    examples: List[ApiDocExampleEntry] = Field(default_factory=list)
    version: str = Field(default_factory=service_version)


class ApiDocIndexResponse(BaseModel):
    documents: List[ApiDocIndexEntry] = Field(default_factory=list)
    version: str = Field(default_factory=service_version)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except Exception:
        return str(path.resolve())


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


DOC_REGISTRY: Dict[str, ApiDocDef] = {
    "overview": ApiDocDef(
        slug="overview",
        title="Service API Overview",
        summary="Top-level API contract for the Edenn Media Service.",
        markdown_path=DEPLOYMENT_DIR / "API_DOCUMENTATION.md",
        examples=(
            ExampleDocDef(
                id="reference-edenn-basic-response",
                label="Reference edenn_basic response",
                path=DEPLOYMENT_DIR / "example_runs" / "reference_edenn_basic.json",
                description="Saved reference response from the video generation API using edenn_basic.",
            ),
        ),
    ),
    "video-music-generation": ApiDocDef(
        slug="video-music-generation",
        title="Video Music Generation API",
        summary="Video-to-music generation endpoints, including the async job API.",
        markdown_path=REPO_ROOT / "docs" / "api-video-music-generation.md",
    ),
    "audio-creative-edit": ApiDocDef(
        slug="audio-creative-edit",
        title="Audio Creative Edit API",
        summary="Detailed guide for the audio creative edit route with provider behavior and saved examples.",
        markdown_path=DEPLOYMENT_DIR / "AUDIO_CREATIVE_EDIT_API_README.md",
        examples=(
            ExampleDocDef(
                id="enhanced-instrumental-response",
                label="Enhanced instrumental response",
                path=DEPLOYMENT_DIR / "example_runs" / "audio_creative_edit_enhanced_instrumental.json",
                description="Saved audio creative edit response using edenn_enhanced.",
            ),
            ExampleDocDef(
                id="studio-vocal-response",
                label="Studio vocal response",
                path=DEPLOYMENT_DIR / "example_runs" / "audio_creative_edit_studio_vocal.json",
                description="Saved audio creative edit response using edenn_studio.",
            ),
            ExampleDocDef(
                id="whistle-reference-source",
                label="Whistle reference source audio",
                path=DEPLOYMENT_DIR / "example_runs" / "whistle_reference_source.mp3",
                description="Short whistle-like source clip used for the provider comparison.",
            ),
            ExampleDocDef(
                id="whistle-enhanced-response",
                label="Whistle enhanced response",
                path=DEPLOYMENT_DIR / "example_runs" / "whistle_audio_creative_edit_enhanced.json",
                description="Saved edenn_enhanced response for the whistle-style melody comparison.",
            ),
            ExampleDocDef(
                id="whistle-studio-response",
                label="Whistle studio response",
                path=DEPLOYMENT_DIR / "example_runs" / "whistle_audio_creative_edit_studio_custom.json",
                description="Saved edenn_studio response for the whistle-style melody comparison.",
            ),
            ExampleDocDef(
                id="whistle-enhanced-primary-audio",
                label="Whistle enhanced primary audio",
                path=DEPLOYMENT_DIR / "example_runs" / "whistle_enhanced_primary.mp3",
                description="Primary edenn_enhanced audio output for the whistle-style melody comparison.",
            ),
            ExampleDocDef(
                id="whistle-studio-primary-audio",
                label="Whistle studio primary audio",
                path=DEPLOYMENT_DIR / "example_runs" / "whistle_studio_primary.mp3",
                description="Primary edenn_studio audio output for the whistle-style melody comparison.",
            ),
            ExampleDocDef(
                id="whistle-enhanced-preview-30s",
                label="Whistle enhanced 30s preview",
                path=DEPLOYMENT_DIR / "example_runs" / "previews" / "whistle_enhanced_primary_preview_30s.mp3",
                description="30-second preview of the primary edenn_enhanced whistle comparison output.",
            ),
            ExampleDocDef(
                id="whistle-studio-preview-30s",
                label="Whistle studio 30s preview",
                path=DEPLOYMENT_DIR / "example_runs" / "previews" / "whistle_studio_primary_preview_30s.mp3",
                description="30-second preview of the primary edenn_studio whistle comparison output.",
            ),
        ),
    ),
    "vocal-clone": ApiDocDef(
        slug="vocal-clone",
        title="Vocal Clone API",
        summary="Guide for creating a reusable vocal clone ID.",
        markdown_path=DEPLOYMENT_DIR / "VOCAL_CLONE_API_README.md",
    ),
}


def _doc_or_404(slug: str, registry: Mapping[str, ApiDocDef]) -> ApiDocDef:
    doc = registry.get(slug)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Unknown documentation slug '{slug}'.")
    return doc


def _example_or_404(doc: ApiDocDef, example_id: str) -> ExampleDocDef:
    for example in doc.examples:
        if example.id == example_id:
            return example
    raise HTTPException(
        status_code=404,
        detail=f"Unknown example '{example_id}' for documentation slug '{doc.slug}'.",
    )


def _example_entry(doc: ApiDocDef, example: ExampleDocDef) -> ApiDocExampleEntry:
    return ApiDocExampleEntry(
        id=example.id,
        label=example.label,
        description=example.description,
        file_path=_repo_relative(example.path),
        media_type=_media_type_for(example.path),
        endpoint=f"/api/v1/docs/{doc.slug}/examples/{example.id}",
    )


def _available_examples(doc: ApiDocDef) -> List[ExampleDocDef]:
    return [example for example in doc.examples if example.path.exists()]


def create_docs_router(
    *,
    registry: Mapping[str, ApiDocDef] | None = None,
) -> APIRouter:
    router = APIRouter()
    active_registry: Mapping[str, ApiDocDef] = registry or DOC_REGISTRY

    @router.get(
        "/api/v1/docs",
        response_model=ApiDocIndexResponse,
        summary="List API documentation pages exposed by the service.",
    )
    async def list_api_docs() -> ApiDocIndexResponse:
        return ApiDocIndexResponse(
            documents=[
                ApiDocIndexEntry(
                    slug=doc.slug,
                    title=doc.title,
                    summary=doc.summary,
                    endpoint=f"/api/v1/docs/{doc.slug}",
                )
                for doc in active_registry.values()
            ]
        )

    @router.get(
        "/api/v1/docs/{slug}",
        response_model=ApiDocDetailResponse,
        summary="Return markdown-backed API documentation and example references.",
    )
    async def get_api_doc(slug: str) -> ApiDocDetailResponse:
        doc = _doc_or_404(slug, active_registry)
        return ApiDocDetailResponse(
            slug=doc.slug,
            title=doc.title,
            summary=doc.summary,
            markdown_path=_repo_relative(doc.markdown_path),
            markdown=doc.markdown_path.read_text(encoding="utf-8"),
            examples=[_example_entry(doc, example) for example in _available_examples(doc)],
        )

    @router.get(
        "/api/v1/docs/{slug}/examples/{example_id}",
        summary="Return a saved example artifact referenced by the docs page.",
    )
    async def get_api_doc_example(slug: str, example_id: str):
        doc = _doc_or_404(slug, active_registry)
        example = _example_or_404(doc, example_id)
        if not example.path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Example file missing on disk: {_repo_relative(example.path)}",
            )
        media_type = _media_type_for(example.path)
        if example.path.suffix.lower() == ".json":
            return JSONResponse(
                content=json.loads(example.path.read_text(encoding="utf-8")),
            )
        if media_type.startswith("text/"):
            return PlainTextResponse(
                content=example.path.read_text(encoding="utf-8"),
                media_type=media_type,
            )
        return FileResponse(
            path=example.path,
            media_type=media_type,
            filename=example.path.name,
        )

    return router


__all__ = [
    "ApiDocDef",
    "create_docs_router",
    "ApiDocDetailResponse",
    "ExampleDocDef",
    "ApiDocExampleEntry",
    "ApiDocIndexEntry",
    "ApiDocIndexResponse",
]
