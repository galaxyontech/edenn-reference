/**
 * API client. Every call carries the Firebase ID token; nothing else.
 *
 * The admin secret is never present in this bundle — admin scope comes from a
 * server-side allowlist keyed on the signed-in uid/phone, so an admin and a
 * customer send byte-identical requests and the server decides.
 */

const CONFIGURED_ORIGIN = (process.env.NEXT_PUBLIC_API_ORIGIN || '').replace(/\/$/, '');

/**
 * Same-origin when the console is served by the API (the Azure deployment),
 * absolute otherwise (Firebase Hosting). Deciding at runtime instead of build
 * time is what lets one export serve both targets.
 */
export function apiBase() {
  if (typeof window === 'undefined') return CONFIGURED_ORIGIN;
  if (!CONFIGURED_ORIGIN) return '';
  return window.location.origin === CONFIGURED_ORIGIN ? '' : CONFIGURED_ORIGIN;
}

async function request(path, { token, method = 'GET', body } = {}) {
  /*
    The mock seam, and the only one on this side of the app.

    Written as a literal `process.env` test rather than an imported flag so the
    condition is a constant *in this file*: Next inlines the value, the branch
    folds to `if (false)`, and the `import()` inside it is unreachable — so a
    normal build emits no chunk for the mock at all. Importing the flag from
    `../mock` would read better and would leave that guarantee up to
    cross-module constant folding.
  */
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const [{ handle }, { delay, scenario }] = await Promise.all([
      import('../mock/backend'), import('../mock'),
    ]);
    await delay();
    return handle(scenario(), path, { method, body });
  }

  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(apiBase() + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // A network-level failure is not an API answer; callers must not read it
    // as "no account" or "no keys".
    return { ok: false, status: 0, code: 'network_error', data: null, cause };
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return {
    ok: response.ok,
    status: response.status,
    code: (data && data.code) || '',
    detail: (data && data.detail) || '',
    data,
  };
}

const USAGE_PAGE = 1000;      // the endpoint's own ceiling
const USAGE_MAX_PAGES = 5;

export const api = {
  balance: (token) => request('/api/v1/account/balance', { token }),
  usage: (token, query = '') =>
    request(`/api/v1/account/usage${query}`, { token }),

  /**
   * Every usage row the chart can reach, paged.
   *
   * `totals` from the server already covers the whole filtered set on every
   * page, so summary numbers are exact regardless of how far we page. The rows
   * are what the by-bucket chart needs, and a single 1000-row page silently
   * drops everything older on a busy account — a chart that quietly
   * under-reports is worse than one that says it stopped counting.
   * `truncated` is that admission; the UI must surface it.
   */
  usageAll: async (token) => {
    const rows = [];
    let last = null;
    for (let page = 0; page < USAGE_MAX_PAGES; page += 1) {
      const query = `?limit=${USAGE_PAGE}&offset=${page * USAGE_PAGE}`;
      last = await request(`/api/v1/account/usage${query}`, { token });
      if (!last.ok) return last;
      rows.push(...(last.data?.rows || []));
      if (!last.data?.page?.has_more) break;
    }
    return {
      ...last,
      data: {
        ...last.data,
        rows,
        truncated: Boolean(last.data?.page?.has_more),
      },
    };
  },
  keys: (token, includeRevoked = false) =>
    request(`/api/v1/account/keys?include_revoked=${includeRevoked}`, { token }),
  createKey: (token, name) =>
    request('/api/v1/account/keys', { token, method: 'POST', body: { name } }),
  renameKey: (token, prefix, name) =>
    request(`/api/v1/account/keys/${encodeURIComponent(prefix)}`, {
      token, method: 'PATCH', body: { name },
    }),
  revokeKey: (token, prefix) =>
    request(`/api/v1/account/keys/${encodeURIComponent(prefix)}`, {
      token, method: 'DELETE',
    }),
  signupVerified: (token, registeredName) =>
    request('/api/v1/signup/verified', {
      token, method: 'POST', body: { registered_name: registeredName },
    }),

  adminSummary: (token, query = '') =>
    request(`/api/v1/admin/usage-summary${query}`, { token }),
  adminKeys: (token) => request('/api/v1/admin/keys', { token }),
  adminUsage: (token, accountId) =>
    request(`/api/v1/admin/usage?user_id=${encodeURIComponent(accountId)}`, { token }),
  adminTransactions: (token, accountId) =>
    request(
      `/api/v1/admin/accounts/${encodeURIComponent(accountId)}/transactions`,
      { token },
    ),
};
