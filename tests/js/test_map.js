// Run: node tests/js/test_map.js   (no dependencies; a fake canvas/Image stand in for the browser)
const assert = require('assert');
const path = require('path');

// ---- minimal browser doubles -------------------------------------------------------------
const images = [];
global.Image = class { constructor() { this.onload = null; this.onerror = null; images.push(this); } set src(v) { this._src = v; } get src() { return this._src; } };
global.requestAnimationFrame = () => 1;   // tests call draw() themselves
global.window = { devicePixelRatio: 1, addEventListener() {} };

function makeCanvas(w = 800, h = 400) {
  const calls = [];
  const ctx = new Proxy({}, {
    get(_, prop) {
      if (prop === 'calls') return calls;
      if (prop === 'measureText') return () => ({ width: 30 });
      return (...a) => { calls.push([prop, ...a]); };
    },
    set(t, prop, val) { return true; },
  });
  const listeners = {};
  const canvas = {
    clientWidth: w, clientHeight: h, width: 0, height: 0, style: {}, parentElement: null,
    getContext: () => ctx,
    addEventListener(type, fn) { listeners[type] = fn; },
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
    setPointerCapture() {},
  };
  return { canvas, ctx, calls, fire: (type, ev) => listeners[type](Object.assign({ preventDefault() {}, pointerId: 1, pointerType: 'mouse', type }, ev)) };
}

const M = require(path.join(__dirname, '../../app/static/map.js'));
let passed = 0;
const test = (name, fn) => { try { fn(); passed++; console.log('PASS ' + name); } catch (e) { console.log('FAIL ' + name + '\n  ' + e.message); process.exitCode = 1; } };
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} expected ${b} +/- ${tol}, got ${a}`);

// Independent reference: the standard slippy-map tile formulas.
const lon2tile = (lon, z) => Math.floor((lon + 180) / 360 * 2 ** z);
const lat2tile = (lat, z) => Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * 2 ** z);

test('projection round-trips at many places and zooms', () => {
  for (const [lat, lon] of [[51.5074, -0.1278], [53.0082, -2.1812], [-33.86, 151.21], [0, 0], [78, 15], [-60, -170]])
    for (const z of [0, 3, 10.5, 17]) {
      const p = M.project(lat, lon, z), u = M.unproject(p.x, p.y, z);
      near(u.lat, lat, 1e-7, `lat ${lat},${lon}@${z}`); near(u.lon, lon, 1e-7, 'lon');
    }
});

test('projection agrees with the standard tile formulas (London and Stoke station at z10/z16)', () => {
  for (const [lat, lon, z] of [[51.5074, -0.1278, 10], [53.0082, -2.1812, 16], [-33.86, 151.21, 12]]) {
    const p = M.project(lat, lon, z);
    assert.strictEqual(Math.floor(p.x / 256), lon2tile(lon, z));
    assert.strictEqual(Math.floor(p.y / 256), lat2tile(lat, z));
  }
  assert.deepStrictEqual([lon2tile(-0.1278, 10), lat2tile(51.5074, 10)], [511, 340]);   // the well-known London tile
});

test('the centre of the view is the centre of the canvas, and toLatLon inverts toScreen', () => {
  const { canvas } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false });
  m.setView(53.0082, -2.1812, 15);
  const c = m.toScreen(53.0082, -2.1812);
  near(c.x, 400, 1e-6); near(c.y, 200, 1e-6);
  const s = m.toScreen(53.01, -2.17), back = m.toLatLon(s.x, s.y);
  near(back.lat, 53.01, 1e-8); near(back.lon, -2.17, 1e-8);
  assert.ok(s.x > 400 && s.y < 200, 'north-east of the centre is up and to the right');
});

test('draw requests exactly the tiles that cover the viewport, including the centre tile', () => {
  images.length = 0; M.tileCache.clear();
  const { canvas } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false, tileUrl: 'https://t.example/{z}/{x}/{y}.png' });
  m.setView(51.5074, -0.1278, 10);
  m.draw();
  const urls = images.map(i => i.src);
  assert.ok(urls.includes('https://t.example/10/511/340.png'), 'centre tile requested');
  const xs = urls.map(u => +u.split('/')[4]), ys = urls.map(u => +u.split('/')[5].replace('.png', ''));
  assert.ok(Math.min(...xs) <= 510 && Math.max(...xs) >= 512 && Math.min(...ys) <= 340 && Math.max(...ys) >= 340);
  assert.ok(urls.length >= 6 && urls.length <= 20, 'a sensible number of tiles: ' + urls.length);
  m.draw();
  assert.strictEqual(images.length, urls.length, 'a second draw reuses cached tiles instead of refetching');
});

test('fractional zoom uses the level below and scales it; tile positions stay continuous', () => {
  images.length = 0; M.tileCache.clear();
  const { canvas, calls } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false, tileUrl: 'https://t.example/{z}/{x}/{y}.png' });
  m.setView(51.5074, -0.1278, 10.5);
  m.draw();
  assert.ok(images.every(i => i._src.includes('/10/')), 'tiles come from zoom 10');
  images.forEach(i => { i.onload(); });     // load them all, redraw
  calls.length = 0; m.draw();
  const draws = calls.filter(c => c[0] === 'drawImage');
  near(draws[0][4], 256 * Math.SQRT2 + 0.5, 0.01, 'tile size on screen');
  const centre = draws.find(d => d[2] <= 400 && d[2] + d[4] >= 400 && d[3] <= 200 && d[3] + d[5] >= 200);
  assert.ok(centre, 'a tile is drawn under the centre point');
});

test('fitBounds shows every point with margin; a single point uses the max zoom', () => {
  const { canvas } = makeCanvas(600, 300);
  const m = new M.TileMap(canvas, { controls: false });
  const pts = [{ lat: 53.0079, lon: -2.1804 }, { lat: 53.0180, lon: -2.1500 }, { lat: 53.0100, lon: -2.2000 }];
  m.fitBounds(pts, 30);
  for (const p of pts) { const s = m.toScreen(p.lat, p.lon); assert.ok(s.x >= 29 && s.x <= 571 && s.y >= 29 && s.y <= 271, `inside with margin: ${s.x},${s.y}`); }
  const tight = pts.map(p => m.toScreen(p.lat, p.lon)), spanX = Math.max(...tight.map(t => t.x)) - Math.min(...tight.map(t => t.x));
  assert.ok(spanX > 200, 'it zoomed in as far as fits, not a tiny view');
  m.fitBounds([{ lat: 53.0079, lon: -2.1804 }], 30, 17);
  near(m.zoom, 17, 1e-9); near(m.lat, 53.0079, 1e-6);
});

test('zoomAround keeps the point under the cursor still; panBy moves content the right way', () => {
  const { canvas } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false });
  m.setView(52, -1.5, 8);
  const anchor = m.toLatLon(620, 90);
  m.zoomAround(620, 90, 9.3);
  const s = m.toScreen(anchor.lat, anchor.lon);
  near(s.x, 620, 0.01); near(s.y, 90, 0.01); near(m.zoom, 9.3, 1e-9);
  const before = m.toLatLon(500, 200);
  m.panBy(100, 0);
  const after = m.toScreen(before.lat, before.lon);
  near(after.x, 400, 1e-6); near(after.y, 200, 1e-6);
  m.zoomAround(400, 200, 99); near(m.zoom, 19, 1e-9, 'zoom is clamped to the maximum');
});

const track = [
  { lat: 53.0000, lon: -2.2000, offset_seconds: 0 },
  { lat: 53.0000, lon: -2.1800, offset_seconds: 100 },
  { lat: 53.0100, lon: -2.1800, offset_seconds: 300 },
];

test('interpolatePosition: ends hold, middles are linear in time, degenerate inputs are safe', () => {
  assert.strictEqual(M.interpolatePosition([], 5), null);
  assert.deepStrictEqual(M.interpolatePosition([track[0]], 99), { lat: 53, lon: -2.2 });
  assert.deepStrictEqual(M.interpolatePosition(track, -50), { lat: 53, lon: -2.2 });
  assert.deepStrictEqual(M.interpolatePosition(track, 9999), { lat: 53.01, lon: -2.18 });
  const a = M.interpolatePosition(track, 50); near(a.lon, -2.19, 1e-12); near(a.lat, 53, 1e-12);
  const b = M.interpolatePosition(track, 200); near(b.lat, 53.005, 1e-12); near(b.lon, -2.18, 1e-12);
  assert.deepStrictEqual(M.interpolatePosition(track, 100), { lat: 53, lon: -2.18 });
  const dup = [{ lat: 1, lon: 1, offset_seconds: 5 }, { lat: 2, lon: 2, offset_seconds: 5 }, { lat: 3, lon: 3, offset_seconds: 9 }];
  assert.doesNotThrow(() => M.interpolatePosition(dup, 5)); assert.ok(Number.isFinite(M.interpolatePosition(dup, 7).lat));
});

test('nearestOnPath: the closest point, its time, and clamping at the ends', () => {
  const pts = [{ offset_seconds: 0 }, { offset_seconds: 100 }, { offset_seconds: 300 }];
  const sc = [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 200 }];
  let r = M.nearestOnPath(pts, sc, 50, 10);        near(r.offset, 50, 1e-9); near(r.distance, 10, 1e-9);
  r = M.nearestOnPath(pts, sc, 110, 100);          near(r.offset, 200, 1e-9); near(r.distance, 10, 1e-9);
  r = M.nearestOnPath(pts, sc, -40, 0);            near(r.offset, 0, 1e-9); near(r.distance, 40, 1e-9);
  r = M.nearestOnPath(pts, sc, 100, 900);          near(r.offset, 300, 1e-9);
  r = M.nearestOnPath([pts[0]], [sc[0]], 3, 4);    assert.strictEqual(r.distance, 5);
  assert.strictEqual(M.nearestOnPath([], [], 0, 0), null);
  r = M.nearestOnPath([pts[0], pts[0]], [{ x: 5, y: 5 }, { x: 5, y: 5 }], 5, 5); assert.strictEqual(r.distance, 0);   // zero-length segment
});

test('clusterPins groups by screen cell and averages positions', () => {
  const pins = [{ x: 10, y: 10, id: 'a' }, { x: 30, y: 20, id: 'b' }, { x: 40, y: 40, id: 'c' }, { x: 300, y: 300, id: 'd' }];
  const cl = M.clusterPins(pins, 60).sort((p, q) => q.count - p.count);
  assert.strictEqual(cl.length, 2); assert.strictEqual(cl[0].count, 3); assert.strictEqual(cl[1].count, 1);
  near(cl[0].x, 80 / 3, 1e-9); assert.deepStrictEqual(cl[0].members.map(m => m.id).sort(), ['a', 'b', 'c']);
  assert.deepStrictEqual(M.clusterPins([], 60), []);
});

test('TrackLayer.hit: clicking the line gives the audio time; away from it gives nothing; clamped to the duration', () => {
  const { canvas } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false });
  m.fitBounds(track, 40);
  const layer = new M.TrackLayer(track, { duration: 250, getTime: () => 0 });
  const mid = m.toScreen(53.0, -2.19);                            // halfway along the first segment = 50 s
  let h = layer.hit(m, mid.x, mid.y + 6); assert.ok(h); near(h.offset, 50, 3);
  assert.strictEqual(layer.hit(m, mid.x, mid.y + 200), null, 'far from the line');
  const end = m.toScreen(53.01, -2.18);
  h = layer.hit(m, end.x, end.y); assert.strictEqual(h.offset, 250, 'the last fix is at 300 s but the audio is only 250 s long');
  const shuffled = new M.TrackLayer([track[2], track[0], track[1]], { duration: 300 });
  near(shuffled.hit(m, mid.x, mid.y).offset, 50, 3);              // input order doesn't matter
});

test('TrackLayer.draw puts the playhead where the audio is, on the route', () => {
  const { canvas, calls } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false });
  m.fitBounds(track, 40);
  let t = 200;
  const layer = new M.TrackLayer(track, { duration: 300, getTime: () => t });
  calls.length = 0; layer.draw(canvas.getContext(), m);
  const playhead = calls.filter(c => c[0] === 'arc' && c[3] === 7)[0];
  const expect = m.toScreen(53.005, -2.18);
  near(playhead[1], expect.x, 1e-6); near(playhead[2], expect.y, 1e-6);
  t = 0; calls.length = 0; layer.draw(canvas.getContext(), m);
  const start = m.toScreen(53.0, -2.2), p0 = calls.filter(c => c[0] === 'arc' && c[3] === 7)[0];
  near(p0[1], start.x, 1e-6); near(p0[2], start.y, 1e-6);
  assert.ok(calls.some(c => c[0] === 'lineTo'), 'the route is drawn as a line');
  layer.setHover(m, mid = m.toScreen(53.0, -2.19).x, m.toScreen(53.0, -2.19).y + 4);
  calls.length = 0; layer.draw(canvas.getContext(), m);
  assert.ok(calls.some(c => c[0] === 'fillText' && /^\d+:\d\d$/.test(c[1])), 'hovering shows the time under the pointer');
});

test('a single fix draws as a pin-like playhead without throwing', () => {
  const { canvas } = makeCanvas();
  const m = new M.TileMap(canvas, { controls: false });
  m.setView(53, -2, 15);
  const layer = new M.TrackLayer([{ lat: 53, lon: -2, offset_seconds: 0 }], { duration: 60 });
  assert.doesNotThrow(() => layer.draw(canvas.getContext(), m));
  assert.doesNotThrow(() => new M.PinLayer(53, -2).draw(canvas.getContext(), m));
});

test('pointer handling: a tap reports lat/lon, a drag pans and does not click, a pinch zooms, ctrl+wheel zooms', () => {
  const { canvas, fire } = makeCanvas(800, 400);
  const m = new M.TileMap(canvas, { controls: false });
  m.setView(52, -1.5, 10);
  let clicked = null; m.onClick = (x, y, ll) => { clicked = { x, y, ll }; };
  fire('pointerdown', { clientX: 400, clientY: 200 }); fire('pointerup', { clientX: 400, clientY: 200 });
  assert.ok(clicked); near(clicked.ll.lat, 52, 1e-6); near(clicked.ll.lon, -1.5, 1e-6);

  clicked = null; const lonBefore = m.lon;
  fire('pointerdown', { clientX: 400, clientY: 200 }); fire('pointermove', { clientX: 460, clientY: 200 }); fire('pointerup', { clientX: 460, clientY: 200 });
  assert.strictEqual(clicked, null, 'a drag is not a click'); assert.ok(m.lon < lonBefore, 'dragging right moves the view west');

  const z0 = m.zoom;
  fire('pointerdown', { pointerId: 1, clientX: 380, clientY: 200 }); fire('pointerdown', { pointerId: 2, clientX: 420, clientY: 200 });
  fire('pointermove', { pointerId: 2, clientX: 500, clientY: 200 });
  assert.ok(m.zoom > z0, 'spreading two fingers zooms in');
  fire('pointerup', { pointerId: 1, clientX: 380, clientY: 200 }); fire('pointerup', { pointerId: 2, clientX: 500, clientY: 200 });

  const z1 = m.zoom; let prevented = 0;
  fire('wheel', { clientX: 400, clientY: 200, deltaY: -100, ctrlKey: false, preventDefault() { prevented++; } });
  assert.strictEqual(m.zoom, z1, 'a plain wheel is left for page scrolling'); assert.strictEqual(prevented, 0);
  fire('wheel', { clientX: 400, clientY: 200, deltaY: -100, ctrlKey: true, preventDefault() { prevented++; } });
  assert.ok(m.zoom > z1 && prevented === 1, 'ctrl+wheel zooms');
  const all = new M.TileMap(makeCanvas().canvas, { controls: false, wheelZoom: 'always' });
});

test('tile cache: identical requests share one image and old tiles are evicted', () => {
  images.length = 0; M.tileCache.clear();
  const { canvas } = makeCanvas();
  const m = new M.TileMap(canvas, { controls: false, tileUrl: 'https://t.example/{z}/{x}/{y}.png' });
  const a = m._tile(5, 1, 1), b = m._tile(5, 1, 1);
  assert.strictEqual(a, b); assert.strictEqual(images.length, 1);
  for (let i = 0; i < 700; i++) m._tile(12, i, 3);
  assert.ok(M.tileCache.size <= 600, 'cache is bounded: ' + M.tileCache.size);
});

test('fmtTime', () => { assert.strictEqual(M.fmtTime(65), '1:05'); assert.strictEqual(M.fmtTime(3725), '1:02:05'); assert.strictEqual(M.fmtTime(-3), '0:00'); });

console.log(`\n${passed} passed` + (process.exitCode ? ', SOME FAILED' : ''));
