"""Addressing value objects (UNDERSTANDING_LAYER §1).

Refs are content + coordinates, never runtime objects:

    asset_ab12                  whole asset
    asset_ab12#t=193.2-203.5    a time span of it

`Ref` is the reference value object. `AssetIdentity` is the content-addressing
policy: an asset id is derived from file bytes, a tree-node id from its span —
both DERIVED, not minted at runtime, which is what makes plans re-renderable
across processes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional


@dataclass(frozen=True)
class Ref:
    """A reference to an asset or a time span within it."""

    asset_id: str
    start_s: Optional[float] = None
    end_s: Optional[float] = None

    _PATTERN: ClassVar[re.Pattern] = re.compile(
        r"^(?P<asset>[A-Za-z0-9_\-]+)(#t=(?P<a>\d+(?:\.\d+)?)-(?P<b>\d+(?:\.\d+)?))?$"
    )

    @property
    def is_span(self) -> bool:
        return self.start_s is not None

    @property
    def base(self) -> str:
        """The asset id alone (the part before ``#``)."""

        return self.asset_id

    def __str__(self) -> str:
        if not self.is_span:
            return self.asset_id
        return f"{self.asset_id}#t={self.start_s:g}-{self.end_s:g}"

    @classmethod
    def parse(cls, text: str) -> "Ref":
        m = cls._PATTERN.match(text.strip())
        if not m:
            raise ValueError(f"not a ref: {text!r}")
        if m.group("a") is None:
            return cls(asset_id=m.group("asset"))
        a, b = float(m.group("a")), float(m.group("b"))
        if b <= a:
            raise ValueError(f"empty span in ref: {text!r}")
        return cls(asset_id=m.group("asset"), start_s=a, end_s=b)

    @classmethod
    def span(cls, asset_id: str, start_s: float, end_s: float) -> "Ref":
        """A span ref with boundaries rounded to millisecond precision."""

        return cls(asset_id, round(float(start_s), 3), round(float(end_s), 3))

    @staticmethod
    def asset_of(ref_text: str) -> str:
        """The asset id of any ref string, span or not."""

        return ref_text.split("#", 1)[0]


class AssetIdentity:
    """Content-addressing policy: file bytes -> asset id, span -> node id."""

    _CHUNK = 1 << 20

    @staticmethod
    def from_file(path: str | Path) -> tuple[str, str]:
        """``(asset_id, sha256)`` for a file's content."""

        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(AssetIdentity._CHUNK), b""):
                h.update(chunk)
        digest = h.hexdigest()
        return f"asset_{digest[:12]}", digest

    @staticmethod
    def node_id(asset_id: str, start_s: float, end_s: float) -> str:
        """Deterministic tree-node identity derived from its span."""

        return f"seg_{asset_id}_{int(round(start_s * 1000))}_{int(round(end_s * 1000))}"
