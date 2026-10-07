/**
 * Package the export for Firebase Hosting (the V1 deployment).
 *
 * The build lives at /console because that is where the Azure container mounts
 * it, and Next bakes that prefix into every asset URL. So Hosting has to serve
 * it from the same path — hence the copy into deploy/console rather than
 * pointing Hosting at out/ directly. One artifact, two homes.
 *
 *   pnpm build && pnpm pack:hosting
 *   firebase deploy --only hosting --project <project-id>
 */
import { cp, mkdir, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(root, 'out');
const deploy = path.join(root, 'deploy');

if (!existsSync(out)) {
  console.error('No out/ directory. Run `pnpm build` first.');
  process.exit(1);
}

await rm(deploy, { recursive: true, force: true });
await mkdir(path.join(deploy, 'console'), { recursive: true });
await cp(out, path.join(deploy, 'console'), { recursive: true });

// Anything not under /console is a mistyped URL, not a route — send it to the
// console rather than serving a blank 404 from a domain that hosts one page.
await writeFile(
  path.join(root, 'firebase.json'),
  `${JSON.stringify(
    {
      hosting: {
        public: 'deploy',
        ignore: ['firebase.json', '**/.*', '**/node_modules/**'],
        redirects: [{ source: '/', destination: '/console/', type: 302 }],
        cleanUrls: true,
      },
    },
    null,
    2,
  )}\n`,
);

console.log('Packed deploy/console + firebase.json.');
console.log('Next: firebase deploy --only hosting --project <project-id>');
