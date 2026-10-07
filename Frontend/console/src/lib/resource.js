/**
 * A fetch in one of exactly three states: `loading`, `ready`, `failed`.
 *
 * Every screen in this console used to load with the same shape:
 *
 *     const [thing, setThing] = useState(null);
 *     if (result.ok) setThing(result.data);
 *     setError(result.ok ? '' : result.detail);
 *
 * which leaves `thing` at `null` when the request fails — and `null` was also
 * how each screen decided to draw its loading skeleton. So a failed request
 * rendered an error line *and* a skeleton that would pulse forever, and the
 * only way out was reloading the browser. Two values cannot represent three
 * states; this is the third.
 *
 * `detail` carries the server's own message when there is one, because
 * "insufficient balance" and "we couldn't reach the server" are different
 * problems with different fixes and the customer is the one who has to tell
 * them apart.
 */

export const LOADING = { state: 'loading', data: null, detail: '' };

/** Fold an `api` result into a resource. `fallback` covers a bodyless error. */
export function settle(result, fallback) {
  if (result.ok) return { state: 'ready', data: result.data, detail: '' };
  return { state: 'failed', data: null, detail: result.detail || fallback };
}
