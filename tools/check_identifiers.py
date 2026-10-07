#!/usr/bin/env python3
"""Fail if a real infrastructure identifier is in the tree.

Reserved namespaces are fine and expected: example.invalid, example.com,
localhost, 127.0.0.1, and the all-zero GUID. What this catches is a hostname,
account name, or subscription identifier that could point a reader at a live
system -- the thing that stays dangerous after every credential is rotated.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             ".pytest_cache", ".next", "dist"}

RESERVED = re.compile(
    r"(?i)(example\.invalid|example\.com|example\.org|\.test\b|\.local\b|"
    r"localhost|127\.0\.0\.1|0\.0\.0\.0|<[^>]+>|\$\{)")

# Names that are self-evidently stand-ins. Tests need a hostname-shaped string
# to assert on, and these carry no information about a real system.
PLACEHOLDER_HOST = re.compile(
    r"(?i)^(?:acct|account|example|examples|storage|media|voice|primary|secondary|"
    r"devaccount|testaccount|actualaccount|staleaccount|oldaccount|newaccount|"
    r"current|old|new|dev|test|stage|staging|fake|dummy|sample|placeholder|"
    r"myaccount|youraccount|mystorage|somestorage|bucket|cdn|host|server|db|"
    r"telemetry-db|telemetry-db-dev|billing-db|studio-db|registry|api|worker|studio)"
    r"(?:[.-]|$)")

RULES = [
    ("cloud application hostname", re.compile(r"(?i)\b[\w-]+\.[\w-]+\.azurecontainerapps\.io\b")),
    ("container registry hostname", re.compile(r"(?i)\b[\w-]+\.azurecr\.io\b")),
    ("object storage hostname", re.compile(r"(?i)\b[\w-]+\.blob\.core\.windows\.net\b")),
    ("managed database hostname", re.compile(r"(?i)\b[\w-]+\.postgres\.database\.azure\.com\b")),
    # Written generically on purpose: naming the vendor service here would be
    # the very thing this scanner exists to prevent.
    ("model service hostname", re.compile(r"(?i)\b[\w-]+\.[\w-]+\.azure\.com\b")),
    ("secret store hostname", re.compile(r"(?i)\b[\w-]+\.vault\.azure\.net\b")),
    ("third-party object storage", re.compile(r"(?i)\b[\w.-]+\.myqcloud\.com\b")),
    ("backend-as-a-service host", re.compile(r"(?i)\b[\w-]+\.supabase\.(?:co|in)\b")),
    ("developer home directory", re.compile(r"/(?:Users|home)/(?!example\b)[a-z][\w.-]{1,31}\b")),
    ("personal email address", re.compile(
        r"(?i)\b[\w.%+-]+@(?!example\.(?:invalid|com|org)\b)[\w.-]*\.(?:edu|gmail\.com|outlook\.com|qq\.com|163\.com)\b")),
]

GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
# A GUID is only reported next to a word that makes it an infrastructure id;
# fixtures use GUIDs as job and run identifiers all the time.
GUID_CONTEXT = re.compile(r"(?i)(subscription|tenant|client[_ -]?id|object[_ -]?id|principal|directory)")


def walk():
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            yield p


def main() -> int:
    findings = []
    for path in walk():
        rel = str(path.relative_to(ROOT))
        if rel.startswith("tools/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label, rule in RULES:
                m = rule.search(line)
                if m and not RESERVED.search(m.group(0)) \
                        and not PLACEHOLDER_HOST.match(m.group(0)):
                    findings.append((rel, n, label, m.group(0)[:60]))
            if GUID_CONTEXT.search(line):
                for m in GUID.finditer(line):
                    if m.group(0).lower() != ZERO_GUID:
                        findings.append((rel, n, "cloud subscription or tenant id", m.group(0)))

    if not findings:
        print("check_identifiers: clean")
        return 0
    print(f"check_identifiers: {len(findings)} finding(s)\n")
    for rel, n, label, val in sorted(set(findings))[:100]:
        print(f"  {rel}:{n}  {label}: {val}")
    print("\nUse the example.invalid namespace, or move the value to configuration.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
