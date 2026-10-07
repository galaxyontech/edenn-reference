'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../src/lib/api';
import { consoleConfig, signOut, watchUser } from '../src/lib/firebase';
import Admin from '../src/components/Admin';
import Home from '../src/components/Home';
import Keys from '../src/components/Keys';
import SignIn from '../src/components/SignIn';
import { CONSOLE_VERSION } from '../src/lib/version';
import Usage from '../src/components/Usage';

/**
 * One page, five states: booting, unconfigured, signing in, opening an account,
 * and the console itself (balance, keys, or the operator view).
 *
 * After sign-in we probe /account/balance rather than calling
 * /signup/verified. Signup is a write — it opens an account and claims the
 * caller's phone number — and firing one on every page load to answer "am I
 * signed up?" would be using a mutation as a question. 404 signup_required is
 * the only signal that a real signup is needed.
 */
export default function Page() {
  const [token, setToken] = useState(null);
  const [phase, setPhase] = useState('booting');
  const [view, setView] = useState('home');
  const [identity, setIdentity] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let stop;
    let live = true;
    (async () => {
      // Config first: without it there is no Firebase app to watch, and the
      // page should say so rather than show a sign-in form that cannot work.
      const config = await consoleConfig().catch(() => null);
      if (!live) return;
      if (!config) { setPhase('unconfigured'); return; }
      watchUser(async (user) => {
        if (!user) { setToken(null); setIdentity(null); setPhase('signed-out'); return; }
        // photoURL is null for phone sign-in and populated for Google; the rail
        // handles both rather than assuming either.
        setIdentity((was) => ({
          ...was,
          uid: user.uid,
          phone: user.phoneNumber,
          email: user.email,
          photo: user.photoURL || '',
          name: user.displayName || was?.name || '',
        }));
        setToken(await user.getIdToken());
      }).then((unsub) => { stop = unsub; })
        .catch(() => setPhase('signed-out'));  // SDK blocked: SignIn explains it
    })();
    return () => { live = false; if (stop) stop(); };
  }, []);

  const probe = useCallback(async (activeToken) => {
    const result = await api.balance(activeToken);
    if (result.status === 404 && result.code === 'signup_required') {
      setPhase('needs-signup');
      return;
    }
    if (result.status === 401) { setPhase('signed-out'); return; }
    setPhase('ready');
    // The account's registered name is the only human-readable label a
    // phone-only user has — Firebase gives them no displayName at all.
    if (result.ok && result.data?.registered_name) {
      setIdentity((was) => ({ ...was, name: result.data.registered_name }));
    }
    // Admin scope is decided by the server; the console only reflects it. A
    // forged local flag would buy nothing — every admin route re-checks.
    const admin = await api.adminSummary(activeToken, '?max_accounts=1');
    setIsAdmin(admin.ok);
  }, []);

  useEffect(() => { if (token) probe(token); }, [token, probe]);

  if (phase === 'booting') {
    return <main className="gate"><p className="note dim">Loading…</p></main>;
  }

  if (phase === 'unconfigured') return <Unconfigured />;

  if (phase === 'signed-out' || !token) {
    return <SignIn onToken={(fresh) => setToken(fresh)} />;
  }

  if (phase === 'needs-signup') {
    // Straight to the key page: a fresh account has no key, and everything
    // else in here reads $0.00 until it has one.
    return (
      <OpenAccount token={token}
                   onDone={() => { setView('keys'); probe(token); }} />
    );
  }

  // Admin scope can be revoked between renders; the view falls back rather
  // than rendering an operator screen every request behind it would refuse.
  const current = view === 'admin' && !isAdmin ? 'home' : view;

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand"><span className="brand-mark">E</span>Edenn</div>
        <nav>
          <button aria-current={current === 'home'}
                  onClick={() => setView('home')}>Home</button>
          <button aria-current={current === 'usage'}
                  onClick={() => setView('usage')}>Usage</button>
          <button aria-current={current === 'keys'}
                  onClick={() => setView('keys')}>API keys</button>
          {isAdmin ? (
            <button aria-current={current === 'admin'}
                    onClick={() => setView('admin')}>Admin</button>
          ) : null}
          {/* A route, not a view switch: the docs are a separate page tree so
              they stay readable without a session. Link keeps the transition
              client-side — being ungated is about the URL, not the anchor. */}
          <Link href="/developer/">API docs</Link>
        </nav>
        <div className="rail-foot">
          {isAdmin ? <span className="badge admin">Admin</span> : null}
          <Identity identity={identity} />
          <button className="btn ghost" onClick={() => signOut().then(() => setToken(null))}>
            Sign out
          </button>
          <span className="rail-version">Console {CONSOLE_VERSION}</span>
        </div>
      </aside>
      <main className="main">
        {current === 'admin' ? <Admin token={token} />
          : current === 'usage' ? <Usage token={token} />
            : current === 'keys' ? <Keys token={token} />
              : <Home token={token} onNavigate={setView} />}
      </main>
    </div>
  );
}

/**
 * Who is signed in, with an avatar.
 *
 * Firebase only supplies a `photoURL` for providers that have one, and phone
 * sign-in never does — so the initial is the normal case here, not the
 * fallback. It comes from the account name when there is one: a phone number
 * has no letter to take, and "+" as an avatar is worse than nothing.
 */
function Identity({ identity }) {
  const label = identity?.name || identity?.email || identity?.phone || '';
  const initial = /^[\p{L}\p{N}]/u.test(label) ? [...label][0].toUpperCase() : '·';
  return (
    <div className="rail-user">
      {identity?.photo ? (
        <img className="avatar" src={identity.photo} alt="" width={28} height={28} />
      ) : (
        <span className="avatar" aria-hidden="true">{initial}</span>
      )}
      <div className="rail-id">
        <div className="rail-name">{label || 'Signed in'}</div>
        {identity?.uid ? (
          <div className="num dim" style={{ fontSize: 10.5 }}>{identity.uid}</div>
        ) : null}
      </div>
    </div>
  );
}

function Unconfigured() {
  return (
    <div className="gate">
      <div className="gate-card panel">
        <div className="panel-body">
          <div className="brand"><span className="brand-mark">E</span>Edenn Console</div>
          <p className="gate-lede">This deployment is not connected to Firebase yet.</p>
          <p className="note">
            Set <code>FIREBASE_PROJECT_ID</code> and
            <code> FIREBASE_WEB_API_KEY</code> on the API container, then reload.
            No frontend rebuild is required.
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Signup opens the account and stops there — no key is issued.
 *
 * The previous version handed over a key on this screen, in the middle of a
 * flow the customer was still reading, with nothing built around it: no name,
 * no list to find it in later, no second chance. Keys are minted on the API
 * key page now, one deliberate click, on a page that exists to show one.
 */
function OpenAccount({ token, onDone }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    const result = await api.signupVerified(token, name.trim());
    setBusy(false);
    if (!result.ok) { setError(result.detail || 'Could not open the account. Try again.'); return; }
    onDone();
  }

  return (
    <div className="gate">
      <div className="gate-card panel">
        <div className="panel-body">
          <div className="brand"><span className="brand-mark">E</span>Edenn Console</div>
          <form onSubmit={submit}>
            <p className="gate-lede">Your phone number is verified. Name your account to finish.</p>
            <div className="field">
              <label htmlFor="name">Company or team name</label>
              <input id="name" value={name} maxLength={128} autoFocus
                     onChange={(e) => setName(e.target.value)}
                     placeholder="e.g. Acme Media Ltd" />
            </div>
            {error ? <p className="err">{error}</p> : null}
            <button className="btn primary block" type="submit"
                    disabled={busy || name.trim().length === 0}>
              {busy ? 'Opening…' : 'Open account'}
            </button>
            <p className="gate-foot">
              Create your first API key afterwards. New accounts start at a zero balance;
              add credit before submitting jobs.
            </p>
          </form>
        </div>
      </div>
    </div>
  );
}
