/**
 * Docs chrome: brand bar, document nav, content, per-page contents.
 *
 * A server component on purpose — the docs are the one part of this app that
 * must render for someone who is not signed in, and every byte of client JS
 * here is JS a reader waits on to see a curl command.
 */
import Link from 'next/link';
import { DOCS_VERSION } from '../../lib/version';

export default function DocsShell({ docs, activeSlug, aside = null, children }) {
  return (
    <div className="docs">
      <header className="docs-bar">
        <Link className="brand" href="/">
          <span className="brand-mark">E</span>Edenn API
          <span className="version">{DOCS_VERSION}</span>
        </Link>
        {/* "Console" rather than "Back": most readers arrive here before they
            have an account, so this is a forward step, not a way back. */}
        <Link className="btn sm" href="/">Console</Link>
      </header>
      <div className={`docs-body${aside ? '' : ' no-aside'}`}>
        <nav className="docs-nav" aria-label="Documentation">
          {docs.map((doc) => (
            // Link, not <a>: these are prerendered pages in the same app, so a
            // client-side transition is instant. A plain anchor made every
            // sidebar click a full document reload — ~103 kB of JS re-parsed to
            // move between two static pages.
            //
            // The href must OMIT the basePath. next.config sets basePath
            // '/console' and Link prepends it; writing '/console/developer/x'
            // here produces '/console/console/developer/x', matches no route,
            // and Link silently falls back to a hard navigation — which looks
            // exactly like it working, only slow.
            <Link key={doc.slug} href={`/developer/${doc.slug}/`}
                  aria-current={doc.slug === activeSlug ? 'page' : undefined}>
              {doc.title}
            </Link>
          ))}
        </nav>
        <main className="docs-main">{children}</main>
        {aside}
      </div>
    </div>
  );
}
