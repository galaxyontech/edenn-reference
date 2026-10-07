import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';

const bundle = await readFile(new URL('../../js/ai-elements.js', import.meta.url), 'utf8');
function setup() {
  const dom = new JSDOM('<!doctype html><body><main></main></body>', { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://studio.test' });
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  dom.window.eval(bundle);
  const host = dom.window.document.createElement('div');
  dom.window.document.querySelector('main').append(host);
  return { dom, host, api: dom.window.EdennElements };
}

test('activity bounds visible history and preserves manual disclosure and focus across updates', () => {
  const { dom, host, api } = setup();
  const state = { id: 'activity-test', label: 'Analyzing', open: true, outcome: 'active', spinning: false,
    steps: Array.from({ length: 8 }, (_, i) => ({ id: String(i), label: `Step ${i}`, status: i === 7 ? 'active' : 'complete' })), expandedHistory: false,
    onToggle(open) { state.open = open; api.activity(host, state); },
    onHistory() { state.expandedHistory = !state.expandedHistory; api.activity(host, state); } };
  api.activity(host, state);
  assert.equal(host.querySelectorAll('.cot__step:not([hidden])').length, 3);
  const head = host.querySelector('.cot__head');
  assert.equal(head.getAttribute('aria-controls'), 'activity-test');
  assert.equal(head.getAttribute('aria-expanded'), 'true');
  head.focus(); head.click();
  assert.equal(head.getAttribute('aria-expanded'), 'false');
  assert.ok(host.querySelector('.cot__body').hasAttribute('inert'));
  state.label = 'Rendering'; state.spinning = true;
  api.activity(host, state);
  assert.equal(dom.window.document.activeElement, head);
  assert.equal(head.getAttribute('aria-expanded'), 'false');
  assert.equal(host.querySelectorAll('.activity-working').length, 1);
  head.click(); host.querySelector('.activity-history').click();
  assert.equal(host.querySelectorAll('.cot__step:not([hidden])').length, 8);
  state.outcome = 'complete'; state.spinning = false;
  api.activity(host, state);
  assert.equal(host.querySelector('.activity-working'), null);
  dom.window.close();
});

test('activity dismisses with Escape or outside click and retains a plain completed trigger', async () => {
  const { dom, host, api } = setup();
  const state = { id: 'activity-dismiss', label: 'Worked for 12s', open: false, outcome: 'complete', spinning: false,
    steps: [{ id: 'one', label: 'Analyzed', detail: 'Four scenes', status: 'complete' }], expandedHistory: false,
    onToggle(open) { state.open = open; api.activity(host, state); }, onHistory() {} };
  api.activity(host, state);
  const head = host.querySelector('.cot__head');
  assert.equal(head.textContent, 'Worked for 12s');
  assert.ok(head.querySelector('.activity-arrow'));
  assert.ok(host.querySelector('.activity-popover').hidden);
  head.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(host.querySelector('.activity-popover').hidden, false);
  dom.window.document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(state.open, false);
  assert.equal(dom.window.document.activeElement, head);
  head.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  dom.window.document.body.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true }));
  assert.equal(state.open, false);
  dom.window.close();
});

test('message prose renders semantic markdown without executable links or remote images', async () => {
  const { dom, host, api } = setup();
  api.message(host, '**Direction**\n\n- Warm pads\n- Soft percussion\n\n[Listen](https://studio.test/take) [unsafe](javascript:alert(1))\n\n![remote](https://other.test/pixel.png)\n\n<script>window.bad=true</script>');
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(host.querySelector('strong').textContent, 'Direction');
  assert.equal(host.querySelectorAll('li').length, 2);
  assert.equal(host.querySelector('a[href="https://studio.test/take"]').rel, 'noopener noreferrer');
  assert.equal(host.querySelector('a[href^="javascript:"]'), null);
  assert.equal(host.querySelector('img'), null);
  assert.equal(host.querySelector('script'), null);
  assert.equal(dom.window.bad, undefined);
  dom.window.close();
});

test('long history can be removed and its hosts remounted without stale roots', async () => {
  const { dom, host, api } = setup();
  const start = performance.now();
  for (let i = 0; i < 100; i++) {
    const message = dom.window.document.createElement('div'); host.append(message);
    api.message(message, `Take **${i + 1}** is ready.\n\n- Warm pads\n- Soft percussion`);
  }
  assert.equal(host.querySelectorAll('.studio-message').length, 100);
  const elapsed = performance.now() - start;
  host.replaceChildren();
  await new Promise(resolve => setTimeout(resolve, 0));
  api.message(host, 'New session');
  assert.equal(host.textContent, 'New session');
  console.log(`100 formatted messages in jsdom: ${Math.round(elapsed)}ms (diagnostic, not a browser frame benchmark)`);
  dom.window.close();
});
