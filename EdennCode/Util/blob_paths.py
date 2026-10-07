from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import quote


_NON_SAFE_BLOB_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_blob_path_component(
    value: str,
    *,
    fallback_prefix: str = "asset",
    max_length: int = 64,
) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback_prefix

    normalized = unicodedata.normalize("NFKD", raw)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = _NON_SAFE_BLOB_CHARS_RE.sub("-", ascii_value).strip("._-").lower()
    if cleaned:
        return cleaned[:max_length].rstrip("._-") or fallback_prefix

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{fallback_prefix}-{digest}"


def encode_blob_name_for_url(blob_name: str) -> str:
    return quote(blob_name, safe="/")
