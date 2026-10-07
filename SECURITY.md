# Security

## Reporting

Report a suspected vulnerability, or a credential or personal datum you believe
survived into this repository, privately to the maintainers. Do not open an
issue and do not post details publicly.

If you find something that looks like a live credential here, treat it as live:
report it, and do not test it against any service.

## What this repository is

A curated, single-commit snapshot of a private codebase, published for reading.
It carries no credentials, no deployment configuration, and no commit history.
[REDACTIONS.md](REDACTIONS.md) is the complete record of what was removed.

## What it is not

It is not a deployable system. The deployment scripts, container definitions,
environment-specific infrastructure definitions, and CI deployment workflows
were all excluded. Code that references them remains, so following a code path
to a deploy step will lead somewhere that is not here.

## Configuration

`.env.example` lists every variable the code reads, with every value blank. It
is a template, not a configuration: no value in it is real, and none has ever
been real.

The scrubbers in `EdennCode/Deployment/response_guardrail.py` and
`EdennCode/Deployment/error_codes.py` read their vendor vocabulary from
`PROVIDER_SCRUB_*` environment variables. Unconfigured, a field whose name
announces upstream identity is still flagged; the free-text and URL passes are
vocabulary-driven and match nothing until tokens are set.

## Automated checks

Three scanners are wired to run on every commit. Two run as committed; the
provider-name check needs a locally supplied token list and refuses to run
without it. They are the supported way to keep this
snapshot clean:

    python tools/scan_secrets.py
    python tools/check_provider_names.py
    python tools/check_identifiers.py

Refreshing this snapshot from the private repository means regenerating
`MANIFEST.tsv` and re-running the export pipeline and these gates. Copying
directories across is not a supported refresh and would reintroduce everything
listed in REDACTIONS.md.
