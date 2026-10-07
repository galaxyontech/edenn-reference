import { build } from 'esbuild';
import { readFile, writeFile } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
const result = await build({
  entryPoints: ['src/bridge.tsx'], bundle: true, minify: true,
  outfile: '../js/ai-elements.js', format: 'iife', target: 'es2022',
  define: { 'process.env.NODE_ENV': '"production"' }, metafile: true,
  legalComments: 'eof',
});
const bytes = await readFile('../js/ai-elements.js');
await writeFile('bundle-report.json', JSON.stringify({
  bytes: bytes.length, gzipBytes: gzipSync(bytes).length,
  inputs: Object.keys(result.metafile.inputs).length,
}, null, 2) + '\n');
if (gzipSync(bytes).length > 250000) throw new Error('Studio component bundle exceeds the 250 KB gzip budget');
console.log(`Studio components: ${bytes.length} bytes; ${gzipSync(bytes).length} gzip bytes`);
