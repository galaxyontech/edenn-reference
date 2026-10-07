import { listDocs } from '../../src/lib/docs';
import DocsShell from '../../src/components/docs/DocsShell';
import { DOCS_VERSION } from '../../src/lib/version';

export const metadata = {
  title: 'Edenn API documentation',
  description: 'Integration documentation for the video and image soundtrack APIs.',
};

export default function DeveloperIndex() {
  const docs = listDocs();
  return (
    <DocsShell docs={docs}>
      <article className="prose">
        <h1>API documentation</h1>
        <p>
          Send a video or a set of images and get back a finished video with an
          AI-composed soundtrack. The API is asynchronous and durable: submit to
          receive a <code>job_id</code>, poll for the result, and nothing is lost
          if the connection drops.
        </p>
        <p className="note dim">
          Documentation version <strong>{DOCS_VERSION}</strong>. Quote it when
          reporting a problem with these pages.
        </p>
      </article>
      <div className="doc-cards">
        {docs.map((doc) => (
          <a key={doc.slug} className="doc-card"
             href={`/console/developer/${doc.slug}/`}>
            <strong>{doc.title}</strong>
            <span>{doc.description}</span>
          </a>
        ))}
      </div>
    </DocsShell>
  );
}
