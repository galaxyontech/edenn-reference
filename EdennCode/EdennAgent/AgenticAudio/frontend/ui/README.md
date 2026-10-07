# Studio component island

The studio keeps its existing warm surfaces, typography, semantic layer colors,
spacing, and motion tokens. Only activity disclosures and assistant prose are
React roots. The native controller owns each host; React owns its descendants.
Disconnected hosts are unmounted. The timeline and the conversation scroll area
remain under their existing controllers.

## Rebuild

Use Node 22 or newer. From this directory:

```sh
npm ci
npm run build
npm test
```

Commit `../js/ai-elements.js` with source changes: the Python app serves this
self-contained, production bundle without a Node runtime. `bundle-report.json`
records uncompressed and gzip size. Current integration adds approximately 217 KB
gzip; this is a component maintenance/accessibility tradeoff, not a claim that
React makes the existing page faster. Optional diagram, code-highlighting, math,
and language plugins are omitted. No Tailwind reset or replacement theme is
shipped. The registry scaffolding remains for future component updates.

## Source and local adaptations

Installed September 9, 2026 from the official component registry using:

```sh
npx shadcn@latest add https://elements.ai-sdk.dev/api/registry/chain-of-thought.json https://elements.ai-sdk.dev/api/registry/message.json --yes
```

- Activity uses a compact button beside the reply's speaker name. A click opens
  a bounded panel with three recent steps, expandable history, and retry details
  for failed requests. Escape and outside clicks dismiss it; Escape restores
  focus to the trigger. The active status glows subtly in place of the speaker name; completed activity
  shows its elapsed duration and a disclosure arrow beside the speaker.
  The original activity primitives remain available in source but are not bundled.
- `MessageResponse` renders static semantic markdown. Local renderers retain
  native strong/emphasis semantics, restrict links, and omit remote images.
  Edenn does not simulate token streaming or invent model reasoning.
- The registry's supporting UI primitives are retained as installed. Their
  utility classes receive no global stylesheet; used elements have explicit
  scoped styles in the existing `styles.css`.

AI Elements source is covered by `AI-ELEMENTS-LICENSE` and `APACHE-2.0.txt`.
Supporting UI source is covered by `UI-PRIMITIVES-LICENSE`. Bundled dependency
license notices are preserved by esbuild. Local modifications are described above.

## Components evaluated but not adopted

[Conversation](https://elements.ai-sdk.dev/components/conversation) provides useful
follow-to-bottom behavior, but it would compete with the native controller's
reading anchors, conditional edge fades, and DOM updates. Keep one scroll owner.
[Reasoning](https://elements.ai-sdk.dev/components/reasoning) overlaps the chosen
step-based activity; a second disclosure would be redundant.
[Shimmer](https://elements.ai-sdk.dev/components/shimmer) adds a second ongoing
animation beside the status icon; the delayed single spinner is sufficient.
Prompt Input and Audio Player would expand the migration into upload and media
ownership without resolving an additional audited issue. Keep the current
composer and synchronized timeline/audio controls.

## Motion and event behavior

Status feedback waits 150 ms before showing a spinner. Label/icon changes use
150 ms opacity transitions. Completed activity folds after one second only when
not manually controlled, focused, hovered, selected, or being read in history.
Jump to latest uses a cancellable 220 ms animation. Pane changes use 160 ms opacity.
Reduced motion removes movement and continuous animation while retaining status.

Request identities are echoed by the socket router and retained by the HTTP/mock
transports. Late correlated events and snapshots fetched before a newer request
are rejected. Interrupted actions are not automatically resent. An unavailable
initial socket recovers the session through HTTP. This is client correlation,
not server-side idempotency or a guarantee that an interrupted job was cancelled.
