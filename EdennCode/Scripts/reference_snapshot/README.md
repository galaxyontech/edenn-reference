# Refreshing the public reference snapshot

`github.com/galaxyontech/edenn-reference` is a curated public snapshot of this
repository: a single commit, no history, with provider names aliased, infra
identifiers replaced, and 248 files excluded by documented rules.

**The original export script was never kept.** That cost a full day of
archaeology the first time the snapshot needed updating, so these live here now.

## Why this is a merge and not a replay

The transformation is not a token map. Part of it is — one vendor name to one
alias, everywhere. The rest is not: a cloud-service name kept inside an
identifier and dropped from the prose beside it, comments reworded rather than
substituted. Re-deriving a map from the published result and replaying it gets
that second kind wrong silently.

So the refresh carries the delta across the existing curation instead:

| private file | action |
|---|---|
| unchanged | published bytes stand — no work, no risk |
| modified  | three-way merge: published file IS curate(base), so applying the base→HEAD diff yields curate(HEAD) |
| added     | classified against the deny rules, refused if it carries a vendor token, else copied |
| deleted   | removed |

A conflict means the branch and the curation edited the same line. That is the
one case a human must resolve, and the one a replayed map would get wrong
without saying so.

## Running it

    .venv/bin/python3 EdennCode/Scripts/reference_snapshot/sync_snapshot.py
    .venv/bin/python3 EdennCode/Scripts/reference_snapshot/update_manifest.py

Then, in the snapshot checkout, with `tools/provider-denylist.txt` written
locally (never committed — the vocabulary in the public repo would itself break
the rule the checker enforces):

    python3 tools/check_provider_names.py
    python3 tools/check_identifiers.py
    python3 tools/scan_secrets.py

`BASE` in `sync_snapshot.py` is the private commit the published snapshot was
last built from. **Update it after every successful push**, or the next refresh
recomputes a delta against the wrong origin.

Nothing here pushes. The snapshot repo is public and a mistake in it is not
revocable, so the diff is reviewed by a human and pushed by hand.
