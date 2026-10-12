// Provider dashboard: connect machines, set prices, control availability, track earnings.
import { api } from '../api.js';
import * as ui from '../ui.js';
import { html } from '../ui.js';

let mode = 'agent';
let showAdd = false;
let nodes = null;
let served = null;
let issuedToken = null; // { nodeId, token } shown once after creation or rotation

function regionSelect(meta, name, selected = 'au-syd') {
  return html`<select class="select" name="${name}" id="p-${name}">${meta.regions.map((r) => html`<option value="${r.code}" ${r.code === selected ? 'selected' : ''}>${r.label}</option>`)}</select>`;
}

function addHtml(ctx) {
  const meta = ctx.store.meta;
  const share = ui.pct(10000 - meta.platform_fee_bps);
  return html`
    <div class="connect">
      <div class="connect-head">
        <h2>Add hardware</h2>
        <div class="segmented" role="group" aria-label="Hardware type">
          <button type="button" data-mode="agent" aria-pressed="${mode === 'agent'}">Connect a machine</button>
          <button type="button" data-mode="simulated" aria-pressed="${mode === 'simulated'}">Simulated listing</button>
        </div>
      </div>
      <form class="form" id="node-form" novalidate>
        <input type="hidden" name="kind" value="${mode}">
        <div class="field"><label for="p-name">Node name</label><input class="input" id="p-name" name="name" required minlength="2" maxlength="60" placeholder="${mode === 'agent' ? 'Studio workstation' : 'Test listing'}"></div>
        <div class="field"><label for="p-region">Region</label>${regionSelect(meta, 'region')}</div>
        ${mode === 'simulated' ? html`
          <div class="field"><label for="p-gpu">GPU model</label><input class="input" id="p-gpu" name="gpu_model" required maxlength="60" placeholder="GeForce RTX 3060"></div>
          <div class="field"><label for="p-vram">VRAM (GB)</label><input class="input num" id="p-vram" name="vram_gb" type="number" min="1" max="192" value="12" required></div>` : ''}
        <div class="field"><label for="p-price">${mode === 'agent' ? 'Default price per GPU-hour' : 'Price per GPU-hour'}</label>
          <div class="input-affix"><span>$</span><input class="input num" id="p-price" name="hourly_usd" type="number" min="0.01" max="100" step="0.01" value="0.30" required></div></div>
        <div class="field full hint">${mode === 'agent'
          ? html`The agent detects GPUs on the machine and reports them with this price; you can change each GPU's price afterwards. Integrated GPUs start paused. You keep ${share} of each completed job's charge.`
          : html`Self-reported and never executed: jobs matched to this listing are timed and settled by the server's simulator. Useful for testing pricing and matching.`}</div>
        <div class="form-error hidden" role="alert"></div>
        <div class="field full"><div class="btn-row">
          <button class="btn btn-primary" type="submit">${mode === 'agent' ? 'Create node and token' : 'Create simulated listing'}</button>
          <button class="btn btn-ghost" type="button" data-act="close-add">Cancel</button></div></div>
      </form>
    </div>`;
}

function tokenHtml(server) {
  if (!issuedToken) return '';
  return html`
    <div class="connect">
      <div class="secret">
        <p><b>Node token for “${issuedToken.name}”.</b> Shown once; it is stored only as a hash. Treat it like a password: anyone with it can act as this node.</p>
        <div class="btn-row"><code class="inline-code mono" id="token-value">${issuedToken.token}</code>
          <button class="btn btn-sm" data-act="copy-token">Copy</button>
          <button class="btn btn-sm btn-ghost" data-act="dismiss-token">I've saved it</button></div>
        <pre class="code">${`# On the provider machine (Python 3.8+, NVIDIA driver for GPU work; no CUDA toolkit needed):
python3 -m venv ~/sc-agent && . ~/sc-agent/bin/activate
pip install ./agent                       # from a checkout of this repository
python -m sharedcompute_agent diagnose    # what will be reported; nothing is offered yet
python -m sharedcompute_agent diagnose --gpu 0 --selftest   # run the real kernel on GPU 0

umask 077; printf '%s' '${issuedToken.token}' > ~/.sharedcompute-node-token
python -m sharedcompute_agent run --server ${server} \\
  --token-file ~/.sharedcompute-node-token --gpu 0      # add --offer-cpu to also offer the CPU`}</pre>
        <p class="faint small-print">Remote machines must use an https:// server URL (add --ca-file for a private CA). Nothing is used unless you pass --gpu or --offer-cpu; a GPU that drives a display also needs --allow-display-gpu. See docs/HARDWARE_TESTING.md.</p>
      </div>
    </div>`;
}

function nodeStatus(n) {
  if (n.kind === 'simulated') return n.enabled ? ui.status('online', 'Simulated · always on') : ui.status('paused');
  if (!n.last_seen_at) return ui.status('offline', 'Waiting for agent');
  if (!n.online) return ui.status('offline', `Offline · seen ${ui.ago(n.last_seen_at)}`);
  return n.enabled ? ui.status('online', `Online · ${ui.ago(n.last_seen_at)}`) : ui.status('paused', 'Paused');
}

function gpuRow(n, g) {
  const m = g.metrics;
  if (g.device_type === 'cpu') {
    return html`
    <tr>
      <td class="cell-main"><div class="primary">${g.model} <span class="tag">CPU</span></div><div class="sub">${g.workloads.join(', ') || 'no workloads'}</div>
        ${g.offered ? '' : html`<div class="sub">${g.offer_note || 'Not offered on the machine'}</div>`}</td>
      <td data-label="VRAM" class="num faint">—</td>
      <td data-label="Status">${ui.status(g.status)}</td>
      <td data-label="Price" class="r">${priceForm(g)}</td>
      <td data-label="Listed" class="r"><input class="switch" type="checkbox" role="switch" data-gpu-toggle="${g.id}" ${g.enabled ? 'checked' : ''} ${g.offered ? '' : 'disabled'} aria-label="List ${g.model}"></td>
    </tr>`;
  }
  const specs = [ui.DETECTION[g.detection] || g.detection, g.cuda ? `CUDA${g.compute_capability ? ` ${g.compute_capability}` : ''}` : (n.kind === 'agent' ? 'no CUDA' : null),
    g.driver_version ? `driver ${g.driver_version}` : null, ui.cudaVersion(g.cuda_driver_version) ? `CUDA ${ui.cudaVersion(g.cuda_driver_version)}` : null,
    g.vram_free_mb !== null ? `${g.vram_free_mb} MB free` : null, g.display_active ? 'drives a display' : null,
    g.integrated ? 'integrated' : null].filter(Boolean).join(' · ');
  return html`
    <tr>
      <td class="cell-main"><div class="primary">${g.model}</div><div class="sub">${g.vendor} · ${specs}</div>
        <div class="sub">${g.offered ? (g.workloads.length ? `Runs: ${g.workloads.join(', ')}` : 'Offered, but no compatible workload') : (g.offer_note || 'Not offered on the machine')}</div></td>
      <td data-label="VRAM" class="num">${ui.gb(g.vram_gb)}</td>
      <td data-label="Status">${ui.status(g.status)}${m && m.util_percent !== null ? html`<div class="sub">${Math.round(m.util_percent)}% util · ${m.temperature_c !== null ? `${Math.round(m.temperature_c)}°C` : ''}</div>` : ''}</td>
      <td data-label="Price" class="r">${priceForm(g)}</td>
      <td data-label="Listed" class="r"><input class="switch" type="checkbox" role="switch" data-gpu-toggle="${g.id}" ${g.enabled ? 'checked' : ''} ${g.offered ? '' : ui.raw('disabled title="Offer it on the machine first (agent flags)"')} aria-label="List ${g.model}"></td>
    </tr>`;
}

function priceForm(g) {
  return html`<form class="price-edit" data-gpu-price="${g.id}">
    <div class="input-affix"><span>$</span><input class="input num" name="hourly_usd" type="number" min="0.01" max="100" step="0.01" value="${(g.hourly_cents / 100).toFixed(2)}" aria-label="Price per hour for ${g.model}"></div>
    <button class="btn btn-sm" type="submit">Save</button>
  </form>`;
}

function nodeHtml(n) {
  const meta = n.kind === 'agent'
    ? [n.platform, n.agent_version ? `agent ${n.agent_version}` : null, n.capabilities.length ? `workloads: ${n.capabilities.join(', ')}` : null].filter(Boolean).join(' · ')
    : 'Self-reported specs; executed by the simulator';
  return html`
    <div class="node">
      <div class="node-head">
        <div>
          <div class="node-title"><b>${n.name}</b> ${ui.sourceTag(n.kind)} <span class="muted">${n.region_label}</span> ${nodeStatus(n)}</div>
          <div class="node-meta">${meta || 'No agent has connected yet'} · earned ${ui.usd(n.earned_cents)} from ${ui.plural(n.completed_jobs, 'job')} · track record ${ui.reliability(n.reliability)}</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-sm" data-act="toggle-node" data-id="${n.id}" data-enabled="${n.enabled}">${n.enabled ? 'Pause node' : 'Resume node'}</button>
          ${n.kind === 'agent' ? html`<button class="btn btn-sm" data-act="rotate" data-id="${n.id}" data-name="${n.name}">New token</button>` : ''}
          <button class="btn btn-sm btn-ghost btn-danger" data-act="remove" data-id="${n.id}" data-name="${n.name}">Remove</button>
        </div>
      </div>
      ${n.gpus.length ? html`<div class="table-wrap"><table class="table stackable">
        <thead><tr><th>Device</th><th>VRAM</th><th>Status</th><th class="r">Price / hour</th><th class="r">Listed</th></tr></thead>
        <tbody>${n.gpus.map((g) => gpuRow(n, g))}</tbody></table></div>`
        : html`<div class="table-empty">No devices reported yet. Start the agent with this node's token.</div>`}
    </div>`;
}

function statsHtml() {
  const earned = nodes.reduce((a, n) => a + n.earned_cents, 0);
  const jobs = nodes.reduce((a, n) => a + n.completed_jobs, 0);
  const seconds = nodes.reduce((a, n) => a + n.billed_seconds, 0);
  const gpus = nodes.flatMap((n) => n.gpus);
  const live = gpus.filter((g) => g.status === 'available' || g.status === 'busy').length;
  return html`<div class="stats">
    <div class="stat"><div class="label">Earned</div><div class="value">${ui.usd(earned)}</div><div class="sub">Simulated credits · no payouts yet</div></div>
    <div class="stat"><div class="label">Completed jobs</div><div class="value">${jobs}</div><div class="sub">on your hardware</div></div>
    <div class="stat"><div class="label">Compute time billed</div><div class="value">${ui.hours(seconds)}</div><div class="sub">simulated + measured</div></div>
    <div class="stat"><div class="label">Devices accepting jobs</div><div class="value">${live}<span class="faint"> / ${gpus.length}</span></div><div class="sub">${ui.plural(nodes.length, 'node')}</div></div>
  </div>`;
}

function servedHtml() {
  if (!served.length) return ui.empty('No jobs on your hardware yet', html`<div>Matched jobs appear here with what they earned you.</div>`);
  return html`<div class="table-wrap"><table class="table stackable">
    <thead><tr><th>Job</th><th>GPU</th><th>Status</th><th class="r">Billed time</th><th class="r">Charge</th><th class="r">You earn</th></tr></thead>
    <tbody>${served.map((j) => html`<tr>
      <td class="cell-main"><div class="primary">#${j.id} · ${j.workload_label} ${j.execution === 'agent' ? (j.verified ? html`<span class="tag tag-ok">VERIFIED</span>` : '') : html`<span class="tag tag-sim">SIM</span>`}</div><div class="sub">${ui.date(j.finished_at || j.started_at)}</div></td>
      <td data-label="GPU">${j.gpu_model}<div class="sub">${j.node_name}</div></td>
      <td data-label="Status">${ui.status(j.status)}</td>
      <td data-label="Billed time" class="r num">${ui.duration(j.billed_seconds)}</td>
      <td data-label="Charge" class="r num">${ui.usd(j.cost_cents)}</td>
      <td data-label="You earn" class="r num"><b>${ui.usd(j.provider_cents)}</b></td></tr>`)}</tbody></table></div>`;
}

function render(ctx) {
  ui.patch(ctx.root.querySelector('#pv-add'), showAdd ? addHtml(ctx) : '');
  ui.patch(ctx.root.querySelector('#pv-token'), tokenHtml(location.origin));
  if (!nodes) return;
  ui.patch(ctx.root.querySelector('#pv-stats'), statsHtml());
  const list = ctx.root.querySelector('#pv-nodes');
  // Don't replace the node list while the provider is typing a price.
  if (!list.contains(document.activeElement) || document.activeElement.type === 'checkbox') {
    ui.patch(list, nodes.length ? nodes.map(nodeHtml) : ui.empty('No hardware yet',
      html`<div>Connect a machine running the agent, or create a simulated listing to try pricing and matching.</div>`));
  }
  if (served) ui.patch(ctx.root.querySelector('#pv-served'), servedHtml());
}

async function refresh(ctx) {
  if (!ctx.store.user) return;
  try {
    [nodes, served] = await Promise.all([api.get('/provider/nodes').then((r) => r.nodes), api.get('/provider/jobs').then((r) => r.jobs)]);
    if (!nodes.length && !issuedToken) showAdd = true;
    render(ctx);
  } catch (err) {
    if (err.status !== 401) ui.toast(err.message, 'error');
  }
}

async function act(ctx, fn, success) {
  try {
    await fn();
    if (success) ui.toast(success);
  } catch (err) {
    ui.toast(err.message, 'error');
  }
  await refresh(ctx);
}

function mount(ctx) {
  if (!ctx.requireAuth(ctx, 'Earn from idle GPUs', 'Create an account to connect hardware, set your price and track earnings.')) return null;
  nodes = null;
  served = null;
  const share = ui.pct(10000 - ctx.store.meta.platform_fee_bps);
  ctx.root.innerHTML = String(html`
    <div class="page-head">
      <div><h1>Provide compute</h1>
        <p>Connect machines, set an hourly price per GPU and choose when they accept work. You receive ${share} of every completed job's charge; nothing is paid for idle time or failed jobs.</p></div>
      <div class="btn-row"><button class="btn btn-primary" data-act="open-add">Add hardware</button></div>
    </div>
    <div id="pv-token"></div>
    <div id="pv-add"></div>
    <div id="pv-stats"></div>
    <div class="section"><div class="section-head"><div><h2>Your nodes</h2><p>Pausing a node or GPU returns its running jobs to the queue.</p></div></div>
      <div id="pv-nodes"><div class="loading">Loading…</div></div></div>
    <div class="section"><div class="section-head"><div><h2>Jobs on your hardware</h2><p>Customer identities and job names are not shared with providers.</p></div></div>
      <div class="table-card" id="pv-served"><div class="loading">Loading…</div></div></div>`);

  const onClick = (event) => {
    const modeBtn = event.target.closest('[data-mode]');
    if (modeBtn) { mode = modeBtn.dataset.mode; render(ctx); return; }
    const b = event.target.closest('[data-act]');
    if (!b) return;
    const id = Number(b.dataset.id);
    switch (b.dataset.act) {
      case 'open-add': showAdd = true; render(ctx); ctx.root.querySelector('#p-name')?.focus(); break;
      case 'close-add': showAdd = false; render(ctx); break;
      case 'dismiss-token': issuedToken = null; render(ctx); break;
      case 'copy-token':
        navigator.clipboard?.writeText(issuedToken.token).then(() => ui.toast('Token copied.'), () => ui.toast('Copy failed; select the token manually.', 'error'));
        break;
      case 'toggle-node': {
        const enabled = b.dataset.enabled !== 'true';
        act(ctx, () => api.patch(`/provider/nodes/${id}`, { enabled }), enabled ? 'Node resumed.' : 'Node paused. Its active jobs were returned to the queue.');
        break;
      }
      case 'rotate':
        if (!window.confirm(`Issue a new token for “${b.dataset.name}”? The current token stops working immediately.`)) return;
        act(ctx, async () => { issuedToken = { name: b.dataset.name, token: (await api.post(`/provider/nodes/${id}/token`)).agent_token }; }, 'New token issued.');
        break;
      case 'remove':
        if (!window.confirm(`Remove “${b.dataset.name}” from the marketplace? Its history is kept.`)) return;
        act(ctx, () => api.del(`/provider/nodes/${id}`), 'Node removed.');
        break;
      default:
    }
  };
  const onChange = (event) => {
    const t = event.target.closest('[data-gpu-toggle]');
    if (!t) return;
    act(ctx, () => api.patch(`/provider/gpus/${t.dataset.gpuToggle}`, { enabled: t.checked }), t.checked ? 'GPU listed.' : 'GPU unlisted.');
  };
  const onSubmit = async (event) => {
    const priceForm = event.target.closest('[data-gpu-price]');
    if (priceForm) {
      event.preventDefault();
      const value = priceForm.elements.hourly_usd.value;
      priceForm.querySelector('input').blur();
      act(ctx, () => api.patch(`/provider/gpus/${priceForm.dataset.gpuPrice}`, { hourly_usd: value }), `Price set to $${Number(value).toFixed(2)}/GPU-hour. Applies to new matches.`);
      return;
    }
    if (event.target.id !== 'node-form') return;
    event.preventDefault();
    const form = event.target;
    const data = ui.formData(form);
    if (data.vram_gb !== undefined) data.vram_gb = Number(data.vram_gb);
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const res = await api.post('/provider/nodes', data);
      showAdd = false;
      if (res.agent_token) issuedToken = { name: data.name, token: res.agent_token };
      ui.toast(res.agent_token ? 'Node created. Start the agent with the token below.' : 'Simulated listing created.');
      await refresh(ctx);
    } catch (err) {
      ui.showFormError(form, err);
    } finally {
      button.disabled = false;
    }
  };
  ctx.root.addEventListener('click', onClick);
  ctx.root.addEventListener('change', onChange);
  ctx.root.addEventListener('submit', onSubmit);
  render(ctx);
  refresh(ctx);
  return () => {
    ctx.root.removeEventListener('click', onClick);
    ctx.root.removeEventListener('change', onChange);
    ctx.root.removeEventListener('submit', onSubmit);
  };
}

export default { title: 'Provide', mount, poll: 4000, refresh: (ctx) => { refresh(ctx); } };
