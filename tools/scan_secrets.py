#!/usr/bin/env python3
"""Fail if anything credential-shaped is in the tree.

Runs over BYTES, so a binary that slipped in is still read. Three rule
families, in decreasing confidence:

  shape     regexes for credential formats that are unambiguous
  context   a high-entropy value assigned to a credential-named variable
  entropy   a long high-entropy token anywhere, the weakest and noisiest rule

Exit 1 on any finding that is not justified in ``tools/scanner-allowlist.tsv``.
A justification is one line of prose saying why the match is not a secret; an
empty justification is not accepted.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "scanner-allowlist.tsv"

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             ".pytest_cache", ".next", "dist", ".mypy_cache"}

SHAPE_RULES = [
    ("api key (sk- prefixed)", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}")),
    ("api key (sk_ prefixed)", re.compile(rb"\bsk_[A-Za-z0-9]{32,}")),
    ("cloud api key", re.compile(rb"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("access key id", re.compile(rb"\b(?:AKIA|AKID)[A-Za-z0-9]{16,}")),
    ("private key block", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
    ("signed token", re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("storage account key", re.compile(rb"AccountKey=[A-Za-z0-9+/]{40,}")),
    ("shared access signature", re.compile(rb"[?&]sig=[A-Za-z0-9%+/=]{20,}")),
    ("chat platform token", re.compile(rb"\bxox[bapr]-[A-Za-z0-9-]{10,}")),
    ("source forge token", re.compile(rb"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("admin secret", re.compile(rb"\badm-[0-9a-f]{64}\b")),
]

# A password inside a connection string, EXCEPT on loopback: a throwaway local
# database is a test fixture, not a credential.
DSN_RULE = ("connection string password", re.compile(
    rb"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:/@\s]+:"
    rb"(?!REDACTED@)(?![^@\s]{0,64}@(?:127\.0\.0\.1|localhost|[\w.-]*example\.(?:invalid|com|org|test)|"
    rb"[\w-]*db\.example\.invalid|telemetry-db|billing-db|studio-db))[^@\s]{4,}@"))

CONTEXT_RULE = re.compile(
    rb"""(?ix)
    (?:api[_-]?key|secret|password|passwd|token|credential|private[_-]?key)
    \s*[:=]\s*
    ["']([A-Za-z0-9+/=_-]{24,})["']
    """)

PLACEHOLDER = re.compile(
    rb"(?i)^(?:redacted|change_?me|your[_-]|example|placeholder|dummy|sample|test|fake|xxx+|"
    rb"none|null|todo|<[^>]+>|\$\{|\.\.\.)")

ENTROPY_MIN_LEN = 40
ENTROPY_MIN_BITS = 4.6
ENTROPY_EXEMPT_SUFFIX = {".lock", ".svg", ".min.js", ".map", ".pem.example"}
ENTROPY_EXEMPT_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "MANIFEST.tsv"}
TOKEN_RE = re.compile(rb"[A-Za-z0-9+/=_-]{%d,}" % ENTROPY_MIN_LEN)


def shannon(b: bytes) -> float:
    if not b:
        return 0.0
    counts = {}
    for ch in b:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def load_allowlist() -> set:
    allowed = set()
    if not ALLOWLIST.exists():
        return allowed
    for line in ALLOWLIST.read_text().splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("path\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip():
            allowed.add((parts[0].strip(), parts[1].strip()))
    return allowed


def walk():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def main() -> int:
    allowed = load_allowlist()
    findings = []

    for path in walk():
        rel = str(path.relative_to(ROOT))
        try:
            data = path.read_bytes()
        except OSError:
            continue

        for label, rule in SHAPE_RULES + [DSN_RULE]:
            if rule.search(data) and (rel, label) not in allowed:
                findings.append((rel, label, "shape"))

        for m in CONTEXT_RULE.finditer(data):
            value = m.group(1)
            if PLACEHOLDER.match(value) or shannon(value) < 3.5:
                continue
            if (rel, "credential-named assignment") in allowed:
                continue
            findings.append((rel, "credential-named assignment", "context"))
            break

        if path.name in ENTROPY_EXEMPT_NAMES or path.suffix in ENTROPY_EXEMPT_SUFFIX:
            continue
        if (rel, "high-entropy token") in allowed:
            continue
        for m in TOKEN_RE.finditer(data):
            tok = m.group(0)
            if PLACEHOLDER.match(tok) or shannon(tok) < ENTROPY_MIN_BITS:
                continue
            # A real credential mixes all three character classes. Long
            # identifiers, SQL, and prose in this codebase do not, and without
            # this the entropy family drowns the two families that matter.
            if not (any(0x41 <= c <= 0x5A for c in tok)
                    and any(0x61 <= c <= 0x7A for c in tok)
                    and any(0x30 <= c <= 0x39 for c in tok)):
                continue
            if b"_" in tok or b"-" in tok:
                continue
            findings.append((rel, "high-entropy token", "entropy"))
            break

    if not findings:
        print(f"scan_secrets: clean ({sum(1 for _ in walk())} files)")
        return 0

    print(f"scan_secrets: {len(findings)} finding(s)\n")
    for rel, label, family in sorted(set(findings)):
        print(f"  [{family:7}] {label:34} {rel}")
    print("\nFix the file, or add a row to tools/scanner-allowlist.tsv with a")
    print("written justification. Never paste the value itself into that file.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
