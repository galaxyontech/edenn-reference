'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import { billedUsd, count, dateTime, since, statusTone, usd } from '../lib/format';
import PageHead from './PageHead';

/**
 * The operator's view: every account, every key, and any account's ledger.
 *
 * Read-only for now. The write endpoints (open account, recharge, deactivate,
 * revoke) already exist and are guarded the same way, so adding forms here is a
 * frontend change rather than a backend one.
 */
export default function Admin({ token }) {
  const [summary, setSummary] = useState(null);
  const [keys, setKeys] = useState(null);
  const [focus, setFocus] = useState(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    const [summaryResult, keysResult] = await Promise.all([
      api.adminSummary(token), api.adminKeys(token),
    ]);
    if (summaryResult.ok) setSummary(summaryResult.data);
    // Optional chaining, not `.data.keys`: a 200 with an unparseable body
    // gives `data: null`, and the throw would skip setError below and leave
    // the screen on "Loading…" with nothing said and no way back.
    if (keysResult.ok) setKeys(keysResult.data?.keys || []);
    const failure = [summaryResult, keysResult].find((r) => !r.ok);
    setError(failure ? (failure.detail || 'Could not load admin data.') : '');
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const head = <PageHead eyebrow="Operations" title="All accounts and API keys" />;
  if (error) {
    return (
      <div className="stack">
        {head}
        <section className="panel">
          <div className="empty-state" role="alert">
            <strong>Couldn’t load admin data</strong>
            {error}
            <div style={{ marginTop: 14 }}>
              <button className="btn" onClick={load}>Retry</button>
            </div>
          </div>
        </section>
      </div>
    );
  }
  if (!summary) {
    return <div className="stack">{head}<p className="empty-state">Loading…</p></div>;
  }

  return (
    <div className="stack">
      {head}
      <PlatformTotals totals={summary.totals} truncated={summary.truncated}
                      scanned={summary.accounts_scanned} />
      <Accounts rows={summary.accounts} onPick={setFocus} focus={focus} />
      {focus ? <AccountDetail token={token} accountId={focus} /> : null}
      <AllKeys keys={keys} />
    </div>
  );
}

function PlatformTotals({ totals, truncated, scanned }) {
  const cells = [
    ['Accounts', count(totals.accounts)],
    ['Jobs', count(totals.jobs)],
    ['Billed', usd(totals.total_billed_usd)],
    ['Total balance', usd(totals.balance_usd)],
  ];
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Platform totals</h2>
        {truncated ? (
          <span className="note dim" style={{ color: 'var(--warn)' }}>
            First {scanned} accounts only, not the full set
          </span>
        ) : <span className="note dim">All accounts</span>}
      </div>
      <div className="panel-body row">
        {cells.map(([label, value]) => (
          <div key={label} className="grow">
            <span className="eyebrow">{label}</span>
            <div className="mono" style={{ fontSize: 24, marginTop: 4 }}>{value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Accounts({ rows, onPick, focus }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Accounts</h2>
        <span className="note dim">Sorted by spend · select a row for detail</span>
      </div>
      <div className="panel-body flush table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th><th>account_id</th><th>Status</th>
              <th className="n">Jobs</th><th className="n">Billed</th><th className="n">Balance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.account_id} className="clickable"
                  aria-selected={focus === row.account_id}
                  onClick={() => onPick(focus === row.account_id ? null : row.account_id)}>
                <td>{row.registered_name || <span className="note dim">unnamed</span>}</td>
                <td className="mono dim">{row.account_id}</td>
                <td>
                  <span className={`badge ${row.is_active ? 'live' : 'dead'}`}>
                    {row.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td className="n">{count(row.jobs)}</td>
                <td className="n">{usd(row.total_billed_usd)}</td>
                <td className="n" style={{ color: row.balance_usd <= 0 ? 'var(--spend)' : undefined }}>
                  {usd(row.balance_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AccountDetail({ token, accountId }) {
  const [usage, setUsage] = useState(null);
  const [txns, setTxns] = useState(null);

  useEffect(() => {
    let live = true;
    setUsage(null);
    setTxns(null);
    (async () => {
      const [usageResult, txnResult] = await Promise.all([
        api.adminUsage(token, accountId), api.adminTransactions(token, accountId),
      ]);
      if (!live) return;
      if (usageResult.ok) setUsage(usageResult.data);
      if (txnResult.ok) setTxns(txnResult.data.transactions || txnResult.data.rows || []);
    })();
    return () => { live = false; };
  }, [token, accountId]);

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Account detail</h2>
        <span className="mono dim">{accountId}</span>
      </div>
      <div className="panel-body stack">
        <div>
          <span className="eyebrow">Transactions</span>
          {!txns ? <p className="empty-state">Loading…</p> : txns.length === 0 ? (
            <p className="empty-state">No transactions.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Time</th><th>Type</th><th>Job</th><th>Note</th>
                      <th className="n">Amount</th><th className="n">Balance after</th></tr>
                </thead>
                <tbody>
                  {txns.slice(0, 20).map((txn, index) => (
                    <tr key={txn.txn_id || index}>
                      <td className="dim">{dateTime(txn.timestamp_utc)}</td>
                      <td>
                        <span className={`badge plain ${txn.amount_usd >= 0 ? 'live' : 'dead'}`}>
                          {txn.txn_type || '—'}
                        </span>
                      </td>
                      <td className="mono dim">
                        {txn.job_id ? String(txn.job_id).slice(0, 16) : '—'}
                      </td>
                      <td className="dim">{txn.note || '—'}</td>
                      <td className="n"
                          style={{ color: txn.amount_usd < 0 ? 'var(--spend)' : 'var(--credit)' }}>
                        {txn.amount_usd > 0 ? '+' : ''}{usd(txn.amount_usd)}
                      </td>
                      <td className="n dim">{usd(txn.balance_after_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <span className="eyebrow">Usage detail</span>
          {!usage ? <p className="empty-state">Loading…</p> : usage.rows.length === 0 ? (
            <p className="empty-state">No jobs.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th><th>Job</th><th>Model</th><th>Key</th>
                    <th>Status</th><th className="n">Billed</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.rows.slice(0, 30).map((row) => (
                    <tr key={row.job_id}>
                      <td className="dim">{dateTime(row.timestamp_utc)}</td>
                      <td className="mono">{String(row.job_id).slice(0, 12)}</td>
                      <td>{row.model_spec || '—'}</td>
                      <td className="mono">{row.key_prefix || '—'}</td>
                      <td>
                        <span className={`badge ${statusTone(row.status)}`}>
                          {row.status}
                        </span>
                      </td>
                      <td className="n">{usd(billedUsd(row))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function AllKeys({ keys }) {
  const [showRevoked, setShowRevoked] = useState(false);
  const rows = (keys || []).filter((key) => showRevoked || key.is_active);

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>All API keys</h2>
        <button className="btn ghost" onClick={() => setShowRevoked(!showRevoked)}>
          {showRevoked ? 'Active only' : 'Include revoked'}
        </button>
      </div>
      <div className="panel-body flush table-wrap">
        {!keys ? <p className="empty-state">Loading…</p> : rows.length === 0 ? (
          <p className="empty-state">No keys.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Key</th><th>Account</th><th>Note</th>
                <th>Created</th><th>Last used</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((key) => (
                <tr key={key.key_prefix + key.user_id}>
                  <td className="mono">{key.key_prefix}…</td>
                  <td className="mono dim">{key.user_id}</td>
                  <td>{key.note || <span className="note dim">—</span>}</td>
                  <td className="note dim">{dateTime(key.created_at)}</td>
                  <td className={key.last_used_at ? '' : 'note'}>{since(key.last_used_at)}</td>
                  <td>
                    <span className={`badge ${key.is_active ? 'live' : 'dead'}`}>
                      {key.is_active ? 'Active' : 'Revoked'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
