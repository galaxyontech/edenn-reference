'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { api, apiBase } from '../lib/api';
import {
  billedUsd, count, dailySpend, shortDay, since, statusTone, usd,
} from '../lib/format';
import { LOADING, settle } from '../lib/resource';
import PageHead from './PageHead';

/**
 * The landing screen.
 *
 * Home renders one of three layouts, chosen by what the account actually is,
 * because the questions differ so completely between them:
 *
 * * **First run** (no jobs) — a path, not a report. Four zeros and a pair of
 *   documentation links is what this page used to show a brand-new account,
 *   which is every account on day one. The three things standing between the
 *   customer and a working request are worth the whole screen instead.
 * * **Active** — a status board: balance and burn rate, the counts, the last
 *   few jobs, then where to read more. In that order, because "how much is
 *   left" and "how fast is it going" are the two questions people open a
 *   billing console with.
 * * **Degraded** — whatever failed says so and offers a retry, per panel.
 *
 * Every figure here is still a *rough* one meant to be read in two seconds;
 * the detail lives one click away in Usage and API keys.
 */

/** Enough rows for the strip below without a second request. See `load`. */
const HOME_ROWS = 500;
const STRIP_DAYS = 14;
const ACTIVITY_ROWS = 5;

export default function Home({ token, onNavigate }) {
  const [balance, setBalance] = useState(LOADING);
  const [usage, setUsage] = useState(LOADING);
  const [keys, setKeys] = useState(LOADING);

  const load = useCallback(async () => {
    setBalance(LOADING);
    setUsage(LOADING);
    setKeys(LOADING);
    const [balanceResult, usageResult, keysResult] = await Promise.all([
      api.balance(token),
      // One request serves the tiles, the strip and the activity table. The
      // server's `totals` covers the whole filtered set regardless of paging,
      // so the headline figures stay exact however few rows come back; only
      // the strip depends on the rows, and it discloses its own limit.
      api.usage(token, `?limit=${HOME_ROWS}`),
      api.keys(token),
    ]);
    setBalance(settle(balanceResult, 'Could not load your balance.'));
    setUsage(settle(usageResult, 'Could not load your usage.'));
    setKeys(settle(keysResult, 'Could not load your keys.'));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const totals = usage.state === 'ready' ? usage.data?.totals : null;
  const rows = usage.state === 'ready' ? (usage.data?.rows || []) : [];
  const keyList = keys.state === 'ready' ? (keys.data?.keys || []) : null;
  const activeKeys = keyList ? keyList.filter((key) => key.is_active) : null;

  const amount = balance.data?.balance_usd ?? 0;
  const jobs = Number(totals?.jobs || 0);

  // Onboarding is only offered on a known-empty account. A failed usage
  // request must not route somebody with 900 jobs into a welcome screen.
  const firstRun = usage.state === 'ready' && jobs === 0;

  const failures = [balance, usage, keys].filter((r) => r.state === 'failed');
  const name = balance.data?.registered_name || 'Your account';

  /*
    A dropped connection fails all three requests at the same instant, and
    per-panel retries then put five identical buttons on one screen — each
    calling the same reload. When nothing loaded there is nothing to retry
    *part* of, so the whole page says so once. Partial failure keeps the
    per-panel treatment, where the button sits on the thing it will fix.
  */
  /*
    One skeleton for the whole page rather than per panel. All three requests
    go out in a single Promise.all, so they settle together — and choosing the
    layout before `usage` lands would paint the active board (bridge, spend
    strip, activity table) for a brand-new account and then tear the whole
    thing down a moment later for the onboarding path.
  */
  if (usage.state === 'loading') {
    return (
      <div className="stack">
        <PageHead eyebrow="Overview" title={name} />
        <section className="panel">
          <div className="panel-body"><div className="skeleton" style={{ height: 88 }} /></div>
        </section>
        <div className="tiles">
          {[0, 1, 2].map((slot) => (
            <section key={slot} className="panel tile">
              <span className="eyebrow">&nbsp;</span>
              <div className="skeleton" style={{ height: 26, width: 96, marginTop: 10 }} />
            </section>
          ))}
        </div>
        <Docs />
      </div>
    );
  }

  if (failures.length === 3) {
    return (
      <div className="stack">
        <PageHead eyebrow="Overview" title="Your account" />
        <section className="panel">
          <div className="empty-state" role="alert">
            <strong>Couldn’t reach your account</strong>
            {balance.detail} Your keys and any running jobs are unaffected —
            this is the console failing to read them.
            <div style={{ marginTop: 14 }}>
              <button className="btn primary" onClick={load}>Retry</button>
            </div>
          </div>
        </section>
        <Docs />
      </div>
    );
  }

  return (
    <div className="stack">
      <PageHead
        eyebrow="Overview"
        title={firstRun ? `Welcome${firstName(name)}` : name}
        action={balance.state === 'ready' && !firstRun ? (
          <span className={`badge ${balance.data.is_active ? 'live' : 'dead'}`}>
            {balance.data.is_active ? 'Active' : 'Deactivated'}
          </span>
        ) : null}
      />

      {/* Announced once rather than per panel: two simultaneous alerts say no
          more than one does. The retry affordance stays on the panel that
          failed, because with a partial failure it is worth knowing which. */}
      {/* Every distinct reason, not just the first: with balance and keys both
          down for different reasons, showing one leaves the other tile saying
          "Couldn't load" with no explanation anywhere on the page. */}
      {failures.length ? (
        <p className="err" role="alert">
          {[...new Set(failures.map((f) => f.detail))].join(' ')}
        </p>
      ) : null}

      {firstRun ? (
        <GetStarted keys={keys} balance={balance} onNavigate={onNavigate} />
      ) : (
        <Bridge balance={balance} usage={usage} rows={rows} onRetry={load} />
      )}

      <div className="tiles">
        {firstRun ? (
          <Tile label="Balance" state={balance.state}
                value={usd(amount)} tone={amount <= 0 ? 'empty' : ''}
                note={amount <= 0 ? 'Submissions return 402' : 'Available'}
                onRetry={load} />
        ) : null}
        <Tile label="Active API keys" state={keys.state}
              value={activeKeys ? count(activeKeys.length) : null}
              note="20 maximum"
              action={{ label: 'Manage', view: 'keys' }}
              onNavigate={onNavigate} onRetry={load} />
        <Tile label="Jobs" state={usage.state} value={count(jobs)}
              note="All time"
              action={jobs > 0 ? { label: 'View usage', view: 'usage' } : null}
              onNavigate={onNavigate} onRetry={load} />
        {firstRun ? null : (
          <Tile label="Total spend" state={usage.state}
                value={usd(totals?.total_billed_usd)}
                note="Successful jobs only" onRetry={load} />
        )}
      </div>

      {firstRun ? null : (
        <Activity state={usage.state} rows={rows} onRetry={load}
                  onNavigate={onNavigate} />
      )}

      <Docs />
    </div>
  );
}

/**
 * ", Kedan" from "Kedan Zha", ", Acme" from "Acme Media Ltd".
 *
 * The first token of whatever the account is registered as. That reads well
 * for a person and acceptably for a company; the length guard is only there
 * to keep a single very long token out of the heading.
 */
function firstName(name) {
  const first = String(name || '').trim().split(/\s+/)[0];
  return first && first.length <= 16 && first !== 'Your' ? `, ${first}` : '';
}

/**
 * A headline number, in whichever of the three states its fetch is in.
 *
 * The failed state is a real state here: a dash, an admission, and a retry.
 */
function Tile({ label, state, value, note, tone = '', action, onNavigate, onRetry }) {
  const failed = state === 'failed';
  return (
    <section className={`panel tile ${failed ? '' : tone}`}>
      <span className="eyebrow">{label}</span>
      {state === 'loading' ? (
        <div className="skeleton" style={{ height: 26, width: 96, marginTop: 10 }} />
      ) : (
        <div className={`readout${failed ? ' dim' : ''}`}>{failed ? '—' : value}</div>
      )}
      <div className="tile-foot">
        <span className="note dim">
          {failed ? 'Couldn’t load' : state === 'loading' ? '' : note}
        </span>
        {failed ? (
          <button className="btn ghost sm" onClick={onRetry}>Retry</button>
        ) : action && state === 'ready' ? (
          <button className="btn ghost sm"
                  onClick={() => onNavigate(action.view)}>{action.label}</button>
        ) : null}
      </div>
    </section>
  );
}

/**
 * Balance beside the last fortnight's spend.
 *
 * These two belong on one line: the balance is a stock and the strip is the
 * flow draining it, and "is this going to last" is a comparison between them
 * that costs nothing when they sit side by side. The `.bridge` / `.balance` /
 * `.wave` styles this uses were already in globals.css, orphaned — the layout
 * had been designed before and never wired to anything.
 */
function Bridge({ balance, usage, rows, onRetry }) {
  const amount = balance.data?.balance_usd ?? 0;
  const warning = balance.data?.balance_warning;
  const tone = balance.state !== 'ready' ? '' : amount <= 0 ? 'empty' : warning ? 'low' : '';

  return (
    <section className={`panel bridge ${tone}`}>
      <div className={`balance ${tone}`}>
        <span className="eyebrow">Balance</span>
        {balance.state === 'loading' ? (
          <div className="skeleton" style={{ height: 32, width: 120, marginTop: 10 }} />
        ) : balance.state === 'failed' ? (
          <>
            <div className="readout dim">—</div>
            <p className="note">Couldn’t load your balance.</p>
            <button className="btn sm bal-cta" onClick={onRetry}>Retry</button>
          </>
        ) : (
          <>
            <div className="readout">{usd(amount)}</div>
            <p className="note">
              {amount <= 0
                ? 'Balance is zero. Submissions return 402 until you add credit.'
                : warning
                  ? 'Balance is low. Add credit soon.'
                  : runway(amount, rows)}
            </p>
            {/* The only blocking number in the product, and until now the only
                tile without a way to act on it. The button is present at every
                balance level and takes the primary style once the balance is
                actually in the way. */}
            <Link className={`btn sm bal-cta${amount <= 0 ? ' primary' : ''}`}
                  href="/developer/recharge/">
              Add credit →
            </Link>
          </>
        )}
      </div>
      <SpendStrip usage={usage} rows={rows} onRetry={onRetry} />
    </section>
  );
}

/**
 * "About 42 more jobs" — from the account's own recent average, not a price
 * list. `/account/balance` carries no pricing, and the customer's own mix of
 * models is a better predictor of their next hundred jobs than a rate card
 * would be. Silent unless there is enough history to mean anything.
 */
function runway(amount, rows) {
  const billed = (rows || []).map(billedUsd).filter((value) => value > 0);
  if (billed.length < 3) return 'Available';
  const average = billed.reduce((sum, value) => sum + value, 0) / billed.length;
  const left = Math.floor(amount / average);
  if (!Number.isFinite(left) || left <= 0) return 'Available';
  return `About ${count(left)} more jobs at your recent average.`;
}

/**
 * Fourteen daily bars. One series, so no legend — the heading names it.
 *
 * Bars are flex children rather than SVG rects because the strip has to span
 * whatever width the grid gives it: scaling an SVG to fit would either squash
 * the corner radii or need a resize observer to avoid it.
 */
function SpendStrip({ usage, rows, onRetry }) {
  if (usage.state === 'loading') {
    return (
      <div className="wave">
        <div className="wave-head"><h2>Spend · last {STRIP_DAYS} days</h2></div>
        <div className="skeleton" style={{ height: 88, marginTop: 10 }} />
      </div>
    );
  }
  if (usage.state === 'failed') {
    return (
      <div className="wave">
        <div className="wave-head"><h2>Spend · last {STRIP_DAYS} days</h2></div>
        <p className="wave-empty">Couldn’t load spend.</p>
        <div style={{ textAlign: 'center' }}>
          <button className="btn sm" onClick={onRetry}>Retry</button>
        </div>
      </div>
    );
  }

  const days = dailySpend(rows, STRIP_DAYS);
  const peak = Math.max(...days.map((day) => day.amount), 0);
  const total = days.reduce((sum, day) => sum + day.amount, 0);

  /*
    Rows come newest first and are capped at HOME_ROWS. That cap only distorts
    the strip when the fetch ran out *inside* the window — an account with a
    year of history under the cap is drawn completely. So the test is whether
    the oldest row we hold is itself newer than the window, not merely whether
    more rows exist.
  */
  const oldest = rows.length
    ? new Date(rows[rows.length - 1].timestamp_utc).getTime() : 0;
  const from = Date.now() - STRIP_DAYS * 86400000;
  const capped = Boolean(usage.data?.page?.has_more) && oldest > from;

  return (
    <div className="wave">
      <div className="wave-head">
        <h2>Spend · last {STRIP_DAYS} days</h2>
        <span className="num">{usd(total)}</span>
      </div>
      {total <= 0 ? (
        <p className="wave-empty">No spend in the last {STRIP_DAYS} days</p>
      ) : (
        <>
          <div className="strip" role="img"
               aria-label={`Daily spend, last ${STRIP_DAYS} days, totalling ${usd(total)}. `
                 + days.map((day) => `${shortDay(day.day)} ${usd(day.amount)}`).join(', ')}>
            {days.map((day) => (
              <span key={day.day}
                    className={`strip-bar${day.amount > 0 ? '' : ' zero'}`}
                    title={`${day.day} · ${usd(day.amount)}`}>
                <i style={{ height: day.amount > 0 && peak > 0
                  ? `${Math.max(3, (day.amount / peak) * 100)}%` : '2px' }} />
              </span>
            ))}
          </div>
          <div className="wave-axis">
            <span>{shortDay(days[0]?.day)}</span>
            <span>{shortDay(days[days.length - 1]?.day)}</span>
          </div>
          {capped ? (
            <p className="note dim" style={{ marginTop: 8 }}>
              Built from the most recent {count(HOME_ROWS)} jobs, which do not
              reach back {STRIP_DAYS} days — earlier bars understate.
              {' '}Usage has the full range.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

/**
 * The last few jobs.
 *
 * Home held no row-level content at all before this, which is most of why it
 * read as thin: a status board with no records on it is a summary of nothing.
 */
function Activity({ state, rows, onRetry, onNavigate }) {
  const recent = rows.slice(0, ACTIVITY_ROWS);
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Recent activity</h2>
        {state === 'ready' && rows.length ? (
          <button className="btn ghost sm" onClick={() => onNavigate('usage')}>
            View all usage →
          </button>
        ) : null}
      </div>
      <div className="panel-body flush">
        {state === 'loading' ? (
          <div style={{ padding: 18, display: 'grid', gap: 10 }}>
            <div className="skeleton" />
            <div className="skeleton" style={{ width: '70%' }} />
          </div>
        ) : state === 'failed' ? (
          <div className="empty-state">
            <strong>Couldn’t load recent activity</strong>
            The jobs themselves are unaffected — this is the console failing to read them.
            <div style={{ marginTop: 14 }}>
              <button className="btn" onClick={onRetry}>Retry</button>
            </div>
          </div>
        ) : recent.length === 0 ? (
          <p className="empty-state">No jobs in the period covered here</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th><th>Model</th><th>Status</th>
                  <th className="n">Billed</th><th className="n">When</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((row) => {
                  const billed = billedUsd(row);
                  return (
                    <tr key={row.job_id}>
                      <td className="mono">{String(row.job_id).slice(0, 12)}</td>
                      <td>{row.model_spec || <span className="dim">—</span>}</td>
                      <td>
                        <span className={`badge ${statusTone(row.status)}`}>{row.status}</span>
                      </td>
                      <td className="n">
                        {billed > 0 ? usd(billed) : <span className="dim">—</span>}
                      </td>
                      <td className="n dim">{since(row.timestamp_utc)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * Three steps, for an account that has never run a job.
 *
 * Numbered because it genuinely is a sequence — step 3 returns 402 without
 * step 2 — rather than as decoration on an unordered list.
 */
function GetStarted({ keys, balance, onNavigate }) {
  /*
    Both steps read from the resource, not from `.data` — a failed fetch also
    leaves `.data` null, and a step that reports "your balance is $0.00" on the
    strength of a request that never came back is inventing the one number the
    customer is here to act on. Unknown is its own answer.
  */
  const keysKnown = keys.state === 'ready';
  const balanceKnown = balance.state === 'ready';
  const hasKey = keysKnown && (keys.data?.keys || []).some((key) => key.is_active);
  const funded = balanceKnown && (balance.data?.balance_usd ?? 0) > 0;

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Three steps to your first track</h2>
        <span className="note dim">About five minutes</span>
      </div>
      <div className="panel-body">
        <ol className="steps" role="list">
          <Step n={1} done={hasKey} current={keysKnown && !hasKey}
                title="Create an API key"
                note="One key, named for wherever you plan to use it. The secret is shown once, at creation."
                action={hasKey ? null : (
                  <button className="btn primary sm" onClick={() => onNavigate('keys')}>
                    Create key
                  </button>
                )} />
          <Step n={2} done={funded} current={hasKey && !funded} title="Add credit"
                note={!balanceKnown
                  ? 'We couldn’t read your balance just now. Submissions need a positive balance, or they return 402.'
                  : funded
                    ? 'Your balance is funded.'
                    : 'Your balance is $0.00, so submissions return 402. Credit is added by hand today — the page below has the contact.'}
                action={funded ? null : (
                  <Link className={`btn sm${hasKey ? ' primary' : ''}`} href="/developer/recharge/">
                    Add credit →
                  </Link>
                )} />
          <Step n={3} done={false} current={hasKey && funded}
                title="Send your first request"
                note="Export the key you saved, then submit any MP4.">
            <FirstRequest />
          </Step>
        </ol>
      </div>
    </section>
  );
}

/**
 * The command, against this deployment's own host.
 *
 * The host is filled in after mount rather than at module scope: this bundle
 * is prerendered at build time, where `window` does not exist, and a value
 * that differs between the prerendered HTML and the first client render is a
 * hydration mismatch. The placeholder is what the docs use, so the pre-mount
 * frame is still a correct command.
 */
function FirstRequest() {
  const [origin, setOrigin] = useState('https://YOUR-EDENN-HOST');
  // apiBase() first, page origin second. On the Firebase Hosting build the
  // console is served from a host that does not answer /api/v2 at all, so the
  // page's own origin would hand a new customer a command that 404s.
  useEffect(() => { setOrigin(apiBase() || window.location.origin); }, []);
  return (
    <CodeBlock text={`export EDENN_API_KEY="sk-..."

curl -X POST "${origin}/api/v2/jobs/video-music" \\
  -H "Authorization: Bearer $EDENN_API_KEY" \\
  -F "video=@sample.mp4" \\
  -F "modelspec=edenn_enhanced" \\
  -F "user_prompt=an upbeat electronic track, female vocals, English"`} />
  );
}

function Step({ n, done, current, title, note, action, children }) {
  return (
    <li className={`step${done ? ' done' : ''}${current && !done ? ' now' : ''}`}>
      <span className="step-n" aria-hidden="true">{done ? '✓' : n}</span>
      <div className="step-body">
        <strong>{title}</strong>
        <span className="note">{note}</span>
        {children}
      </div>
      {done ? <span className="badge live">Done</span> : action}
    </li>
  );
}

function CodeBlock({ text }) {
  // '' | 'copied' | 'failed'. The clipboard is denied outright on an insecure
  // origin, and a button that silently does nothing reads as a broken page —
  // the other two copy controls in this console say so too.
  const [state, setState] = useState('');

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState('copied');
      setTimeout(() => setState(''), 1600);
    } catch {
      setState('failed');
    }
  }

  return (
    <div className="code-block">
      <button className="btn ghost sm code-copy" onClick={copy}>
        {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy manually' : 'Copy'}
      </button>
      <pre><code>{text}</code></pre>
    </div>
  );
}

/**
 * One link, and no claims.
 *
 * This replaces a pair of cards that restated the documentation's model table
 * from a second location. They had already drifted — one deep-linked to the
 * latency section rather than the models section, and the comment above them
 * still described three tiers after `edenn_basic` was removed. Worse, the
 * labels ("Vocal track", "Full song") read as guarantees, and the section they
 * pointed at closes by withdrawing exactly that: vocals are a result of the
 * prompt, not of the model. One place describes the models now.
 */
function Docs() {
  return (
    <Link className="panel doclink" href="/developer/">
      <span className="doclink-body">
        <strong>API documentation</strong>
        <span className="note">Endpoints, models, parameters, error codes, and billing.</span>
      </span>
      <span className="btn sm">Open docs →</span>
    </Link>
  );
}
