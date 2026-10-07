#!/usr/bin/env python3
"""Fail if an upstream provider or model name appears outside the SDK carve-out.

The house rule: no upstream AI provider, product, model family, or model
identifier may appear in an identifier, filename, configuration key, log field,
error message, API response, document, test name, fixture, or commit message.

The one carve-out is that a vendor SDK cannot be installed under another name.
Package lines in requirements files, their import statements, and the classes
those imports bind are permitted -- and ONLY inside the adapter paths listed in
ADAPTER_PATHS. Anywhere else, they are a leak.

The vocabulary this checks is deliberately NOT written here: writing it down
would itself break the rule this file exists to enforce. It is loaded from
tools/provider-denylist.txt, which is generated at export time and is kept
private. With that file absent this script checks NOTHING and says so with a
non-zero exit: a gate that reports a pass it did not earn is worse than no gate.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = Path(__file__).resolve().parent / "provider-denylist.txt"

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             ".pytest_cache", ".next", "dist"}

# The only files allowed to name a vendor package. Inside these the SDK may be
# imported and used; the boundary is that the name may not travel outside them
# — which is why adapters bind it under a neutral alias for their callers.
ADAPTER_PATHS = (
    "EdennCode/ModelFactory/",
    "EdennCode/MusicGenerationCore/providers/",
    "EdennCode/EdennAgent/Creation/",
    "requirements.txt",
)

def load_denylist() -> list[tuple[str, "re.Pattern[str]"]]:
    """Compile each token with the same boundaries the runtime scrubber uses.

    A bare substring search is useless here: the shortest vendor tokens are
    fragments of ordinary words, and one of them sits inside "audio", which
    appears on nearly every line of this codebase. A token therefore may not be
    glued to a preceding alphanumeric, and may not be followed by a LOWERCASE
    letter -- so it still catches a version digit (``name4``) and a camel-cased
    tail (``NameAI``), while never matching inside a longer ordinary word.
    """
    if not DENYLIST.exists():
        return []
    out = []
    for line in DENYLIST.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append((line, re.compile(
                r"(?<![A-Za-z0-9])(?i:" + re.escape(line) + r")(?![a-z])")))
    return out


def walk():
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            yield p


def main() -> int:
    tokens = load_denylist()
    findings = []

    if not tokens:
        # A gate that cannot check must not print a pass. This file is
        # advertised in the README and runs as a CI step named for the rule it
        # enforces; returning 0 here told every reader who ran it as documented
        # that the repository had been checked, when nothing had been read at
        # all. Fail loudly and say what is missing.
        print("check_provider_names: NOT RUN — no vocabulary configured.\n"
              "  tools/provider-denylist.txt is absent, and the token list is\n"
              "  deliberately not committed (writing it here would itself break\n"
              "  the rule this file enforces). Write it locally, one token per\n"
              "  line, then run this again.")
        return 2

    for path in walk():
        rel = str(path.relative_to(ROOT))
        if rel.startswith("tools/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        # a vendor token in the PATH is a leak wherever it appears
        for tok, rule in tokens:
            if rule.search(rel):
                findings.append((rel, 0, f"vendor token in filename: {tok}"))

        in_adapter = rel.startswith(ADAPTER_PATHS)
        for n, line in enumerate(text.splitlines(), 1):
            for tok, rule in tokens:
                if not rule.search(line):
                    continue
                if in_adapter:
                    # The carve-out: inside an adapter the vendor SDK may be
                    # imported AND used. The boundary is that nothing outside
                    # these paths may name it at all.
                    continue
                findings.append((rel, n, f"vendor token outside the carve-out: {tok}"))
                break

    if not findings:
        print("check_provider_names: clean")
        return 0
    print(f"check_provider_names: {len(findings)} finding(s)\n")
    for rel, n, why in sorted(set(findings))[:100]:
        where = f"{rel}:{n}" if n else rel
        print(f"  {where:70} {why}")
    if len(set(findings)) > 100:
        print(f"  ... and {len(set(findings)) - 100} more")
    print("\nUse a neutral alias, or move the SDK import into an adapter module.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
