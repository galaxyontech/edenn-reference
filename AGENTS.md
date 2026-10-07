# Working in this repository

This is a reference snapshot. Read [README.md](README.md) for what the code
does and [REDACTIONS.md](REDACTIONS.md) for what was removed from it before you
conclude a file is missing.

## The rules that are enforced mechanically

**Never name an upstream AI provider or model.** Not in an identifier, a
filename, a configuration key, a log field, an error message, an API response,
a document, a test name, a fixture, or a commit message. Use the neutral
aliases already present (`provider_a`…`provider_e`, `model_gateway`,
`speech_recognition`) or a configuration key.

The one carve-out is a vendor SDK, which cannot be installed under another
name: the package line in `requirements.txt`, its `import` statement, and the
classes that import binds. Those stay inside the adapter module that already
holds them. Everything downstream of the adapter uses the alias.

**Never commit a real identifier.** Hostnames, storage accounts, subscription
identifiers, database servers, and usernames belong in configuration. In code
and tests use the `example.invalid` namespace, which is reserved and cannot
resolve.

**Never let a raw upstream error reach a client.** `Deployment/error_codes.py`
is the boundary. Send a catalogued public payload, never `str(exc)` and never a
`to_dict()` of an internal error.

**Tests spend nothing by default.** `pytest` deselects `remote_integration`.
Anything that reaches a real provider carries that marker.

## Before committing

```bash
python tools/scan_secrets.py
python tools/check_provider_names.py
python tools/check_identifiers.py
pytest
```

They are the gates that produced this snapshot. Two run as committed.
`check_provider_names.py` needs `tools/provider-denylist.txt`, which is not
committed; without it it refuses to run rather than reporting a pass it did not
earn. `pytest` is not fully green either — see REDACTIONS.md rule `R2`.
