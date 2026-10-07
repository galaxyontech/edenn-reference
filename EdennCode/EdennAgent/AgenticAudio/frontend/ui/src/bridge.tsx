import { createRoot, type Root } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { useEffect, useRef } from 'react';
import { MessageResponse } from './components/ai-elements/message';

type Step = { id: string; label: string; detail?: string; status: 'active' | 'complete' | 'pending' };
type Activity = {
  id: string; label: string; open: boolean; outcome: string; spinning: boolean;
  steps: Step[]; expandedHistory: boolean; message?: string;
  onToggle: (open: boolean) => void; onHistory: () => void; onRetry?: () => void;
};
const roots = new Map<HTMLElement, Root>();
function render(node: HTMLElement, content: React.ReactNode) {
  let root = roots.get(node);
  if (!root) { root = createRoot(node); roots.set(node, root); }
  flushSync(() => root.render(content));
}
function ActivityView(a: Activity) {
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const hidden = !a.expandedHistory ? Math.max(0, a.steps.length - 3) : 0;
  const failed = a.outcome !== 'active' && a.outcome !== 'complete';
  useEffect(() => {
    if (!a.open) return;
    const doc = root.current!.ownerDocument;
    function dismiss(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) a.onToggle(false);
    }
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape') { a.onToggle(false); trigger.current?.focus(); }
    }
    doc.addEventListener('pointerdown', dismiss);
    doc.addEventListener('keydown', escape);
    return () => {
      doc.removeEventListener('pointerdown', dismiss);
      doc.removeEventListener('keydown', escape);
    };
  }, [a.open, a.onToggle]);
  return <div ref={root} className="studio-activity">
    <button ref={trigger} type="button" className="cot__head" aria-controls={a.id}
      aria-expanded={a.open} aria-label={failed ? 'View activity — needs attention' : 'View activity'}
      title={a.label} onClick={() => a.onToggle(!a.open)}>
      <span className={a.outcome === 'active' ? 'activity-working' : 'activity-result'}>{a.label}</span>
      <i className="ti ti-chevron-down activity-arrow" aria-hidden="true" />
    </button>
    <div id={a.id} className="cot__body activity-popover" hidden={!a.open} inert={!a.open}
      role="region" aria-label="Response activity">
      <div className="activity-heading">{a.outcome === 'active' ? a.label : failed ? 'Needs attention' : 'Activity'}</div>
      {hidden > 0 && <button className="activity-history" onClick={a.onHistory}>Show {hidden} earlier steps</button>}
      {a.steps.map((step, index) => <div key={step.id} hidden={index < hidden}
        className={'cot__step' + (step.status === 'active' ? ' is-active' : '')}>
        <div className="cot__status">{step.label}</div>
        {step.detail && <div className="cot__thought">{step.detail}</div>}
      </div>)}
      {a.expandedHistory && a.steps.length > 3 && <button className="activity-history" onClick={a.onHistory}>Show fewer steps</button>}
      {a.message && <p className="cot__thought">{a.message}</p>}
      {a.onRetry && <button className="activity-retry" onClick={a.onRetry}>Review and retry</button>}
    </div>
  </div>;
}

const api = {
  activity(node: HTMLElement, activity: Activity) { render(node, <ActivityView {...activity} />); },
  message(node: HTMLElement, text: string) {
    render(node, <MessageResponse mode="static" className="studio-message" isAnimating={false}
      controls={false} components={{
        img: () => null,
        strong: ({ children }) => <strong>{children}</strong>,
        em: ({ children }) => <em>{children}</em>,
        a: ({ href, children }) => <a href={href && /^(https?:|mailto:)/i.test(href) ? href : undefined} target="_blank" rel="noopener noreferrer">{children}</a>,
      }}>{text}</MessageResponse>);
  },
};
// The legacy controller owns only the host node; each React root owns its descendants.
new MutationObserver(() => {
  for (const [node, root] of roots) if (!node.isConnected) { root.unmount(); roots.delete(node); }
}).observe(document.body, { subtree: true, childList: true });
(window as unknown as { EdennElements: typeof api }).EdennElements = api;
