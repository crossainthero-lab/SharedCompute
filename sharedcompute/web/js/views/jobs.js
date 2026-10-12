// Customer dashboard: submit jobs with a live quote, watch the queue, inspect each job's history.
import { api } from '../api.js';
import * as ui from '../ui.js';
import { html } from '../ui.js';

const STATUSES = ['pending', 'assigned', 'running', 'completed', 'failed', 'cancelled'];
const VRAM = [1, 4, 8, 12, 16, 24, 32, 48, 80];
let filter = 'all';
let openJob = null;
let detailCache = { id: null, html: '' };
let list = null;
let quoteTimer = null;
let quoteSeq = 0;

function workloadOptions(meta, selected) {
  const group = (execution, label) => html`<optgroup label="${label}">${meta.workloads.filter((w) => w.execution === execution)
    .map((w) => html`<option value="${w.id}" ${w.id === selected ? 'selected' : ''}>${w.label}</option>`)}</optgroup>`;
  return html`${group('simulated', 'Simulated (nothing executes)')}${group('agent', 'Runs on a provider agent')}`;
}

function formHtml(ctx) {
  const p = ctx.params;
  const meta = ctx.store.meta;
  const workload = meta.workloads.some((w) => w.id === p.get('workload')) ? p.get('workload') : 'sim.image';
  const vram = Number(p.get('vram')) || 12;
  const region = p.get('region') || 'any';
  const vramChoices = VRAM.includes(vram) ? VRAM : [...VRAM, vram].sort((a, b) => a - b);
  return html`
    <form class="card card-pad form" id="job-form" novalidate>
      <div class="field full section-head"><h2>New job</h2></div>
      <div class="field full"><label for="j-name">Job name</label>
        <input class="input" id="j-name" name="name" required minlength="2" maxlength="80" value="${workload.startsWith('agent.') ? 'Pipeline check' : 'Image batch'}"></div>
      <div class="field full"><label for="j-workload">Workload</label>
        <select class="select" id="j-workload" name="workload">${workloadOptions(meta, workload)}</select>
        <span class="hint" id="j-workload-hint"></span></div>
      <div class="field"><label for="j-vram">Minimum VRAM</label>
        <select class="select" id="j-vram" name="min_vram_gb">${vramChoices.map((v) => html`<option value="${v}" ${v === vram ? 'selected' : ''}>${v} GB</option>`)}</select></div>
      <div class="field"><label for="j-minutes">Estimated GPU-minutes</label>
        <input class="input num" id="j-minutes" name="est_minutes" type="number" min="1" max="10080" step="1" value="${workload.startsWith('agent.') ? 2 : 45}" required></div>
      <div class="field"><label for="j-budget">Maximum budget</label>
        <div class="input-affix"><span>$</span><input class="input num" id="j-budget" name="max_budget_usd" type="number" min="0.01" max="100000" step="0.01" value="2.00" required></div></div>
      <div class="field"><label for="j-region">Region</label>
        <select class="select" id="j-region" name="region"><option value="any">Any region</option>
          ${meta.regions.map((r) => html`<option value="${r.code}" ${r.code === region ? 'selected' : ''}>${r.label}</option>`)}</select></div>
      <div class="quote" id="j-quote" aria-live="polite"><span class="faint">Calculating a quote…</span></div>
      <div class="form-error hidden" role="alert"></div>
      <div class="field full">
        <button class="btn btn-primary" type="submit" id="j-submit">Submit job</button>
        <span class="hint" id="j-balance"></span>
      </div>
    </form>`;
}

function quoteHtml(q, budgetCents, feeBps) {
  if (!q) return html`<span class="faint">Fill in the job to see a quote.</span>`;
  const titles = { match: 'Ready to run', queue: 'Will queue', over_budget: 'Over budget', no_capacity: 'No compatible GPU' };
  let lines = '';
  if (q.match) {
    const m = q.match;
    const provider = Math.round(m.quote_cents * (10000 - feeBps) / 10000);
    lines = html`<div class="q-lines num">
      <span>GPU</span><span>${m.model} · ${ui.gb(m.vram_gb)} · ${m.region_label} ${ui.sourceTag(m.execution)}</span>
      <span>Rate</span><span>${ui.rate(m.hourly_cents)}</span>
      <span>Quote</span><span><b>${ui.usd(m.quote_cents)}</b> of ${ui.usd(budgetCents)} budget</span>
      <span>Split</span><span>${ui.usd(provider)} provider · ${ui.usd(m.quote_cents - provider)} fee</span></div>`;
  } else if (q.cheapest_quote_cents !== null) {
    lines = html`<div class="q-lines num"><span>Cheapest compatible</span><span>${ui.usd(q.cheapest_quote_cents)}</span></div>`;
  }
  return html`<div class="q-title">${titles[q.outcome]}</div><div class="muted">${q.reason}</div>${lines}`;
}

async function updateQuote(ctx) {
  const form = ctx.root.querySelector('#job-form');
  if (!form) return;
  const data = ui.formData(form);
  const meta = ctx.store.meta;
  const w = meta.workloads.find((x) => x.id === data.workload);
  ctx.root.querySelector('#j-workload-hint').textContent = w ? w.description : '';
  const budget = Math.round(Number(data.max_budget_usd) * 100);
  const submit = ctx.root.querySelector('#j-submit');
  const available = ctx.store.balances?.available_cents ?? 0;
  submit.textContent = Number.isFinite(budget) && budget > 0 ? `Submit job · hold ${ui.usd(budget)}` : 'Submit job';
  ctx.root.querySelector('#j-balance').innerHTML = String(budget > available
    ? html`<span class="neg">Only ${ui.usd(available)} available. <a href="#/economics">Add demo credits</a></span>`
    : html`The budget is held while the job runs; you're charged the actual cost and the rest is released. ${ui.usd(available)} available.`);
  const seq = ++quoteSeq;
  const box = ctx.root.querySelector('#j-quote');
  try {
    const q = await api.post('/quotes', { workload: data.workload, min_vram_gb: Number(data.min_vram_gb),
      est_minutes: Number(data.est_minutes), max_budget_usd: data.max_budget_usd, region: data.region });
    if (seq !== quoteSeq) return;
    box.className = `quote ${q.outcome}`;
    box.innerHTML = String(quoteHtml(q, budget, meta.platform_fee_bps));
  } catch (err) {
    if (seq !== quoteSeq) return;
    box.className = 'quote';
    box.innerHTML = String(html`<span class="faint">${err.message}</span>`);
  }
}

function scheduleQuote(ctx) {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(() => updateQuote(ctx), 200);
}

function costCell(j) {
  if (j.cost_cents !== null) return html`<b>${ui.usd(j.cost_cents)}</b>`;
  if (j.status === 'failed' || j.status === 'cancelled') return html`<span class="faint">Not charged</span>`;
  if (j.quote_cents !== null) return html`<span title="Quoted; charged on completion">${ui.usd(j.quote_cents)} <span class="faint">quote</span></span>`;
  return html`<span class="faint" title="Held from your balance">${ui.usd(j.max_budget_cents)} held</span>`;
}

function jobRow(j) {
  const when = j.finished_at || j.started_at || j.assigned_at || j.created_at;
  return html`
    <tr class="job-row" data-job="${j.id}" aria-expanded="${openJob === j.id}">
      <td data-label="Status">${ui.status(j.status)}</td>
      <td class="cell-main">
        <div class="primary">${j.name}</div>
        <div class="sub">#${j.id} · ${j.workload_label} · ${j.min_vram_gb} GB+ · ${j.est_minutes} min est.</div>
      </td>
      <td data-label="GPU">${j.gpu ? html`${j.gpu.model} ${ui.sourceTag(j.gpu.node_kind)}<div class="sub">${j.gpu.region_label}</div>` : html`<span class="faint">${['failed', 'cancelled'].includes(j.status) ? '—' : 'Waiting for a match'}</span>`}</td>
      <td data-label="Cost" class="r num nowrap">${costCell(j)}</td>
      <td data-label="Updated" class="r faint nowrap">${ui.ago(when)}</td>
    </tr>
    ${openJob === j.id ? html`<tr class="job-detail"><td colspan="5"><div id="job-detail-${j.id}">${detailCache.id === j.id ? ui.raw(detailCache.html) : html`<div class="loading">Loading…</div>`}</div></td></tr>` : ''}`;
}

function resultHtml(j) {
  if (j.status === 'failed') {
    return html`<div class="result"><b class="neg">Failed.</b> ${j.failure_reason || ''}${j.result?.verification ? html`<div class="faint">${j.result.verification}</div>` : ''}<div class="faint">Nothing was charged; the hold was released.</div></div>`;
  }
  if (!j.result) return '';
  if (j.result.simulated) return html`<div class="result"><span class="tag tag-sim">SIM</span> ${j.result.note}</div>`;
  const out = j.result.output || {};
  return html`<div class="result">
    <div>${j.verified ? html`<span class="tag tag-ok">VERIFIED</span>` : html`<span class="tag tag-bad">UNVERIFIED</span>`} ${j.result.verification}</div>
    ${out.digest ? html`<div class="faint">Digest <code>${out.digest}</code></div>` : ''}
    ${out.checksum !== undefined ? html`<div class="faint">Checksum <code>${out.checksum}</code></div>` : ''}
    ${out.tflops !== undefined ? html`<div class="faint">${out.device} · ${out.tflops} TFLOPS (provider-reported, not verified)</div>` : ''}
    ${out.device && out.tflops === undefined ? html`<div class="faint">Executed on: ${out.device}</div>` : ''}
  </div>`;
}

function detailHtml(j) {
  const kv = (label, value) => html`<dt>${label}</dt><dd>${value}</dd>`;
  return html`
    <div class="detail">
      <div>
        <h3>Timeline</h3>
        <ol class="timeline">${j.events.map((e) => html`<li><time>${ui.clock(e.at)}</time><div><span class="k">${e.kind}</span>${e.detail ? html` <span class="d">· ${e.detail}</span>` : ''}</div></li>`)}</ol>
      </div>
      <div>
        <h3>Billing</h3>
        <dl class="kv">
          ${kv('Budget held', ui.usd(j.max_budget_cents))}
          ${kv('Rate', j.hourly_cents !== null ? ui.rate(j.hourly_cents) : '—')}
          ${kv('Quote', ui.usd(j.quote_cents))}
          ${kv('Billed time', j.billed_seconds !== null ? `${ui.duration(j.billed_seconds)}${j.execution === 'simulated' ? ' (simulated)' : ' (server-measured)'}` : '—')}
          ${kv('Charged', j.cost_cents !== null ? ui.usd(j.cost_cents) : 'Not charged')}
          ${j.cost_cents !== null ? html`${kv('To provider', ui.usd(j.provider_cents))}${kv(`Marketplace fee (${ui.pct(j.fee_bps)})`, ui.usd(j.platform_cents))}` : ''}
          ${kv('Attempts', `${j.attempts} of ${j.max_attempts}`)}
        </dl>
        ${resultHtml(j)}
        ${j.can_cancel || j.demo_controls ? html`<div class="btn-row section-tight">
          ${j.can_cancel ? html`<button class="btn btn-sm btn-danger" data-act="cancel" data-id="${j.id}">Cancel job</button>` : ''}
          ${j.demo_controls ? html`<span class="faint">Demo:</span>
            <button class="btn btn-sm" data-act="demo-complete" data-id="${j.id}">Finish now</button>
            <button class="btn btn-sm" data-act="demo-fail" data-id="${j.id}">Simulate failure</button>` : ''}
        </div>` : ''}
      </div>
    </div>`;
}

function queueHtml() {
  if (!list) return html`<div class="loading">Loading jobs…</div>`;
  const rows = filter === 'all' ? list.jobs : list.jobs.filter((j) => j.status === filter);
  if (!rows.length) {
    return ui.empty(filter === 'all' ? 'No jobs yet' : `No ${filter} jobs`,
      filter === 'all' ? html`<div>Submit a job to see matching, queueing and billing in action.</div>` : '');
  }
  return html`<div class="table-wrap"><table class="table stackable">
    <thead><tr><th>Status</th><th>Job</th><th>GPU</th><th class="r">Cost</th><th class="r">Updated</th></tr></thead>
    <tbody>${rows.map(jobRow)}</tbody></table></div>`;
}

function chipsHtml() {
  const counts = list?.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chip = (key, label, n) => html`<button class="chip" data-filter="${key}" aria-pressed="${filter === key}">${label}<b>${n}</b></button>`;
  return html`${chip('all', 'All', total)}${STATUSES.map((s) => chip(s, s[0].toUpperCase() + s.slice(1), counts[s] || 0))}`;
}

async function loadDetail(ctx) {
  if (openJob === null) return;
  const target = ctx.root.querySelector(`#job-detail-${openJob}`);
  if (!target) return;
  try {
    const { job } = await api.get(`/jobs/${openJob}`);
    detailCache = { id: job.id, html: String(detailHtml(job)) };
    ui.patch(ctx.root.querySelector(`#job-detail-${job.id}`), detailCache.html);
  } catch (err) {
    ui.patch(target, html`<div class="loading">${err.message}</div>`);
  }
}

function renderQueue(ctx) {
  ui.patch(ctx.root.querySelector('#q-chips'), chipsHtml());
  ui.patch(ctx.root.querySelector('#q-table'), queueHtml());
}

async function refresh(ctx) {
  if (!ctx.store.user) return;
  try {
    list = await api.get('/jobs');
    renderQueue(ctx);
    loadDetail(ctx);
    ctx.refreshBalances();
    updateQuote(ctx);
  } catch (err) {
    if (err.status !== 401) ui.toast(err.message, 'error');
  }
}

function mount(ctx) {
  if (!ctx.requireAuth(ctx, 'Run AI workloads on shared GPUs', 'Create an account to submit jobs. New accounts can add simulated credits to try the full flow.')) return null;
  list = null;
  ctx.root.innerHTML = String(html`
    <div class="page-head">
      <div><h1>Jobs</h1>
        <p>Set the requirements and a budget; the scheduler picks the cheapest compatible GPU. Simulated workloads are timed by the server. Agent workloads run on a real connected machine and are verified before billing.</p></div>
    </div>
    <div class="split">
      <div>${formHtml(ctx)}</div>
      <div>
        <div class="section-head"><div><h2>Queue</h2><p>Updates live. Select a job for its timeline and billing.</p></div></div>
        <div class="chips toolbar" id="q-chips"></div>
        <div class="table-card" id="q-table"><div class="loading">Loading jobs…</div></div>
      </div>
    </div>`);

  const form = ctx.root.querySelector('#job-form');
  const onInput = (event) => {
    if (event.target.closest('#job-form')) { ui.clearFormError(form); scheduleQuote(ctx); }
  };
  const onSubmit = async (event) => {
    if (event.target.id !== 'job-form') return;
    event.preventDefault();
    const data = ui.formData(form);
    const button = ctx.root.querySelector('#j-submit');
    button.disabled = true;
    try {
      const { job } = await api.post('/jobs', { ...data, min_vram_gb: Number(data.min_vram_gb), est_minutes: Number(data.est_minutes) });
      ui.toast(job.status === 'pending' ? `Job #${job.id} queued. It will start when a compatible GPU frees up.` : `Job #${job.id} matched to ${job.gpu.model}.`);
      openJob = job.id;
      filter = 'all';
      await refresh(ctx);
      scheduleQuote(ctx);
    } catch (err) {
      ui.showFormError(form, err);
    } finally {
      button.disabled = false;
    }
  };
  const onClick = async (event) => {
    const chip = event.target.closest('[data-filter]');
    if (chip) { filter = chip.dataset.filter; renderQueue(ctx); return; }
    const act = event.target.closest('[data-act]');
    if (act) {
      act.disabled = true;
      const id = Number(act.dataset.id);
      const paths = { cancel: `/jobs/${id}/cancel`, 'demo-complete': `/jobs/${id}/demo/complete`, 'demo-fail': `/jobs/${id}/demo/fail` };
      const done = { cancel: 'Job cancelled; hold released.', 'demo-complete': 'Simulated job completed and settled.', 'demo-fail': 'Simulated failure injected; hold released.' };
      try {
        await api.post(paths[act.dataset.act]);
        ui.toast(done[act.dataset.act]);
      } catch (err) {
        ui.toast(err.message, 'error');
      }
      await refresh(ctx);
      return;
    }
    const row = event.target.closest('[data-job]');
    if (row && !event.target.closest('a, button')) {
      const id = Number(row.dataset.job);
      openJob = openJob === id ? null : id;
      renderQueue(ctx);
      loadDetail(ctx);
    }
  };
  ctx.root.addEventListener('input', onInput);
  ctx.root.addEventListener('change', onInput);
  ctx.root.addEventListener('submit', onSubmit);
  ctx.root.addEventListener('click', onClick);
  updateQuote(ctx);
  refresh(ctx);
  return () => {
    clearTimeout(quoteTimer);
    ctx.root.removeEventListener('input', onInput);
    ctx.root.removeEventListener('change', onInput);
    ctx.root.removeEventListener('submit', onSubmit);
    ctx.root.removeEventListener('click', onClick);
  };
}

export default {
  title: 'Jobs', mount, poll: 2500,
  refresh: (ctx) => { refresh(ctx); },
};
