'use client';

import { useMemo, useState } from 'react';
import { GRAINS, bucketSpend } from '../lib/buckets';
import { usd } from '../lib/format';

/**
 * Spend over time. One bar per bucket; stacked by API key on request.
 *
 * **Colour follows the key, not its rank.** Slots are handed out from the sorted
 * key list once and then held: if a colour tracked position, filtering the range
 * would repaint every surviving key and the reader would think the data changed.
 *
 * The eight-hue categorical order is fixed and never cycled — past `SLOTS.length`
 * keys the tail folds into Other rather than reusing a hue, because two keys the
 * same colour is worse than one honest bucket. On the light surface three of
 * these hues sit under 3:1 against white, so the legend is not optional: it is
 * the visible-label relief that makes the chart readable without colour.
 */
const SLOTS = [
  { light: '#2a78d6', dark: '#3987e5' },
  { light: '#eb6834', dark: '#d95926' },
  { light: '#1baf7a', dark: '#199e70' },
  { light: '#eda100', dark: '#c98500' },
  { light: '#e87ba4', dark: '#d55181' },
  { light: '#008300', dark: '#008300' },
];
const OTHER = 'Other';

export default function SpendChart({
  rows, grain, onGrain, byKey, onByKey, loading = false,
}) {
  const [hover, setHover] = useState(null);

  const { buckets, series, total } = useMemo(
    () => bucketSpend(rows, { grain, byKey }), [rows, grain, byKey]);

  // Fold the tail before assigning colour, so Other always lands on the neutral.
  const named = series.slice(0, SLOTS.length).map((s) => s.id);
  const colorOf = (id) => {
    const at = named.indexOf(id);
    return at === -1 ? 'var(--text-3)' : `var(--series-${at + 1})`;
  };
  const memberOf = (id) => (named.includes(id) ? id : OTHER);

  const legend = byKey
    ? [...named, ...(series.length > SLOTS.length ? [OTHER] : [])]
    : [];

  const peak = Math.max(...buckets.map((b) => b.total), 0);
  const width = 1000;
  const height = 190;
  const slot = width / Math.max(buckets.length, 1);
  const bar = Math.max(3, Math.min(38, slot * 0.62));

  return (
    <section className="panel chart-panel">
      <div className="panel-head">
        <div>
          <h2>Total spend</h2>
          <div className="readout" style={{ fontSize: 26, marginTop: 4 }}>
            {loading ? '—' : usd(total)}
          </div>
        </div>
        <div className="chart-controls">
          <button className={`btn ghost sm${byKey ? ' on' : ''}`}
                  aria-pressed={byKey} onClick={() => onByKey(!byKey)}>
            Split by key
          </button>
          <div className="seg" role="group" aria-label="Time range">
            {GRAINS.map((g) => (
              <button key={g.id} aria-pressed={g.id === grain}
                      onClick={() => onGrain(g.id)}>{g.label}</button>
            ))}
          </div>
        </div>
      </div>

      <div className="panel-body">
        {loading ? (
          <div className="skeleton" style={{ height: 190 }} />
        ) : peak <= 0 ? (
          <p className="empty-state">No usage in this period</p>
        ) : (
          <div className="chart-plot">
            <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
                 role="img" aria-label={`Spend by ${GRAINS.find((g) => g.id === grain)?.label}, total ${usd(total)}`}>
              <line x1="0" y1={height - 0.5} x2={width} y2={height - 0.5}
                    stroke="var(--border)" strokeWidth="1" />
              {buckets.map((bucket, index) => {
                if (bucket.total <= 0) return null;
                const x = index * slot + (slot - bar) / 2;
                const parts = byKey
                  ? [...bucket.parts.entries()].sort(
                      (a, b) => named.indexOf(memberOf(a[0])) - named.indexOf(memberOf(b[0])))
                  : [['total', bucket.total]];
                let y = height;
                return (
                  <g key={bucket.key}
                     onMouseEnter={() => setHover(index)}
                     onMouseLeave={() => setHover(null)}>
                    {/* Hit target spans the whole slot, not just the bar. */}
                    <rect x={index * slot} y="0" width={slot} height={height}
                          fill="transparent" />
                    {parts.map(([member, amount]) => {
                      // 2px surface gap between stacked segments so adjacent
                      // fills read as separate marks rather than one block.
                      const raw = (amount / peak) * (height - 8);
                      const h = Math.max(2, raw - (parts.length > 1 ? 2 : 0));
                      y -= raw;
                      // No dimming of the other bars on hover. The first
                      // version faded them to 0.35 and the whole chart went
                      // pale the moment the pointer entered it — the tooltip
                      // is the affordance; the marks stay readable.
                      return (
                        <rect key={member} x={x} y={y} width={bar} height={h}
                              rx={3}
                              fill={byKey ? colorOf(memberOf(member)) : 'var(--accent)'} />
                      );
                    })}
                  </g>
                );
              })}
            </svg>
            {hover !== null && buckets[hover] ? (
              // Clamped away from both edges: an unclamped tooltip on the last
              // bucket — which is the one people look at most — hangs off the
              // panel and gets clipped.
              <Tip bucket={buckets[hover]} byKey={byKey} colorOf={colorOf}
                   memberOf={memberOf}
                   at={Math.min(86, Math.max(14,
                     ((hover + 0.5) / Math.max(buckets.length, 1)) * 100))} />
            ) : null}
          </div>
        )}

        <div className="chart-axis">
          <span>{buckets[0]?.label}</span>
          <span className="dim">peak {usd(peak)}</span>
          <span>{buckets[buckets.length - 1]?.label}</span>
        </div>

        {legend.length > 0 ? (
          <div className="legend">
            {legend.map((id) => (
              <span key={id}>
                <i style={{ background: colorOf(id) }} />
                <span className="mono">{id}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function Tip({ bucket, byKey, colorOf, memberOf, at }) {
  const parts = byKey
    ? [...bucket.parts.entries()].sort((a, b) => b[1] - a[1])
    : [];
  return (
    <div className="chart-tip" style={{ left: `${at}%` }}>
      <div className="chart-tip-head">
        <span>{bucket.label}</span>
        <span className="num">{usd(bucket.total)}</span>
      </div>
      {parts.map(([member, amount]) => (
        <div key={member} className="chart-tip-row">
          <i style={{ background: colorOf(memberOf(member)) }} />
          <span className="mono">{member}</span>
          <span className="num">{usd(amount)}</span>
        </div>
      ))}
    </div>
  );
}

export { SLOTS };
