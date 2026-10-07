const test = require('node:test');
const assert = require('node:assert/strict');
const { setTimeout: nextTask } = require('node:timers/promises');
const { MockBackend } = require('../frontend/mock-backend.js');
const { RequestRegistry, upsertStep, resolveOutput } = require('../frontend/js/studio-state.js');

test('late terminal events and pre-turn snapshots cannot replace a newer attempt', () => {
  const requests = new RequestRegistry();
  const revision = requests.revision;
  requests.begin('first');
  assert.equal(requests.refreshIsCurrent(revision), false);
  requests.finish('first', 'interrupted');
  requests.begin('retry', 'first');
  assert.equal(requests.accepts('first'), false);
  assert.equal(requests.finish('first', 'complete'), false);
  assert.equal(requests.active, 'retry');
  assert.equal(requests.requests.get('retry').retryOf, 'first');
  assert.equal(requests.finish('retry', 'complete'), true);
});

test('repeated identified steps update details without duplicating activity', () => {
  const steps = [];
  upsertStep(steps, { step_id: 'analyze', status: 'Analyzing' });
  upsertStep(steps, { step_id: 'analyze', status: 'Analyzed', thought: 'Four scenes', state: 'complete' });
  upsertStep(steps, { step_id: 'plan', status: 'Planning' });
  assert.equal(steps.length, 2);
  assert.equal(steps[0].label, 'Analyzed');
  assert.equal(steps[0].detail, 'Four scenes');
  assert.equal(steps[0].status, 'complete');
  assert.equal(steps[1].status, 'active');
});

test('mock events retain the request identity through asynchronous hydration', async () => {
  const { connection, events, snapshot } = await session();
  await connection.choose({ choice_type: 'clarification', target_id: 'music_only', request_id: 'layers' });
  assert.equal(events.at(-1).request_id, 'layers');
  await connection.choose({ choice_type: 'proposal', target_id: snapshot().state.proposals[0].proposal_id, request_id: 'generate' });
  await nextTask(10);
  assert.equal(events.at(-1).request_id, 'generate');
  assert.equal(events.at(-1).payload.turn_complete, false);
});

async function session(options) {
  const backend = new MockBackend({ delay: 0, ...options });
  const { session_id: id } = backend.createSession({ initial_message: 'Score this reel' });
  const events = [];
  const connection = backend.connect(id, { onEvent: event => events.push(event) });
  await Promise.resolve();
  return { connection, events, snapshot: () => backend.getSnapshot(id) };
}

test('bootstrap completes the activity turn so the layer picker can proceed', async () => {
  const { events, snapshot } = await session();
  assert.equal(events[0].event_type, 'session.opened');
  assert.equal(events[0].payload.turn_complete, true);
  assert.equal(snapshot().state.pending_clarification.gate, 'intent');
});

test('voice-only selection drafts narration without offering music directions', async () => {
  const { connection, snapshot } = await session();
  await connection.choose({ choice_type: 'clarification', target_id: 'voiceover_only' });
  assert.deepEqual(snapshot().state.production_plan.layers, ['voiceover']);
  assert.equal(snapshot().state.layers.voiceover.status, 'draft');
  assert.equal(snapshot().state.proposals.length, 0);
  assert.equal(snapshot().state.pending_clarification, null);
});

test('demo effects appear only when explicitly included in a full build', async () => {
  for (const includeEffects of [false, true]) {
    const { connection, snapshot } = await session();
    await connection.choose({ choice_type: 'clarification', target_id: 'full_audio',
      payload: { layers: includeEffects ? ['music', 'voiceover', 'sfx'] : ['music', 'voiceover'] } });
    assert.equal(snapshot().state.layers.sfx.length, includeEffects ? 3 : 0);
    assert.ok(snapshot().state.layers.sfx.every(effect => effect.start_s < effect.end_s));
  }
});

test('background take hydration cannot complete a subsequent activity turn', async () => {
  const { connection, events, snapshot } = await session();
  await connection.choose({ choice_type: 'clarification', target_id: 'music_only' });
  await connection.choose({ choice_type: 'proposal', target_id: snapshot().state.proposals[0].proposal_id });
  assert.equal(snapshot().state.candidates[0].status, 'queued');
  assert.equal(events.at(-1).payload.turn_complete, true);
  await nextTask(10);
  assert.ok(snapshot().state.candidates.every(candidate => candidate.status === 'completed'));
  assert.equal(events.at(-1).payload.turn_complete, false);
  await connection.choose({ choice_type: 'candidate', target_id: snapshot().state.candidates[1].candidate_id });
  assert.equal(snapshot().state.final_artifact.deliverable, 'music_candidate');
  assert.equal(snapshot().state.final_artifact.video_url, null);
});

test('failed generation stays failed after retry instead of reporting audio ready', async () => {
  const { connection, snapshot } = await session({ failMode: true });
  await connection.choose({ choice_type: 'clarification', target_id: 'music_only' });
  await connection.choose({ choice_type: 'proposal', target_id: snapshot().state.proposals[0].proposal_id });
  await nextTask(10);
  const take = snapshot().state.candidates[0];
  assert.equal(take.status, 'failed');
  assert.equal(take.audio_url, null);
  await connection.choose({ choice_type: 'variation', target_id: take.candidate_id });
  await nextTask(10);
  assert.ok(snapshot().state.candidates.every(candidate => candidate.status === 'failed'));
  assert.equal(snapshot().state.final_artifact, null);
});

test('mock mix adjustments do not mislabel a WAV as rendered video', async () => {
  const { connection, snapshot } = await session();
  await connection.choose({ choice_type: 'clarification', target_id: 'voiceover_only' });
  await connection.choose({ choice_type: 'voiceover', payload: { script: 'A short narration.' } });
  await nextTask(10);
  await connection.choose({ choice_type: 'mix', payload: { voiceover_start_s: 4 } });
  assert.equal(snapshot().state.mix.voiceover_start_s, 4);
  assert.equal(snapshot().state.mix.video_url, null);
});


test('export follows the latest rendered mix instead of an earlier audio-only final', () => {
  const state = { selected_candidate_id: 'take', final_artifact: { audio_url: '/old.wav' },
    candidates: [{ candidate_id: 'take', audio_url: '/take.wav', remixed_video_url: '/remix.mp4' }] };
  assert.deepEqual(resolveOutput(state), { url: '/remix.mp4', kind: 'video' });
  state.mix = { video_url: '/composed.mp4' };
  assert.deepEqual(resolveOutput(state), { url: '/composed.mp4', kind: 'video' });
});
