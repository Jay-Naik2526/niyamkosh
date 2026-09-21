/* NiyamKosh content script.
 *
 * Reads the page an officer is already looking at — a GeM listing, an eProcure
 * tender — finds the IS numbers in its text, and asks the local backend what is
 * wrong with them. Nothing is sent anywhere except http://localhost:8000, which
 * is the whole point: tender text never leaves the machine.
 *
 * The page is not ours, so this script is deliberately conservative. It reads
 * text nodes, never rewrites values, never touches form fields, and namespaces
 * every class it injects.
 */

const API = 'http://localhost:8000';
const IS_RE = /\bIS[:\s]*\d{2,6}(?:\s*\([^)]{0,40}\))?/gi;

const SEVERITY = { dispute_risk: 'high', statutory_omission: 'high',
                   missing_connected: 'warn', not_in_register: 'warn' };

function pageText() {
  // Skip script/style and anything the user is typing into.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA|INPUT)$/.test(p.tagName)) return NodeFilter.FILTER_REJECT;
      if (p.closest('#manak-panel')) return NodeFilter.FILTER_REJECT;
      return n.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const out = [];
  let n;
  while ((n = walker.nextNode())) out.push(n.nodeValue);
  return out.join('\n');
}

function highlight(bySeverity) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA|INPUT)$/.test(p.tagName)) return NodeFilter.FILTER_REJECT;
      if (p.closest('#manak-panel') || p.classList.contains('manak-hl')) return NodeFilter.FILTER_REJECT;
      return IS_RE.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);

  targets.forEach(node => {
    const frag = document.createDocumentFragment();
    let last = 0;
    node.nodeValue.replace(IS_RE, (match, offset) => {
      frag.appendChild(document.createTextNode(node.nodeValue.slice(last, offset)));
      const key = match.replace(/[^0-9]/g, '');
      const sev = bySeverity[key];
      const el = document.createElement('span');
      el.className = 'manak-hl';
      el.dataset.sev = sev ? SEVERITY[sev.kind] || 'warn' : 'ok';
      el.title = sev ? `${sev.headline}\n\n${sev.detail}` : `${match} — no finding against this citation`;
      el.textContent = match;
      frag.appendChild(el);
      last = offset + match.length;
      return match;
    });
    frag.appendChild(document.createTextNode(node.nodeValue.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function panel(html) {
  document.getElementById('manak-panel')?.remove();
  const el = document.createElement('div');
  el.id = 'manak-panel';
  el.innerHTML = html;
  document.body.appendChild(el);
  el.querySelector('[data-close]')?.addEventListener('click', () => el.remove());
}

async function run() {
  const text = pageText();
  if (!IS_RE.test(text)) {
    panel(`<div class="mh"><b>NiyamKosh</b><button data-close>&times;</button></div>
      <div class="mv">No IS numbers found on this page.</div>
      <div class="mfoot">The check reads visible text only. A scanned PDF opened in the
      browser has no text layer — download it and use the console's Audit screen.</div>`);
    return;
  }

  panel(`<div class="mh"><b>NiyamKosh</b><button data-close>&times;</button></div>
    <div class="mv">Checking against the register…</div>`);

  let d;
  try {
    const r = await fetch(`${API}/audit-text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.slice(0, 300000), document: location.hostname }),
    });
    if (!r.ok) throw new Error(`backend returned ${r.status}`);
    d = await r.json();
  } catch (e) {
    panel(`<div class="mh"><b>NiyamKosh</b><button data-close>&times;</button></div>
      <div class="mv review">Backend unreachable.</div>
      <div class="mfoot">Start it with <code>uvicorn main:app</code> on port 8000.
      Nothing on this page was sent anywhere. (${esc(e.message)})</div>`);
    return;
  }

  const bySeverity = {};
  d.findings.forEach(f => {
    const k = String(f.is_number).replace(/[^0-9]/g, '');
    if (!bySeverity[k]) bySeverity[k] = f;
  });
  highlight(bySeverity);

  const shown = d.findings.filter(f => f.severity !== 'low').slice(0, 12);
  panel(`<div class="mh"><b>NiyamKosh</b><button data-close>&times;</button></div>
    <div class="mv ${esc(d.verdict)}">${esc(d.summary)}</div>
    ${shown.map(f => `<div class="mf">
      <span class="n">${esc(f.is_number)}</span>
      <div class="h">${esc(f.headline)}</div>
      <div class="d">${esc(f.detail)}</div>
    </div>`).join('') || '<div class="mf">Nothing blocking on this page.</div>'}
    <div class="mfoot">${d.cited_count} citations read from this page and checked locally.
    Nothing was uploaded.</div>`);
}

// Exposed so the check can be driven from a test page, where chrome.* is absent.
window.manakCheck = run;

if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg?.action === 'check') { run().then(() => reply({ ok: true })); return true; }
  });
}
