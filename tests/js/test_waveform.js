// node tests/js/test_waveform.js  -- the waveform component (app/static/waveform.js) with a fake canvas.
const assert = require('assert');
const path = require('path');
global.window = { devicePixelRatio: 1, addEventListener() {} };
global.requestAnimationFrame = () => 1;
const W = require(path.join(__dirname, '../../app/static/waveform.js'));

let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} expected ${b} +/- ${tol}, got ${a}`);

function makeCanvas(w = 800, h = 200) {
  const calls = [];
  const ctx = new Proxy({}, { get: (_, k) => (k === 'calls' ? calls : k === 'measureText' ? () => ({ width: 20 }) : (...a) => { calls.push([k, ...a]); }), set: () => true });
  const ls = {};
  const canvas = { clientWidth: w, clientHeight: h, width: 0, height: 0, style: {}, getContext: () => ctx, addEventListener: (t, f) => { ls[t] = f; }, getBoundingClientRect: () => ({ left: 0, top: 0 }), setPointerCapture() {} };
  const fire = (type, ev = {}) => ls[type](Object.assign({ pointerId: 1, preventDefault() {} }, ev));
  return { canvas, calls, fire, ctx };
}

function makeDat(seconds, { rate = 48000, spp = 480, quiet = 6, loud = 70, version = 1 } = {}) {
  const length = Math.round(seconds * rate / spp), hdr = version === 2 ? 24 : 20, buf = new ArrayBuffer(hdr + length * 2), dv = new DataView(buf);
  dv.setInt32(0, version, true); dv.setUint32(4, 1, true); dv.setInt32(8, rate, true); dv.setInt32(12, spp, true); dv.setUint32(16, length, true);
  if (version === 2) dv.setInt32(20, 1, true);
  const raw = new Int8Array(buf, hdr);
  for (let i = 0; i < length; i++) { const a = i < length / 2 ? quiet : loud; raw[2 * i] = -a; raw[2 * i + 1] = a; }
  return buf;
}
const res = (status, body) => ({ ok: status < 400, status, statusText: 'x', json: async () => body || {}, arrayBuffer: async () => body });

test('parseDat reads the header, splits min/max, computes duration and a display gain', () => {
  const wf = W.parseDat(makeDat(60));
  assert.deepStrictEqual([wf.rate, wf.spp, wf.length, wf.pps], [48000, 480, 6000, 100]);
  near(wf.duration, 60, 1e-9);
  assert.strictEqual(wf.mins[0], -6); assert.strictEqual(wf.maxs[5999], 70);
  const quiet = W.parseDat(makeDat(10, { quiet: 4, loud: 4 })), loud = W.parseDat(makeDat(10, { quiet: 120, loud: 120 }));
  assert.ok(quiet.gain > 10 && quiet.gain <= 16 && loud.gain === 1);
  assert.strictEqual(W.parseDat(makeDat(5, { version: 2 })).length, 500);
});

test('parseDat rejects anything that is not a valid 8-bit mono file', () => {
  assert.throws(() => W.parseDat(new ArrayBuffer(10)), /expected format/);
  const bad = makeDat(5); new DataView(bad).setInt32(0, 9, true); assert.throws(() => W.parseDat(bad), /expected format/);
  const sixteen = makeDat(5); new DataView(sixteen).setUint32(4, 0, true); assert.throws(() => W.parseDat(sixteen), /expected format/);
  assert.throws(() => W.parseDat(makeDat(5).slice(0, 100)), /expected format/);
  const stereo = makeDat(5, { version: 2 }); new DataView(stereo).setInt32(20, 2, true); assert.throws(() => W.parseDat(stereo), /expected format/);
});

test('loadWaveform: 202 -> null, ok -> parsed and cached, errors carry the reason', async () => {
  W.waveCache.id = null;
  let calls = 0;
  const f = (seq) => async (url) => { calls++; return seq.shift()(url); };
  assert.strictEqual(await W.loadWaveform('a', f([() => res(202)])), null);
  const wf = await W.loadWaveform('a', f([(u) => { assert.strictEqual(u, '/api/resources/a/waveform'); return res(200, makeDat(20)); }]));
  assert.ok(wf && wf.length === 2000);
  const before = calls;
  assert.strictEqual(await W.loadWaveform('a', f([() => { throw new Error('should not refetch'); }])), wf);
  assert.strictEqual(calls, before, 'the second call came from the cache');
  await assert.rejects(W.loadWaveform('b', f([() => res(500, { error: 'the audio file could not be read' })])), /could not be read/);
  await W.loadWaveform('c', f([() => res(200, makeDat(5))]));
  assert.strictEqual(W.waveCache.id, 'c', 'a different recording replaces the cache');
});

test('niceStep, fmtClock and parseTime', () => {
  assert.strictEqual(W.niceStep(60, 12), 5); assert.strictEqual(W.niceStep(3600, 10), 600); assert.strictEqual(W.niceStep(2, 10), 0.2);
  assert.strictEqual(W.niceStep(1e9, 10), 14400);
  assert.strictEqual(W.fmtClock(65), '1:05'); assert.strictEqual(W.fmtClock(3725), '1:02:05'); assert.strictEqual(W.fmtClock(7.25, 1), '0:07.3'); assert.strictEqual(W.fmtClock(-4), '0:00');
  assert.strictEqual(W.parseTime('12.5'), 12.5); assert.strictEqual(W.parseTime('1:02'), 62); assert.strictEqual(W.parseTime('1:02.5'), 62.5); assert.strictEqual(W.parseTime('0:01:02.5'), 62.5);
  for (const bad of ['', 'x', '1:', ':5', '1:2:3:4', '-3', '1,5']) assert.ok(Number.isNaN(W.parseTime(bad)), bad);
});

test('geometry: time and x invert each other; zoom keeps the anchor; the view stays inside the recording', () => {
  const { canvas } = makeCanvas(800);
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), zoomable: true });
  near(v.timeAtX(400), 30, 1e-9); near(v.xAtTime(15), 200, 1e-9); near(v.timeAtX(-50), 0, 1e-9); near(v.timeAtX(9999), 60, 1e-9);
  v.zoomBy(4, 30);
  near(v.span, 15, 1e-9); near(v.xAtTime(30), 400, 1e-6, 'the anchor stays put');
  v.panTo(-100); assert.strictEqual(v.start, 0); v.panTo(1000); near(v.start, 45, 1e-9);
  v.zoomBy(1e6); near(v.span, 0.25, 1e-9, 'cannot zoom past the minimum span'); v.fit(); near(v.span, 60, 1e-9);
  v.zoomBy(0.001); near(v.span, 60, 1e-9, 'cannot zoom out past the whole recording');
  v.zoomToRange(20, 30); assert.ok(v.start < 20 && v.start + v.span > 30 && v.span < 20);
  v.fit(); v.zoomBy(10, 30); v.ensureVisible(1); assert.ok(v.start <= 1, 'scrolls back to bring a time into view');
});

test('scrub mode (the bottom player): pressing and dragging seeks, releasing does nothing more', () => {
  const { canvas, fire } = makeCanvas(800, 48);
  const seeks = [];
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), scrub: true, onSeek: (t) => seeks.push(t) });
  assert.strictEqual(v.zoomable, false);
  fire('pointerdown', { clientX: 400, clientY: 20 }); fire('pointermove', { clientX: 600, clientY: 20 }); fire('pointerup', { clientX: 600, clientY: 20 });
  near(seeks[0], 30, 1e-9); near(seeks[seeks.length - 1], 45, 1e-9); assert.strictEqual(seeks.length, 2);
  fire('wheel', { clientX: 400, deltaY: -100, ctrlKey: true });     // no zoom in scrub mode
  near(v.span, 60, 1e-9);
});

test("setWaveform can be told the recording's real length, and a selection never runs past it", () => {
  const { canvas } = makeCanvas(800);
  const v = new W.WaveView(canvas, { duration: 901.802542, selectable: true });
  v.setWaveform(W.parseDat(makeDat(902)), 901.802542);
  near(v.duration, 901.802542, 1e-9); near(v.span, 901.802542, 1e-9);
  v.setSelection(800, 5000); assert.ok(v.getSelection().end <= 901.802542 + 1e-9, 'clamped to the real length, not the waveform\'s 902');
  const plain = new W.WaveView(makeCanvas().canvas, {}); plain.setWaveform(W.parseDat(makeDat(30))); near(plain.duration, 30, 1e-9);
});

test('works before the waveform exists: a bare progress line that still scrubs', () => {
  const { canvas, fire, calls } = makeCanvas(600, 48);
  let t = 12;
  const seeks = [];
  const v = new W.WaveView(canvas, { duration: 120, scrub: true, getTime: () => t, onSeek: (s) => seeks.push(s) });
  v.draw();
  assert.ok(calls.some(c => c[0] === 'fillRect' && c[3] === 2), 'the playhead line is drawn');
  fire('pointerdown', { clientX: 300, clientY: 10 }); near(seeks[0], 60, 1e-9);
  v.setWaveform(W.parseDat(makeDat(120))); near(v.duration, 120, 1e-9); assert.ok(v.wf);
});

test('zoomable view: a tap seeks, a drag pans, two fingers pinch, ctrl+wheel zooms and a plain wheel does not', () => {
  const { canvas, fire } = makeCanvas(800);
  const seeks = [];
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), zoomable: true, onSeek: (t) => seeks.push(t) });
  fire('pointerdown', { clientX: 200, clientY: 50 }); fire('pointerup', { clientX: 200, clientY: 50 });
  assert.deepStrictEqual(seeks.map(Math.round), [15]);
  v.zoomBy(4, 30); const s0 = v.start;
  fire('pointerdown', { clientX: 400, clientY: 50 }); fire('pointermove', { clientX: 500, clientY: 50 }); fire('pointerup', { clientX: 500, clientY: 50 });
  assert.ok(v.start < s0, 'dragging right moves the view earlier'); assert.strictEqual(seeks.length, 1, 'a drag is not a tap');
  const span = v.span;
  fire('pointerdown', { pointerId: 1, clientX: 380, clientY: 50 }); fire('pointerdown', { pointerId: 2, clientX: 420, clientY: 50 }); fire('pointermove', { pointerId: 2, clientX: 520, clientY: 50 });
  assert.ok(v.span < span, 'spreading two fingers zooms in'); fire('pointerup', { pointerId: 1, clientX: 380 }); fire('pointerup', { pointerId: 2, clientX: 520 });
  const s1 = v.span; let prevented = 0;
  fire('wheel', { clientX: 400, deltaY: -100, ctrlKey: false, preventDefault() { prevented++; } }); assert.strictEqual(v.span, s1); assert.strictEqual(prevented, 0);
  fire('wheel', { clientX: 400, deltaY: -100, ctrlKey: true, preventDefault() { prevented++; } }); assert.ok(v.span < s1 && prevented === 1);
});

test('select mode: dragging makes a selection (either direction, clamped), a tap only seeks, edges can be dragged', () => {
  const { canvas, fire } = makeCanvas(800);
  const seeks = [], picked = [];
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), zoomable: true, selectable: true, mode: 'select', onSeek: (t) => seeks.push(t), onSelect: (s) => picked.push(s) });
  fire('pointerdown', { clientX: 200, clientY: 60 }); fire('pointermove', { clientX: 400, clientY: 60 }); fire('pointerup', { clientX: 400, clientY: 60 });
  let s = v.getSelection(); near(s.start, 15, 1e-9); near(s.end, 30, 1e-9); assert.strictEqual(seeks.length, 0, 'a drag does not seek');
  assert.ok(picked.length >= 1 && picked[picked.length - 1].end === s.end, 'onSelect reports it');
  fire('pointerdown', { clientX: 600, clientY: 60 }); fire('pointermove', { clientX: 500, clientY: 60 }); fire('pointerup', { clientX: 500, clientY: 60 });
  s = v.getSelection(); near(s.start, 37.5, 1e-9); near(s.end, 45, 1e-9, 'dragging backwards gives the same selection');
  fire('pointerdown', { clientX: 799, clientY: 60 }); fire('pointermove', { clientX: 5000, clientY: 60 }); fire('pointerup', { clientX: 5000, clientY: 60 });
  assert.ok(v.getSelection().end <= 60, 'clamped to the recording');
  v.setSelection(10, 20, true);                                   // edges at x = 133.3 and 266.7
  fire('pointerdown', { clientX: 134, clientY: 60 }); fire('pointermove', { clientX: 240, clientY: 60 }); fire('pointerup', { clientX: 240, clientY: 60 });
  s = v.getSelection(); near(s.start, 18, 1e-9, 'grabbing the start handle moves only the start'); assert.strictEqual(s.end, 20);
  fire('pointerdown', { clientX: 400, clientY: 60 }); fire('pointerup', { clientX: 400, clientY: 60 });
  assert.strictEqual(seeks.length, 1, 'a plain tap in select mode seeks and leaves the selection alone'); assert.ok(v.getSelection());
});

test('select mode: grabbing an edge resizes it, and it cannot cross the other edge', () => {
  const { canvas, fire } = makeCanvas(800);
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), selectable: true, mode: 'select' });
  v.setSelection(15, 30, true);                                   // x = 200 .. 400
  fire('pointerdown', { clientX: 396, clientY: 60 }); fire('pointermove', { clientX: 480, clientY: 60 }); fire('pointerup', { clientX: 480, clientY: 60 });
  let s = v.getSelection(); near(s.start, 15, 1e-9); near(s.end, 36, 1e-9);
  fire('pointerdown', { clientX: 203, clientY: 60 }); fire('pointermove', { clientX: 900, clientY: 60 }); fire('pointerup', { clientX: 900, clientY: 60 });
  s = v.getSelection(); assert.ok(s.start < s.end, 'the start edge cannot pass the end: ' + JSON.stringify(s));
  v.clearSelection(); assert.strictEqual(v.getSelection(), null);
  v.setSelection(30, 30); assert.strictEqual(v.getSelection(), null, 'an empty range is no selection');
});

test('clip regions are drawn, and tapping one (in the label strip) selects it instead of seeking', () => {
  const { canvas, fire, calls } = makeCanvas(800, 200);
  const clicked = [], seeks = [];
  const regions = [{ id: 'a', start: 10, end: 20, label: 'Jacob says hello', selected: false }, { id: 'b', start: 40, end: 50, label: 'x', selected: true }];
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), selectable: true, mode: 'select', regions: () => regions, onRegionClick: (r) => clicked.push(r.id), onSeek: (t) => seeks.push(t) });
  v.draw();
  const band = calls.find(c => c[0] === 'fillRect' && Math.abs(c[1] - 10 * 800 / 60) < 0.01);
  assert.ok(band, 'a band starts at the region start');
  assert.ok(calls.some(c => c[0] === 'fillText' && c[1] === 'Jacob says hello'), 'the label is drawn');
  fire('pointerdown', { clientX: 200, clientY: 195 }); fire('pointerup', { clientX: 200, clientY: 195 });
  assert.deepStrictEqual(clicked, ['a']); assert.strictEqual(seeks.length, 0);
  fire('pointerdown', { clientX: 200, clientY: 60 }); fire('pointerup', { clientX: 200, clientY: 60 });
  assert.strictEqual(seeks.length, 1, 'above the strip a tap seeks');
});

test('overview strip: pressing or dragging moves the main view, centred on the pointer', () => {
  const main = makeCanvas(800), over = makeCanvas(800, 40);
  const mv = new W.WaveView(main.canvas, { wf: W.parseDat(makeDat(600)), zoomable: true });
  const ov = new W.WaveView(over.canvas, { wf: W.parseDat(makeDat(600)), overviewOf: mv });
  mv.zoomBy(10);                                                   // 60 s window
  over.fire('pointerdown', { clientX: 400, clientY: 10 });         // the middle: 300 s
  near(mv.start, 270, 1e-6); near(mv.span, 60, 1e-9);
  over.fire('pointermove', { clientX: 800, clientY: 10 }); near(mv.start, 540, 1e-6, 'clamped to the end');
  over.fire('pointerup', { clientX: 800, clientY: 10 });
  over.fire('pointerdown', { clientX: 0, clientY: 10 }); near(mv.start, 0, 1e-9); over.fire('pointerup', { clientX: 0 });
  ov.draw(); assert.ok(over.calls.some(c => c[0] === 'strokeRect'), 'the window outline is drawn');
});

test('drawing: the ruler is labelled at round times, the selection band spans its times, the playhead is at the audio time', () => {
  const { canvas, calls } = makeCanvas(900, 300);
  let t = 12;
  const v = new W.WaveView(canvas, { wf: W.parseDat(makeDat(60)), ruler: true, selectable: true, getTime: () => t });
  v.setSelection(20, 30, true);
  v.draw();
  const labels = calls.filter(c => c[0] === 'fillText').map(c => c[1]);
  assert.ok(labels.includes('0:10') && labels.includes('0:20'), 'ticks at round times: ' + labels.join(' '));
  const sel = calls.find(c => c[0] === 'fillRect' && Math.abs(c[1] - 20 * 15) < 0.01 && Math.abs(c[3] - 150) < 0.01);
  assert.ok(sel, 'the selection band is 150 px wide starting at x=300');
  const head = calls.filter(c => c[0] === 'fillRect' && c[3] === 2 && c[4] === 300).pop();
  near(head[1], 12 * 15 - 1, 1.01, 'playhead x');
  v.zoomToRange(20, 21); calls.length = 0; v.draw();
  assert.ok(calls.some(c => c[0] === 'fillText' && /\d:\d\d\.\d/.test(c[1])), 'zoomed in far enough, the ruler shows tenths');
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log('PASS ' + name); } catch (e) { console.log('FAIL ' + name + '\n  ' + (e.stack || e.message).split('\n').slice(0, 3).join('\n  ')); process.exitCode = 1; }
  }
  console.log(`\n${passed} passed` + (process.exitCode ? ', SOME FAILED' : ''));
})();
