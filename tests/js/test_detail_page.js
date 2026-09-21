// Runs the recording page's own script (app/templates/resource_detail.html) under a fake DOM and fake fetch, and
// checks what a user would see happen. Exists because a bug shipped that unit tests of the parser couldn't catch:
// the waveform's first load ran before its section was attached to the page and never retried.
//   node tests/js/test_detail_page.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom } = require('./minidom');

const ROOT = path.join(__dirname, '../..');
const RID = 'aaaaaaaa-0000-4000-8000-000000000001';

// A valid audiowaveform .dat: 60 s at 100 peaks/s (48 kHz, 480 samples per pixel), quiet then loud.
function makeDat(seconds = 60) {
  const length = seconds * 100, buf = new ArrayBuffer(20 + length * 2), dv = new DataView(buf);
  dv.setInt32(0, 1, true); dv.setUint32(4, 1, true); dv.setInt32(8, 48000, true); dv.setInt32(12, 480, true); dv.setUint32(16, length, true);
  const raw = new Int8Array(buf, 20);
  for (let i = 0; i < length; i++) { const a = i < length / 2 ? 6 : 70; raw[2 * i] = -a; raw[2 * i + 1] = a; }
  return buf;
}

function resource(over = {}) {
  return Object.assign({
    id: RID, filename: 'test.wav', duration_seconds: 60, status: 'filed', category: 'ambient', project_id: null, session_id: null, session: null,
    captured_at: '2026-09-17T08:11:24Z', captured_at_source: 'filename', captured_at_precision: 'exact', suggested_captured_at: null, filename_info: {},
    location: null, has_track: false, has_photos: false, has_waveform: true, has_preview: true, waveform_error: null, preview_error: null,
    tags: [], role: 'original', notes: null, size_bytes: 1000,
  }, over);
}

const jsonRes = (data, status = 200) => ({ ok: status < 400, status, statusText: 'x', json: async () => data, arrayBuffer: async () => { throw new Error('not binary'); } });

async function run(routes, over = {}) {
  const dom = createDom();
  const calls = [];
  const fetchStub = async (url, opts) => {
    calls.push(String(url));
    const u = String(url).split('?')[0];
    if (routes[u]) return routes[u](opts);
    const defaults = {
      [`/api/resources/${RID}`]: () => jsonRes(resource(over)), '/api/projects': () => jsonRes([]), '/api/tags': () => jsonRes([]), '/api/sessions': () => jsonRes([]),
      '/api/categories': () => jsonRes([{ slug: 'ambient', label: 'Field recordings' }]), '/api/map/config': () => jsonRes({ tile_url: 'https://t/{z}/{x}/{y}.png', attribution: 'x', max_zoom: 19 }),
      [`/api/resources/${RID}/clips`]: () => jsonRes([]), [`/api/resources/${RID}/photos`]: () => jsonRes([]), [`/api/resources/${RID}/track`]: () => jsonRes([]),
    };
    if (defaults[u]) return defaults[u](opts);
    throw new Error('unexpected fetch: ' + u);
  };
  const ctx = {
    document: dom.document, window: { addEventListener() {}, devicePixelRatio: 1 }, fetch: fetchStub, location: { href: '', search: '', hash: '' },
    console, setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms || 0, 15)), clearTimeout, requestAnimationFrame: () => 1, alert: (m) => { ctx.alerts.push(m); }, confirm: () => true, prompt: () => null,
    Image: class { set src(v) {} }, navigator: {}, alerts: [], URLSearchParams, Promise, Math, Date, JSON, Array, Object, Number, String, Set, Map, Error,
  };
  ctx.window.document = dom.document;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app/static/waveform.js'), 'utf8'), ctx);
  ctx.AudioWave = ctx.window.AudioWave || vm.runInContext('typeof AudioWave !== "undefined" ? AudioWave : undefined', ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app/static/map.js'), 'utf8'), ctx);
  ctx.AudioMap = ctx.window.AudioMap || ctx.AudioMap || vm.runInContext('typeof AudioMap !== "undefined" ? AudioMap : undefined', ctx);
  // The page's own script, with the Jinja placeholders resolved.
  const html = fs.readFileSync(path.join(ROOT, 'app/templates/resource_detail.html'), 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
  const detail = dom.document.createElement('div'); detail.id = 'detail'; dom.document.body.append(detail);
  vm.runInContext(scripts.replace('{{ resource_id | tojson }}', JSON.stringify(RID)), ctx);
  const settle = async (ms = 60) => { await new Promise(r => setTimeout(r, ms)); };
  await settle();
  return { ctx, dom, calls, detail, settle };
}

let passed = 0;
const test = async (name, fn) => { try { await fn(); passed++; console.log('PASS ' + name); } catch (e) { console.log('FAIL ' + name + '\n  ' + (e.stack || e.message).split('\n').slice(0, 4).join('\n  ')); process.exitCode = 1; } };
const ok200 = (buf) => ({ ok: true, status: 200, statusText: 'OK', json: async () => ({}), arrayBuffer: async () => buf });
const text = (el) => el.textContent;
const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol, `expected ${b} +/- ${tol}, got ${a}`);

(async () => {
  await test('a ready waveform is fetched and drawn (the page does not stay on "Generating")', async () => {
    const { calls, detail, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => ok200(makeDat()) });
    await settle(120);
    assert.ok(calls.some(c => c.endsWith(`/api/resources/${RID}/waveform`)), 'the waveform was requested: ' + calls.join(' | '));
    assert.ok(detail.querySelector('.wf-canvas'), 'a waveform canvas is on the page');
    assert.ok(!/Generating the waveform/.test(text(detail)), 'the "Generating" note is gone');
  });

  await test('a waveform still being made (202) is polled again until it is ready', async () => {
    let n = 0;
    const { calls, detail, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => (++n < 3 ? { ok: false, status: 202, statusText: 'Accepted', json: async () => ({ status: 'generating' }), arrayBuffer: async () => new ArrayBuffer(0) } : ok200(makeDat())) }, { has_waveform: false });
    for (let i = 0; i < 40 && !detail.querySelector('.wf-canvas'); i++) await settle(30);
    assert.ok(n >= 3, 'it kept asking (' + n + ' requests)');
    assert.ok(detail.querySelector('.wf-canvas'), 'and drew it when it arrived');
  });

  await test('a failed generation shows the reason and a retry button, not "Generating"', async () => {
    const { detail } = await run({}, { has_waveform: false, waveform_error: 'the audio file could not be read' });
    assert.ok(/could not be made: the audio file could not be read/.test(text(detail)));
    assert.ok(!/Generating the waveform/.test(text(detail)));
    assert.ok(detail.querySelectorAll('button').some(b => /Try again/.test(text(b))));
  });

  await test('a server error while loading shows a message, not an endless "Generating"', async () => {
    const { detail, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => ({ ok: false, status: 500, statusText: 'x', json: async () => ({ error: 'boom' }), arrayBuffer: async () => new ArrayBuffer(0) }) });
    await settle(120);
    assert.ok(/could not be loaded: boom/.test(text(detail)), text(detail).slice(0, 300));
  });

  await test('the bottom player is a custom bar: play button, times, a waveform scrubber, and a link to the editor', async () => {
    const { dom, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => ok200(makeDat()) });
    await settle(150);
    const bar = dom.document.getElementById('player-bar'), audio = dom.document.getElementById('player');
    assert.ok(bar && audio, 'the bar and its audio element exist');
    assert.ok(audio.src.endsWith(`/api/resources/${RID}/preview`), 'it prefers the compressed listening copy: ' + audio.src);
    const canvas = bar.querySelector('.pb-wave');
    assert.ok(canvas.ctxCalls.filter(c => c[0] === 'fillRect').length > 100, 'the waveform is drawn in the bar');
    assert.strictEqual(bar.querySelector('.pb-expand').attributes.href, `/edit/${RID}`);
    const play = bar.querySelector('.pb-play');
    play.click();
    assert.strictEqual(audio.paused, false, 'the play button plays'); assert.ok(/\u23f8/.test(play.textContent), 'and becomes pause');
    play.click(); assert.strictEqual(audio.paused, true);
    canvas.dispatch('pointerdown', { pointerId: 1, clientX: 300, clientY: 20 });          // the middle of a 60 s recording
    near(audio.currentTime, 30, 0.01);
    canvas.dispatch('pointermove', { pointerId: 1, clientX: 450, clientY: 20 }); near(audio.currentTime, 45, 0.01);
    canvas.dispatch('pointerup', { pointerId: 1, clientX: 450, clientY: 20 });
  });

  await test('the bar works before the waveform exists (a plain progress line that still scrubs)', async () => {
    const { dom, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => ({ ok: false, status: 202, statusText: 'A', json: async () => ({ status: 'generating' }), arrayBuffer: async () => new ArrayBuffer(0) }) }, { has_waveform: false });
    await settle(60);
    const audio = dom.document.getElementById('player'), canvas = dom.document.getElementById('player-bar').querySelector('.pb-wave');
    canvas.dispatch('pointerdown', { pointerId: 1, clientX: 150, clientY: 20 });
    near(audio.currentTime, 15, 0.01);
  });

  await test('clicking the route on the map plays the audio from that moment', async () => {
    const track = [{ lat: 53.0, lon: -2.20, offset_seconds: 0 }, { lat: 53.0, lon: -2.19, offset_seconds: 60 }, { lat: 53.0, lon: -2.18, offset_seconds: 120 }];
    const { dom, detail, settle } = await run({ [`/api/resources/${RID}/waveform`]: () => ok200(makeDat(120)), [`/api/resources/${RID}/track`]: () => jsonRes(track) },
      { has_track: true, duration_seconds: 120, location: { lat: 53.0, lon: -2.19, source: 'dawarich-auto', place_name: 'Stoke-on-Trent', place_source: 'photon' } });
    await settle(200);
    const audio = dom.document.getElementById('player');
    const map = detail.querySelectorAll('canvas').find(c => c.parentNode && c.parentNode.classList.contains('map-box'));
    assert.ok(map, 'the map is on the page');
    assert.strictEqual(audio.paused, true);
    map.dispatch('pointerdown', { pointerId: 1, clientX: 300, clientY: 100 }); map.dispatch('pointerup', { pointerId: 1, clientX: 300, clientY: 100 });
    near(audio.currentTime, 60, 6);
    assert.strictEqual(audio.paused, false, 'clicking the route plays');
    map.dispatch('pointerdown', { pointerId: 1, clientX: 20, clientY: 190 }); map.dispatch('pointerup', { pointerId: 1, clientX: 20, clientY: 190 });
    assert.ok(audio.currentTime > 40, 'a click away from the route does not move the audio');
  });

  await test('the place name is shown and editable next to the map', async () => {
    const { detail, settle } = await run({}, { location: { lat: 53.0, lon: -2.19, source: 'dawarich-auto', place_name: 'Rock, Wyre Forest, Worcestershire', place_source: 'photon' } });
    await settle(60);
    const inputs = detail.querySelectorAll('input');
    assert.ok(inputs.some(i => i.value === 'Rock, Wyre Forest, Worcestershire'), 'the name is in a text box');
    assert.ok(/auto/.test(text(detail)), 'marked as looked up automatically');
  });

  console.log(`\n${passed} passed` + (process.exitCode ? ', SOME FAILED' : ''));
})();
