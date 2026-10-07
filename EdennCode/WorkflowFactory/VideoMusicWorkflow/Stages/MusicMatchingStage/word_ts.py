from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class WordTS:
    text: str
    startS: float
    endS: float
    i: Optional[int] = None
