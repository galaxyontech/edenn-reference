"""Back-compat shim. The gold scenario catalog moved into ``Testing/e2e/scenarios``.
This re-exports it so existing imports keep working; new code should import from
``Testing.e2e.scenarios``.
"""

from __future__ import annotations

from EdennCode.EdennAgent.AgenticAudio.Testing.e2e.scenarios import SCENARIOS

__all__ = ["SCENARIOS"]
