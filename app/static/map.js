// A small slippy-map of our own: OpenStreetMap raster tiles on a canvas, with pan, pinch/wheel zoom, and two
// overlays we need: a recording's GPS TRACK (click the line to jump the audio to that moment; the playhead moves
// along it as the audio plays) and clustered PINS for the all-recordings map. No library, no CDN: the only
// outside request is for the tile images themselves (URL configurable, default OSM; attribution is shown).
//
// The maths (projection, interpolation along a track, nearest point on a path, clustering) is in plain functions
// with no DOM so it can be tested in Node: tests/js/test_map.js.
(function (root) {
  const TILE = 256;

  // ---------------------------------------------------------------- pure maths
  const clampLat = (lat) => Math.max(-85.0511, Math.min(85.0511, lat));

  // Web Mercator: lat/lon -> world pixel at a (possibly fractional) zoom, where the world is 256 * 2^zoom wide.
  function project(lat, lon, zoom) {
    const size = TILE * Math.pow(2, zoom);
    const sin = Math.sin(clampLat(lat) * Math.PI / 180);
    return { x: (lon + 180) / 360 * size, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size };
  }

  function unproject(x, y, zoom) {
    const size = TILE * Math.pow(2, zoom);
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    return { lat: 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))), lon };
  }

  // Where was the recorder at audio time t? `points` are sorted by offset_seconds (seconds from the recording start).
  // Linear between the two surrounding fixes; before the first / after the last fix it stays at the end point.
  function interpolatePosition(points, t) {
    if (!points.length) return null;
    if (t <= points[0].offset_seconds) return { lat: points[0].lat, lon: points[0].lon };
    const last = points[points.length - 1];
    if (t >= last.offset_seconds) return { lat: last.lat, lon: last.lon };
    let lo = 0, hi = points.length - 1;                      // binary search for the segment containing t
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (points[mid].offset_seconds <= t) lo = mid; else hi = mid; }
    const a = points[lo], b = points[hi];
    const span = b.offset_seconds - a.offset_seconds;
    const f = span > 0 ? (t - a.offset_seconds) / span : 0;
    return { lat: a.lat + (b.lat - a.lat) * f, lon: a.lon + (b.lon - a.lon) * f };
  }

  // The closest point on the polyline (in SCREEN pixels) to (px, py), and the audio time it corresponds to.
  // `screen` is [{x, y}] parallel to `points`. Returns { offset, distance, index } or null.
  function nearestOnPath(points, screen, px, py) {
    if (!points.length) return null;
    if (points.length === 1) return { offset: points[0].offset_seconds, distance: Math.hypot(screen[0].x - px, screen[0].y - py), index: 0 };
    let best = null;
    for (let i = 0; i < points.length - 1; i++) {
      const ax = screen[i].x, ay = screen[i].y, bx = screen[i + 1].x, by = screen[i + 1].y;
      const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
      const f = len2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
      const d = Math.hypot(ax + dx * f - px, ay + dy * f - py);
      if (!best || d < best.distance) {
        const t = points[i].offset_seconds + (points[i + 1].offset_seconds - points[i].offset_seconds) * f;
        best = { offset: t, distance: d, index: i };
      }
    }
    return best;
  }

  // Group pins into screen-space grid cells so a crowded area shows one numbered bubble instead of hundreds of dots.
  // `pins` are [{x, y, ...}] in screen pixels. Returns [{x, y, count, members}] (x, y = mean of the members).
  function clusterPins(pins, cell) {
    const cells = new Map();
    for (const p of pins) {
      const key = Math.floor(p.x / cell) + ':' + Math.floor(p.y / cell);
      let c = cells.get(key);
      if (!c) { c = { sx: 0, sy: 0, members: [] }; cells.set(key, c); }
      c.sx += p.x; c.sy += p.y; c.members.push(p);
    }
    return [...cells.values()].map(c => ({ x: c.sx / c.members.length, y: c.sy / c.members.length, count: c.members.length, members: c.members }));
  }

  function fmtTime(s) {
    s = Math.max(0, s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
  }

  // ---------------------------------------------------------------- the map
  // Tiles are shared between every map on the page (the detail screen rebuilds its map on each re-render).
  const tileCache = new Map();

  class TileMap {
    // canvas: an element with a parent that is position:relative. opts: tileUrl, attribution, maxZoom, minZoom,
    // wheelZoom ('always' | 'ctrl'), controls (default true)
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.tileUrl = opts.tileUrl || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
      this.maxZoom = opts.maxZoom || 19;
      this.minZoom = opts.minZoom === undefined ? 2 : opts.minZoom;
      this.wheelZoom = opts.wheelZoom || 'ctrl';
      this.lat = 52.5; this.lon = -1.9; this.zoom = 6;      // the Midlands, until told otherwise
      this.overlays = [];
      this.onClick = null;                                  // (x, y, latlon, event) => void, only for taps/clicks, not drags
      this.onHover = null;                                  // (x, y) => void, mouse only
      this.cursor = 'grab';
      this._raf = null;
      this._pointers = new Map();
      this._bind();
      if (opts.controls !== false && canvas.parentElement) this._addControls(canvas.parentElement, opts.attribution);
    }

    size() { return { w: this.canvas.clientWidth, h: this.canvas.clientHeight }; }

    toScreen(lat, lon) {
      const { w, h } = this.size();
      const c = project(this.lat, this.lon, this.zoom), p = project(lat, lon, this.zoom);
      return { x: p.x - c.x + w / 2, y: p.y - c.y + h / 2 };
    }

    toLatLon(x, y) {
      const { w, h } = this.size();
      const c = project(this.lat, this.lon, this.zoom);
      return unproject(c.x - w / 2 + x, c.y - h / 2 + y, this.zoom);
    }

    setView(lat, lon, zoom) {
      this.lat = clampLat(lat); this.lon = lon;
      this.zoom = Math.max(this.minZoom, Math.min(this.maxZoom, zoom));
      this.schedule();
    }

    // Choose the view that shows all of `points` ([{lat, lon}]) with `pad` pixels of margin.
    fitBounds(points, pad = 30, maxZoom = 17) {
      if (!points.length) return;
      const lats = points.map(p => p.lat), lons = points.map(p => p.lon);
      const minLat = Math.min(...lats), maxLat = Math.max(...lats), minLon = Math.min(...lons), maxLon = Math.max(...lons);
      const a = project(maxLat, minLon, 0), b = project(minLat, maxLon, 0);       // world pixels at zoom 0
      const bw = b.x - a.x, bh = b.y - a.y;
      const { w, h } = this.size();
      let zoom = maxZoom;
      if (bw > 0) zoom = Math.min(zoom, Math.log2(Math.max(1, w - 2 * pad) / bw));
      if (bh > 0) zoom = Math.min(zoom, Math.log2(Math.max(1, h - 2 * pad) / bh));
      this.setView((minLat + maxLat) / 2, (minLon + maxLon) / 2, zoom);
    }

    // Change zoom while keeping the point under (x, y) fixed on screen (wheel, pinch, double-click).
    zoomAround(x, y, newZoom) {
      const anchor = this.toLatLon(x, y);
      this.zoom = Math.max(this.minZoom, Math.min(this.maxZoom, newZoom));
      const s = this.toScreen(anchor.lat, anchor.lon);
      this.panBy(s.x - x, s.y - y);
    }

    // Move the map so its content shifts by (-dx, -dy) on screen.
    panBy(dx, dy) {
      const c = project(this.lat, this.lon, this.zoom);
      const n = unproject(c.x + dx, c.y + dy, this.zoom);
      this.lat = clampLat(n.lat); this.lon = n.lon;
      this.schedule();
    }

    schedule() {
      if (this._raf) return;
      const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(fn, 0);
      this._raf = raf(() => { this._raf = null; this.draw(); });
    }

    _tile(z, x, y) {
      const key = this.tileUrl + '|' + z + '/' + x + '/' + y;
      let t = tileCache.get(key);
      if (t) { tileCache.delete(key); tileCache.set(key, t); return t; }      // keep recently used at the end
      t = { loaded: false, failed: false, img: new Image() };
      t.img.onload = () => { t.loaded = true; this.schedule(); };
      t.img.onerror = () => { t.failed = true; };
      t.img.src = this.tileUrl.replace('{z}', z).replace('{x}', x).replace('{y}', y);
      tileCache.set(key, t);
      if (tileCache.size > 600) { let i = 0; for (const k of tileCache.keys()) { tileCache.delete(k); if (++i >= 150) break; } }
      return t;
    }

    draw() {
      const { w, h } = this.size();
      if (!w || !h) return;
      const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
        this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      }
      const ctx = this.canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = '#e6e3da';
      ctx.fillRect(0, 0, w, h);

      // On high-density screens use the next zoom level's tiles at half size so they are crisp.
      const bias = dpr >= 2 && Math.floor(this.zoom) + 1 <= this.maxZoom ? 1 : 0;
      const zi = Math.min(this.maxZoom, Math.max(0, Math.floor(this.zoom) + bias));
      const size = TILE * Math.pow(2, this.zoom - zi);           // a tile's size on screen, in CSS pixels
      const c = project(this.lat, this.lon, this.zoom);
      const left = c.x - w / 2, top = c.y - h / 2;
      const n = Math.pow(2, zi);
      for (let ty = Math.floor(top / size); ty <= Math.floor((top + h) / size); ty++) {
        if (ty < 0 || ty >= n) continue;
        for (let tx = Math.floor(left / size); tx <= Math.floor((left + w) / size); tx++) {
          const wx = ((tx % n) + n) % n;
          const t = this._tile(zi, wx, ty);
          const dx = tx * size - left, dy = ty * size - top;
          if (t.loaded) ctx.drawImage(t.img, dx, dy, size + 0.5, size + 0.5);       // +0.5 hides hairline seams
          else this._parentFallback(ctx, zi, wx, ty, dx, dy, size);
        }
      }
      for (const overlay of this.overlays) overlay(ctx, this);
    }

    // While a tile loads, show the blurry piece of the already-loaded tile one level up.
    _parentFallback(ctx, zi, x, y, dx, dy, size) {
      if (zi === 0) return;
      const key = this.tileUrl + '|' + (zi - 1) + '/' + (x >> 1) + '/' + (y >> 1);
      const p = tileCache.get(key);
      if (p && p.loaded) ctx.drawImage(p.img, (x & 1) * 128, (y & 1) * 128, 128, 128, dx, dy, size + 0.5, size + 0.5);
    }

    _bind() {
      const c = this.canvas;
      c.style.touchAction = 'none';                                // the map handles its own drags and pinches
      c.style.cursor = this.cursor;
      let drag = null, moved = false, pinch = null;
      const pos = (ev) => { const r = c.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };

      c.addEventListener('pointerdown', (ev) => {
        if (c.setPointerCapture) c.setPointerCapture(ev.pointerId);
        this._pointers.set(ev.pointerId, pos(ev));
        if (this._pointers.size === 1) { drag = pos(ev); moved = false; }
        if (this._pointers.size === 2) {
          const [p, q] = [...this._pointers.values()];
          pinch = { dist: Math.hypot(p.x - q.x, p.y - q.y), zoom: this.zoom };
          drag = null; moved = true;
        }
      });
      c.addEventListener('pointermove', (ev) => {
        const p = pos(ev);
        if (!this._pointers.has(ev.pointerId)) { if (this.onHover && ev.pointerType === 'mouse') this.onHover(p.x, p.y); return; }
        this._pointers.set(ev.pointerId, p);
        if (this._pointers.size === 2 && pinch) {
          const [a, b] = [...this._pointers.values()];
          const d = Math.hypot(a.x - b.x, a.y - b.y);
          if (pinch.dist > 0 && d > 0) this.zoomAround((a.x + b.x) / 2, (a.y + b.y) / 2, pinch.zoom + Math.log2(d / pinch.dist));
        } else if (drag) {
          const dx = p.x - drag.x, dy = p.y - drag.y;
          if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
          if (moved) { this.panBy(-dx, -dy); drag = p; c.style.cursor = 'grabbing'; }
        }
      });
      const finish = (ev) => {
        const p = pos(ev);
        const wasSingle = this._pointers.size === 1;
        this._pointers.delete(ev.pointerId);
        if (this._pointers.size < 2) pinch = null;
        if (this._pointers.size === 0) { drag = null; c.style.cursor = this.cursor; }
        if (wasSingle && !moved && ev.type === 'pointerup' && this.onClick) this.onClick(p.x, p.y, this.toLatLon(p.x, p.y), ev);
      };
      c.addEventListener('pointerup', finish);
      c.addEventListener('pointercancel', (ev) => { this._pointers.delete(ev.pointerId); drag = null; pinch = null; });
      c.addEventListener('dblclick', (ev) => { const p = pos(ev); this.zoomAround(p.x, p.y, this.zoom + 1); });
      c.addEventListener('wheel', (ev) => {
        if (this.wheelZoom === 'ctrl' && !ev.ctrlKey) return;      // otherwise a plain wheel scrolls the page
        ev.preventDefault();
        const p = pos(ev);
        this.zoomAround(p.x, p.y, this.zoom + (ev.deltaY < 0 ? 0.5 : -0.5));
      }, { passive: false });
      if (typeof window !== 'undefined') window.addEventListener('resize', () => this.schedule());
    }

    _addControls(parent, attribution) {
      const mk = (text, label, fn) => {
        const b = document.createElement('button');
        b.type = 'button'; b.textContent = text; b.setAttribute('aria-label', label);
        b.style.cssText = 'display:block;width:2rem;height:2rem;margin-bottom:2px;font-size:1.2rem;line-height:1;padding:0;border:1px solid #8886;border-radius:4px;background:Canvas;color:CanvasText;cursor:pointer';
        b.addEventListener('click', fn);
        return b;
      };
      const box = document.createElement('div');
      box.style.cssText = 'position:absolute;top:8px;right:8px;z-index:2';
      box.append(mk('+', 'Zoom in', () => { const s = this.size(); this.zoomAround(s.w / 2, s.h / 2, this.zoom + 1); }),
                 mk('−', 'Zoom out', () => { const s = this.size(); this.zoomAround(s.w / 2, s.h / 2, this.zoom - 1); }));
      parent.append(box);
      const attr = document.createElement('div');
      attr.style.cssText = 'position:absolute;right:0;bottom:0;z-index:2;font-size:10px;padding:1px 5px;background:rgba(255,255,255,.75);color:#333;border-top-left-radius:4px';
      const a = document.createElement('a');
      a.href = 'https://www.openstreetmap.org/copyright'; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = attribution || '© OpenStreetMap contributors';
      a.style.color = '#0645ad';
      attr.append(a);
      parent.append(attr);
    }
  }

  // ---------------------------------------------------------------- overlay: one recording's track
  class TrackLayer {
    // points: [{lat, lon, offset_seconds}] (any order); getTime(): the audio's current time in seconds
    constructor(points, opts = {}) {
      this.points = points.slice().sort((a, b) => a.offset_seconds - b.offset_seconds);
      this.getTime = opts.getTime || (() => 0);
      this.duration = opts.duration || 0;
      this.hoverOffset = null;
      this.hoverAt = null;
    }

    screenPoints(map) { return this.points.map(p => map.toScreen(p.lat, p.lon)); }

    // Is (x, y) close enough to the line to count as pointing at it? Returns { offset, distance } or null.
    hit(map, x, y, radius = 22) {
      const near = nearestOnPath(this.points, this.screenPoints(map), x, y);
      if (!near || near.distance > radius) return null;
      const max = this.duration || this.points[this.points.length - 1].offset_seconds;
      return { offset: Math.max(0, Math.min(max, near.offset)), distance: near.distance };
    }

    setHover(map, x, y) {
      const h = x === null ? null : this.hit(map, x, y, 26);
      this.hoverOffset = h ? h.offset : null;
      this.hoverAt = h ? { x, y } : null;
      map.schedule();
    }

    draw(ctx, map) {
      const pts = this.screenPoints(map);
      if (!pts.length) return;
      if (pts.length > 1) {
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        for (const [width, colour] of [[7, 'rgba(255,255,255,.95)'], [4, 'rgba(224,83,61,.95)']]) {   // white halo, then the route
          ctx.lineWidth = width; ctx.strokeStyle = colour; ctx.beginPath();
          pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.stroke();
        }
      }
      ctx.fillStyle = '#fff'; ctx.strokeStyle = 'rgba(224,83,61,1)'; ctx.lineWidth = 1.5;
      for (const p of pts) { ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
      if (pts.length > 1) {                                        // start (green) and end (dark)
        for (const [p, colour] of [[pts[0], '#2a9d4b'], [pts[pts.length - 1], '#333']]) {
          ctx.fillStyle = colour; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        }
      }
      // The playhead: where the recorder was at the moment the audio is playing.
      const at = interpolatePosition(this.points, this.getTime());
      if (at) {
        const s = map.toScreen(at.lat, at.lon);
        ctx.fillStyle = 'rgba(58,123,213,.25)'; ctx.beginPath(); ctx.arc(s.x, s.y, 13, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = '#3a7bd5'; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.arc(s.x, s.y, 7, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      }
      if (this.hoverOffset !== null && this.hoverAt) {            // the time under the pointer, so it's clear what a click will do
        const text = fmtTime(this.hoverOffset);
        ctx.font = '12px system-ui, sans-serif';
        const tw = ctx.measureText(text).width + 12;
        const bx = Math.min(Math.max(4, this.hoverAt.x + 12), map.size().w - tw - 4), by = Math.max(4, this.hoverAt.y - 30);
        ctx.fillStyle = 'rgba(30,30,30,.88)'; ctx.fillRect(bx, by, tw, 20);
        ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.fillText(text, bx + 6, by + 10);
      }
    }
  }

  // ---------------------------------------------------------------- overlay: a single pin
  class PinLayer {
    constructor(lat, lon, colour) { this.lat = lat; this.lon = lon; this.colour = colour || '#e0533d'; }
    draw(ctx, map) {
      const s = map.toScreen(this.lat, this.lon);
      ctx.fillStyle = this.colour; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.arc(s.x, s.y, 8, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
  }

  const api = { TILE, project, unproject, interpolatePosition, nearestOnPath, clusterPins, fmtTime, TileMap, TrackLayer, PinLayer, tileCache };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.AudioMap = api;
})(typeof window !== 'undefined' ? window : globalThis);
