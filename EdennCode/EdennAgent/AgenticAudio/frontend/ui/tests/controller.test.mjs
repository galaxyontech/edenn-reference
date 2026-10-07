import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
const root = new URL('../../', import.meta.url);
const html = await readFile(new URL('index.html', root), 'utf8');
const scripts = await Promise.all(['studio-state.js', 'ai-elements.js', 'library-ui.js', 'app.js'].map(name => readFile(new URL('js/' + name, root), 'utf8')));
const tick = () => new Promise(resolve => setTimeout(resolve, 15));
const snapshot = (id, text) => ({ session: { session_id: id }, state: {}, messages: [{ role: 'assistant', content: text }] });
async function setup() {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://studio.test/?backend=real&session=first' });
  const pending = [], gets = [];
  dom.window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  dom.window.EdennUI = { reducedMotion: () => true, waveform() {} };
  dom.window.HTMLMediaElement.prototype.pause = function () {};
  dom.window.HTMLMediaElement.prototype.load = function () {};
  dom.window.ResizeObserver = class { observe() {} disconnect() {} };
  dom.window.WebSocket = class { constructor() { throw Error('socket unavailable'); } };
  dom.window.fetch = async (url, options) => {
    if (options?.method === 'POST') return new Promise(resolve => pending.push({ body: JSON.parse(options.body), resolve }));
    gets.push(url);
    const data = url.endsWith('/sessions') ? { sessions: [{ session_id: 'second', phase: 'planning' }] }
      : snapshot(url.endsWith('/second') ? 'second' : 'first', url.endsWith('/second') ? 'Second session' : 'First session');
    return { ok: true, json: async () => data };
  };
  await tick();
  scripts.forEach(script => dom.window.eval(script));
  await tick();
  return { dom, pending, gets, controller: dom.window.__edenn };
}

test('unavailable socket recovers a session and rejects old terminal and refresh events during the next request', async () => {
  const { dom, pending, gets, controller: { app, onEvent } } = await setup();
  assert.equal(gets.length, 2); // resume plus handshake fallback
  assert.equal(dom.window.document.querySelectorAll('.studio-message').length, 1);
  const revision = app.requests.revision;
  app.conn.send({ content: 'First request' });
  const first = pending[0].body.request_id;
  app.conn.send({ content: 'Second request' });
  assert.equal(pending.length, 1); // serialize actions
  pending[0].resolve({ ok: true, json: async () => ({ events: [], snapshot: snapshot('first', 'First session') }) });
  await tick();
  const second = pending[1].body.request_id;
  assert.notEqual(first, second);
  onEvent({ event_type: 'session.opened', request_id: first, payload: { snapshot: snapshot('first', 'Stale') } });
  onEvent({ event_type: 'session.opened', source: 'refresh', revision, payload: { snapshot: snapshot('first', 'Old refresh') } });
  assert.equal(app.requests.active, second);
  assert.equal(app.snapshot.messages[0].content, 'First session');
  pending[1].resolve({ ok: true, json: async () => ({ events: [], snapshot: snapshot('first', 'First session') }) });
  await tick();
  assert.equal(app.requests.active, null);
  dom.window.close();
});

test('History switches conversation roots and composer listeners without keeping the previous session', async () => {
  const { dom, controller: { app } } = await setup();
  dom.window.document.getElementById('tb-history').click();
  await tick();
  dom.window.document.querySelector('.history-pop__row').click();
  await tick();
  assert.equal(app.sessionId, 'second');
  assert.equal(dom.window.document.querySelectorAll('.studio-message').length, 1);
  assert.equal(dom.window.document.querySelector('.studio-message').textContent, 'Second session');
  assert.equal(app.renderedCount, 1);
  dom.window.close();
});

test('incoming activity preserves the reading anchor and Jump to latest restores following', async () => {
  const { dom, controller: { app, onEvent } } = await setup();
  const thread = dom.window.document.getElementById('thread');
  const anchor = dom.window.document.getElementById('thread-inner').firstElementChild;
  let scrollTop = 300, addedHeight = 0;
  Object.defineProperties(thread, {
    clientHeight: { value: 200 }, scrollHeight: { value: 1000 },
    scrollTop: { get: () => scrollTop, set: value => { scrollTop = Math.max(0, Math.min(800, value)); } },
  });
  thread.getBoundingClientRect = () => ({ top: 0, bottom: 200 });
  anchor.getBoundingClientRect = () => ({ top: 200 + addedHeight - scrollTop, bottom: 500 + addedHeight - scrollTop });
  app._programmaticScroll = false;
  thread.dispatchEvent(new dom.window.Event('scroll'));
  assert.equal(app._followLatest, false);
  addedHeight = 50;
  onEvent({ event_type: 'agent.reasoning', payload: { status: 'Rendering', thought: 'Preparing your clip' } });
  assert.equal(scrollTop, 350);
  assert.equal(anchor.getBoundingClientRect().top, -100);
  const jump = dom.window.document.getElementById('jump-latest');
  assert.equal(jump.hidden, false);
  jump.click();
  assert.equal(scrollTop, 800);
  assert.equal(app._followLatest, true);
  dom.window.close();
});


test('completed activity sits beside its reply and energy curves stay in the primary choices', async () => {
  const { dom, controller: { app, onEvent } } = await setup();
  onEvent({ event_type: 'agent.reasoning', payload: { status: 'Planning', thought: 'Comparing two directions' } });
  app._thinking.started = Date.now() - 65000;
  const next = snapshot('first', 'First session');
  next.messages.push({ role: 'assistant', content: 'Choose a direction.' });
  next.state.proposals = [
    { proposal_id: 'a', title: 'Driving', attributes: { energy: [0.1, 0.7, 0.9], energy_note: 'Builds', bpm: 128 } },
    { proposal_id: 'b', title: 'Warm', attributes: { energy: [0.2, 0.3, 0.4], energy_note: 'Steady', instruments: ['Piano'] } },
  ];
  onEvent({ event_type: 'session.opened', payload: { snapshot: next, turn_complete: true } });
  await tick();
  assert.equal(dom.window.document.querySelectorAll('#thread-inner > .cot').length, 0);
  assert.equal(dom.window.document.querySelectorAll('.aname .cot__head').length, 1);
  assert.equal(dom.window.document.querySelector('.activity-result').textContent, 'Worked for 1m 5s');
  assert.equal(dom.window.document.querySelector('.aname .cot__head').getAttribute('aria-expanded'), 'false');
  assert.equal(dom.window.document.querySelectorAll('.direction-option .dt-spark').length, 2);
  assert.equal(dom.window.document.querySelectorAll('.direction-option .dt-badge').length, 0);
  assert.equal(dom.window.document.querySelectorAll('.choice-details .dt-spark').length, 0);
  dom.window.close();
});

test('local disclosures pause following, retain their anchor, and support interrupted transitions', async () => {
  const { dom, controller: { app } } = await setup();
  const doc = dom.window.document;
  const details = doc.createElement('details');
  details.innerHTML = '<summary>Previous direction</summary><div>Direction details</div>';
  doc.getElementById('thread-inner').append(details);
  const summary = details.querySelector('summary');
  summary.getBoundingClientRect = () => ({ top: 120, height: 24 });
  details.getBoundingClientRect = () => ({ top: 120, height: details.open ? 180 : 24 });
  app._followLatest = true;
  summary.click();
  assert.equal(details.open, true);
  assert.equal(app._followLatest, false);
  assert.equal(app._disclosureAnchor.node, summary);
  assert.equal(app._disclosureAnchor.top, 120);
  summary.click();
  assert.equal(details.open, false);
  assert.ok(doc.getElementById('thread-inner').style.minHeight);
  dom.window.EdennUI.reducedMotion = () => false;
  const animations = [];
  details.animate = () => {
    const animation = { cancel() { this.cancelled = true; }, onfinish: null };
    animations.push(animation);
    return animation;
  };
  summary.click();
  summary.click();
  assert.equal(animations[0].cancelled, true);
  animations[1].onfinish();
  assert.equal(details.open, false);
  assert.equal(details.style.height, '');
  doc.getElementById('thread').dispatchEvent(new dom.window.Event('wheel'));
  assert.equal(app._disclosureAnchor, null);
  assert.equal(doc.getElementById('thread-inner').style.minHeight, '');
  dom.window.close();
});

test('selecting a direction animates the existing card block into its receipt', async () => {
  const { dom, controller: { app, onEvent } } = await setup();
  const next = snapshot('first', 'First session');
  next.state.proposals = [{ proposal_id: 'a', title: 'Driving', attributes: {} }];
  onEvent({ event_type: 'session.opened', payload: { snapshot: next, turn_complete: true } });
  const original = app._blocks.proposals;
  original.getBoundingClientRect = () => ({ height: 240, top: 100 });
  dom.window.EdennUI.reducedMotion = () => false;
  let transition;
  dom.window.Element.prototype.animate = function (frames, options) {
    transition = { frames, options, cancel() {} };
    return transition;
  };
  next.state.approved_direction = true;
  next.state.approved_proposal_id = 'a';
  onEvent({ event_type: 'session.opened', payload: { snapshot: next, turn_complete: true } });
  assert.equal(app._blocks.proposals, original);
  assert.equal(transition.frames[0].height, '240px');
  assert.equal(transition.options.duration, 320);
  assert.equal(original.querySelector('details').open, true);
  transition.onfinish();
  assert.equal(original.querySelector('details').open, false);
  assert.equal(app._followLatest, false);
  dom.window.close();
});

test('library pages filter directions and prepare a draft without submitting generation', async () => {
  const { dom, pending } = await setup();
  const doc = dom.window.document;
  doc.querySelector('[data-home="gallery"]').click();
  assert.equal(doc.querySelector('#library-page h1').textContent, 'Gallery');
  assert.equal(doc.querySelectorAll('.library-sound').length, 6);
  const search = doc.querySelector('.library-search');
  search.value = 'Momentum'; search.dispatchEvent(new dom.window.Event('input'));
  assert.equal(doc.querySelectorAll('.library-sound').length, 1);
  doc.querySelector('.library-action').click();
  doc.querySelector('.library-drawer .btn-primary').click();
  assert.equal(doc.getElementById('new-session-page').hidden, false);
  assert.match(doc.getElementById('start-text').value, /Driving electronic/);
  assert.equal(pending.length, 0);
  doc.querySelector('[data-home="sessions"]').click();
  await tick();
  assert.equal(doc.querySelectorAll('.library-session-open').length, 1);
  assert.match(doc.querySelector('.library-session-open').textContent, /Session second/);
  dom.window.close();
});
