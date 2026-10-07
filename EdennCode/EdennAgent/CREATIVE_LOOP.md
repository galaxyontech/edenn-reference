# The Creative Loop — agent grounded on real assets, closed by ad performance

**Status:** design direction (owner, 2026-07-16). Extends UNDERSTANDING_LAYER.md.
**One sentence:** a creative agent that plans from the library first, generates
only to fill named gaps, publishes variants to ad channels, and reads CTR/CVR
lift back onto the SAME ledger its decisions came from — so performance
attributes to creative components, not just whole videos.

## 1. The loop

```
brief ─▶ ground (library search: moments, tracks, stills — refs)
     ─▶ plan  (slots = REUSE by default; gaps named explicitly)
     ─▶ gap decisions (reuse alternative | generate, PRICED, user-approved)
     ─▶ variants (hypothesis axes, all slots ref-addressed)
     ─▶ publish (TikTok / Meta / Google Ads APIs; provenance stamped)
     ─▶ outcomes (CTR/CVR/spend per variant, per placement, pulled daily)
     ─▶ attribute (roll outcomes DOWN lineage edges to spans/tracks/hooks)
     ─▶ iterate ("keep the winning hook, vary the close — 3 more?")
```

## 2. Grounded agent policy (generation only when needed)

The reuse ladder (PRD §7.3) becomes the agent's visible decision policy:
- Every plan slot is REUSE unless the planner can name the gap it cannot fill
  ("no product close-up ending exists in the library").
- A gap presents OPTIONS with costs: reuse-adjacent (still + Ken Burns, crop,
  restore) before generate (bridge shot, track, VO) — each generation choice
  is priced and gated on approval (the agentic_audio spend-gate pattern).
- Plan cards show the ratio ("11 slots: 10 reused · 1 generated") — the
  trust number for brand teams, and the cost number for budget owners.

## 3. Outcomes are annotations; attribution is a lineage rollup

No new machinery — the understanding layer absorbs performance natively:
- **OutcomeEvent** = an annotation on an output ref
  (`take_x`, layer=outcome, producer=tiktok_ads@v1, payload={impressions,
  ctr, cvr, spend, placement, audience, date}).
- **Component attribution** = GROUP BY lineage edge: every output's slots
  reference spans/tracks/narrations, so "CTR of variants whose slot-1 is
  asset_9f31#t=340-346" is a query, not a model. Openings, tracks, closers,
  moments — each becomes a rankable component with sample counts.
- Honesty rule (PRD §16): correlation ≠ experiment. The UI labels lift as
  observed until variants were structured as a controlled axis test (which
  the variation planner can do deliberately: same everything, vary the hook).

## 4. Channel integration (thin, deliberately)

- Publish: per-channel adapters (TikTok Business, Meta Marketing, Google Ads
  APIs) that upload the rendered variant + metadata, and record the channel's
  ad/creative id ON the output ref (an edge: `published_as`).
- Ingest: a daily metrics pull per published ref → OutcomeEvent annotations.
- Nothing else: no bidding, no budget management, no audience tooling — we
  reflect performance, we don't run media (PRD non-goal).

## 5. Why this compounds (the moat mechanics)

Each cycle deposits: which components won, in which placements, for which
audiences — as queryable history on refs the next plan draws from. The agent's
grounding queries start preferring proven components ("this opening carried
+23% CTR in 3 of 3 variants") — preference learning (PRD §16.1) emerges from
the ledger without a training pipeline. Competitors with opaque pipelines
can't retrofit this: attribution requires span-level lineage at creation time.

## 6. Screens (the design tool project "Edenn Asset Library" → Screens)

- **Agent — grounded plan** (`screens/agent-grounded-plan.html`): the brief
  conversation; plan card with reused moments as chips, the ONE gap called
  out with priced options, reuse ratio, variant axes, make-variants CTA.
- **Performance — what's working** (`screens/performance-loop.html`):
  variants row with per-channel CTR/CVR + lift; component insights ranked by
  lineage rollup (openings / tracks / moments); agent iterate strip.
