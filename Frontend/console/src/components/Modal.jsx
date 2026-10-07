'use client';

import { useEffect, useRef } from 'react';

/**
 * Modal with the two behaviours people expect and notice the absence of:
 * Escape closes it, and focus starts inside it rather than back at the top of
 * the page. `window.confirm` gives neither, and reads as a browser error.
 */
export default function Modal({ title, children, onClose, dismissable = true }) {
  const panel = useRef(null);

  useEffect(() => {
    // React applies `autoFocus` during the commit phase, before passive
    // effects run — so focusing the panel here unconditionally would undo
    // every autoFocus a caller set, which is how the revoke dialog ended up
    // opening with nothing focused rather than on Cancel.
    const wanted = panel.current?.querySelector('[autofocus]');
    if (wanted) wanted.focus();
    else panel.current?.focus();
    if (!dismissable) return undefined;
    const onKey = (event) => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, dismissable]);

  return (
    <div className="scrim"
         onMouseDown={(e) => {
           if (dismissable && e.target === e.currentTarget) onClose();
         }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}
           tabIndex={-1} ref={panel}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}
