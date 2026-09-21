// The waveform view used everywhere a recording is drawn: the strip in the bottom player (scrub), the zoomable one on
// the recording page, and the big one in the editor (ruler, clip regions, a draggable selection, an overview strip).
//
// Data is audiowaveform's native 8-bit .dat: about 100 min/max peak pairs per second, whose header carries the sample
// rate and samples-per-pixel it was built with, so time = index * spp / rate exactly (no drift). See jobs/previews.py.
//
// Everything that does not need a canvas is a plain function so it can be tested in Node (tests/js/test_waveform.js).
(function (root) {
  // ---------------------------------------------------------------- data
  function parseDat(buf) {
    const dv = new DataView(buf);
    if (buf.byteLength < 20) throw new Error('the waveform file is not in the expected format');
    const version = dv.getInt32(0, true), flags = dv.getUint32(4, true);
    const rate = dv.getInt32(8, true), spp = dv.getInt32(12, true), length = dv.getUint32(16, true);
    const channels = version === 2 ? dv.getInt32(20, true) : 1;
    const headerSize = version === 2 ? 24 : 20;
    if ((version !== 1 && version !== 2) || !(flags & 1) || channels !== 1 || rate <= 0 || spp <= 0
        || buf.byteLength !== headerSize + length * 2) {
      throw new Error('the waveform file is not in the expected format');
    }
    const raw = new Int8Array(buf, headerSize, length * 2);
    const mins = new Int8Array(length), maxs = new Int8Array(length), mags = [];
    for (let i = 0; i < length; i++) {
      mins[i] = raw[2 * i]; maxs[i] = raw[2 * i + 1];
      if (i % 10 === 0) mags.push(Math.max(Math.abs(mins[i]), Math.abs(maxs[i])));
    }
    mags.sort((a, b) => a - b);
    const p95 = mags.length ? mags[Math.floor(mags.length * 0.95)] : 0;
    // Show quiet recordings at a readable height: scale so the 95th percentile reaches ~80%, within 1x-16x.
    const gain = p95 > 0 ? Math.min(16, Math.max(1, (0.8 * 127) / p95)) : 1;
    return { rate, spp, length, mins, maxs, gain, pps: rate / spp, duration: length * spp / rate };
  }

  const waveCache = { id: null, wf: null };          // survives the page re-rendering its sections

  // -> parsed waveform, or null while it is still being generated (202). Throws with the server's reason on failure.
  async function loadWaveform(id, fetchFn) {
    if (waveCache.id === id && waveCache.wf) return waveCache.wf;
    const f = fetchFn || (typeof fetch === 'function' ? fetch : null);
    const r = await f(`/api/resources/${id}/waveform`);
    if (r.status === 202) return null;
    if (!r.ok) {
      let d = {};
      try { d = await r.json(); } catch (e) { /* no body */ }
      throw new Error(d.error || r.statusText);
    }
    const wf = parseDat(await r.arrayBuffer());
    waveCache.id = id; waveCache.wf = wf;
    return wf;
  }

  // ---------------------------------------------------------------- time formatting
  const STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];

  // The smallest "round" tick spacing (in seconds) that gives no more than about `target` ticks across `span`.
  function niceStep(span, target) {
    for (const s of STEPS) if (span / s <= target) return s;
    return STEPS[STEPS.length - 1];
  }

  function fmtClock(t, decimals) {
    t = Math.max(0, t);
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
    let s = t - h * 3600 - m * 60;
    const sec = decimals ? s.toFixed(decimals).padStart(decimals + 3, '0') : String(Math.floor(s)).padStart(2, '0');
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + sec;
  }

  // "12.5", "1:02", "1:02.5", "0:01:02.5" -> seconds, or NaN if it isn't a time.
  function parseTime(text) {
    const s = String(text).trim();
    if (!s) return NaN;
    const parts = s.split(':');
    if (parts.length > 3 || parts.some(p => p.trim() === '' || !/^\d+(\.\d+)?$/.test(p.trim()))) return NaN;
    return parts.map(Number).reduce((acc, v) => acc * 60 + v, 0);
  }

  // ---------------------------------------------------------------- the view
  class WaveView {
    // opts: wf, duration, getTime(), onSeek(t), scrub, zoomable, ruler, selectable, mode ('select'|'move'), regions() ->
    //       [{id, start, end, label, selected}], onSelect(sel|null), onRegionClick(region), overviewOf (a WaveView),
    //       wheelZoom ('ctrl'|'always')
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.wf = opts.wf || null;
      this.duration = opts.duration || (this.wf ? this.wf.duration : 0);
      this.getTime = opts.getTime || (() => 0);
      this.onSeek = opts.onSeek || null;
      this.scrub = !!opts.scrub;
      this.zoomable = opts.zoomable !== false && !this.scrub && !opts.overviewOf;
      this.ruler = !!opts.ruler;
      this.selectable = !!opts.selectable;
      this.mode = opts.mode || 'move';
      this.getRegions = opts.regions || (() => []);
      this.onSelect = opts.onSelect || null;
      this.onRegionClick = opts.onRegionClick || null;
      this.overviewOf = opts.overviewOf || null;
      this.wheelZoom = opts.wheelZoom || 'ctrl';
      this.onDraw = opts.onDraw || null;       // called after every draw (keep a time label in step)
      this.minSpan = 0.25;
      this.start = 0;
      this.span = Math.max(this.duration, 0.001);
      this.sel = null;
      this._pointers = new Map();
      this._raf = null;
      if (this.overviewOf) this.overviewOf._overview = this;
      this._bind();
    }

    // `duration` (the recording's real length) overrides the waveform's own, which can be a fraction longer because its
    // last peak covers a whole 10 ms cell; anything that must not run past the audio (a clip end) clamps to it.
    setWaveform(wf, duration) { this.wf = wf; this.duration = duration || wf.duration; this.fit(); }
    setDuration(d) { if (!this.wf) { this.duration = d; this.span = Math.max(d, 0.001); this.draw(); } }

    size() { return { w: this.canvas.clientWidth, h: this.canvas.clientHeight }; }
    _top() { return this.ruler ? 18 : 0; }
    _bottom() { return this.selectable ? 14 : 0; }        // a strip for clip labels

    // ---- view maths
    _clamp() {
      this.span = Math.min(this.duration || this.span, Math.max(Math.min(this.minSpan, this.duration || this.minSpan), this.span));
      this.start = Math.min(Math.max(0, this.start), Math.max(0, (this.duration || 0) - this.span));
    }
    fit() { this.start = 0; this.span = Math.max(this.duration, 0.001); this.draw(); }
    timeAtX(x) { const { w } = this.size(); return this.start + this.span * Math.min(1, Math.max(0, x / (w || 1))); }
    xAtTime(t) { const { w } = this.size(); return ((t - this.start) / this.span) * w; }
    zoomBy(factor, anchorTime) {
      const anchor = anchorTime === undefined ? this.start + this.span / 2 : anchorTime;
      const frac = (anchor - this.start) / this.span;
      this.span /= factor; this._clamp();
      this.start = anchor - frac * this.span; this._clamp();
      this.draw();
    }
    panTo(start) { this.start = start; this._clamp(); this.draw(); }
    zoomToRange(a, b, pad = 0.15) {
      const len = Math.max(b - a, this.minSpan);
      this.span = len * (1 + 2 * pad); this._clamp();
      this.start = a - len * pad; this._clamp(); this.draw();
    }
    // Bring time t into view, scrolling only if needed (used to follow the playhead).
    ensureVisible(t, margin = 0.08) {
      if (t > this.start + this.span * (1 - margin) || t < this.start) { this.start = t - this.span * margin; this._clamp(); this.draw(); }
    }

    // ---- selection
    setSelection(a, b, silent) {
      if (a === null) { this.sel = null; } else {
        const lo = Math.max(0, Math.min(a, b)), hi = Math.min(this.duration || Math.max(a, b), Math.max(a, b));
        this.sel = hi - lo > 0 ? { start: lo, end: hi } : null;
      }
      this.draw();
      if (!silent && this.onSelect) this.onSelect(this.sel);
    }
    getSelection() { return this.sel ? { start: this.sel.start, end: this.sel.end } : null; }
    clearSelection() { this.setSelection(null); }
    regionAt(x, y) {
      const { h } = this.size();
      if (!this.selectable || y < h - this._bottom()) return null;
      const t = this.timeAtX(x);
      return this.getRegions().find(r => t >= r.start && t <= r.end) || null;
    }

    schedule() {
      if (this._raf) return;
      const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(fn, 0);
      this._raf = raf(() => { this._raf = null; this.draw(); });
    }

    // ---- drawing
    draw() {
      const { w, h } = this.size();
      if (!w || !h || !this.canvas.getContext) return;
      const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
        this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      }
      const ctx = this.canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const fg = (typeof getComputedStyle === 'function' && getComputedStyle(this.canvas).color) || '#444';
      const top = this._top(), bottom = h - this._bottom();
      const mid = (top + bottom) / 2, scale = ((bottom - top) / 2 / 128) * (this.wf ? this.wf.gain : 1);

      if (this.ruler) this._drawRuler(ctx, w, top, fg);

      ctx.fillStyle = fg; ctx.globalAlpha = 0.28; ctx.fillRect(0, Math.round(mid), w, 1);
      if (this.wf) {
        ctx.globalAlpha = 0.85;
        const wf = this.wf;
        for (let x = 0; x < w; x++) {
          const a = Math.max(0, Math.floor((this.start + this.span * x / w) * wf.pps));
          const b = Math.max(a + 1, Math.floor((this.start + this.span * (x + 1) / w) * wf.pps));
          let lo = 127, hi = -128;
          for (let i = a; i < b && i < wf.length; i++) { if (wf.mins[i] < lo) lo = wf.mins[i]; if (wf.maxs[i] > hi) hi = wf.maxs[i]; }
          if (hi < lo) continue;
          const y0 = Math.max(top, mid - hi * scale), y1 = Math.min(bottom, mid - lo * scale);
          ctx.fillRect(x, y0, 1, Math.max(1, y1 - y0));
        }
      }
      ctx.globalAlpha = 1;

      this._drawRegions(ctx, w, h, top, bottom);
      if (this.sel) this._drawSelection(ctx, w, top, bottom);
      if (this.overviewOf) this._drawViewport(ctx, w, h);
      else this._drawPlayhead(ctx, h);
      if (this.onDraw) this.onDraw(this);
    }

    _drawRuler(ctx, w, top, fg) {
      const step = niceStep(this.span, Math.max(4, Math.floor(w / 90)));
      const decimals = step < 1 ? (step < 0.1 ? 2 : 1) : 0;
      ctx.font = '10px system-ui, sans-serif'; ctx.textBaseline = 'middle'; ctx.fillStyle = fg;
      ctx.globalAlpha = 0.6; ctx.fillRect(0, top - 1, w, 1);
      for (let t = Math.ceil(this.start / step) * step; t <= this.start + this.span + 1e-9; t += step) {
        const x = Math.round(this.xAtTime(t));
        ctx.globalAlpha = 0.5; ctx.fillRect(x, top - 6, 1, 6);
        ctx.globalAlpha = 0.8; ctx.fillText(fmtClock(t, decimals), x + 3, 7);
      }
      ctx.globalAlpha = 1;
    }

    _drawRegions(ctx, w, h, top, bottom) {
      if (!this.selectable) return;
      ctx.font = '10px system-ui, sans-serif'; ctx.textBaseline = 'middle';
      for (const r of this.getRegions()) {
        const x0 = this.xAtTime(r.start), x1 = this.xAtTime(r.end);
        if (x1 < 0 || x0 > w) continue;
        ctx.fillStyle = r.selected ? 'rgba(42,157,75,.30)' : 'rgba(58,123,213,.16)';
        ctx.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top);
        ctx.fillStyle = r.selected ? 'rgba(42,157,75,.9)' : 'rgba(58,123,213,.75)';
        ctx.fillRect(x0, bottom, Math.max(1, x1 - x0), this._bottom());
        if (x1 - x0 > 36 && r.label) { ctx.fillStyle = '#fff'; ctx.fillText(r.label, Math.max(x0, 0) + 4, bottom + this._bottom() / 2); }
      }
    }

    _drawSelection(ctx, w, top, bottom) {
      const x0 = this.xAtTime(this.sel.start), x1 = this.xAtTime(this.sel.end);
      ctx.fillStyle = 'rgba(224,83,61,.22)'; ctx.fillRect(x0, top, Math.max(1, x1 - x0), bottom - top);
      ctx.fillStyle = 'rgba(224,83,61,.95)';
      ctx.fillRect(Math.round(x0), top, 2, bottom - top); ctx.fillRect(Math.round(x1) - 2, top, 2, bottom - top);
      const mid = (top + bottom) / 2;
      ctx.fillRect(Math.round(x0) - 3, mid - 9, 8, 18); ctx.fillRect(Math.round(x1) - 5, mid - 9, 8, 18);     // grab handles
    }

    _drawPlayhead(ctx, h) {
      const now = this.getTime();
      const x = this.xAtTime(now);
      if (x >= 0 && x <= this.size().w) { ctx.fillStyle = '#e0533d'; ctx.fillRect(Math.round(x) - 1, 0, 2, h); }
    }

    // The overview strip shows the whole file with the main view's window outlined and the rest dimmed.
    _drawViewport(ctx, w, h) {
      const main = this.overviewOf;
      const x0 = this.xAtTime(main.start), x1 = this.xAtTime(main.start + main.span);
      ctx.fillStyle = 'rgba(0,0,0,.28)'; ctx.fillRect(0, 0, Math.max(0, x0), h); ctx.fillRect(x1, 0, Math.max(0, w - x1), h);
      ctx.strokeStyle = 'rgba(224,83,61,.95)'; ctx.lineWidth = 2; ctx.strokeRect(x0 + 1, 1, Math.max(2, x1 - x0 - 2), h - 2);
      const now = this.xAtTime(main.getTime());
      ctx.fillStyle = '#e0533d'; ctx.fillRect(Math.round(now) - 1, 0, 2, h);
    }

    // ---- interaction
    _bind() {
      const c = this.canvas;
      if (c.style) c.style.touchAction = 'none';
      let drag = null;
      const pos = (ev) => { const r = c.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
      const edgeAt = (x) => {
        if (!this.sel || !this.selectable) return null;
        const a = Math.abs(x - this.xAtTime(this.sel.start)), b = Math.abs(x - this.xAtTime(this.sel.end));
        if (Math.min(a, b) > 12) return null;
        return a <= b ? 'start' : 'end';
      };

      c.addEventListener('pointerdown', (ev) => {
        if (c.setPointerCapture) c.setPointerCapture(ev.pointerId);
        const p = pos(ev);
        this._pointers.set(ev.pointerId, p);
        if (this._pointers.size === 2 && this.zoomable) {
          const [a, b] = [...this._pointers.values()];
          drag = { kind: 'pinch', dist: Math.abs(a.x - b.x) };
          return;
        }
        if (this.overviewOf) { drag = { kind: 'overview', moved: true }; this._overviewTo(p.x); return; }
        if (this.scrub) { drag = { kind: 'scrub', moved: true }; if (this.onSeek) this.onSeek(this.timeAtX(p.x)); this.draw(); return; }
        const edge = edgeAt(p.x);
        if (edge) { drag = { kind: 'edge', edge, moved: true }; return; }
        if (this.selectable && this.mode === 'select' && !this.regionAt(p.x, p.y)) { drag = { kind: 'new', x: p.x, anchor: this.timeAtX(p.x), moved: false }; return; }
        drag = { kind: 'pan', x: p.x, start: this.start, moved: false, at: p };
      });

      c.addEventListener('pointermove', (ev) => {
        if (!this._pointers.has(ev.pointerId)) return;
        const p = pos(ev);
        this._pointers.set(ev.pointerId, p);
        if (!drag) return;
        if (drag.kind === 'pinch' && this._pointers.size === 2) {
          const [a, b] = [...this._pointers.values()];
          const d = Math.abs(a.x - b.x);
          if (drag.dist > 0 && d > 0) this.zoomBy(d / drag.dist, this.timeAtX((a.x + b.x) / 2));
          drag.dist = d;
        } else if (drag.kind === 'overview') this._overviewTo(p.x);
        else if (drag.kind === 'scrub') { if (this.onSeek) this.onSeek(this.timeAtX(p.x)); this.draw(); }
        else if (drag.kind === 'edge') {
          const t = this.timeAtX(p.x);
          if (drag.edge === 'start') this.setSelection(Math.min(t, this.sel.end - 0.01), this.sel.end);
          else this.setSelection(this.sel.start, Math.max(t, this.sel.start + 0.01));
        } else if (drag.kind === 'new') {
          if (Math.abs(p.x - drag.x) > 4) drag.moved = true;
          if (drag.moved) this.setSelection(drag.anchor, this.timeAtX(p.x));
        } else if (drag.kind === 'pan' && this.zoomable) {
          if (Math.abs(p.x - drag.x) > 4) drag.moved = true;
          if (drag.moved) { this.start = drag.start - ((p.x - drag.x) / (this.size().w || 1)) * this.span; this._clamp(); this.draw(); }
        }
      });

      const end = (ev) => {
        const p = pos(ev);
        const tap = drag && !drag.moved && this._pointers.size === 1;
        this._pointers.delete(ev.pointerId);
        if (this._pointers.size < 2 && drag && drag.kind === 'pinch') drag = null;
        if (tap) {
          const region = this.regionAt(p.x, p.y);
          if (region && this.onRegionClick) this.onRegionClick(region);
          else if (this.onSeek) this.onSeek(this.timeAtX(p.x));
          this.draw();
        }
        if (this._pointers.size === 0) drag = null;
      };
      c.addEventListener('pointerup', end);
      c.addEventListener('pointercancel', (ev) => { this._pointers.delete(ev.pointerId); drag = null; });
      c.addEventListener('wheel', (ev) => {
        if (!this.zoomable) return;
        if (this.wheelZoom === 'ctrl' && !ev.ctrlKey) return;
        ev.preventDefault();
        this.zoomBy(ev.deltaY < 0 ? 1.25 : 0.8, this.timeAtX(pos(ev).x));
      }, { passive: false });
      if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('resize', () => this.schedule());
    }

    _overviewTo(x) {
      const main = this.overviewOf;
      main.panTo(this.timeAtX(x) - main.span / 2);
      this.draw();
    }
  }

  const api = { parseDat, loadWaveform, niceStep, fmtClock, parseTime, WaveView, waveCache };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.AudioWave = api;
})(typeof window !== 'undefined' ? window : globalThis);
