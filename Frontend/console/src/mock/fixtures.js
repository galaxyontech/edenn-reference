/**
 * The invented account, built fresh per scenario.
 *
 * Two rules the data follows, both of which cost a little effort and are worth
 * it:
 *
 *   * **Deterministic.** Seeded from the scenario name, so a reload redraws the
 *     same chart. Randomness that changes under you turns "did my change move
 *     that bar?" into a question you cannot answer.
 *   * **Shaped like real traffic.** Weekday-heavy, with quiet stretches and a
 *     couple of spikes. Uniform noise makes every chart look like a brick and
 *     hides exactly the layout problems — a lone tall bar, a run of zeroes —
 *     that only show up on real accounts.
 *
 * Timestamps are relative to now, so the last-14-days strip and the 30-day
 * chart are always populated rather than drifting off the left edge.
 */

/** mulberry32 — small, fast, and stable across engines. */
function rng(seed) {
  let state = seed >>> 0;
  return function next() {
    state = (state + 0x6D2B79F5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seedOf(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

const HEX = '0123456789abcdef';

function hex(random, length) {
  let out = '';
  for (let i = 0; i < length; i += 1) out += HEX[Math.floor(random() * 16)];
  return out;
}

function pick(random, list) {
  return list[Math.floor(random() * list.length)];
}

const DAY_MS = 86400000;

const MODELS = ['edenn_enhanced', 'edenn_enhanced', 'edenn_pro', 'edenn_fast'];

/** The two billed products, at the endpoints `productOf()` actually knows. */
const ENDPOINTS = [
  '/api/v2/jobs/video-music',
  '/api/v2/jobs/video-music',
  '/api/v2/jobs/video-music',
  '/api/v2/jobs/multi-image-music',
];

/**
 * What each scenario is, in the only terms that matter to the UI: how much
 * money, how much history, how many keys, and who the server thinks you are.
 */
const SHAPES = {
  active: { balance: 184.2, jobs: 430, days: 120, keys: 4 },
  low: { balance: 6.4, jobs: 380, days: 120, keys: 3, warning: true },
  broke: { balance: 0, jobs: 210, days: 90, keys: 2 },
  new: { balance: 0, jobs: 0, days: 0, keys: 0 },
  signup: { balance: 0, jobs: 0, days: 0, keys: 0, account: false },
  admin: { balance: 184.2, jobs: 430, days: 120, keys: 4, admin: true },
  heavy: { balance: 1240.75, jobs: 5200, days: 150, keys: 7 },
  degraded: { balance: 0, jobs: 0, days: 0, keys: 0, down: true },
  // The same account as `active`, reached the long way round — signing in and
  // landing on an empty console would make the flow look like it half-failed.
  'signed-out': { balance: 184.2, jobs: 430, days: 120, keys: 4 },
};

const KEY_NAMES = [
  'production', 'staging', 'batch-ingest', 'mobile-app',
  'partner-sandbox', 'analytics', 'load-test',
];

/**
 * How many jobs land on a given day, as a multiplier.
 *
 * Weekends run light, one week in five is quiet, and a slow upward drift keeps
 * the 12-month view from being a flat wall. `daysAgo` counts backwards, so 0 is
 * today.
 */
function intensity(daysAgo, total, random) {
  const date = new Date(Date.now() - daysAgo * DAY_MS);
  const weekend = date.getDay() === 0 || date.getDay() === 6;
  const lull = Math.floor(daysAgo / 7) % 5 === 3;
  const drift = 1 - (daysAgo / Math.max(total, 1)) * 0.55;
  const spike = random() < 0.04 ? 2.8 : 1;
  return Math.max(0, drift * spike * (weekend ? 0.35 : 1) * (lull ? 0.15 : 1));
}

function makeKeys(random, howMany) {
  const keys = [];
  for (let i = 0; i < howMany; i += 1) {
    const createdDaysAgo = 210 - i * 26 - Math.floor(random() * 9);
    // One key per account is revoked, and one predates the day we started
    // recording the last four characters — both are states the key table draws
    // differently and neither is reachable on a fresh mock account otherwise.
    const revoked = howMany > 2 && i === howMany - 1;
    const legacy = howMany > 3 && i === 0;
    keys.push({
      name: KEY_NAMES[i % KEY_NAMES.length],
      key_prefix: `sk-${hex(random, 9)}`,
      key_suffix: legacy ? '' : hex(random, 4),
      is_active: !revoked,
      created_at: new Date(Date.now() - createdDaysAgo * DAY_MS).toISOString(),
      last_used_at: revoked || random() < 0.2
        ? null
        : new Date(Date.now() - random() * 6 * DAY_MS).toISOString(),
      revoked_at: revoked
        ? new Date(Date.now() - 11 * DAY_MS).toISOString()
        : null,
    });
  }
  return keys;
}

/**
 * Usage rows, newest first — the order the API returns and the order Home's
 * "recent activity" and the strip's oldest-row check both assume.
 */
function makeUsage(random, shape, keys) {
  const live = keys.filter((key) => key.is_active);
  const rows = [];
  const weights = [];
  for (let day = 0; day < shape.days; day += 1) {
    weights.push(intensity(day, shape.days, random));
  }
  const sum = weights.reduce((a, b) => a + b, 0) || 1;

  for (let day = 0; day < shape.days; day += 1) {
    const count = Math.round((weights[day] / sum) * shape.jobs);
    for (let i = 0; i < count; i += 1) {
      // Spread within the working day rather than uniformly across 24h; the
      // ledger's timestamps read as nonsense otherwise.
      const at = Date.now() - day * DAY_MS
        - Math.floor((8 + random() * 11) * 3600000) - Math.floor(random() * 3600000);
      const endpoint = pick(random, ENDPOINTS);
      const roll = random();
      const status = roll < 0.9 ? 'completed' : roll < 0.965 ? 'failed' : 'processing';
      const seconds = endpoint.includes('multi-image')
        ? 15 + Math.floor(random() * 45)
        : 30 + Math.floor(random() * 8) * 30;
      // Failed and in-flight jobs bill nothing. Both products bill on
      // delivered duration, so the amount tracks the seconds rather than
      // wandering off on its own.
      const billed = status === 'completed'
        ? Number(((seconds / 30) * (endpoint.includes('multi-image') ? 0.08 : 0.42)).toFixed(4))
        : 0;
      rows.push({
        job_id: `job_${hex(random, 20)}`,
        timestamp_utc: new Date(at).toISOString(),
        endpoint,
        model_spec: pick(random, MODELS),
        status,
        billed_amount_usd: billed,
        key_prefix: live.length ? pick(random, live).key_prefix : null,
        total_tokens: status === 'completed' ? 400 + Math.floor(random() * 5200) : 0,
        video_duration_s: seconds,
      });
    }
  }
  rows.sort((a, b) => b.timestamp_utc.localeCompare(a.timestamp_utc));
  return rows;
}

/** The other accounts an operator sees. Only the admin scenario has any. */
function makeAccounts(random, self) {
  const names = [
    'Acme Media Ltd', 'Northwind Studios', 'Rivermark Post', 'Kite & Co',
    'Halcyon Interactive', 'Blue Harbour Films', 'Sundial Audio', 'Meridian Ads',
    'Tessera Labs', 'Ridgeway Content',
  ];
  const accounts = names.map((name, i) => {
    const jobs = Math.floor(random() * 900) + (i === 0 ? 400 : 5);
    return {
      account_id: `acct_${hex(random, 12)}`,
      registered_name: i === 6 ? '' : name,   // one unnamed account: the UI says so
      is_active: i !== 8,
      jobs,
      total_billed_usd: Number((jobs * (0.3 + random() * 0.5)).toFixed(2)),
      balance_usd: Number((random() < 0.25 ? -0 : random() * 900).toFixed(2)),
    };
  });
  accounts[0] = { ...accounts[0], ...self };
  return accounts.sort((a, b) => b.total_billed_usd - a.total_billed_usd);
}

/**
 * A ledger that walks forwards and lands on the balance the account actually
 * has.
 *
 * The obvious way — start from today's balance and subtract backwards — makes
 * a ledger where "balance after" goes negative a fortnight ago, because the
 * credits that paid for those jobs are further back still. Building it in the
 * direction time runs, and topping up whenever the balance would go through
 * the floor, gives a column that reads the way a real one does. The last entry
 * is sized to close the gap so the ledger and the balance tile agree.
 */
function makeTransactions(random, target) {
  const rows = [];
  let balance = 0;
  const steps = 14;

  for (let i = steps - 1; i >= 1; i -= 1) {
    const charge = Number((random() * 22 + 0.5).toFixed(2));
    const topUp = balance - charge < 5;
    const amount = topUp
      ? Number((100 + Math.floor(random() * 400)).toFixed(2))
      : -charge;
    balance = Number((balance + amount).toFixed(2));
    rows.push({
      txn_id: `txn_${hex(random, 10)}`,
      timestamp_utc: new Date(Date.now() - i * 2.4 * DAY_MS).toISOString(),
      txn_type: topUp ? 'recharge' : 'job_charge',
      job_id: topUp ? null : `job_${hex(random, 20)}`,
      note: topUp ? 'Manual credit — wire transfer' : '',
      amount_usd: amount,
      balance_after_usd: balance,
    });
  }

  const closing = Number((target - balance).toFixed(2));
  rows.push({
    txn_id: `txn_${hex(random, 10)}`,
    timestamp_utc: new Date(Date.now() - 5 * 3600000).toISOString(),
    txn_type: closing >= 0 ? 'recharge' : 'job_charge',
    job_id: closing >= 0 ? null : `job_${hex(random, 20)}`,
    note: closing >= 0 ? 'Manual credit — wire transfer' : '',
    amount_usd: closing,
    balance_after_usd: target,
  });

  return rows.reverse();   // newest first, as the API returns them
}

/**
 * One scenario's whole world. Mutable on purpose: the backend edits `keys` in
 * place so a key you create in mock mode is still there when you switch tabs.
 */
export function build(scenarioId) {
  const shape = SHAPES[scenarioId] || SHAPES.active;
  const random = rng(seedOf(scenarioId));
  const keys = makeKeys(random, shape.keys);
  const usage = makeUsage(random, shape, keys);
  const self = {
    registered_name: 'Acme Media Ltd',
    is_active: true,
    jobs: usage.length,
    total_billed_usd: Number(
      usage.reduce((sum, row) => sum + row.billed_amount_usd, 0).toFixed(2)),
    balance_usd: shape.balance,
  };

  return {
    scenario: scenarioId,
    shape,
    account: shape.account === false ? null : {
      registered_name: self.registered_name,
      is_active: true,
      balance_usd: shape.balance,
      balance_warning: Boolean(shape.warning),
    },
    keys,
    usage,
    accounts: shape.admin ? makeAccounts(random, self) : [],
    transactions: makeTransactions(random, shape.balance || 240),
    // The plaintext of every key minted this session, so the "save this key"
    // modal has something real to copy rather than a literal placeholder.
    minted: new Map(),
    random,
    hex: (n) => hex(random, n),
  };
}
