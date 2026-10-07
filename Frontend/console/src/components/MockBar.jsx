'use client';

import { useEffect, useState } from 'react';
import { MOCK, SCENARIOS, scenario } from '../mock';

/**
 * The "none of this is real" badge, and the scenario switcher.
 *
 * The badge is not decoration. Mock mode renders plausible money — a balance, a
 * spend chart, an itemized bill — and a screenshot of it is indistinguishable
 * from a screenshot of a customer's account. Anything that can be mistaken for
 * production data has to say what it is on the same screen, not in a terminal
 * two windows away.
 *
 * It renders nothing at all unless the build set `NEXT_PUBLIC_MOCK=1` — `MOCK`
 * is a build-time constant, so in a normal build this whole component minifies
 * down to a `return null` and the markup below never ships. And it waits for
 * mount before drawing: the scenario comes from the query string, which the
 * prerender cannot see, and a server frame that disagrees with the first
 * client frame is a hydration error.
 */
export default function MockBar() {
  const [mounted, setMounted] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  if (!MOCK || !mounted) return null;

  const current = scenario();
  const meta = SCENARIOS.find((s) => s.id === current);

  function go(id) {
    const url = new URL(window.location.href);
    url.searchParams.set('mock', id);
    // A full navigation rather than a router push: the fixtures, the fake
    // session and every component's state all have to start over, and half a
    // reset would be a confusing thing to debug against.
    window.location.href = url.toString();
  }

  return (
    <div style={wrap}>
      {open ? (
        <div style={menu}>
          {SCENARIOS.map((option) => (
            <button key={option.id} type="button" onClick={() => go(option.id)}
                    style={{ ...item, ...(option.id === current ? itemOn : null) }}>
              <span style={{ fontWeight: 600 }}>{option.label}</span>
              <span style={{ opacity: 0.6, fontSize: 11 }}>{option.note}</span>
            </button>
          ))}
          <button type="button" onClick={() => window.location.reload()} style={reset}>
            Reload — discards keys created in this session
          </button>
        </div>
      ) : null}
      <button type="button" onClick={() => setOpen((was) => !was)} style={pill}>
        <span style={dot} />
        MOCK DATA
        <span style={{ opacity: 0.65, fontWeight: 400 }}>{meta?.label || current}</span>
        <span style={{ opacity: 0.5 }}>{open ? '▾' : '▴'}</span>
      </button>
    </div>
  );
}

/*
  Inline styles, deliberately. This is development scaffolding and it should
  leave no trace in globals.css — nobody should have to wonder later whether a
  `.mock-pill` rule is load-bearing for the real console.
*/
const wrap = {
  position: 'fixed', right: 16, bottom: 16, zIndex: 9999,
  display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8,
  fontFamily: 'ui-sans-serif, system-ui, sans-serif',
};

const pill = {
  display: 'flex', alignItems: 'center', gap: 8,
  padding: '7px 12px', borderRadius: 999, cursor: 'pointer',
  background: '#3a2a00', color: '#ffd66b', border: '1px solid #6b4e00',
  font: '600 11.5px/1 ui-sans-serif, system-ui, sans-serif', letterSpacing: '0.06em',
  boxShadow: '0 6px 20px rgba(0,0,0,0.45)',
};

const dot = {
  width: 7, height: 7, borderRadius: 999, background: '#ffb020',
  boxShadow: '0 0 0 3px rgba(255,176,32,0.22)',
};

const menu = {
  width: 268, padding: 6, borderRadius: 12,
  background: '#141414', border: '1px solid #2a2a2a',
  boxShadow: '0 18px 44px rgba(0,0,0,0.55)',
  display: 'grid', gap: 2,
};

const item = {
  display: 'grid', gap: 2, textAlign: 'left', cursor: 'pointer',
  padding: '8px 10px', borderRadius: 8, border: 0,
  background: 'transparent', color: '#e8e8e8', font: '400 12.5px/1.35 inherit',
};

const itemOn = { background: '#242424', color: '#ffd66b' };

const reset = {
  marginTop: 4, padding: '7px 10px', borderRadius: 8, cursor: 'pointer',
  border: '1px solid #2a2a2a', background: 'transparent',
  color: '#8a8a8a', font: '400 11px/1.3 inherit', textAlign: 'left',
};
