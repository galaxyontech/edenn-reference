"""Paths into a persisted job ``result_json``.

The job result is a nested 5-block document (see ``VideoJobResponse``), but rows
written before that shape existed are flat, and they are replayed verbatim — v2
serves ``result_json`` as an unvalidated dict. Anything that reads or rewrites a
media URL inside a result therefore has to cope with both layouts.

Centralising the paths here keeps the SAS-refresh tables in ``api.py`` and the
workers' event payloads from drifting apart: if they disagree about where
``audio_url`` lives, URLs silently stop being re-signed and clients get expired
links with a clean 200.
"""

from __future__ import annotations

from typing import Any, Optional

# Nested paths into the current result shape.
AUDIO_URL_PATH = ("audio_metadata", "audio_url")
COMPLETE_AUDIO_URL_PATH = ("audio_metadata", "complete_audio_url")
VIDEO_URL_PATH = ("video_metadata", "video_url")
THUMBNAIL_URL_PATH = ("video_metadata", "thumbnail_url")


def get_path(result: Any, path: tuple[str, ...]) -> Any:
    """Read ``path`` out of ``result``; None when any segment is missing."""
    current: Any = result
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def set_path_if_present(result: Any, path: tuple[str, ...], value: Any) -> bool:
    """Write ``value`` at ``path``, but only if the leaf key is already there.

    Requiring the leaf to exist is what keeps the two shapes from bleeding into
    each other. A freshly written result carries every key (pydantic dumps None
    fields), and a legacy row carries the flat keys — so a nested write no-ops on
    a legacy row, a flat write no-ops on a current row, and neither ever grows a
    half-migrated hybrid of the two.
    """
    current: Any = result
    for key in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(key), dict):
            return False
        current = current[key]
    leaf = path[-1]
    if not isinstance(current, dict) or leaf not in current:
        return False
    current[leaf] = value
    return True


def result_audio_url(result: Any) -> Optional[str]:
    """The generated track's URL, from either result shape."""
    if not isinstance(result, dict):
        return None
    audio = result.get("audio_metadata")
    if isinstance(audio, dict):
        value = audio.get("audio_url") or audio.get("complete_audio_url")
        if value:
            return value
    return result.get("audio_url") or result.get("complete_audio_url")


def result_video_url(result: Any) -> Optional[str]:
    """The rendered video's URL, from either result shape."""
    if not isinstance(result, dict):
        return None
    video = result.get("video_metadata")
    if isinstance(video, dict) and video.get("video_url"):
        return video["video_url"]
    # A legacy flat result also has a `video_metadata`, but it holds the probed
    # geometry rather than URLs — so fall through to the flat key.
    return result.get("video_url")


def _canonical_key_order() -> dict[str, tuple[str, ...]]:
    """Declared field order of the response, top level and per block.

    Built from the multi-image models: their blocks subclass the video blocks, so
    the shared fields come first in the same order and the multi-image-only fields
    follow. A video result simply has none of the latter.
    """
    from EdennCode.Deployment.api_multi_image_generation import MultiImageJobResponse

    order: dict[str, tuple[str, ...]] = {
        "": tuple(MultiImageJobResponse.model_fields),
    }
    for name, field in MultiImageJobResponse.model_fields.items():
        annotation = field.annotation
        if hasattr(annotation, "model_fields"):
            order[name] = tuple(annotation.model_fields)
    return order


def order_result_keys(result: Any) -> Any:
    """Re-emit a result with its keys in the model's declared order.

    The result is persisted to a JSONB column, which physically reorders object
    keys by (length, name) — so a job read back from the database serializes in a
    different order than the same job returned inline by the v1 routes, even though
    the keys and values are identical. Restore the declared order on the way out so
    the two transports are byte-comparable and the payload reads top-down.

    Keys the order doesn't know about are kept, appended after the known ones: a
    result written before the response was restructured is flat, and must pass
    through untouched rather than being silently pruned.
    """
    if not isinstance(result, dict):
        return result

    order = _canonical_key_order()

    def apply(value: dict[str, Any], block: str) -> dict[str, Any]:
        known = order.get(block, ())
        ordered = {key: value[key] for key in known if key in value}
        ordered.update({k: v for k, v in value.items() if k not in ordered})
        return {
            key: apply(item, key) if isinstance(item, dict) and key in order else item
            for key, item in ordered.items()
        }

    return apply(result, "")


def strip_result_envelope_duplicates(result_block: dict) -> dict:
    """Drop envelope fields duplicated inside the result block.

    The envelope already carries job_id/status/version; repeating them inside
    ``result`` is noise for clients. They stay in the STORED result_json on
    purpose — ``result.version`` records which build computed the result
    (distinct from the envelope version, which is the serving API's build), a
    forensic signal for stale-worker incidents — and are stripped at every
    client egress (status reads and callbacks).
    """
    for key in ("job_id", "status", "version"):
        result_block.pop(key, None)
    return result_block


def strip_cost_metadata(result_block: dict) -> dict:
    """Drop the entire cost_metadata block from a client-facing result, in place.

    Clients see no cost accounting at all; the full breakdown (total_cost/
    creation_cost/creation_times/token_num/token_cost/model_spec_name/
    creative_duration) stays in the STORED result_json for per-key billing and
    is stripped at every client egress — status reads AND callbacks — so the two
    surfaces stay consistent across both job types (video-music and multi-image).

    Legacy rows persisted before the blocks were restructured carried the same
    accounting under a flat ``cost`` key; those replay verbatim otherwise, so
    strip that spelling too.
    """
    result_block.pop("cost_metadata", None)
    result_block.pop("cost", None)
    return result_block


__all__ = [
    "AUDIO_URL_PATH",
    "COMPLETE_AUDIO_URL_PATH",
    "THUMBNAIL_URL_PATH",
    "VIDEO_URL_PATH",
    "get_path",
    "order_result_keys",
    "result_audio_url",
    "result_video_url",
    "set_path_if_present",
    "strip_cost_metadata",
    "strip_result_envelope_duplicates",
]
