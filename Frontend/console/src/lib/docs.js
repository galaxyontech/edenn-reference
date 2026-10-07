/**
 * Build-time loader for the developer docs.
 *
 * Runs only during `next build` — the export is plain HTML, so nothing here
 * reaches the browser and the docs need no runtime beside the static files.
 *
 * Heading ids are injected by rewriting the rendered HTML rather than by
 * overriding marked's renderer. The renderer API has changed shape across
 * marked's majors; `<h2>…</h2>` has not. One regex pass produces the ids and
 * the table of contents together, so an anchor and its TOC entry cannot
 * disagree — which is the only bug that actually matters here.
 */
import fs from 'node:fs';
import path from 'node:path';
import { marked } from 'marked';

const DOCS_DIR = path.join(process.cwd(), 'content', 'docs');

/**
 * GitHub-flavoured slug, minus the transliteration: CJK headings keep their
 * characters. Stripping them would leave every Chinese heading with the same
 * empty slug, and every anchor on the page pointing at the first one.
 */
export function slugify(text) {
  return String(text)
    .trim()
    .toLowerCase()
    .replace(/[\s]+/g, '-')
    .replace(/[^\p{L}\p{N}_-]/gu, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '') || 'section';
}

function parseFrontmatter(raw) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(raw);
  if (!match) return { meta: {}, body: raw };
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const at = line.indexOf(':');
    if (at === -1) continue;
    meta[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return { meta, body: raw.slice(match[0].length) };
}

const stripTags = (html) => html.replace(/<[^>]+>/g, '').trim();

/** Rendered HTML with anchored headings, plus the TOC built from the same pass. */
function renderWithToc(markdown) {
  const toc = [];
  const seen = new Map();
  const html = marked.parse(markdown).replace(
    /<h([23])>([\s\S]*?)<\/h\1>/g,
    (_whole, level, inner) => {
      const text = stripTags(inner);
      let id = slugify(text);
      // Two "Examples" headings in one document would otherwise share an anchor and
      // the second would be unreachable.
      const taken = seen.get(id) || 0;
      seen.set(id, taken + 1);
      if (taken) id = `${id}-${taken}`;
      toc.push({ id, text, level: Number(level) });
      return `<h${level} id="${id}">${inner}</h${level}>`;
    },
  );
  return { html, toc };
}

/** Every doc, ordered by the numeric filename prefix. */
export function listDocs() {
  return fs.readdirSync(DOCS_DIR)
    .filter((name) => name.endsWith('.md'))
    .sort()
    .map((name) => {
      const raw = fs.readFileSync(path.join(DOCS_DIR, name), 'utf8');
      const { meta } = parseFrontmatter(raw);
      return {
        slug: name.replace(/^\d+-/, '').replace(/\.md$/, ''),
        title: meta.title || name,
        description: meta.description || '',
      };
    });
}

export function loadDoc(slug) {
  const found = fs.readdirSync(DOCS_DIR).find(
    (name) => name.endsWith('.md')
      && name.replace(/^\d+-/, '').replace(/\.md$/, '') === slug,
  );
  if (!found) return null;
  const raw = fs.readFileSync(path.join(DOCS_DIR, found), 'utf8');
  const { meta, body } = parseFrontmatter(raw);
  const { html, toc } = renderWithToc(body);
  return {
    slug,
    title: meta.title || slug,
    description: meta.description || '',
    html,
    toc,
  };
}
