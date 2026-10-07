"""Campaign backend — the thin shell around the built creation/aul/ads engines.

This package adds ONLY the campaign-shaped objects and orchestration the
advertiser journey needs (Campaign, LaunchPlan, Proposal, the stage machine);
every heavy capability — ingest/understanding, role resolution, planning,
preview, render, publish, outcomes, attribution, lineage — is delegated to
the existing engines. Nothing here talks to a paid provider: rendering uses
the local compose path and publishing uses the sandbox channel.
"""
