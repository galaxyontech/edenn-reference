/**
 * Firebase phone sign-in.
 *
 * The page itself makes no external requests — the SDK is bundled, not loaded
 * from a CDN. But phone verification genuinely has to reach Google
 * (identitytoolkit + reCAPTCHA), and from mainland China those hosts are
 * unreachable.
 *
 * Timeouts are deliberately asymmetric, because the two waits are different
 * things:
 *
 * * **Loading the SDK** is pure I/O against our own origin, so a hard 8s cap is
 *   safe — nothing a human does can make it legitimately slower.
 * * **Sending the code** may include a reCAPTCHA image challenge. A human
 *   picking out traffic lights takes fifteen, thirty, sixty seconds, and
 *   aborting them mid-puzzle produces "sign-in failed" for a flow that was working.
 *   So there is no hard cap here at all: after `SLOW_HINT_MS` the caller is
 *   told it is taking a while and offered the manual route, and the promise is
 *   left to finish. We cannot reliably tell "blocked" from "still thinking",
 *   so we stop pretending we can and let the person decide.
 */

import { apiBase } from './api';

const SDK_LOAD_MS = 8000;
export const SLOW_HINT_MS = 10000;

export class NetworkBlocked extends Error {
  constructor(step) {
    super(`firebase step timed out: ${step}`);
    this.name = 'NetworkBlocked';
    this.step = step;
  }
}

export class NotConfigured extends Error {
  constructor() {
    super('the API has no Firebase configuration');
    this.name = 'NotConfigured';
  }
}

// Build-time values are a local-development fallback only. In every deployment
// the config comes from the API, so it cannot drift from the project id the
// backend verifies tokens against.
const ENV_CONFIG = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
};

function withLimit(promise, step, ms) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new NetworkBlocked(step)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

let configPromise = null;

/*
  Mock mode replaces this file's six moving parts — config, the user
  subscription, the current token, sign-out, and the two halves of phone
  verification — and touches nothing else. The Firebase SDK is never imported,
  so `next dev` needs no project, no Blaze plan, and no SMS.

  Each guard tests `process.env` directly rather than an imported flag, and
  spells out its own `import()` rather than sharing a helper: a constant
  condition is what lets webpack skip the dead branch while it is still
  building the module graph, so a normal build emits no chunk for the mock at
  all. A shared helper would be reachable code and would defeat that. See the
  matching note in `api.js`.
*/

/** The project's public client config, or null when the API has none set. */
export function consoleConfig() {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    return import('../mock/session').then((m) => m.mockConfig());
  }
  if (!configPromise) {
    configPromise = withLimit(fetchConfig(), 'config', SDK_LOAD_MS);
    configPromise.catch(() => { configPromise = null; });
  }
  return configPromise;
}

async function fetchConfig() {
  try {
    const response = await fetch(`${apiBase()}/api/v1/console/config`);
    if (response.ok) {
      const body = await response.json();
      if (body.configured && body.firebase) return body.firebase;
      return null;  // reachable and deliberately unconfigured
    }
  } catch {
    // Falls through to the env fallback: running `next dev` without a backend
    // should not be a dead end.
  }
  return ENV_CONFIG.apiKey && ENV_CONFIG.projectId ? ENV_CONFIG : null;
}

let authPromise = null;

async function loadAuth() {
  const config = await consoleConfig();
  if (!config) throw new NotConfigured();
  const [{ initializeApp, getApps }, auth] = await Promise.all([
    import('firebase/app'),
    import('firebase/auth'),
  ]);
  const app = getApps().length ? getApps()[0] : initializeApp(config);
  const instance = auth.getAuth(app);
  // Stated rather than inherited. browserLocalPersistence is already the web
  // default, but the default is a property of the SDK version, not of this
  // application — and "does closing the browser sign me out?" is a question
  // whose answer should be visible in our own source, not looked up in a
  // changelog. Sessions survive a browser restart and end only at sign-out or
  // when the refresh token is revoked server-side.
  await auth.setPersistence(instance, auth.browserLocalPersistence);
  instance.useDeviceLanguage();
  return { auth, instance };
}

export function getAuthModule() {
  if (!authPromise) {
    authPromise = withLimit(loadAuth(), 'sdk', SDK_LOAD_MS);
    // A failed load must not be cached, or one blocked attempt would poison
    // every retry for the life of the tab.
    authPromise.catch(() => { authPromise = null; });
  }
  return authPromise;
}

let verifier = null;

/**
 * A fresh verifier per attempt, on a container we emptied ourselves.
 *
 * `clear()` releases Firebase's handle but leaves the widget's markup in the
 * DOM, and grecaptcha refuses to render twice into the same element — which is
 * why a second sign-in attempt used to fail instantly with an error that had
 * nothing to do with the phone number.
 */
async function freshVerifier(auth, instance, containerId, visible) {
  if (verifier) {
    try { verifier.clear(); } catch { /* already gone */ }
    verifier = null;
  }
  const container = document.getElementById(containerId);
  if (container) container.innerHTML = '';
  verifier = new auth.RecaptchaVerifier(instance, containerId, {
    size: visible ? 'normal' : 'invisible',
  });
  if (visible) await verifier.render();
  return verifier;
}

/** Codes that mean "the browser could not produce a usable reCAPTCHA token". */
const CAPTCHA_CODES = new Set([
  'auth/invalid-app-credential',
  'auth/captcha-check-failed',
  'auth/internal-error',
]);

export function isCaptchaFailure(error) {
  return CAPTCHA_CODES.has(String(error?.code || ''));
}

/**
 * Sends the SMS. Returns a confirmation handle whose `confirm(code)` yields the
 * ID token. `onSlow` fires once if Google has not answered within
 * SLOW_HINT_MS — a hint, not an abort.
 */
export async function sendCode(phoneNumber, containerId, onSlow, options = {}) {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const m = await import('../mock/session');
    return m.mockSendCode(phoneNumber, containerId, onSlow);
  }
  const { auth, instance } = await getAuthModule();
  const captcha = await freshVerifier(auth, instance, containerId,
                                      Boolean(options.visible));
  const hint = onSlow ? setTimeout(onSlow, SLOW_HINT_MS) : null;
  try {
    return await auth.signInWithPhoneNumber(instance, phoneNumber, captcha);
  } catch (error) {
    // A spent verifier cannot be reused; the next attempt builds its own.
    try { captcha.clear(); } catch { /* already gone */ }
    verifier = null;
    throw error;
  } finally {
    if (hint) clearTimeout(hint);
  }
}

export async function confirmCode(confirmation, code) {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const m = await import('../mock/session');
    return m.mockConfirmCode(confirmation, code);
  }
  const credential = await confirmation.confirm(code);
  return credential.user.getIdToken();
}

export async function currentToken(forceRefresh = false) {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const m = await import('../mock/session');
    return m.mockCurrentToken();
  }
  const { instance } = await getAuthModule();
  const user = instance.currentUser;
  return user ? user.getIdToken(forceRefresh) : null;
}

/**
 * onIdTokenChanged, not onAuthStateChanged: ID tokens expire after an hour and
 * the SDK refreshes them silently. Watching auth *state* would miss every
 * refresh, and the console would start answering 401 an hour into a session.
 */
export async function watchUser(callback) {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const m = await import('../mock/session');
    return m.mockWatchUser(callback);
  }
  const { auth, instance } = await getAuthModule();
  return auth.onIdTokenChanged(instance, callback);
}

export async function signOut() {
  if (process.env.NEXT_PUBLIC_MOCK === '1') {
    const m = await import('../mock/session');
    return m.mockSignOut();
  }
  const { auth, instance } = await getAuthModule();
  await auth.signOut(instance);
}
