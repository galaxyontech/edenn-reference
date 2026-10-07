# Contributing

This is a **reference snapshot**, not an upstream. It is published for reading
and evaluation. Issues and questions are welcome; pull requests are unlikely to
be merged, because development happens in a private repository and this copy is
regenerated rather than edited.

If you do work in this tree, three project conventions apply.

## 1. Never name an upstream provider

No upstream AI provider, product, model family, or model identifier may appear
in source identifiers, filenames, configuration keys, log fields, error text,
API responses, documentation, test names, fixtures, or commit messages. Use the
neutral aliases already in the codebase (`provider_a`…`provider_e`,
`model_gateway`, `speech_recognition`) or an internal configuration key.

The single carve-out is a vendor SDK, which cannot be installed under another
name: the package in `requirements.txt`, its `import` line, and the classes that
import binds. The intended home for those imports is two adapter modules, and
new code must go through them:

- `EdennCode/ModelFactory/LanguageModelFactory/gateway_clients.py`
- `EdennCode/ModelFactory/VoiceOverModelFactory/speech_clients.py`

Ask those for a client by role. Three older adapters still import the SDK
directly and predate the facades — `LanguageModelFactory/azure_based_model_model_gateway.py`,
`LanguageModelFactory/voiceover_llm_client.py`, and
`VoiceOverModelFactory/model_gateway_base_model.py`. Moving them behind the
facades is outstanding work, not a decision. `tools/check_provider_names.py` enforces the
boundary, and it needs a vocabulary to enforce it with:
`tools/provider-denylist.txt` is deliberately not committed, because writing the
names down is the thing the rule forbids. Get it from a maintainer, or set it
from a CI secret. **Without it the check does not run at all** — it exits
non-zero and says so, rather than reporting a pass it did not earn.

When in doubt, omit the name and use a role word.

## 2. Never commit a real identifier

Hostnames, account names, subscription identifiers, database servers, and
usernames belong in configuration. Use the `example.invalid` namespace in code
and tests — it is reserved and can never resolve, so a missed follow-up cannot
point a reader at live infrastructure.

Phone numbers in fixtures must come from a reserved test range and say so in a
comment.

## 3. Tests do not spend money and do not reach the network

The default suite excludes tests marked `remote_integration`:

    pytest

That default is deliberate. A suite that fails on a laptop for want of a
credential trains everyone to ignore a red suite. Tests that reach a real
provider must carry the marker.

## Before you commit

    python tools/scan_secrets.py
    python tools/check_provider_names.py
    python tools/check_identifiers.py
    pytest

They are the same gates the export pipeline runs. Two run as committed;
`check_provider_names.py` refuses to run without a locally supplied
`tools/provider-denylist.txt`, and `pytest` is not fully green (REDACTIONS.md
rule `R2` removed the media fixtures). They
are the reason this snapshot can be refreshed safely.
