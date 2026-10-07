/**
 * Sign-in, without Google.
 *
 * Phone auth is the one part of this console that genuinely cannot be
 * exercised locally: it needs a Blaze-plan Firebase project, an authorised
 * domain, a reCAPTCHA that will not run on `localhost` for everyone, and a real
 * SMS per attempt. So the sign-in screen — five error states, a slow-network
 * hint, a visible-captcha fallback — was the least reviewable screen in the
 * product despite being the first one every customer meets.
 *
 * The substitute keeps the same seams and the same shapes: a `user` with a
 * `getIdToken()`, an `onIdTokenChanged`-style subscription, and errors carrying
 * Firebase's own `code` strings so `describe()` in SignIn.jsx resolves them the
 * way it will in production.
 */
import { delay, scenario } from './index';

/**
 * Not a Firebase config — a marker.
 *
 * `page.jsx` only asks whether config exists; a truthy object is enough to get
 * past the "not connected" gate, and putting plausible-looking keys here would
 * only invite somebody to wonder whether they were real.
 */
const CONFIG = { projectId: 'edenn-mock', apiKey: 'mock', mock: true };

const USER = {
  uid: 'mock-uid-000000000001',
  phoneNumber: '+8613800000000',
  email: null,
  photoURL: '',
  displayName: '',
  getIdToken: async () => `mock-token-${scenario()}`,
};

let current = null;
let started = false;
const watchers = new Set();

function announce() {
  for (const watcher of watchers) watcher(current);
}

/** Signed in for every scenario except the one that exists to show sign-in. */
function initial() {
  return scenario() === 'signed-out' ? null : USER;
}

export async function mockConfig() {
  await delay(120);
  return CONFIG;
}

export async function mockWatchUser(callback) {
  if (!started) { started = true; current = initial(); }
  watchers.add(callback);
  // Asynchronously, like the real listener: a synchronous first call would
  // fire during render and hide any ordering bug that only bites in production.
  delay(80).then(() => callback(current));
  return () => watchers.delete(callback);
}

export async function mockCurrentToken() {
  return current ? current.getIdToken() : null;
}

export async function mockSignOut() {
  await delay(120);
  current = null;
  announce();
}

/**
 * "Sends" the code. Any well-formed number is accepted; the error paths are
 * reached by specific inputs rather than by luck, so they can be demonstrated:
 *
 *   * a number ending `0000` → `auth/invalid-phone-number`
 *   * a number ending `9999` → the slow-network hint, then success
 */
export async function mockSendCode(phoneNumber, containerId, onSlow) {
  const number = String(phoneNumber || '');
  if (number.endsWith('0000')) {
    await delay(400);
    const error = new Error('mock: invalid phone number');
    error.code = 'auth/invalid-phone-number';
    throw error;
  }
  if (number.endsWith('9999')) {
    if (onSlow) onSlow();
    await delay(2600);
  } else {
    await delay(700);
  }
  return { mock: true, phoneNumber: number, sentAt: Date.now() };
}

/** Any six digits work, except `000000`, which is the wrong-code path. */
export async function mockConfirmCode(confirmation, code) {
  await delay(500);
  if (!/^\d{6}$/.test(String(code)) || code === '000000') {
    const error = new Error('mock: bad code');
    error.code = 'auth/invalid-verification-code';
    throw error;
  }
  current = USER;
  announce();
  return current.getIdToken();
}
