export const usd = (value) =>
  `$${Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

/**
 * Dollars for a usage row.
 *
 * The customer endpoint projects `billed_amount_usd`; the admin endpoint hands
 * back raw ledger entities, which only carry `billed_amount_micros`. One helper
 * so the two tables can never disagree about the same job.
 */
export function billedUsd(row) {
  const explicit = row?.billed_amount_usd;
  if (explicit !== undefined && explicit !== null && explicit !== '') {
    return Number(explicit);
  }
  const micros = Number(row?.billed_amount_micros ?? 0);
  return Number.isFinite(micros) ? micros / 1e6 : 0;
}

export const count = (value) => Number(value || 0).toLocaleString('en-US');

/**
 * Badge tone for a usage row's status.
 *
 * This existed inline as `status === 'success' ? 'live' : 'bad'`, and the API
 * has never written `"success"` — it writes `"completed"` and `"failed"`. So
 * every successful job rendered in the red "something is wrong" style, on the
 * one screen people open to check that nothing is. Red has to stay rare to
 * keep meaning anything.
 */
/**
 * Which billed product a usage row belongs to.
 *
 * Mirrors `ENDPOINT_PRODUCTS` in `billing/engine.py` — that map is what decides
 * the price, so this list has to track it. Substring matching was the obvious
 * shortcut and it is wrong: `/api/v1/jobs/multi-image` contains neither
 * "video" nor a stable product word, and a row that matched nothing would
 * silently vanish from both product panels while still counting in the total.
 * An unknown endpoint returns null and is reported as such.
 */
const ENDPOINT_PRODUCTS = {
  '/api/v1/jobs/video': 'video_music',
  '/api/v1/jobs/async_video_music_gen': 'video_music',
  '/api/v2/jobs/video-music': 'video_music',
  '/api/v1/jobs/multi-image': 'image_music',
  '/api/v1/jobs/async_multi-image': 'image_music',
  '/api/v2/jobs/multi-image-music': 'image_music',
};

export const productOf = (endpoint) =>
  ENDPOINT_PRODUCTS[String(endpoint || '').split('?')[0]] || null;

/** Jobs / billed / delivered-seconds per product, plus anything unrecognised. */
export function productTotals(rows) {
  const empty = () => ({ jobs: 0, billed: 0, seconds: 0 });
  const out = { video_music: empty(), image_music: empty(), unknown: empty() };
  for (const row of rows || []) {
    const bucket = out[productOf(row.endpoint) || 'unknown'];
    bucket.jobs += 1;
    bucket.billed += billedUsd(row);
    bucket.seconds += Number(row.video_duration_s || 0) || 0;
  }
  return out;
}

export function statusTone(status) {
  const value = String(status || '').toLowerCase();
  if (value === 'completed' || value === 'success') return 'live';
  if (value === 'failed' || value === 'error') return 'bad';
  return 'dead';  // queued / processing / canceled: neither good nor bad news
}

export function dateTime(iso) {
  if (!iso) return '—';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return String(iso);
  return parsed.toLocaleString('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

export function dayKey(iso) {
  return String(iso || '').slice(0, 10);
}

export function shortDay(key) {
  return key ? key.slice(5) : '';
}

/** Relative age: "3d ago" answers "is this key dead?" faster than a date. */
export function since(iso) {
  if (!iso) return 'Never used';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return String(iso);
  const seconds = Math.max(0, (Date.now() - parsed.getTime()) / 1000);
  if (seconds < 90) return 'just now';
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 30) return `${Math.round(days)}d ago`;
  return parsed.toLocaleDateString('en-CA');
}

/**
 * Daily spend buckets covering the whole window, gaps included — a waveform
 * with the silent days removed would misread as continuous activity.
 */
export function dailySpend(rows, days = 30) {
  const buckets = new Map();
  const today = new Date();
  for (let i = days - 1; i >= 0; i -= 1) {
    const day = new Date(today);
    day.setDate(today.getDate() - i);
    buckets.set(day.toISOString().slice(0, 10), 0);
  }
  for (const row of rows || []) {
    const key = dayKey(row.timestamp_utc);
    if (buckets.has(key)) {
      buckets.set(key, buckets.get(key) + billedUsd(row));
    }
  }
  return [...buckets.entries()].map(([day, amount]) => ({ day, amount }));
}
