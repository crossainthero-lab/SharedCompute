// Rendering helpers. Every interpolated value is escaped unless wrapped by html``/raw().

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);

class Safe {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
export const raw = (s) => new Safe(String(s));

function part(v) {
  if (v instanceof Safe) return v.s;
  if (Array.isArray(v)) return v.map(part).join('');
  if (v === null || v === undefined || v === false) return '';
  return esc(v);
}

export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((v, i) => { out += part(v) + strings[i + 1]; });
  return new Safe(out);
}

// Replace an element's content only when it changed, so polling never resets scroll or focus.
export function patch(el, content) {
  if (!el) return;
  const next = String(content);
  if (el.__html !== next) {
    el.innerHTML = next;
    el.__html = next;
  }
}

// --- Formatting ---------------------------------------------------------------------------

export const usd = (cents) => {
  if (cents === null || cents === undefined) return '—';
  const sign = cents < 0 ? '−' : '';
  return `${sign}$${(Math.abs(cents) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};
export const rate = (cents) => `${usd(cents)}/hr`;
export const gb = (n) => `${Number.isInteger(n) ? n : n.toFixed(1)} GB`;
export const pct = (bps) => `${(bps / 100).toFixed(bps % 100 ? 1 : 0)}%`;

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} min${seconds % 60 >= 1 ? ` ${Math.round(seconds % 60)} s` : ''}`;
  const h = seconds / 3600;
  return `${h.toFixed(h < 10 ? 1 : 0)} h`;
}

// GPU time totals: hours once they are meaningful, otherwise the exact short duration.
export const hours = (seconds) => {
  if (!seconds) return '0 h';
  if (seconds < 360) return duration(seconds);
  return `${(seconds / 3600).toLocaleString('en-US', { maximumFractionDigits: seconds < 36000 ? 2 : 1 })} h`;
};

export function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return 'just now';
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—');
export const date = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—');

export const plural = (n, word, many = `${word}s`) => `${n.toLocaleString()} ${n === 1 ? word : many}`;

// --- Shared fragments ---------------------------------------------------------------------

const STATUS_LABEL = {
  available: 'Available', busy: 'Busy', paused: 'Paused', offline: 'Offline',
  pending: 'Pending', assigned: 'Assigned', running: 'Running', completed: 'Completed',
  failed: 'Failed', cancelled: 'Cancelled', online: 'Online',
};
export const status = (s, label) => html`<span class="status ${s}">${label || STATUS_LABEL[s] || s}</span>`;

export const sourceTag = (execution) => (execution === 'agent'
  ? html`<span class="tag tag-agent" title="Hardware reported by a connected provider agent">AGENT</span>`
  : html`<span class="tag tag-sim" title="Simulated listing: self-reported, executed by the server's simulator">SIM</span>`);

export const DETECTION = {
  nvml: 'NVML', 'nvidia-smi': 'nvidia-smi', system_profiler: 'macOS report', lspci: 'lspci', self_reported: 'Self-reported',
};

export function reliability(r) {
  if (!r || r.score === null || r.score === undefined) {
    const n = r ? r.completed + r.issues : 0;
    return html`<span class="faint" title="A score appears after 3 finished attempts">${n ? `${n} run${n === 1 ? '' : 's'}` : 'No history'}</span>`;
  }
  return html`<span title="${r.completed} completed, ${r.issues} interrupted or failed">${Math.round(r.score * 100)}%</span>`;
}

export function empty(title, body, action = '') {
  return html`<div class="table-empty"><b>${title}</b>${body}${action ? html`<div class="btn-row empty-actions">${action}</div>` : ''}</div>`;
}

// --- Toasts & dialog ----------------------------------------------------------------------

export function toast(message, kind = 'info') {
  const host = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast${kind === 'error' ? ' error' : ''}`;
  el.textContent = message;
  host.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3500);
}

export function openDialog(content, onMount) {
  const dialog = document.getElementById('dialog');
  dialog.innerHTML = String(content);
  dialog.showModal();
  if (onMount) onMount(dialog);
  return dialog;
}

export function closeDialog() {
  const dialog = document.getElementById('dialog');
  if (dialog.open) dialog.close();
}

// Show a field error returned by the API next to the input, falling back to a toast.
export function showFormError(form, err) {
  form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
  const box = form.querySelector('.form-error');
  const input = err.field && form.querySelector(`[name="${CSS.escape(err.field)}"]`);
  if (input) {
    input.setAttribute('aria-invalid', 'true');
    input.focus();
  }
  if (box) {
    box.textContent = err.message;
    box.classList.remove('hidden');
  } else {
    toast(err.message, 'error');
  }
}

export function clearFormError(form) {
  form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
  const box = form.querySelector('.form-error');
  if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

export const formData = (form) => Object.fromEntries(new FormData(form).entries());
