'use client';

/** Page title, with room for the one action that belongs to the whole page. */
export default function PageHead({ eyebrow, title, action = null }) {
  return (
    <div className="page-head">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
      </div>
      {action}
    </div>
  );
}
