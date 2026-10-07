import { notFound } from 'next/navigation';
import { listDocs, loadDoc } from '../../../src/lib/docs';
import DocsShell from '../../../src/components/docs/DocsShell';
import DocBody from '../../../src/components/docs/DocBody';
import Toc from '../../../src/components/docs/Toc';

/** Required by `output: 'export'` — the whole doc set becomes static HTML. */
export function generateStaticParams() {
  return listDocs().map((doc) => ({ slug: doc.slug }));
}

export async function generateMetadata({ params }) {
  const { slug } = await params;
  const doc = loadDoc(slug);
  if (!doc) return {};
  return {
    title: `${doc.title} — Edenn API`,
    description: doc.description,
  };
}

export default async function DocPage({ params }) {
  const { slug } = await params;
  const doc = loadDoc(slug);
  if (!doc) notFound();
  const docs = listDocs();

  return (
    <DocsShell docs={docs} activeSlug={slug} aside={<Toc items={doc.toc} />}>
      <div className="doc-head">
        <h1>{doc.title}</h1>
        {doc.description ? <p className="note">{doc.description}</p> : null}
      </div>
      <DocBody html={doc.html} />
    </DocsShell>
  );
}
