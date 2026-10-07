'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import {
  billedUsd, count, dateTime, productTotals, statusTone, usd,
} from '../lib/format';
import { inWindow } from '../lib/buckets';
import { LOADING, settle } from '../lib/resource';
import PageHead from './PageHead';
import SpendChart from './SpendChart';

/**
 * Usage: the spend chart, the two products, the itemized bill.
 *
 * This was the account page — balance, a small waveform strip, two breakdowns and the
 * ledger. The chart is now the page's centrepiece with its own time grain,
 * because "how much are we spending and is it going up" is the question people
 * actually open this screen with; the balance moved to Home where it belongs
 * beside the other headline numbers.
 */
export default function Usage({ token }) {
  const [usage, setUsage] = useState(LOADING);
  const [grain, setGrain] = useState('day');
  const [byKey, setByKey] = useState(false);

  const load = useCallback(async () => {
    setUsage(LOADING);
    setUsage(settle(await api.usageAll(token), 'Could not load usage.'));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  // One window for the whole page. Everything below the chart describes the
  // same span the chart draws, so no two numbers here can disagree about
  // "how much" by silently meaning different periods.
  const ready = usage.state === 'ready';
  const loading = usage.state === 'loading';
  const rows = usage.data?.rows || [];
  const scoped = ready ? inWindow(rows, grain) : [];
  const products = ready ? productTotals(scoped) : null;
  const models = ready ? modelTotals(scoped) : null;

  // A failed load says so once. Rendering the five panels anyway would fill
  // the page with zeros and empty states, which reads as "you have no usage"
  // — a different and much more alarming claim than "we could not read it".
  if (usage.state === 'failed') {
    return (
      <div className="stack">
        <PageHead eyebrow="Account" title="Usage" />
        <section className="panel">
          <div className="empty-state" role="alert">
            <strong>Couldn’t load usage</strong>
            {usage.detail} Your jobs and balance are unaffected — this is the
            console failing to read them.
            <div style={{ marginTop: 14 }}>
              <button className="btn" onClick={load}>Retry</button>
            </div>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="stack">
      <PageHead eyebrow="Account" title="Usage" action={
        <span className="note dim">Every figure below covers the range selected in the chart</span>
      } />
      {/*
        Every number on this page is summed in the browser from the rows above,
        so the ceiling on those rows is a ceiling on the figures too. This used
        to claim "the totals above are complete", which was true of an earlier
        version that rendered the server's own totals and stopped being true
        when the page moved to a single client-side window.
      */}
      {usage.data?.truncated ? (
        <p className="note" style={{ color: 'var(--warn)' }}>
          More than 5,000 jobs. This page covers the most recent 5,000, so
          longer ranges understate older periods.
        </p>
      ) : null}

      <SpendChart rows={scoped} grain={grain} onGrain={setGrain}
                  byKey={byKey} onByKey={setByKey} loading={loading} />

      <div className="row">
        {/* Both products bill on delivered duration; only the unit differs.
            See content/docs/09-billing.md — these two labels and that page
            have to move together. */}
        <Product title="Video soundtrack" note="Per 30-second block, rounded up"
                 bucket={products?.video_music} loading={loading} />
        <Product title="Image soundtrack" note="Per second, minimum 15"
                 bucket={products?.image_music} showSeconds loading={loading} />
      </div>
      {products?.unknown.jobs ? (
        <p className="note dim">
          A further {count(products.unknown.jobs)} jobs came from endpoints not
          broken out here, totalling {usd(products.unknown.billed)}. They are included above.
        </p>
      ) : null}

      <Breakdown title="By model" buckets={models} loading={loading} />
      <Ledger rows={scoped} loading={loading} />
    </div>
  );
}

/**
 * Per-product rollup.
 *
 * The server's breakdowns are by key and by model; neither separates the two
 * products, because the product is decided by the *endpoint*. So this is summed
 * client-side from the rows — which means it covers only what was fetched, and
 * `usage.truncated` above is what discloses that.
 */
/**
 * Per-model rollup for the window.
 *
 * The server's `by_model` covers the account's whole history, which is the
 * right answer to a different question than the one this page is asking.
 */
function modelTotals(rows) {
  const out = {};
  for (const row of rows) {
    const label = row.model_spec || 'unspecified';
    const bucket = out[label] || (out[label] = { jobs: 0, total_billed_usd: 0 });
    bucket.jobs += 1;
    bucket.total_billed_usd += billedUsd(row);
  }
  return out;
}

function Product({ title, note, bucket, showSeconds = false, loading }) {
  return (
    <section className="panel grow">
      <div className="panel-head">
        <h2>{title}</h2>
        <span className="note dim">{note}</span>
      </div>
      <div className="panel-body">
        {loading || !bucket ? (
          <div className="skeleton" style={{ height: 26, width: 90 }} />
        ) : (
          <>
            <div className="readout" style={{ fontSize: 24 }}>
              {usd(bucket.billed)}
            </div>
            <p className="note dim" style={{ marginTop: 6 }}>
              {count(bucket.jobs)} jobs
              {showSeconds && bucket.seconds
                ? ` · ${count(Math.round(bucket.seconds))}s delivered`
                : ''}
            </p>
          </>
        )}
      </div>
    </section>
  );
}

function Breakdown({ title, buckets, loading }) {
  const entries = Object.entries(buckets || {})
    .sort((a, b) => b[1].total_billed_usd - a[1].total_billed_usd);
  const peak = Math.max(...entries.map(([, v]) => v.total_billed_usd), 0);

  return (
    <section className="panel">
      <div className="panel-head"><h2>{title}</h2></div>
      <div className="panel-body flush">
        {loading ? (
          <div style={{ padding: 18, display: 'grid', gap: 10 }}>
            <div className="skeleton" /><div className="skeleton" style={{ width: '70%' }} />
          </div>
        ) : entries.length === 0 ? (
          <p className="empty-state">No usage recorded</p>
        ) : (
          <table>
            <tbody>
              {entries.map(([label, value]) => (
                <tr key={label}>
                  <td style={{ width: '34%' }}>{label}</td>
                  <td style={{ width: '30%' }}>
                    <span className="bar">
                      <i style={{ width: peak > 0 ? `${(value.total_billed_usd / peak) * 100}%` : 0 }} />
                    </span>
                  </td>
                  <td className="n dim">{count(value.jobs)}</td>
                  <td className="n">{usd(value.total_billed_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function Ledger({ rows, loading }) {
  const [limit, setLimit] = useState(25);

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Usage detail</h2>
        <span className="note dim">{count(rows.length)} rows</span>
      </div>
      <div className="panel-body flush">
        {loading ? (
          <div style={{ padding: 18, display: 'grid', gap: 10 }}>
            <div className="skeleton" /><div className="skeleton" style={{ width: '75%' }} />
          </div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            <strong>No jobs yet</strong>
            Create a key on the API key page and submit your first job. Each billed job appears here.
          </div>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th><th>Job</th><th>Model</th><th>Tracking ID</th>
                    <th>Status</th><th className="n">Tokens</th><th className="n">Billed</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(0, limit).map((row) => (
                    <tr key={row.job_id}>
                      <td className="dim">{dateTime(row.timestamp_utc)}</td>
                      <td className="mono">{String(row.job_id).slice(0, 12)}</td>
                      <td>{row.model_spec || <span className="dim">—</span>}</td>
                      <td className="mono">{row.key_prefix || <span className="dim">—</span>}</td>
                      <td>
                        <span className={`badge ${statusTone(row.status)}`}>
                          {row.status}
                        </span>
                      </td>
                      <td className="n">{count(row.total_tokens)}</td>
                      <td className="n">{usd(billedUsd(row))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {limit < rows.length ? (
              <div style={{ padding: 14, borderTop: '1px solid var(--border)' }}>
                <button className="btn ghost" onClick={() => setLimit(limit + 100)}>
                  Show 100 more
                </button>
              </div>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
