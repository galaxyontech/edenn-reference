'use client';

import { useEffect, useRef, useState } from 'react';
import { NotConfigured, confirmCode, isCaptchaFailure, sendCode } from '../lib/firebase';
import PhoneField, { e164, prettyE164 } from './PhoneField';
import { DEFAULT_COUNTRY } from '../data/countries';

const RECAPTCHA_ID = 'edenn-recaptcha';

/**
 * Phone sign-in.
 *
 * Three things here are deliberate and easy to get wrong:
 *
 * 1. **The raw Firebase error code is shown.** Backend token verification is
 *    opaque on purpose — a forger must not be able to bisect our checks. A
 *    client-side SDK error is the opposite situation: it is almost always a
 *    project-configuration mistake (`auth/unauthorized-domain`,
 *    `auth/billing-not-enabled`), and hiding the code turns a ten-second fix
 *    into an evening of guessing.
 * 2. **Nothing aborts on a timer.** reCAPTCHA can show an image challenge, and
 *    a person solving one legitimately takes longer than any timeout worth
 *    setting. After ten seconds we say it is slow and offer the manual route;
 *    we do not cancel the attempt out from under them.
 * 3. **The country code is picked, not typed.** See PhoneField.
 */
export default function SignIn({ onToken }) {
  const [country, setCountry] = useState(DEFAULT_COUNTRY);
  const [national, setNational] = useState('');
  const [code, setCode] = useState('');
  const [confirmation, setConfirmation] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [slow, setSlow] = useState(false);
  const [manual, setManual] = useState(false);
  // Invisible reCAPTCHA is silently defeated by tracking protection and a long
  // tail of extensions; the visible checkbox survives all of it. We try the
  // frictionless one first and fall back rather than making everyone click.
  const [visibleCaptcha, setVisibleCaptcha] = useState(false);
  const codeRef = useRef(null);
  const number = e164(country, national);

  useEffect(() => {
    if (confirmation && codeRef.current) codeRef.current.focus();
  }, [confirmation]);

  if (manual) {
    return (
      <Card lede="SMS verification cannot reach this network.">
        <p className="note">
          The code is sent by Google, and this network cannot reach Google. This is not a
          problem with your phone number. Send us your phone number or email and we will
          open the account for you.
        </p>
        <p className="note" style={{ marginTop: 12 }}>
          <a href="mailto:support@edenn.ai">support@edenn.ai</a>
        </p>
        <button className="btn block" style={{ marginTop: 16 }}
                onClick={() => { setManual(false); setError(null); setSlow(false); }}>
          Try again
        </button>
      </Card>
    );
  }

  async function handleSend(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSlow(false);
    try {
      setConfirmation(await sendCode(number, RECAPTCHA_ID,
                                     () => setSlow(true),
                                     { visible: visibleCaptcha }));
    } catch (caught) {
      if (isCaptchaFailure(caught) && !visibleCaptcha) {
        setVisibleCaptcha(true);
        setError({
          message: 'Invisible reCAPTCHA did not pass. Switched to the visible checkbox.',
          code: String(caught?.code || ''),
          fix: 'Tick "I am not a robot" below, then send again.',
        });
      } else {
        setError(describe(caught));
      }
    } finally {
      setBusy(false);
      setSlow(false);
    }
  }

  async function handleConfirm(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onToken(await confirmCode(confirmation, code.trim()));
    } catch (caught) {
      setError(describe(caught));
      setBusy(false);
    }
  }

  return (
    <Card lede={confirmation
      ? `Code sent to ${prettyE164(country, national)}`
      : 'Sign in with your phone number to manage balance, usage and API keys.'}>
      {confirmation ? (
        <form onSubmit={handleConfirm}>
          <div className="field">
            <label htmlFor="code">Verification code</label>
            <input id="code" ref={codeRef} className="code" inputMode="numeric"
                   autoComplete="one-time-code" maxLength={6} value={code}
                   onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
          </div>
          <Problem error={error} />
          <button className="btn primary block" type="submit"
                  disabled={busy || code.length < 6}>
            {busy ? 'Verifying…' : 'Continue'}
          </button>
          <button className="btn ghost block" type="button" style={{ marginTop: 8 }}
                  onClick={() => { setConfirmation(null); setCode(''); setError(null); }}>
            Use a different number
          </button>
        </form>
      ) : (
        <form onSubmit={handleSend}>
          <div className="field">
            <label htmlFor="phone">Phone number</label>
            <PhoneField
              country={country} national={national} disabled={busy} autoFocus
              onChange={(next) => {
                setCountry(next.country);
                setNational(next.national);
              }}
            />
            <p className="note dim num" style={{ minHeight: 18 }}>
              {number ? `Sending to ${prettyE164(country, national)}` : ''}
            </p>
          </div>
          <Problem error={error} />
          {slow ? (
            <p className="note" style={{ marginTop: 10 }}>
              Still waiting on Google. If an image challenge appeared, take your time — it will not time out.
              {' '}
              <button type="button" className="btn ghost sm" onClick={() => setManual(true)}>
                Stuck? Request manual setup
              </button>
            </p>
          ) : null}
          <button className="btn primary block" type="submit"
                  disabled={busy || national.replace(/\D/g, '').length < 5}>
            {busy ? 'Sending…' : 'Send code'}
          </button>
          <p className="gate-foot">
            First sign-in walks you through opening an account. New accounts start at a zero
            balance; add credit before submitting jobs.
          </p>
        </form>
      )}
      <div id={RECAPTCHA_ID} style={{ marginTop: visibleCaptcha ? 14 : 0 }} />
    </Card>
  );
}

function Problem({ error }) {
  if (!error) return null;
  return (
    <div className="err">
      {error.message}
      {error.code ? (
        <div className="num dim" style={{ marginTop: 4 }}>{error.code}</div>
      ) : null}
      {error.fix ? <div className="note" style={{ marginTop: 6 }}>{error.fix}</div> : null}
    </div>
  );
}

/**
 * The sign-in card, with a way out to the docs.
 *
 * The docs were always served without a session — anonymous requests to
 * /console/developer/ have always returned 200 — but the only link to them
 * lived inside the signed-in shell, so nobody who had not already logged in
 * could find them. API documentation that requires an account to discover is
 * documentation for the wrong audience: the person deciding whether to
 * integrate reads it *before* signing up.
 */
function Card({ lede, children }) {
  return (
    <div className="gate">
      <div className="gate-card panel">
        <div className="panel-body">
          <div className="brand">
            <span className="brand-mark">E</span>
            Edenn Console
          </div>
          <p className="gate-lede">{lede}</p>
          {children}
        </div>
      </div>
      <p className="gate-aside">
        No account yet? Read the <a href="/console/developer/">API documentation</a> first.
      </p>
    </div>
  );
}

/** {message, code, fix} — the code is shown, because it is what unblocks setup. */
function describe(error) {
  if (error instanceof NotConfigured) {
    return { message: 'This deployment is not connected to Firebase.', code: '' };
  }
  const code = String(error?.code || error?.name || '');
  const known = {
    'auth/invalid-phone-number': ['Google does not recognise this number.', 'Check the country selected on the left, and whether the number includes a national trunk 0.'],
    'auth/invalid-verification-code': ['That code is not correct.', ''],
    'auth/code-expired': ['That code has expired. Request a new one.', ''],
    'auth/too-many-requests': ['Too many attempts. Try again later.', 'Firebase rate-limits repeated requests to one number.'],
    'auth/quota-exceeded': ['The SMS quota is exhausted.', 'Check quota and billing in the Firebase console.'],
    'auth/billing-not-enabled': ['This Firebase project has no billing enabled, so it cannot send SMS.', 'Upgrade to the Blaze plan.'],
    'auth/operation-not-allowed': ['Phone sign-in is not enabled on this project.', 'Authentication → Sign-in method → enable Phone.'],
    'auth/unauthorized-domain': ['This domain is not in the Firebase authorised list.', 'Authentication → Settings → Authorised domains, and add this host.'],
    // The hint used to lead with "authorized domains / API key restrictions",
    // which cost an evening: both are checkable in one curl each and both were
    // fine. By the time this message is reachable the visible-checkbox
    // fallback has already been tried, so the remaining cause is almost always
    // the browser refusing to run reCAPTCHA at all.
    'auth/invalid-app-credential': ['reCAPTCHA verification did not pass.', 'Usually the browser is blocking google.com/recaptcha — try a private window with extensions disabled. It can also be an unauthorised domain or a restricted API key, but each of those is one curl to check.'],
    'auth/captcha-check-failed': ['reCAPTCHA verification failed.', 'As above: rule out browser extensions and blocking first, then check authorised domains.'],
    'auth/network-request-failed': ['Cannot reach the Google verification service.', 'This network may not be able to reach Google.'],
  };
  const hit = known[code];
  if (hit) return { message: hit[0], code, fix: hit[1] };
  return {
    message: 'Sign-in failed.',
    code: code || String(error?.message || '').slice(0, 120),
    fix: 'Send us the error code above and we can identify the cause.',
  };
}
