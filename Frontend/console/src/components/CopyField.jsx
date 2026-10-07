'use client';

import { useEffect, useState } from 'react';

/**
 * A secret shown once, with the copy button that makes "shown once" survivable.
 *
 * Without this the customer hand-selects 47 characters of base64 from a wrapped
 * <div>, and a partial selection produces a key that fails authentication with
 * no hint that it was truncated. The button is the feature; the styling is not.
 */
export default function CopyField({ value, label }) {
  const [state, setState] = useState('idle');  // idle | copied | failed

  useEffect(() => {
    if (state === 'idle') return undefined;
    const timer = setTimeout(() => setState('idle'), 2200);
    return () => clearTimeout(timer);
  }, [state]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
    } catch {
      // Clipboard access can be denied outright (permissions, insecure
      // context). Say so, rather than showing a success that did not happen.
      setState('failed');
    }
  }

  return (
    <div>
      {label ? <div className="eyebrow" style={{ marginBottom: 6 }}>{label}</div> : null}
      <div className="secret-row">
        <code className="secret">{value}</code>
        <button type="button" className="btn" onClick={copy}
                aria-label={state === 'copied' ? 'Copied' : 'Copy to clipboard'}>
          {state === 'copied' ? 'Copied' : 'Copy'}
        </button>
      </div>
      {state === 'failed' ? (
        <p className="note" style={{ color: 'var(--danger)' }}>
          The browser denied clipboard access. Select the value and copy it manually.
        </p>
      ) : null}
    </div>
  );
}
