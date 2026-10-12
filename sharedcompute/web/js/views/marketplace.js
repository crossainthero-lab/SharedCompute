// Marketplace: every listed GPU with live availability. Public; no account needed to browse.
import { api } from '../api.js';
import * as ui from '../ui.js';
import { html } from '../ui.js';

const filters = { availability: 'all', vram: 0, region: 'any', source: 'all', sort: 'price' };
let data = null;

function visible(gpus) {
  const out = gpus.filter((g) => (filters.availability === 'all' || g.status === 'available')
    && g.vram_gb >= filters.vram
    && (filters.region === 'any' || g.region === filters.region)
    && (filters.source === 'all' || g.execution === filters.source));
  const byPrice = (a, b) => a.hourly_cents - b.hourly_cents || b.vram_gb - a.vram_gb;
  out.sort(filters.sort === 'vram' ? (a, b) => b.vram_gb - a.vram_gb || byPrice(a, b) : byPrice);
  return out;
}

function statsHtml(s) {
  return html`
    <div class="stats cols-5">
      <div class="stat"><div class="label">Listed GPUs</div><div class="value">${s.listed}</div>
        <div class="sub">${s.simulated} simulated</div></div>
      <div class="stat"><div class="label">Available now</div><div class="value">${s.available}</div>
        <div class="sub">${s.busy} busy</div></div>
      <div class="stat"><div class="label">Agent-connected</div><div class="value">${s.agent_connected}</div>
        <div class="sub">Real hardware, online</div></div>
      <div class="stat"><div class="label">Regions</div><div class="value">${s.regions}</div>
        <div class="sub">with listings</div></div>
      <div class="stat"><div class="label">From</div><div class="value">${s.lowest_available_cents === null ? '—' : ui.usd(s.lowest_available_cents)}</div>
        <div class="sub">per GPU-hour, available</div></div>
    </div>`;
}

function useLink(g) {
  const workload = g.workloads[0] || 'sim.image';
  const vram = Math.max(1, Math.floor(g.vram_gb));
  return `#/jobs?workload=${encodeURIComponent(workload)}&vram=${vram}&region=${encodeURIComponent(g.region)}`;
}

function gpuRow(g) {
  const noCuda = g.execution === 'agent' && !g.cuda;
  const metrics = g.metrics && g.metrics.util_percent !== null
    ? html` · ${Math.round(g.metrics.util_percent)}% util${g.metrics.temperature_c !== null ? html` · ${Math.round(g.metrics.temperature_c)}°C` : ''}` : '';
  return html`
    <tr>
      <td class="cell-main">
        <div class="primary">${g.model} ${ui.sourceTag(g.execution)} ${noCuda ? html`<span class="tag" title="Not an NVIDIA CUDA device">NO CUDA</span>` : ''}</div>
        <div class="sub">${g.vendor} · ${ui.DETECTION[g.detection] || g.detection} · ${g.node.name}${metrics}</div>
      </td>
      <td data-label="VRAM" class="num">${ui.gb(g.vram_gb)}</td>
      <td data-label="Region">${g.region_label}</td>
      <td data-label="Price" class="r num nowrap"><b>${ui.usd(g.hourly_cents)}</b><span class="faint">/hr</span></td>
      <td data-label="Status">${ui.status(g.status)}</td>
      <td data-label="Track record" class="num">${ui.reliability(g.reliability)}</td>
      <td class="r"><a class="btn btn-sm" href="${useLink(g)}" title="Start a job with this GPU's VRAM and region">Use</a></td>
    </tr>`;
}

function tableHtml() {
  const rows = visible(data.gpus);
  if (!rows.length) {
    return ui.empty('No GPUs match these filters',
      data.gpus.length ? html`<div>Try a lower VRAM requirement or another region.</div>` : html`<div>No hardware is listed yet.</div>`,
      html`<a class="btn btn-sm" href="#/provide">List your GPU</a>`);
  }
  return html`
    <div class="table-wrap"><table class="table stackable">
      <thead><tr><th>GPU</th><th>VRAM</th><th>Region</th><th class="r">Price</th><th>Status</th><th title="Share of finished attempts that completed">Track record</th><th></th></tr></thead>
      <tbody>${rows.map(gpuRow)}</tbody>
    </table></div>
    <div class="table-foot faint">${ui.plural(rows.length, 'GPU')} shown</div>`;
}

function regionOptions(meta) {
  const present = new Set(data ? data.gpus.map((g) => g.region) : []);
  return meta.regions.filter((r) => present.has(r.code))
    .map((r) => html`<option value="${r.code}" ${filters.region === r.code ? 'selected' : ''}>${r.label}</option>`);
}

function render(ctx) {
  ui.patch(ctx.root.querySelector('#mk-stats'), statsHtml(data.summary));
  ui.patch(ctx.root.querySelector('#mk-region'), html`<option value="any">All regions</option>${regionOptions(ctx.store.meta)}`);
  ui.patch(ctx.root.querySelector('#mk-table'), tableHtml());
}

async function refresh(ctx) {
  try {
    data = await api.get('/marketplace');
    render(ctx);
  } catch (err) {
    ui.patch(ctx.root.querySelector('#mk-table'), ui.empty('Could not load the marketplace', html`<div>${err.message}</div>`));
  }
}

function mount(ctx) {
  const vramOptions = [0, 8, 12, 16, 24, 48, 80];
  ctx.root.innerHTML = String(html`
    <div class="page-head">
      <div>
        <h1>GPU marketplace</h1>
        <p>Providers set their own hourly price. Submitted jobs are matched automatically to the cheapest compatible GPU that fits the job's budget.</p>
      </div>
      <div class="btn-row"><a class="btn" href="#/provide">List hardware</a><a class="btn btn-primary" href="#/jobs">Submit a job</a></div>
    </div>
    <div id="mk-stats"><div class="stats cols-5"><div class="stat"><div class="label">Loading…</div><div class="value">—</div></div></div></div>
    <div class="section">
      <div class="toolbar">
        <div class="segmented" role="group" aria-label="Availability">
          <button data-availability="all" aria-pressed="${filters.availability === 'all'}">All</button>
          <button data-availability="available" aria-pressed="${filters.availability === 'available'}">Available now</button>
        </div>
        <select class="select" id="mk-vram" aria-label="Minimum VRAM">
          ${vramOptions.map((v) => html`<option value="${v}" ${filters.vram === v ? 'selected' : ''}>${v ? `${v} GB+ VRAM` : 'Any VRAM'}</option>`)}
        </select>
        <select class="select" id="mk-region" aria-label="Region"><option value="any">All regions</option></select>
        <select class="select" id="mk-source" aria-label="Source">
          <option value="all" ${filters.source === 'all' ? 'selected' : ''}>All sources</option>
          <option value="agent" ${filters.source === 'agent' ? 'selected' : ''}>Agent-connected</option>
          <option value="simulated" ${filters.source === 'simulated' ? 'selected' : ''}>Simulated</option>
        </select>
        <span class="spacer"></span>
        <select class="select" id="mk-sort" aria-label="Sort">
          <option value="price" ${filters.sort === 'price' ? 'selected' : ''}>Lowest price</option>
          <option value="vram" ${filters.sort === 'vram' ? 'selected' : ''}>Most VRAM</option>
        </select>
      </div>
      <div class="table-card" id="mk-table"><div class="loading">Loading listings…</div></div>
    </div>`);

  const onClick = (event) => {
    const b = event.target.closest('[data-availability]');
    if (!b) return;
    filters.availability = b.dataset.availability;
    ctx.root.querySelectorAll('[data-availability]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    if (data) render(ctx);
  };
  const onChange = (event) => {
    const id = event.target.id;
    if (id === 'mk-vram') filters.vram = Number(event.target.value);
    else if (id === 'mk-region') filters.region = event.target.value;
    else if (id === 'mk-source') filters.source = event.target.value;
    else if (id === 'mk-sort') filters.sort = event.target.value;
    else return;
    if (data) render(ctx);
  };
  ctx.root.addEventListener('click', onClick);
  ctx.root.addEventListener('change', onChange);
  refresh(ctx);
  return () => {
    ctx.root.removeEventListener('click', onClick);
    ctx.root.removeEventListener('change', onChange);
  };
}

export default { title: 'Marketplace', mount, refresh, poll: 5000 };
