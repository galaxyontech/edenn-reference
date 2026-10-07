'use client';

import { useEffect, useState } from 'react';

/**
 * Per-page contents, with the current section highlighted.
 *
 * The highlight is driven by which headings are *above* the top of the
 * viewport, not by IntersectionObserver's "is visible" — a long section whose
 * heading has scrolled away is still the section you are reading, and the
 * observer alone would leave the marker stuck on the last heading that
 * happened to be on screen.
 */
export default function Toc({ items }) {
  const [active, setActive] = useState(items[0]?.id || '');

  useEffect(() => {
    if (items.length === 0) return undefined;
    const headings = items
      .map((item) => document.getElementById(item.id))
      .filter(Boolean);
    if (headings.length === 0) return undefined;

    let frame = 0;
    const measure = () => {
      frame = 0;
      // 96px: below the sticky bar, so a heading counts as "reached" when it
      // arrives at the reading position rather than at the window edge.
      const line = 96;
      let current = headings[0];
      for (const heading of headings) {
        if (heading.getBoundingClientRect().top <= line) current = heading;
        else break;
      }
      // At the very bottom no further heading can cross the line, so the last
      // section would never light up on a short final section.
      const atBottom = window.innerHeight + window.scrollY
        >= document.documentElement.scrollHeight - 2;
      setActive(atBottom ? headings[headings.length - 1].id : current.id);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };

    measure();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
    };
  }, [items]);

  if (items.length === 0) return <aside className="docs-toc" />;

  return (
    <aside className="docs-toc">
      <div className="docs-toc-inner">
        <span className="eyebrow">On this page</span>
        <nav>
          {items.map((item) => (
            <a key={item.id} href={`#${item.id}`}
               className={item.level === 3 ? 'sub' : ''}
               aria-current={item.id === active ? 'true' : undefined}>
              {item.text}
            </a>
          ))}
        </nav>
      </div>
    </aside>
  );
}
