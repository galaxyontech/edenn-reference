/**
 * Time bucketing for the spend chart.
 *
 * Buckets are built from a generated calendar and then filled, never from the
 * rows alone: a week with no jobs has to appear as an empty slot. Dropping it
 * would compress an idle fortnight into a continuous run and make the chart lie
 * about the shape of the account's usage.
 */
import { billedUsd } from './format';

export const GRAINS = [
  { id: 'day', label: 'Day', slots: 30 },
  { id: 'week', label: 'Week', slots: 12 },
  { id: 'month', label: 'Month', slots: 12 },
  { id: 'year', label: 'Year', slots: 3 },
];

const pad = (n) => String(n).padStart(2, '0');

/** Monday of that date's week, in local time. */
function weekStart(date) {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  // getDay() is 0 for Sunday; shift so Monday starts the week.
  copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
  return copy;
}

function keyOf(date, grain) {
  const d = new Date(date);
  if (grain === 'year') return String(d.getFullYear());
  if (grain === 'month') return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
  const day = grain === 'week' ? weekStart(d) : d;
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function labelOf(key, grain) {
  if (grain === 'year') return key;
  if (grain === 'month') return key.slice(2);      // 26-08
  return key.slice(5);                              // 08-03
}

/**
 * First instant the grain's window covers.
 *
 * Everything on the usage page filters through this, so the chart's total, the
 * per-product panels and the per-model table all describe the same span. They
 * did not at first — the chart windowed and the panels summed everything
 * fetched — and a page showing $27.71 spent above $206.28 spent is a page
 * nobody can trust, however correct each number is on its own.
 */
export function windowStart(grain, now = new Date()) {
  const meta = GRAINS.find((g) => g.id === grain) || GRAINS[0];
  const back = meta.slots - 1;
  let from = new Date(now);
  from.setHours(0, 0, 0, 0);
  if (grain === 'year') from.setFullYear(from.getFullYear() - back, 0, 1);
  else if (grain === 'month') from.setMonth(from.getMonth() - back, 1);
  else if (grain === 'week') {
    from.setDate(from.getDate() - back * 7);
    from = weekStart(from);
  } else from.setDate(from.getDate() - back);
  return from;
}

/** The rows the current grain's window covers. */
export function inWindow(rows, grain, now = new Date()) {
  const from = windowStart(grain, now).getTime();
  return (rows || []).filter((row) => {
    const at = new Date(row?.timestamp_utc).getTime();
    return Number.isFinite(at) && at >= from;
  });
}

/** Empty buckets covering the window, oldest first. */
function calendar(grain, slots, now) {
  const keys = [];
  for (let i = slots - 1; i >= 0; i -= 1) {
    const d = new Date(now);
    if (grain === 'year') d.setFullYear(d.getFullYear() - i);
    else if (grain === 'month') d.setMonth(d.getMonth() - i);
    else if (grain === 'week') d.setDate(d.getDate() - i * 7);
    else d.setDate(d.getDate() - i);
    keys.push(keyOf(d, grain));
  }
  return keys;
}

/**
 * `{ buckets, series, total }` for the chart.
 *
 * `series` is the set of stack members — one per API key when `byKey`, or a
 * single total series otherwise. Members are ordered by spend so the colour a
 * key gets is stable within a render; `SpendChart` is what pins colour to the
 * key itself across renders.
 */
export function bucketSpend(rows, { grain = 'day', byKey = false, now = new Date() } = {}) {
  const meta = GRAINS.find((g) => g.id === grain) || GRAINS[0];
  const keys = calendar(grain, meta.slots, now);
  const index = new Map(keys.map((key, at) => [key, at]));
  const buckets = keys.map((key) => ({
    key, label: labelOf(key, grain), total: 0, parts: new Map(),
  }));
  const totals = new Map();

  for (const row of rows || []) {
    const stamp = row?.timestamp_utc;
    if (!stamp) continue;
    const when = new Date(stamp);
    if (Number.isNaN(when.getTime())) continue;
    const at = index.get(keyOf(when, grain));
    if (at === undefined) continue;          // outside the window
    const amount = billedUsd(row);
    const member = byKey ? (row.key_prefix || 'unattributed') : 'total';
    const bucket = buckets[at];
    bucket.total += amount;
    bucket.parts.set(member, (bucket.parts.get(member) || 0) + amount);
    totals.set(member, (totals.get(member) || 0) + amount);
  }

  const series = [...totals.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([id, amount]) => ({ id, amount }));

  return {
    buckets,
    series,
    total: buckets.reduce((sum, bucket) => sum + bucket.total, 0),
  };
}
