import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
const source = await readFile(new URL('../../js/timeline-mode.js', import.meta.url), 'utf8');

test('clip selection preserves timeline nodes, scroll position and chosen proportions', () => {
  const dom = new JSDOM('<div id="tstage"></div>', { runScripts: 'outside-only', url: 'https://studio.test/' });
  const w = dom.window;
  w.ResizeObserver = class { observe() {} disconnect() {} };
  w.eval(source);
  w.EdennTimeline.boot();
  const snapshot = { session_id: 'selection-test', state: { observation: { duration_s: 12, scenes: [] }, candidates: [], layers: { sfx: [{ id: 'hit', label: 'Hit', start_s: 2, end_s: 3 }] } } };
  w.EdennTimeline.render(snapshot);
  const stage = w.document.querySelector('#tstage');
  const timeline = stage.querySelector('.tl-tl');
  const lane = timeline.querySelector('.lane-sfx');
  const clip = timeline.querySelector('[data-clip-id]');
  assert.ok(clip);
  stage.style.setProperty('--timeline-share', '40%');
  timeline.style.setProperty('--timeline-zoom', '2');
  timeline.scrollLeft = 80;
  timeline.scrollTop = 12;
  for (const selected of [true, false]) {
    clip.click();
    w.EdennTimeline.render(snapshot);
    assert.equal(timeline.querySelector('[data-clip-id]'), clip);
    assert.equal(timeline.querySelector('.lane-sfx'), lane);
    assert.equal(clip.getAttribute('aria-pressed'), String(selected));
    assert.equal(timeline.querySelector('.tl-selection'), null);
    assert.equal(timeline.scrollLeft, 80);
    assert.equal(timeline.scrollTop, 12);
    assert.equal(stage.style.getPropertyValue('--timeline-share'), '40%');
    assert.equal(timeline.style.getPropertyValue('--timeline-zoom'), '2');
  }
  dom.window.close();
});
