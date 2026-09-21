// node tests/js/test_editor_page.js -- the full-screen waveform editor (app/templates/edit.html) under a fake DOM.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom } = require('./minidom');
const ROOT = path.join(__dirname, '../..');
const RID = 'bbbbbbbb-0000-4000-8000-000000000002';

function makeDat(seconds = 60) {
  const length = seconds * 100, buf = new ArrayBuffer(20 + length * 2), dv = new DataView(buf);
  dv.setInt32(0, 1, true); dv.setUint32(4, 1, true); dv.setInt32(8, 48000, true); dv.setInt32(12, 480, true); dv.setUint32(16, length, true);
  const raw = new Int8Array(buf, 20);
  for (let i = 0; i < length; i++) { const a = 10 + (i % 50); raw[2 * i] = -a; raw[2 * i + 1] = a; }
  return buf;
}
const json = (data, status = 200) => ({ ok: status < 400, status, statusText: 'x', json: async () => data, arrayBuffer: async () => { throw new Error('not binary'); } });
const bin = (buf) => ({ ok: true, status: 200, statusText: 'OK', json: async () => ({}), arrayBuffer: async () => buf });
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} expected ${b} +/- ${tol}, got ${a}`);

async function run(opts = {}) {
  const dom = createDom();
  const state = { clips: (opts.clips || []).map(c => ({ ...c })), calls: [], exportPolls: 0 };
  const resource = { id: RID, filename: 'Parkridge birds.wav', duration_seconds: 60, has_preview: true, ...(opts.resource || {}) };
  const fetchStub = async (url, o = {}) => {
    const u = String(url).split('?')[0], method = (o.method || 'GET').toUpperCase(), body = o.body ? JSON.parse(o.body) : null;
    state.calls.push([method, u, body]);
    if (u === `/api/resources/${RID}` && method === 'GET') return json(resource);
    if (u === `/api/resources/${RID}/clips` && method === 'GET') return json(state.clips);
    if (u === `/api/resources/${RID}/clips` && method === 'POST') { const c = { id: 'c' + (state.clips.length + 1), resource_id: RID, ...body }; state.clips.push(c); return json(c, 201); }
    if (/^\/api\/clips\/[^/]+$/.test(u) && method === 'PATCH') { const c = state.clips.find(x => x.id === u.split('/').pop()); Object.assign(c, body); return json(c); }
    if (/^\/api\/clips\/[^/]+$/.test(u) && method === 'DELETE') { state.clips = state.clips.filter(x => x.id !== u.split('/').pop()); return { ok: true, status: 204, statusText: 'x', json: async () => null }; }
    if (u === `/api/resources/${RID}/export` && method === 'POST') return json({ id: 'e1', status: 'queued' }, 202);
    if (u === '/api/exports/e1') return json(++state.exportPolls < 2 ? { id: 'e1', status: 'running' } : { id: 'e1', status: 'success' });
    if (u === `/api/resources/${RID}/waveform`) return opts.waveform ? opts.waveform() : bin(makeDat());
    throw new Error('unexpected fetch: ' + method + ' ' + u);
  };
  const ctx = {
    document: dom.document, window: { addEventListener() {}, devicePixelRatio: 1 }, fetch: fetchStub, console, Promise, Math, Date, JSON, Array, Object, Number, String, Set, Map, Error, isFinite,
    setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms || 0, 10)), clearTimeout, requestAnimationFrame: (fn) => { setTimeout(fn, 4); return 1; },
    confirm: () => true, alert: () => {}, location: { href: '' },
  };
  ctx.window.document = dom.document;
  vm.createContext(ctx);
  for (const f of ['app/static/app.js', 'app/static/waveform.js']) vm.runInContext(fs.readFileSync(path.join(ROOT, f), 'utf8'), ctx);
  ctx.AudioWave = ctx.window.AudioWave || vm.runInContext('AudioWave', ctx);
  const html = fs.readFileSync(path.join(ROOT, 'app/templates/edit.html'), 'utf8');
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n').replace('{{ resource_id | tojson }}', JSON.stringify(RID));
  const ed = dom.document.createElement('div'); ed.id = 'ed'; const title = dom.document.createElement('h1'); title.id = 'title';
  dom.document.body.append(title, ed);
  vm.runInContext(script, ctx);
  const settle = (ms = 80) => new Promise(r => setTimeout(r, ms));
  await settle(120);
  const q = (sel) => ed.querySelector(sel), qa = (sel) => ed.querySelectorAll(sel);
  const audio = ed.querySelectorAll('audio')[0];
  const button = (label) => qa('button').find(b => b.textContent.includes(label));
  const key = (k, extra = {}) => dom.document.dispatch('keydown', { key: k, ...extra });
  const drag = (canvas, x0, x1, y = 60) => { canvas.dispatch('pointerdown', { pointerId: 1, clientX: x0, clientY: y }); canvas.dispatch('pointermove', { pointerId: 1, clientX: x1, clientY: y }); canvas.dispatch('pointerup', { pointerId: 1, clientX: x1, clientY: y }); };
  return { dom, state, ed, title, audio, q, qa, button, key, drag, settle, ctx };
}

let passed = 0;
const test = async (name, fn) => { try { await fn(); passed++; console.log('PASS ' + name); } catch (e) { console.log('FAIL ' + name + '\n  ' + (e.stack || e.message).split('\n').slice(0, 4).join('\n  ')); process.exitCode = 1; } };

(async () => {
  await test('the editor loads: title, waveform on both canvases, listening copy as the audio, no clips message', async () => {
    const t = await run();
    assert.strictEqual(t.title.textContent, 'Parkridge birds.wav');
    assert.ok(t.audio.src.endsWith(`/api/resources/${RID}/preview`));
    for (const cls of ['.ed-wave', '.ed-over']) assert.ok(t.q(cls).ctxCalls.filter(c => c[0] === 'fillRect').length > 100, cls + ' has the waveform drawn');
    assert.ok(!/Generating the waveform/.test(t.ed.textContent), 'the waiting note is gone');
    assert.ok(/No clips yet/.test(t.ed.textContent));
    assert.ok(t.q('.ed-wave').ctxCalls.some(c => c[0] === 'fillText' && /\d:\d\d/.test(c[1])), 'the ruler is labelled');
  });

  await test('dragging on the waveform selects a range and fills the start/end/length boxes', async () => {
    const t = await run();
    t.drag(t.q('.ed-wave'), 150, 300);                                   // 600 px = 60 s: 15 s to 30 s
    assert.strictEqual(t.q('#sel-start').value, '0:15.00'); assert.strictEqual(t.q('#sel-end').value, '0:30.00'); assert.strictEqual(t.q('#sel-len').value, '15.00 s');
    assert.strictEqual(t.button('Save as clip').disabled, false);
  });

  await test('saving a clip posts the times and label, then lists it', async () => {
    const t = await run();
    t.drag(t.q('.ed-wave'), 150, 300);
    t.q('#clip-label').value = 'Blackbird';
    t.button('Save as clip').click(); await t.settle();
    const post = t.state.calls.find(c => c[0] === 'POST' && c[1].endsWith('/clips'));
    assert.deepStrictEqual([post[2].start_seconds, post[2].end_seconds, post[2].label], [15, 30, 'Blackbird']);
    const row = t.qa('.clip')[0];
    assert.ok(row, 'the clip is listed'); assert.ok(row.classList.contains('sel'), 'and selected');
    assert.strictEqual(t.q('#sel-start').value, '', 'the selection was cleared after saving');
    assert.ok(/Saved a clip of 15\.0 s/.test(t.q('#msg').textContent));
  });

  await test('typing times sets the selection; nonsense is refused with a message and changes nothing', async () => {
    const t = await run();
    t.q('#sel-start').value = '0:10'; t.q('#sel-end').value = '1:05.5';
    t.q('#sel-start').dispatch('change'); t.q('#sel-end').dispatch('change');
    assert.strictEqual(t.q('#sel-start').value, '0:10.00'); assert.strictEqual(t.q('#sel-end').value, '1:00.00', 'clamped to the recording length');
    t.q('#sel-start').value = 'banana'; t.q('#sel-start').dispatch('change');
    assert.ok(/Type times like/.test(t.q('#msg').textContent)); assert.strictEqual(t.q('#sel-end').value, '1:00.00');
  });

  await test('keyboard: I and O set the range at the playhead, arrows seek (Shift 10 s, Alt 0.1 s), Space plays, Esc clears, keys are ignored while typing', async () => {
    const t = await run();
    t.audio.currentTime = 20; t.key('i');
    near(t.q('#sel-start').value === '0:20.00' ? 20 : -1, 20, 0, 'I set the start');
    t.audio.currentTime = 25; t.key('o');
    assert.deepStrictEqual([t.q('#sel-start').value, t.q('#sel-end').value], ['0:20.00', '0:25.00']);
    t.key('ArrowRight'); near(t.audio.currentTime, 26, 1e-9); t.key('ArrowRight', { shiftKey: true }); near(t.audio.currentTime, 36, 1e-9);
    t.key('ArrowLeft', { altKey: true }); near(t.audio.currentTime, 35.9, 1e-9);
    t.key(' '); assert.strictEqual(t.audio.paused, false); t.key(' '); assert.strictEqual(t.audio.paused, true);
    const typing = t.ed.querySelector('#clip-label'); const before = t.audio.currentTime;
    t.dom.document.dispatch('keydown', { key: 'ArrowRight', target: typing }); assert.strictEqual(t.audio.currentTime, before, 'typing in a box does not move the audio');
    t.key('Escape'); assert.strictEqual(t.q('#sel-start').value, '');
    t.audio.currentTime = 100; t.key('ArrowLeft', { shiftKey: true }); t.audio.currentTime = 3; t.key('ArrowLeft', { shiftKey: true }); assert.strictEqual(t.audio.currentTime, 0, 'never before the start');
  });

  await test('play selection starts at the selection start and stops at its end; loop repeats it', async () => {
    const t = await run();
    t.drag(t.q('.ed-wave'), 150, 300);
    t.button('Play selection').click();
    assert.strictEqual(t.audio.paused, false); near(t.audio.currentTime, 15, 1e-9);
    t.audio.currentTime = 30.2; await t.settle(40);
    assert.strictEqual(t.audio.paused, true, 'stopped at the end'); near(t.audio.currentTime, 30, 1e-9);
    t.q('#loop').checked = true;
    t.button('Play selection').click(); t.audio.currentTime = 30.2; await t.settle(40);
    assert.strictEqual(t.audio.paused, false, 'with loop on it keeps playing'); near(t.audio.currentTime, 15, 0.5, 'and jumps back to the start');
    t.audio.pause();
  });

  await test('an existing clip shows as a region; tapping it selects the clip and the range; Delete removes it', async () => {
    const t = await run({ clips: [{ id: 'c1', resource_id: RID, start_seconds: 15, end_seconds: 30, label: 'Wren' }] });
    assert.strictEqual(t.qa('.clip').length, 1);
    assert.ok(t.q('.ed-wave').ctxCalls.some(c => c[0] === 'fillText' && c[1] === 'Wren'), 'the region is labelled on the waveform');
    t.q('.ed-wave').dispatch('pointerdown', { pointerId: 1, clientX: 200, clientY: 195 }); t.q('.ed-wave').dispatch('pointerup', { pointerId: 1, clientX: 200, clientY: 195 });
    assert.ok(t.q('.clip').classList.contains('sel'), 'the clip is selected'); assert.strictEqual(t.q('#sel-start').value, '0:15.00');
    t.button('Delete').click(); await t.settle();
    assert.ok(t.state.calls.some(c => c[0] === 'DELETE' && c[1] === '/api/clips/c1')); assert.strictEqual(t.qa('.clip').length, 0);
  });

  await test('renaming a clip and updating it from the current selection', async () => {
    const t = await run({ clips: [{ id: 'c1', resource_id: RID, start_seconds: 15, end_seconds: 30, label: 'Wren' }] });
    const name = t.qa('.clip')[0].querySelectorAll('input')[0]; name.value = 'Wren, close'; name.dispatch('change'); await t.settle();
    assert.deepStrictEqual(t.state.calls.find(c => c[0] === 'PATCH')[2], { label: 'Wren, close' });
    t.drag(t.q('.ed-wave'), 300, 450);                                       // 30 s - 45 s
    t.button('Use current selection').click(); await t.settle();
    const p = t.state.calls.filter(c => c[0] === 'PATCH').pop();
    assert.deepStrictEqual([p[2].start_seconds, p[2].end_seconds], [30, 45]);
  });

  await test('exporting a clip queues the job, waits for it, and offers the download', async () => {
    const t = await run({ clips: [{ id: 'c1', resource_id: RID, start_seconds: 15, end_seconds: 30, label: 'Wren' }] });
    const sel = t.qa('.clip')[0].querySelectorAll('select')[0]; sel.value = 'flac';
    t.button('Export').click(); await t.settle(250);
    const post = t.state.calls.find(c => c[1].endsWith('/export'));
    assert.deepStrictEqual(post[2], { clip_id: 'c1', format: 'flac' });
    const link = t.qa('a').find(a => a.textContent === 'Download');
    assert.ok(link && link.attributes.href === '/api/exports/e1/download', 'a download link replaces the button');
  });

  await test('Move mode pans instead of selecting; Select mode selects', async () => {
    const t = await run();
    t.key('+'); t.key('+');                                                  // zoom in so there is something to pan
    t.key('s'); assert.ok(/Move/.test(t.button('Mode').textContent));
    t.drag(t.q('.ed-wave'), 400, 200);
    assert.strictEqual(t.q('#sel-start').value, '', 'no selection was made in Move mode');
    t.key('s'); t.drag(t.q('.ed-wave'), 100, 300);
    assert.notStrictEqual(t.q('#sel-start').value, '', 'a selection in Select mode');
  });

  await test('a clip that runs to the very end of the recording is sent as a valid range (rounded down, never past the end)', async () => {
    const t = await run({ resource: { duration_seconds: 901.802542 }, waveform: () => bin(makeDat(902)) });       // the waveform is a little longer than the audio, as in real life
    t.drag(t.q('.ed-wave'), 200, 5000);                                    // far past the right edge: clamps to the end
    t.button('Save as clip').click(); await t.settle();
    const post = t.state.calls.find(c => c[0] === 'POST' && c[1].endsWith('/clips'));
    assert.ok(post, 'it was saved'); assert.ok(post[2].end_seconds <= 901.802542, 'end ' + post[2].end_seconds + ' is not past the recording');
    assert.ok(post[2].end_seconds > 901.79, 'and is the end');
  });

  await test('with no waveform yet the editor still works; a failed waveform says why', async () => {
    const t = await run({ waveform: () => ({ ok: false, status: 202, statusText: 'A', json: async () => ({}), arrayBuffer: async () => new ArrayBuffer(0) }) });
    assert.ok(/Generating the waveform/.test(t.ed.textContent));
    t.q('#sel-start').value = '0:05'; t.q('#sel-end').value = '0:09'; t.q('#sel-start').dispatch('change'); t.q('#sel-end').dispatch('change');
    assert.strictEqual(t.q('#sel-len').value, '4.00 s', 'times can be chosen without the picture');
    const bad = await run({ waveform: () => ({ ok: false, status: 500, statusText: 'x', json: async () => ({ error: 'the audio file could not be read' }), arrayBuffer: async () => new ArrayBuffer(0) }) });
    assert.ok(/could not be loaded: the audio file could not be read/.test(bad.ed.textContent));
  });

  console.log(`\n${passed} passed` + (process.exitCode ? ', SOME FAILED' : ''));
})();
