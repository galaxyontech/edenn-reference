'use client';

import { useEffect, useRef } from 'react';

/**
 * The rendered markdown, plus the two things it cannot do as static HTML.
 *
 * 1. **A copy button on every code block.** This is a curl-heavy document;
 *    hand-selecting a wrapped multi-line command is how people end up pasting
 *    a broken one.
 * 2. **The real base URL.** Every occurrence of ORIGIN_TOKEN becomes the origin
 *    this page is served from. The docs ship inside the same image as the API,
 *    so a hardcoded host would be wrong on every deployment but one — and a
 *    wrong base URL in a copyable snippet costs someone an afternoon.
 *
 *    It is a plain token in the markdown rather than a `<span>` because the
 *    place it is needed most is inside a fenced code block, and markdown
 *    escapes HTML there — the span would have rendered as visible tag soup.
 *    The token is written to read as a placeholder, so a reader with JS
 *    disabled still sees something honest rather than `{{ORIGIN}}`.
 */
const ORIGIN_TOKEN = 'https://YOUR-EDENN-HOST';

export default function DocBody({ html }) {
  const root = useRef(null);

  useEffect(() => {
    const host = root.current;
    if (!host) return undefined;

    const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeValue.includes(ORIGIN_TOKEN)) {
        node.nodeValue = node.nodeValue.split(ORIGIN_TOKEN)
          .join(window.location.origin);
      }
    }

    const cleanups = [];
    for (const pre of host.querySelectorAll('pre')) {
      if (pre.querySelector('.copy')) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy';
      button.textContent = 'Copy';
      const onClick = async () => {
        const code = pre.querySelector('code');
        try {
          await navigator.clipboard.writeText(code ? code.innerText : pre.innerText);
          button.textContent = 'Copied';
        } catch {
          // Clipboard is permission-gated and blocked outright on insecure
          // origins. Saying so beats a button that silently does nothing.
          button.textContent = 'Copy manually';
        }
        setTimeout(() => { button.textContent = 'Copy'; }, 1600);
      };
      button.addEventListener('click', onClick);
      pre.appendChild(button);
      cleanups.push(() => {
        button.removeEventListener('click', onClick);
        button.remove();
      });
    }
    return () => cleanups.forEach((undo) => undo());
  }, [html]);

  return (
    <article className="prose" ref={root}
             dangerouslySetInnerHTML={{ __html: html }} />
  );
}
