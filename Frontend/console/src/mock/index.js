/**
 * The mock layer: the whole console, with no backend behind it.
 *
 * Without this, `next dev` gets you exactly one screen — "not connected to
 * Firebase" — because both halves of the app hang off a server: sign-in needs
 * Firebase config the API hands down at runtime, and every number on every page
 * comes from `/api/v1/account/*`. So the screens worth designing were the
 * screens you could not open.
 *
 * Two seams, and only two, are faked:
 *
 *   * `src/lib/firebase.js` — who is signed in, and how they signed in.
 *   * `src/lib/api.js` — the single `request()` all API calls funnel through.
 *
 * Nothing else in the app knows. Components take the same shapes, the same
 * three-state resources, the same failures, so what you see in mock mode is the
 * real render path — not a storybook copy that drifts.
 *
 * OFF BY DEFAULT, and off by a *build-time* constant. `NEXT_PUBLIC_MOCK` is
 * inlined by Next, so in a normal build every guard folds to `if (false)` and
 * webpack never follows the imports inside them: the fixtures, the fake
 * backend, the fake session and the badge's markup are all absent from the
 * export. (Verified by grep, not by hope — the scenario list below is the one
 * thing that survives, about a kilobyte of labels in the layout chunk.) There
 * is no runtime switch that could flip a deployed console onto invented
 * numbers.
 */

export const MOCK = process.env.NEXT_PUBLIC_MOCK === '1';

/**
 * Every state the console can be in, as something you can reach by URL.
 *
 * These are not "test data variants" — they are the branches the components
 * already contain and that a real account walks through once each, if ever. The
 * first-run Home, a $0.00 balance, all three panels failing at once: each is
 * hard-won UI that nobody could look at without arranging an account to match.
 */
export const SCENARIOS = [
  { id: 'active', label: 'Active account', note: 'Funded, four months of jobs, four keys' },
  { id: 'low', label: 'Low balance', note: 'Balance warning, "add credit soon"' },
  { id: 'broke', label: 'Zero balance', note: 'Submissions return 402' },
  { id: 'new', label: 'First run', note: 'No jobs, no keys — the three-step Home' },
  { id: 'signup', label: 'Needs signup', note: 'Phone verified, account not opened yet' },
  { id: 'admin', label: 'Admin', note: 'Active account plus the operator view' },
  { id: 'heavy', label: 'Heavy usage', note: 'Past the 5,000-row ceiling; truncation notices' },
  { id: 'degraded', label: 'Backend down', note: 'Every account request fails' },
  { id: 'signed-out', label: 'Signed out', note: 'The phone sign-in flow; any code works' },
];

const DEFAULT_SCENARIO = process.env.NEXT_PUBLIC_MOCK_SCENARIO || 'active';

const KNOWN = new Set(SCENARIOS.map((s) => s.id));

/**
 * Which scenario this page load is in — `?mock=admin`, else the env default.
 *
 * The URL rather than an env var is the point: `NEXT_PUBLIC_*` is baked at
 * compile time, so switching scenarios that way costs a dev-server restart
 * each. A query parameter is a reload, and it survives being pasted to
 * whoever you want to show the screen to.
 *
 * Prerendering has no `location`, so the build sees the default — and the
 * scenario switcher waits for mount before it renders anything, rather than
 * handing React a server frame that says "active" and a client frame that says
 * "admin".
 */
export function scenario() {
  if (typeof window === 'undefined') return DEFAULT_SCENARIO;
  const asked = new URLSearchParams(window.location.search).get('mock');
  return asked && KNOWN.has(asked) ? asked : DEFAULT_SCENARIO;
}

/**
 * A pause, because instant is its own kind of lie.
 *
 * Every loading state in this console — the Home skeleton, the chart's, the
 * key table's — is invisible against a synchronous mock, and invisible states
 * are the ones that ship broken. 260ms is roughly what the real API costs from
 * a warm container and long enough to see a skeleton paint.
 */
export const LATENCY_MS = 260;

export function delay(ms = LATENCY_MS) {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}
