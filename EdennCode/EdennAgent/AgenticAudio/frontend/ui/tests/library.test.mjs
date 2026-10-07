import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
const script = await readFile(new URL('../../js/library-ui.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function setup(page = 'gallery', transport = {}, preview = false) {
  const dom = new JSDOM('<main id="root"></main>', { runScripts: 'outside-only', url: 'https://studio.test/' });
  const w = dom.window, revoked = [], media = [];
  w.matchMedia = () => ({ matches: true });
  w.URL.createObjectURL = () => 'blob:sketch-' + Math.random(); w.URL.revokeObjectURL = url => revoked.push(url);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Audio = class extends w.EventTarget {
    constructor() { super(); this.pending = []; this.currentTime = 0; this.paused = true; media.push(this); }
    play() { this.paused = false; return new Promise((resolve, reject) => this.pending.push({ resolve, reject })); }
    pause() { this.paused = true; this.dispatchEvent(new w.Event('pause')); }
    load() {} removeAttribute() {}
  };
  w.eval(script);
  const root = w.document.getElementById('root');
  const view = w.EdennLibrary.mount({ root, page, preview, transport: { kind: 'mock', listSessions: async () => ({ sessions: [] }), ...transport }, onNew() {}, onOpen() {} });
  const click = text => [...root.querySelectorAll('button')].find(b => b.textContent === text).click();
  return { dom, w, root, view, audio: media[0], revoked, click, cleanup() { view.destroy(); dom.window.close(); } };
}

test('playback keeps controls mounted through pause, buffering, finish, replay and failure', async () => {
  const t = setup(), { root, audio, w } = t;
  const play = root.querySelector('[data-preview="momentum"]'), dock = root.querySelector('.library-dock'), toggle = dock.querySelector('button');
  play.click(); assert.equal(toggle.dataset.state, 'loading');
  audio.pending[0].resolve(); await tick(); assert.equal(toggle.dataset.state, 'playing');
  audio.dispatchEvent(new w.Event('waiting')); assert.equal(toggle.dataset.state, 'loading');
  audio.dispatchEvent(new w.Event('playing')); assert.equal(toggle.dataset.state, 'playing');
  toggle.click(); assert.equal(toggle.dataset.state, 'paused');
  toggle.click(); audio.pending[1].resolve(); await tick();
  audio.ended = true; audio.currentTime = 12; audio.dispatchEvent(new w.Event('ended'));
  assert.equal(toggle.dataset.state, 'finished'); assert.equal(root.querySelector('.library-clock').textContent, '0:12 / 0:12');
  audio.ended = false; toggle.click(); assert.equal(audio.currentTime, 0);
  audio.pending[2].reject(Error('unavailable')); await tick(); assert.equal(toggle.dataset.state, 'error');
  toggle.click(); audio.pending[3].resolve(); await tick(); assert.equal(toggle.dataset.state, 'playing');
  assert.equal(root.querySelector('.library-dock'), dock); assert.equal(dock.querySelector('button'), toggle);
  assert.ok(t.revoked.length); t.cleanup();
});

test('stale play results cannot change the active sound and leaving releases the audio', async () => {
  const t = setup(), { root, audio } = t;
  root.querySelector('[data-preview="momentum"]').click();
  root.querySelector('[data-preview="first-light"]').click();
  audio.pending[0].reject(Error('old source')); await tick();
  assert.equal(root.querySelector('.library-dock-meta strong').textContent, 'First light');
  assert.equal(root.querySelector('.library-dock button').dataset.state, 'loading');
  audio.pending[1].resolve(); await tick();
  const dock = root.querySelector('.library-dock'); t.click('Cinematic');
  assert.equal(root.querySelector('.library-dock'), dock); assert.equal(dock.querySelector('button').dataset.state, 'playing');
  t.view.destroy(); assert.equal(audio.paused, true); assert.equal(root.children.length, 0); assert.equal(t.revoked.length, 2); t.dom.window.close();
});

test('pausing a pending preview invalidates its completion', async () => {
  const t = setup(), play = t.root.querySelector('[data-preview="momentum"]');
  play.click(); play.click(); t.audio.pending[0].resolve(); await tick();
  assert.equal(play.dataset.state, 'paused'); assert.equal(t.audio.paused, true); t.cleanup();
});

test('saved filtering preserves the clicked card when removing a saved direction', () => {
  const t = setup();
  t.root.querySelector('[aria-label="Save Momentum"]').click(); t.click('Saved');
  const card = t.root.querySelector('.library-sound'); t.root.querySelector('[aria-label="Unsave Momentum"]').click();
  assert.equal(t.root.querySelector('.library-sound'), card);
  t.click('All'); t.click('Saved'); assert.equal(t.root.querySelectorAll('.library-sound').length, 0); t.cleanup();
});

test('selection preserves row nodes; local archive can be undone and session fetch errors can retry', async () => {
  let fails = true;
  const t = setup('sessions', { listSessions: async () => { if (fails) throw Error('offline'); return { sessions: [{ session_id: 'one', title: 'Reel' }] }; } });
  await tick(); assert.match(t.root.querySelector('.library-status').textContent, /Couldn’t/);
  fails = false; t.click('Try again'); await tick();
  const row = t.root.querySelector('[data-session="one"]'), checkbox = row.querySelector('input'); checkbox.click();
  assert.equal(t.root.querySelector('[data-session="one"]'), row); assert.equal(checkbox.checked, true);
  t.click('Archive'); assert.equal(t.root.querySelectorAll('[data-session]').length, 0);
  t.click('Undo'); assert.equal(t.root.querySelectorAll('[data-session]').length, 1); t.cleanup();
});

test('late session load does not repopulate a destroyed page', async () => {
  let resolve;
  const t = setup('sessions', { listSessions: () => new Promise(r => { resolve = r; }) });
  t.view.destroy(); resolve({ sessions: [{ session_id: 'old' }] }); await tick();
  assert.equal(t.root.children.length, 0); t.dom.window.close();
});

test('folder creation and moving persist locally without modifying source sessions', async () => {
  const t = setup('sessions', { listSessions: async () => ({ sessions: [{ session_id: 'one', title: 'Reel' }] }) });
  await tick(); t.click('New folder');
  const input = t.root.querySelector('#library-folder-name'); input.value = 'Campaign';
  t.root.querySelector('form').dispatchEvent(new t.w.Event('submit', { cancelable: true }));
  t.root.querySelector('[aria-label="Move Reel"]').click();
  t.root.querySelector('#library-destination').value = 'Campaign';
  t.root.querySelector('form').dispatchEvent(new t.w.Event('submit', { cancelable: true }));
  assert.equal(t.root.querySelector('.library-session-open small').textContent, 'Campaign');
  assert.equal(JSON.parse(t.w.localStorage.getItem('edenn.library.v1.mock')).placement.one, 'Campaign');
  t.click('Undo'); assert.equal(t.root.querySelector('.library-session-open small'), null); t.cleanup();
});


test('explicit layout preview includes active and archived samples without fetching or opening sessions', async () => {
  let reads = 0;
  const t = setup('sessions', { listSessions: async () => { reads++; return { sessions: [] }; } }, true);
  await tick();
  assert.equal(reads, 0);
  assert.equal(t.root.querySelectorAll('[data-session]').length, 8);
  assert.equal(t.root.querySelector('.library-session-open').tagName, 'DIV');
  assert.equal(t.root.querySelectorAll('.library-preview-poster').length, 8);
  t.click('Archived');
  assert.equal(t.root.querySelectorAll('[data-session]').length, 3);
  assert.match(t.root.textContent, /summer_first_cut/);
  t.cleanup();
});
