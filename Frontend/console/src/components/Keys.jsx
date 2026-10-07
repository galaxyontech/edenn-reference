'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import { dateTime, since } from '../lib/format';
import { LOADING, settle } from '../lib/resource';
import CopyField from './CopyField';
import Modal from './Modal';
import PageHead from './PageHead';

/**
 * API keys, on their own page.
 *
 * It used to be a panel between the spend chart and the itemized bill, which
 * put a destructive control (revoke) and a write action (create) inside a screen
 * people open to read numbers. Splitting it also lets the table say what a key
 * table has to say: which key is this, is it live, and is anything still using
 * it.
 *
 * Two columns carry the identity, because they answer different questions:
 *
 * * **Tracking ID** — the 12-character prefix. This is the string that appears
 *   on usage rows and in `?key_prefix=` filters, so it is the one to quote in a
 *   support ticket.
 * * **Secret key** — `sk-…LAST4`. The head of a key is nearly the same on every key
 *   ("sk-" plus a few characters people misread); the tail is what someone
 *   actually compares against the value in their .env. Keys minted before the
 *   suffix was recorded have no tail and say so, rather than showing a made-up
 *   one — their plaintext is gone for good.
 */
export default function Keys({ token }) {
  const [keys, setKeys] = useState(LOADING);
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [minted, setMinted] = useState(null);
  const [pendingRevoke, setPendingRevoke] = useState(null);

  const load = useCallback(async () => {
    setKeys(LOADING);
    const result = await api.keys(token, includeRevoked);
    if (!result.ok) { setKeys(settle(result, 'Could not load your keys.')); return; }
    // Newest first, decided here rather than trusted from the server: the
    // two key stores disagree about row order, and "my new key" belongs at
    // the top on both.
    setKeys({
      state: 'ready',
      detail: '',
      // `data` is null for a 200 with an unparseable body (see api.js). Reading
      // through it throws inside load(), which leaves the resource on LOADING
      // forever — the skeleton bug, wearing a different hat.
      data: [...(result.data?.keys || [])].sort(
        (a, b) => String(b.created_at).localeCompare(String(a.created_at))),
    });
  }, [token, includeRevoked]);

  useEffect(() => { load(); }, [load]);

  async function revoke() {
    const prefix = pendingRevoke.key_prefix;
    setPendingRevoke(null);
    const result = await api.revokeKey(token, prefix);
    if (!result.ok) setError(result.detail || 'Could not revoke the key. Try again.');
    else load();
  }

  const list = keys.data || [];
  const live = list.filter((key) => key.is_active).length;

  return (
    <div className="stack">
      <PageHead
        eyebrow="Account" title="API keys"
        action={(
          <button className="btn primary" onClick={() => setCreating(true)}>
            Create key
          </button>
        )}
      />
      {error ? <p className="err" role="alert">{error}</p> : null}

      <section className="panel">
        <div className="panel-head">
          <h2>{keys.state === 'ready' ? `${live} active` : 'API keys'}</h2>
          <button className="btn ghost sm"
                  aria-pressed={includeRevoked}
                  onClick={() => setIncludeRevoked((v) => !v)}>
            {includeRevoked ? 'Hide revoked' : 'Show revoked'}
          </button>
        </div>
        <div className="panel-body flush">
          {keys.state === 'loading' ? (
            <div style={{ padding: 18, display: 'grid', gap: 10 }}>
              <div className="skeleton" />
              <div className="skeleton" style={{ width: '60%' }} />
            </div>
          ) : keys.state === 'failed' ? (
            <div className="empty-state" role="alert">
              <strong>Couldn’t load your keys</strong>
              {keys.detail} Any key already in use keeps working — this is the
              console failing to list them, not the keys failing.
              <div style={{ marginTop: 14 }}>
                <button className="btn" onClick={load}>Retry</button>
              </div>
            </div>
          ) : list.length === 0 ? (
            <div className="empty-state">
              <strong>No API keys yet</strong>
              Create one to start calling the API. The key is shown only once, at creation.
              <div style={{ marginTop: 14 }}>
                <button className="btn" onClick={() => setCreating(true)}>
                  Create your first key
                </button>
              </div>
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Tracking ID</th>
                    <th>Secret key</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {list.map((key) => (
                    <tr key={key.key_prefix}>
                      <td>
                        {key.is_active ? (
                          <KeyName token={token} keyRow={key}
                                   onSaved={load} onError={setError} />
                        ) : (
                          <span className="dim">{key.name || 'Unnamed'}</span>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${key.is_active ? 'live' : 'dead'}`}>
                          {key.is_active ? 'Active' : 'Revoked'}
                        </span>
                      </td>
                      <td className="mono">{key.key_prefix}</td>
                      <td className="mono"><Masked suffix={key.key_suffix} /></td>
                      <td className="dim">{dateTime(key.created_at)}</td>
                      <td className={key.last_used_at ? '' : 'dim'}>
                        {key.is_active
                          ? since(key.last_used_at)
                          : `Revoked ${dateTime(key.revoked_at)}`}
                      </td>
                      <td className="n">
                        {key.is_active ? (
                          <button className="btn danger sm"
                                  onClick={() => setPendingRevoke(key)}>
                            Revoke
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {creating ? (
        <CreateKey token={token}
                   onClose={() => setCreating(false)}
                   onCreated={(created) => {
                     setCreating(false);
                     setMinted(created);
                     load();
                   }} />
      ) : null}

      {/*
        The only screen in the console showing something that cannot be
        recovered, so it is the only one that refuses to close by accident:
        no Escape, no backdrop click. Dismissing this by a stray click costs
        the customer a key they have to revoke and replace.
      */}
      {minted ? (
        <Modal title="Save this key" dismissable={false} onClose={() => setMinted(null)}>
          <p className="note">
            This is the only time it is shown. The server stores only a hash — closing this window loses it.
          </p>
          <CopyField value={minted.api_key} label={minted.name || 'New key'} />
          <div className="modal-actions">
            <button className="btn primary" onClick={() => setMinted(null)}>
              I&rsquo;ve saved it
            </button>
          </div>
        </Modal>
      ) : null}

      {pendingRevoke ? (
        <Modal title="Revoke this key?" onClose={() => setPendingRevoke(null)}>
          <p className="note">
            Anything still using <code className="num">{pendingRevoke.key_prefix}</code> will start
            receiving 401. Revocation takes effect globally within 60 seconds. This cannot be undone.
          </p>
          {/* Danger styling, and the key named in the button: the confirm slot
              is pressed from muscle memory, so it has to say what it destroys
              rather than wear the affirmative style of a safe action. */}
          <div className="modal-actions">
            <button className="btn" autoFocus onClick={() => setPendingRevoke(null)}>Cancel</button>
            <button className="btn danger solid" onClick={revoke}>
              Revoke {pendingRevoke.name || pendingRevoke.key_prefix}
            </button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}

/** `sk-…LAST4`, or an honest blank tail for keys minted before we kept one. */
function Masked({ suffix }) {
  if (!suffix) {
    return (
      <span className="dim"
            title="Created before the last four characters were recorded. The plaintext no longer exists.">
        sk-…
      </span>
    );
  }
  return <>sk-<span className="dim">…</span>{suffix}</>;
}

function CreateKey({ token, onClose, onCreated }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    const result = await api.createKey(token, name.trim());
    setBusy(false);
    if (!result.ok) { setError(result.detail || 'Could not create the key. Try again.'); return; }
    onCreated(result.data);
  }

  return (
    <Modal title="Create an API key" onClose={onClose}>
      <form onSubmit={submit}>
        <p className="note">
          The name is for your reference only and can be changed later. The key itself is shown on the next step, once.
        </p>
        <div className="field" style={{ marginTop: 14 }}>
          <label htmlFor="key-name">Name</label>
          <input id="key-name" autoFocus value={name} maxLength={64}
                 placeholder="e.g. production"
                 onChange={(event) => setName(event.target.value)} />
        </div>
        {error ? <p className="err">{error}</p> : null}
        <div className="modal-actions">
          <button className="btn" type="button" onClick={onClose}>Cancel</button>
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? 'Creating…' : 'Create key'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Click-to-rename. A key's label is the only thing about it a customer can
 * change, so "revoke and make another" is far too blunt an answer to a bad name.
 */
function KeyName({ token, keyRow, onSaved, onError }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(keyRow.name || '');
  const [busy, setBusy] = useState(false);

  async function save() {
    const name = value.trim();
    if (!name || name === keyRow.name) { setEditing(false); return; }
    setBusy(true);
    const result = await api.renameKey(token, keyRow.key_prefix, name);
    setBusy(false);
    setEditing(false);
    if (result.ok) { onError(''); onSaved(); return; }
    // Reverting the field without a word looks like the rename was accepted
    // and then undone by someone else. Say the server refused it.
    setValue(keyRow.name || '');
    onError(result.detail || 'Could not rename the key. Try again.');
  }

  if (!editing) {
    return (
      <button type="button" className="btn ghost sm"
              style={{ padding: '2px 6px', margin: '-2px -6px' }}
              onClick={() => { setValue(keyRow.name || ''); setEditing(true); }}
              title="Click to rename">
        {keyRow.name || <span className="dim">Unnamed</span>}
      </button>
    );
  }
  return (
    <input autoFocus value={value} maxLength={64} disabled={busy}
           style={{ padding: '4px 7px', fontSize: 13, maxWidth: 180 }}
           onChange={(event) => setValue(event.target.value)}
           onBlur={save}
           onKeyDown={(event) => {
             if (event.key === 'Enter') save();
             if (event.key === 'Escape') {
               setValue(keyRow.name || '');
               setEditing(false);
             }
           }} />
  );
}

