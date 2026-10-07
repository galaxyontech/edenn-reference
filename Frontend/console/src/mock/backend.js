/**
 * The API, in the browser.
 *
 * `src/lib/api.js` funnels every call through one `request()`, which is the
 * only reason this file can be small: match the method and path, answer in the
 * same `{ ok, status, code, detail, data }` shape, and the twelve call sites
 * upstream never learn the difference.
 *
 * Answers here are held to the real contract, including the parts that are
 * inconvenient:
 *
 *   * `usage` pages, and reports `page.has_more` honestly — `api.usageAll()`
 *     walks five pages and gives up, and the truncation notices that hang off
 *     that are UI you cannot otherwise see without a 5,000-job account.
 *   * A missing account is `404 signup_required`, not an empty balance. The
 *     signup screen is reached by that exact code.
 *   * Admin routes 403 unless the scenario is an admin one, because that is
 *     precisely how `page.jsx` decides whether to draw the Admin tab.
 *   * A dead backend is `status: 0`, matching what `request()` synthesises for
 *     a network-level failure — not a 500, which would be an *answer*.
 */
import { build } from './fixtures';

/**
 * One world per scenario, kept for the life of the tab.
 *
 * Mutations have to stick: creating a key and watching it vanish on the next
 * list would be a worse lie than having no mock at all.
 */
const worlds = new Map();

function world(scenarioId) {
  if (!worlds.has(scenarioId)) worlds.set(scenarioId, build(scenarioId));
  return worlds.get(scenarioId);
}

const ok = (data) => ({ ok: true, status: 200, code: '', detail: '', data });

const fail = (status, code, detail) => ({
  ok: false, status, code, detail, data: { code, detail },
});

/** What `request()` returns when fetch itself throws. */
const unreachable = () => ({
  ok: false,
  status: 0,
  code: 'network_error',
  detail: 'Could not reach the server.',
  data: null,
});

function usageTotals(rows) {
  return {
    jobs: rows.length,
    total_billed_usd: Number(
      rows.reduce((sum, row) => sum + Number(row.billed_amount_usd || 0), 0).toFixed(2)),
  };
}

/**
 * Route the call. `path` is everything after the origin, query included.
 *
 * Handlers are matched on method plus a path pattern; `:name` captures one
 * segment. That is more machinery than a chain of ifs needs today, but the key
 * routes already differ only by method on the same path, and reading
 * `DELETE /api/v1/account/keys/:prefix` off the left column is the point.
 */
const ROUTES = [
  ['GET', '/api/v1/account/balance', (w) => (
    w.account
      ? ok({
        registered_name: w.account.registered_name,
        is_active: w.account.is_active,
        balance_usd: w.account.balance_usd,
        balance_warning: w.account.balance_warning,
      })
      : fail(404, 'signup_required', 'No account for this phone number yet.')
  )],

  ['GET', '/api/v1/account/usage', (w, { query }) => {
    if (!w.account) return fail(404, 'signup_required', 'No account yet.');
    const limit = Math.min(Number(query.get('limit') || 100), 1000);
    const offset = Number(query.get('offset') || 0);
    const page = w.usage.slice(offset, offset + limit);
    return ok({
      rows: page,
      totals: usageTotals(w.usage),
      page: {
        limit,
        offset,
        returned: page.length,
        has_more: offset + limit < w.usage.length,
      },
    });
  }],

  ['GET', '/api/v1/account/keys', (w, { query }) => {
    if (!w.account) return fail(404, 'signup_required', 'No account yet.');
    const includeRevoked = query.get('include_revoked') === 'true';
    return ok({
      keys: w.keys.filter((key) => includeRevoked || key.is_active),
    });
  }],

  ['POST', '/api/v1/account/keys', (w, { body }) => {
    if (w.keys.filter((k) => k.is_active).length >= 20) {
      return fail(409, 'key_limit', 'This account already has 20 active keys.');
    }
    const prefix = `sk-${w.hex(9)}`;
    const suffix = w.hex(4);
    const row = {
      name: (body?.name || '').trim() || 'Unnamed',
      key_prefix: prefix,
      key_suffix: suffix,
      is_active: true,
      created_at: new Date().toISOString(),
      last_used_at: null,
      revoked_at: null,
    };
    w.keys.unshift(row);
    // The plaintext exists exactly once, here, and is never listed again —
    // same as the real thing, so the "shown only once" modal is telling the
    // truth even in mock mode.
    const secret = `${prefix}${w.hex(28)}${suffix}`;
    w.minted.set(prefix, secret);
    return ok({ ...row, api_key: secret });
  }],

  ['PATCH', '/api/v1/account/keys/:prefix', (w, { params, body }) => {
    const row = w.keys.find((key) => key.key_prefix === params.prefix);
    if (!row) return fail(404, 'not_found', 'No such key.');
    if (!row.is_active) return fail(409, 'revoked', 'A revoked key cannot be renamed.');
    row.name = (body?.name || '').trim() || row.name;
    return ok(row);
  }],

  ['DELETE', '/api/v1/account/keys/:prefix', (w, { params }) => {
    const row = w.keys.find((key) => key.key_prefix === params.prefix);
    if (!row) return fail(404, 'not_found', 'No such key.');
    row.is_active = false;
    row.revoked_at = new Date().toISOString();
    return ok({ revoked: true });
  }],

  ['POST', '/api/v1/signup/verified', (w, { body }) => {
    w.account = {
      registered_name: (body?.registered_name || '').trim() || 'Unnamed account',
      is_active: true,
      balance_usd: 0,
      balance_warning: false,
    };
    return ok({ account_id: `acct_${w.hex(12)}`, registered_name: w.account.registered_name });
  }],

  ['GET', '/api/v1/admin/usage-summary', (w, { query }) => {
    if (!w.shape.admin) return fail(403, 'forbidden', 'Admin scope required.');
    const max = Number(query.get('max_accounts') || 0);
    const scanned = max > 0 ? Math.min(max, w.accounts.length) : w.accounts.length;
    const accounts = w.accounts.slice(0, scanned);
    return ok({
      accounts,
      accounts_scanned: scanned,
      truncated: scanned < w.accounts.length,
      totals: {
        accounts: accounts.length,
        jobs: accounts.reduce((sum, a) => sum + a.jobs, 0),
        total_billed_usd: Number(
          accounts.reduce((sum, a) => sum + a.total_billed_usd, 0).toFixed(2)),
        balance_usd: Number(
          accounts.reduce((sum, a) => sum + a.balance_usd, 0).toFixed(2)),
      },
    });
  }],

  ['GET', '/api/v1/admin/keys', (w) => {
    if (!w.shape.admin) return fail(403, 'forbidden', 'Admin scope required.');
    return ok({
      keys: w.accounts.flatMap((account, i) => w.keys.slice(0, (i % 3) + 1).map((key) => ({
        key_prefix: key.key_prefix,
        user_id: account.account_id,
        note: key.name,
        created_at: key.created_at,
        last_used_at: key.last_used_at,
        is_active: key.is_active,
      }))),
    });
  }],

  ['GET', '/api/v1/admin/usage', (w) => {
    if (!w.shape.admin) return fail(403, 'forbidden', 'Admin scope required.');
    // The admin endpoint hands back raw ledger entities: micros, not dollars.
    // `billedUsd()` exists to reconcile the two, and it only gets exercised if
    // the mock keeps them different.
    return ok({
      rows: w.usage.slice(0, 60).map((row) => ({
        ...row,
        billed_amount_usd: undefined,
        billed_amount_micros: Math.round(row.billed_amount_usd * 1e6),
      })),
    });
  }],

  ['GET', '/api/v1/admin/accounts/:id/transactions', (w) => {
    if (!w.shape.admin) return fail(403, 'forbidden', 'Admin scope required.');
    return ok({ transactions: w.transactions });
  }],
];

function match(method, pathname) {
  for (const [verb, pattern, handler] of ROUTES) {
    if (verb !== method) continue;
    const wanted = pattern.split('/');
    const got = pathname.split('/');
    if (wanted.length !== got.length) continue;
    const params = {};
    let hit = true;
    for (let i = 0; i < wanted.length; i += 1) {
      if (wanted[i].startsWith(':')) params[wanted[i].slice(1)] = decodeURIComponent(got[i]);
      else if (wanted[i] !== got[i]) { hit = false; break; }
    }
    if (hit) return { handler, params };
  }
  return null;
}

export function handle(scenarioId, path, { method = 'GET', body } = {}) {
  const w = world(scenarioId);
  // "Backend down" is the whole point of that scenario, and it has to take out
  // the account routes only — the console still has to decide it is not an
  // admin, and a 403 is an answer a dead server cannot give.
  if (w.shape.down && !path.startsWith('/api/v1/admin/')) return unreachable();

  const [pathname, search = ''] = path.split('?');
  const found = match(method, pathname);
  if (!found) return fail(404, 'not_found', `No mock route for ${method} ${pathname}.`);
  return found.handler(w, {
    query: new URLSearchParams(search),
    body,
    params: found.params,
  });
}

/** Drop a scenario's accumulated edits — the switcher's "reset" affordance. */
export function reset(scenarioId) {
  if (scenarioId) worlds.delete(scenarioId);
  else worlds.clear();
}
