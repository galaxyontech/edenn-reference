# What was removed from this copy, and why

This repository is a **curated snapshot** of a private codebase, not a mirror of
it. It was produced mechanically: every file here was named on an allowlist and
extracted from the source repository's object database, and every substitution
below was applied by a script whose rules are recorded, counted, and asserted.

Nothing was removed quietly. This file is the complete ledger.

Two things follow from how it was built, and both are deliberate:

- **There is no upstream commit history.** The snapshot began as a single
  commit and has gained only its own later refreshes; nothing from the source
  repository's history travelled. History was abandoned rather than rewritten,
  because the source history contains material
  that could not be published and proving a rewrite clean across every commit,
  ref, and unreachable object is a weaker guarantee than never carrying them.
  Do not file a bug about missing `git blame`.
- **Some code paths reference things that are not here.** Where a deployment
  script, a media fixture, or an internal document was removed, code that
  referenced it still exists. That is a consequence of publishing the codebase
  rather than a runnable system.

---

## 1. Credentials and secrets

**No credential of any kind is present in this repository**, and none is present
in its history, because it has no history.

Every value below was removed, and separately **rotated at the source**. Files
whose only purpose was to carry such a value were excluded outright rather than
edited, because a redacted file still tells a reader exactly which system the
removed string opened.

| Class | Handling |
|---|---|
| Generation-provider API keys | file excluded, or value replaced with `REDACTED_API_KEY` |
| Hosted language-model gateway keys | excluded |
| Object-storage account keys and connection strings | `AccountKey=REDACTED` |
| Shared access signatures on storage URLs | `REDACTED_SAS` |
| Database passwords in connection strings | `postgresql://user:REDACTED@…` |
| Platform admin secret | excluded with the document that carried it |
| Issued customer API keys | excluded with the document that carried it |
| Provider callback webhook secrets | excluded |
| Signed tokens (JWT-shaped) | `REDACTED_TOKEN` |
| Third-party object-storage access-key pairs | `REDACTED_ACCESS_KEY_ID` |

A loopback connection string such as `postgresql://postgres:postgres@127.0.0.1`
is **not** redacted. It is a local test fixture, it grants nothing, and blanking
it would break the tests that stand up a throwaway database.

## 2. Infrastructure identifiers

Every name that pointed at live infrastructure was replaced with a placeholder
in a reserved namespace that can never resolve. This is not cosmetic: even
without a credential, a list of real hostnames is a targeting package.

| What | Replaced with | Occurrences |
|---|---|---|
| Container registry hostname and name | `registry.example.invalid`, `exampleregistry` | 9 |
| Application hostnames | `api` / `worker` / `studio`.`example.invalid` | 16 |
| Container application names | `prod-app`, `staging-app`, `console-app`, `studio-app`, … | 24 |
| Storage account hostnames | `*.storage.example.invalid` | 14 |
| Storage account names | `primarystorage`, `secondarystorage`, `mediastorage`, `voicestorage` | 35 |
| Database server hostnames | `telemetry-db` / `billing-db` / `db`.`example.invalid` | 7 |
| Database server names | `telemetry-db`, `billing-db`, `studio-db` | 7 |
| Database administrator username | `dbadmin` | 3 |
| Secret store name | `example-vault` | 4 |
| Third-party object-storage endpoint | `objectstore.example.invalid` | 2 |
| Cloud subscription / tenant identifier | all-zero GUID | 4 |

Placeholder hostnames that were **already** placeholders in the source
(`example`, `acct`, `current`, `old`, `devaccount`, `staleaccount`) were left
exactly as they were. The redaction script asserts this, so a future run cannot
quietly rewrite an example into a different example.

## 3. Upstream provider and model names

A standing house rule forbids naming an upstream AI provider or model anywhere a
reader can see: identifiers, filenames, configuration keys, log fields, error
text, API responses, documentation, tests. Roughly **3,100 occurrences across
229 files** were renamed, and **51 files and directories** were renamed with
them.

Each vendor was given one opaque alias, and **only the vendor token was
substituted**. That detail matters. One vendor supplies three different
capabilities here — music composition, sound effects, and speech — and the
surrounding identifier already carries the capability
(`…MusicProvider`, `…SoundEffectProvider`, `…TTS`). Substituting the whole
identifier with a capability-named alias would have merged three distinct things
into one and produced a codebase that compiles, passes its tests, and describes
the wrong system. Substituting only the vendor token cannot do that.

| Alias | Role in this codebase |
|---|---|
| `provider_a` | music composition, sound effects, and hosted speech |
| `provider_b` | music composition |
| `provider_c` | music composition |
| `provider_d` | music composition |
| `provider_e` | video-conditioned sound effects |
| `model_gateway` | hosted language model gateway |
| `model_gateway_alt`, `model_vendor_alt` | alternative hosted language models |
| `speech_recognition` | speech recognition |

Model identifiers became capability names: `chat-standard`, `chat-advanced`,
`chat-legacy`, `speech-standard`, `embed-standard`.

### The one carve-out

A vendor SDK cannot be installed under another name. Package names in
`requirements.txt`, their `import` lines, and the classes those imports bind
(used as constructors and type annotations) are kept **verbatim and only there**.
They appear in roughly a dozen adapter call sites and nowhere else: not in a
config key, not in a log field, not in an error message, not in a filename, not
in documentation. This is the isolation the rule allows, and
`tools/check_provider_names.py` enforces the boundary on every commit.

### The scrubbers now read their vocabulary from configuration

The product ships two defensive scrubbers that strip vendor names from
client-facing text. They previously hardcoded the vocabulary, which this
repository cannot do. They now load it from the environment — see
[`EdennCode/Deployment/provider_vocabulary.py`](EdennCode/Deployment/provider_vocabulary.py)
and the `PROVIDER_SCRUB_*` variables in `.env.example`.

**With nothing configured they are reduced, not switched off.** A field whose
NAME announces upstream identity is still flagged, because that check does not
consult the vocabulary. The free-text and URL passes are vocabulary-driven and
have nothing to match until tokens are configured, which is the right behaviour
for a deployment that has not said which names to hide. Their tests configure a
synthetic vocabulary and assert every collision rule the original encoded,
including the unconfigured case.

## 4. Personal data

| What | Handling |
|---|---|
| Developer home directory paths | `/Users/example`, `/path/to/repo`, `/tmp/workdir` (17) |
| Personal messaging account identifier | `REDACTED_ACCOUNT_ID` (1) |
| Personal and university email addresses | replaced with a role address |
| Administrator phone-number allowlist | left with the documents that carried it |
| Commit authorship | a single role identity; no personal address appears |

Phone numbers that remain are reserved, non-routable test numbers in fixtures
that assert country-code normalisation. They are commented as such.

### One deliberate exception

`.gitignore` names five editor and assistant state directories literally, three of
which are AI tools. Generalising them was considered and rejected: in the source
repository a committed assistant state directory carried a storage account key,
and an ignore pattern that does not name the directory does not ignore it. The
names appear in repository hygiene configuration and nowhere in the product, its
code, or its output.

## 5. Whole files excluded

**251 of the source repository's 1,181 tracked files** did not travel. Every
exclusion is attributable to a named rule, and the complete row-by-row record is
in [`MANIFEST.tsv`](MANIFEST.tsv).

| Rule | Files | What it covers |
|---|---:|---|
| `R7_INTERNAL_DOCS` | 114 | Internal plans, status reports, readiness audits, operations runbooks, incident analyses, benchmark write-ups, and design records written for an internal audience. Removing a value from such a document still leaves the sentence describing what the value opened. |
| `R2_MEDIA_AND_BINARY` | 61 | All video, audio, image, PDF, archive, and notebook files. This also removes the dependency on a large-file store entirely. |
| `R6_DEPLOY_SURFACE` | 37 | Deployment scripts, container definitions, CI deployment workflows, environment-specific infrastructure definitions, and the model-resource module. A map of live infrastructure, and not needed to read the code. |
| `R4_EDITOR_AND_AGENT_STATE` | 12 | Per-developer editor and assistant configuration. |
| `R10_THIRD_PARTY_OR_GENERATED` | 10 | Third-party footage, brand assets, captured run output, and generated test artifacts. Publishing these would redistribute other people's work and likeness. |
| `R1_CREDENTIAL_BEARING` | 7 | Files whose content included a live credential, an issued customer key, a signed storage URL, or an administrator phone allowlist. |
| `R12_REWRITTEN_FROM_SCRATCH` | 6 | Environment templates and repository configuration, retyped rather than edited. A fresh file cannot carry a value a substitution missed. |
| `R3_TRACKED_BUT_IGNORED` | 3 | Files tracked only because they predate an ignore rule. |
| `R13_SYMLINK` | 1 | A symbolic link that would dangle outside the exported tree. |

### What this costs you as a reader

- **Test fixtures are gone.** Media assets under the test suites were removed, so
  tests that need real audio or video are skipped or fail. The unit and contract
  tests that make up the bulk of the suite run offline and pass.
- **There is no way to deploy this.** That is intentional. It is a reference
  copy, not a runnable service.
- **Some documentation is missing.** Package-level `README`, `ARCHITECTURE`, and
  `USAGE` files that sit beside the code were kept and redacted. Internal
  planning and operations documents were not.

---

## How this was verified

Each stage of the export ended in a check that fails the build rather than
warning:

1. The isolating clone from which this snapshot was built carried exactly one
   ref, zero unreachable objects, no remotes, and the expected file count.
2. Every one of the 1,181 source paths carries exactly one of a deny rule or a
   keep mark, never both and never neither, and every rule matched at least one
   file — a rule that matches nothing is a build error, not a no-op.
3. Every extracted file came from the object database, never from a working
   directory - which is how untracked credential files get picked up. Files that
   no substitution rule touched are byte-identical to their blob; the rest differ
   from their blob only by the substitutions recorded in section 2.
4. No file in this repository begins with media or archive magic bytes.
5. Every substitution rule declares a minimum hit count and the build fails
   below it, because a stale rule that silently matches nothing is exactly how a
   leak survives a pass that reports success.
6. The scanners in `tools/` run over this repository and are wired to run again
   on every commit.

The scanners ship here on purpose. A one-time check protects the first
publication and nothing after it; the failure mode this repository is most
exposed to is a future refresh done by copying directories, which would bring
back everything listed above.
