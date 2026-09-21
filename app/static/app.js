// Small helpers shared by the newer pages. (The older pages carry their own copies.)
// el() builds DOM with append(), never innerHTML: filenames, tag names and notes are untrusted text.
function el(tag, props = {}, children = []) {
  const e = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? '' : v);
  });
  children.forEach(c => e.append(c));
  return e;
}

async function apiJson(method, url, body) {
  const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) { const err = new Error((data && data.error) || r.statusText); err.data = data; err.status = r.status; throw err; }
  return data;
}

function formatBytes(n) {
  if (n === null || n === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + units[i];
}

function formatDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return '';
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = Math.round(seconds % 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}
