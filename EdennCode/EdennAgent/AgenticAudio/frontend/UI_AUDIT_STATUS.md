# Audio Studio audit implementation status

Updated September 9, 2026 on `ui-dev-mock`.

The frontend implementation preserves the warm-light design system. The original
visual audit is historical; this file records the resulting behavior and actual
verification. AI Elements integration details and source notices are in
[the component README](ui/README.md).

| Audited area | Implemented behavior |
| --- | --- |
| Identity and redundancy | No Edenn avatar markup or avatar gutter; one compact name per response group. Repeated clarification wording removed. Layer and direction choices use compact controls and expandable completed receipts. |
| Session flow | Entrance → analysis → layers → directions → approval → takes → audition → selection → mix → export. Unavailable effects remain disabled in the real backend; demo effects require explicit selection. |
| Activity | Installed Chain of Thought components; controlled keyboard disclosure, stable step identities, completed tool states, three visible recent steps, expandable history, delayed spinner, truthful waiting/error/interruption states, review before retry. |
| Motion | Token-based transitions; restrained opacity changes, protected completion folding, cancellable jump-to-latest, pane crossfade, reduced-motion CSS and JavaScript. No simulated reasoning or invented percentage progress. |
| Scrolling | Conditional top/bottom fades, no pointer interception, focus-aware edges, reading-anchor preservation, jump-to-latest and explicit follow state. |
| Timeline | One measured clip language for music, narration, and effects; actual decoded audio peaks, honest plain-clip fallback, correct timing, overlap subrows, keyboard selection, separate audition and selected state. No fabricated waveforms or perpetual scene-analysis placeholder. |
| Output | Latest rendered mix takes precedence over an earlier audio-only result. The receipt reports audio/video accurately; native download links work without popup-dependent export. |
| Recovery | Request correlation across socket, HTTP and mock transports; stale events/refreshes rejected; ordered actions with coalesced mix changes; initial socket failure recovers through HTTP; session switching clears prior render state without duplicate composer listeners. |
| Responsive access | 300/380/560px chat widths, tablet boundaries, phone stacking, short-window scrolling; resize separator reports its value and supports arrows, Home and End. |

## Verification performed

- 79 deterministic Python tests passed, including socket event ordering, echoed
  request identities, errors, and the frontend asset allowlist. Three tests that
  require external services or live generation were deliberately excluded.
- 10 standalone Node regression tests passed for routing, effects, asynchronous
  hydration, request state, failed takes, and latest-mix export selection.
- Six component/controller tests passed for manual disclosure and focus, bounded
  activity history, safe semantic markdown, root cleanup, HTTP recovery and stale
  events, History switching, reading anchors, and jump-to-latest. The JavaScript
  reduced-motion branch is exercised with a reduced-motion media preference stub.
- TypeScript check and production build passed. The checked-in bundle is 771,428
  bytes raw and 237,470 bytes gzip, below the enforced 250,000-byte gzip budget.
  This dependency cost is explicit; no load-time speedup is claimed.
- Browser walkthrough: uploaded a playable synthetic 15-second video to the real
  local FastAPI router; selected music and a direction; approved local test takes;
  auditioned Take 2; selected it; changed music level; rendered and played the
  video mix; reloaded the session; triggered the native export download event.
  The resulting MP4 was independently probed: H.264 video, AAC audio, 15 seconds.
- The walkthrough used a scripted local director and placeholder audio, with
  external generation explicitly disabled. Video upload, playback, FFmpeg mixing,
  transport and export were real. This does not establish production model quality.
- Browser socket bootstrap and a choice turn passed with WebSocket support enabled.
  HTTP fallback was separately exercised against a server without WebSocket support.
- Earlier mock walkthroughs covered music, narration-only, full-build demo effects,
  failed takes, retry confirmation, audition switching, keyboard clip selection,
  confirmation cancellation and focus restoration, and edge fades.
- Browser width checks: 300, 380 and 560px chat panes at 1280px; 860 and 760px
  boundaries; 390×844 phone. All reported zero horizontal document/chat overflow.
  A 640×400 viewport checked the reflow dimensions of a 1280×800 window at 200%
  zoom. That exposed and verified the short-window scroll fix. Actual browser zoom
  scaling was not available through the current browser controls.
- Keyboard Space toggled the activity header while preserving focus and its
  controlled-region relationship. Keyboard resize and short-window scrolling were
  exercised. Checked application tabs reported no console errors or warnings.

## Local frame diagnostic

Run `ui/benchmark.html` through the standalone frontend server, then click
**Run benchmark**. The page is a development diagnostic and is not on the
production asset allowlist. It mounts 100 formatted messages, records an idle
baseline, then performs 12 activity updates while keeping the history mounted.

| Measurement | Local result |
| --- | --- |
| Initial render of 100 messages, including next frame | 45.9 ms |
| Idle frame interval, 95th percentile / maximum | 10.2 / 16.7 ms |
| Activity-update frame interval, 95th percentile / maximum | 10.2 / 16.5 ms |
| Frame intervals above 50 ms during updates | 0 |
| Visible activity steps after 12 updates | 3 |

These measurements describe one local browser run, not a universal frame-rate
promise or a production load benchmark. The shipped status animation adds no
observed frame-interval regression in this workload.

## Verification limits

Production generation, production credentials/storage, cross-browser/device
performance, and a manual screen-reader pass have not been verified. Reduced-motion
styles were inspected and the JavaScript path tested; an OS-level preference
walkthrough was not performed. The request identity is correlation, not server
idempotency: an interrupted job may still finish, so retries require review.
Actual 200% browser scaling remains a manual check beyond the tested reflow size.

No backend capability for production sound effects was added by this visual work.
The unavailable state is intentional and does not imply that effects generated.
