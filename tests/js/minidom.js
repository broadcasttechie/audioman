// A very small DOM, just enough to run our pages' scripts under Node without a browser or any package.
// It models elements, text, events, ids/classes and `contains`; it does NOT do layout, CSS or rendering.
class TextNode { constructor(t) { this.nodeType = 3; this.textContent = String(t); this.parentNode = null; } }

class Element {
  constructor(tag, doc) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = []; this.parentNode = null;
    this.attributes = {}; this.style = {}; this.dataset = {}; this._listeners = {}; this._text = null;
    this.className = ''; this.hidden = false; this.disabled = false; this.checked = false; this.value = ''; this.type = '';
    this.clientWidth = 600; this.clientHeight = 200; this.width = 0; this.height = 0; this.scrollTop = 0;
  }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get firstChild() { return this.children[0] || null; }
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = v; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.replaceChildren(String(v)); }
  set innerHTML(v) { this.children.forEach(c => (c.parentNode = null)); this.children = []; if (v) this.append(new TextNode(String(v))); }
  get innerHTML() { return this.children.map(c => (c.nodeType === 3 ? c.textContent : c.outerHTML || '')).join(''); }
  get classList() {
    const self = this, list = () => self.className.split(/\s+/).filter(Boolean);
    return { add: (...c) => { self.className = [...new Set([...list(), ...c])].join(' '); }, remove: (...c) => { self.className = list().filter(x => !c.includes(x)).join(' '); },
             contains: (c) => list().includes(c), toggle: (c, on) => { const has = list().includes(c); const want = on === undefined ? !has : on; if (want && !has) self.className = [...list(), c].join(' '); if (!want && has) self.className = list().filter(x => x !== c).join(' '); return want; } };
  }
  append(...nodes) { for (const n of nodes) { const node = (typeof n === 'string' || typeof n === 'number') ? new TextNode(n) : n; if (node === null || node === undefined) continue; if (node.parentNode) node.parentNode.removeChild(node); node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children.forEach(c => (c.parentNode = null)); this.children = []; this.append(...nodes); }
  removeChild(n) { this.children = this.children.filter(c => c !== n); n.parentNode = null; return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'class') this.className = String(v); if (k === 'value') this.value = String(v); if (k === 'checked') this.checked = true; if (k === 'type') this.type = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this._listeners[t] = (this._listeners[t] || []).filter(f => f !== fn); }
  dispatch(t, ev = {}) { const e = Object.assign({ type: t, target: this, preventDefault() {}, stopPropagation() {} }, ev); (this._listeners[t] || []).slice().forEach(f => f.call(this, e)); return e; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight, right: this.clientWidth, bottom: this.clientHeight }; }
  querySelectorAll(sel) { const out = []; const test = matcher(sel); const walk = (n) => { for (const c of n.children) { if (c.nodeType === 1) { if (test(c)) out.push(c); walk(c); } } }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getContext() { const calls = []; return new Proxy({ calls }, { get: (t, k) => (k in t ? t[k] : k === 'measureText' ? () => ({ width: 24 }) : (...a) => { calls.push([k, ...a]); }), set: () => true }); }
  setPointerCapture() {} focus() {} scrollIntoView() {} click() { this.dispatch('click'); }
}

function matcher(sel) {
  if (sel.startsWith('#')) return (e) => e.id === sel.slice(1);
  if (sel.startsWith('.')) return (e) => e.classList.contains(sel.slice(1));
  return (e) => e.tagName === sel.toUpperCase();
}

function createDom() {
  const doc = { createElement: (t) => new Element(t, doc), createTextNode: (t) => new TextNode(t) };
  doc.body = new Element('body', doc);
  doc.head = new Element('head', doc);
  doc.getElementById = (id) => { const walk = (n) => { for (const c of n.children) { if (c.nodeType === 1) { if (c.id === id) return c; const f = walk(c); if (f) return f; } } return null; }; return walk(doc.body); };
  doc.querySelectorAll = (s) => doc.body.querySelectorAll(s);
  doc.querySelector = (s) => doc.body.querySelector(s);
  doc.addEventListener = () => {};
  doc.hidden = false;
  return { document: doc, Element, TextNode };
}

module.exports = { createDom, Element, TextNode };
