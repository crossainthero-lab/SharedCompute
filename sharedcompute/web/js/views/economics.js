// Economics: your balances and ledger; platform volume, revenue and integrity for operators.
import { api } from '../api.js';
import * as ui from '../ui.js';
import { html } from '../ui.js';

let data = null;

const ENTRY_LABEL = { demo_topup: 'Demo credits added', hold: 'Budget held', release: 'Hold released', charge: 'Job settled' };

function splitBar(feeBps) {
  const provider = (10000 - feeBps) / 100;
  return html`<svg class="split-bar" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label="${provider}% provider, ${feeBps / 100}% marketplace">
    <rect class="p" x="0" y="0" width="${provider}" height="8"/><rect class="f" x="${provider}" y="0" width="${100 - provider}" height="8"/></svg>`;
}

function personalHtml(p, meta) {
  const b = p.balances;
  return html`
    <div class="stats">
      <div class="stat"><div class="label">Available</div><div class="value">${ui.usd(b.available_cents)}</div><div class="sub">Simulated credits</div></div>
      <div class="stat"><div class="label">Held for jobs</div><div class="value">${ui.usd(b.held_cents)}</div><div class="sub">Released or charged on finish</div></div>
      <div class="stat"><div class="label">Spent on compute</div><div class="value">${ui.usd(p.customer.spent_cents)}</div><div class="sub">${ui.plural(p.customer.completed_jobs, 'completed job')} · ${ui.hours(p.customer.billed_seconds)}</div></div>
      <div class="stat"><div class="label">Earned as provider</div><div class="value">${ui.usd(p.provider.earned_cents)}</div><div class="sub">${ui.plural(p.provider.completed_jobs, 'job')} · ${ui.hours(p.provider.billed_seconds)}</div></div>
    </div>
    <div class="grid-2 section-tight">
      <div class="card card-pad">
        <div class="section-head"><div><h2>Add demo credits</h2><p>Simulated balance for testing. No payment is taken and credits have no cash value.</p></div></div>
        ${meta.demo_credits_enabled ? html`<form class="form" id="topup-form" novalidate>
          <div class="field"><label for="t-amount">Amount</label><div class="input-affix"><span>$</span><input class="input num" id="t-amount" name="amount_usd" type="number" min="0.01" max="1000" step="0.01" value="25.00"></div></div>
          <div class="field"><label>&nbsp;</label><button class="btn btn-primary" type="submit">Add credits</button></div>
          <div class="form-error hidden" role="alert"></div>
        </form>` : html`<p class="muted">Demo credits are disabled on this deployment.</p>`}
      </div>
      <div class="card card-pad">
        <div class="section-head"><div><h2>Fee policy</h2><p>Applied to each completed job's charge.</p></div></div>
        ${splitBar(meta.platform_fee_bps)}
        <div class="legend"><span><i class="p"></i>Provider ${ui.pct(10000 - meta.platform_fee_bps)}</span><span><i class="f"></i>Marketplace ${ui.pct(meta.platform_fee_bps)}</span></div>
        <p class="muted small-print">Charges are rounded to the cent per job, with ties going to the provider. Failed and cancelled jobs cost nothing. Simulated jobs bill their estimated minutes; agent jobs bill server-measured runtime, capped at the quote.</p>
      </div>
    </div>`;
}

function chart(daily) {
  const max = Math.max(1, ...daily.map((d) => d.gross_cents));
  const slot = 700 / daily.length;
  return html`<svg class="bars" viewBox="0 0 700 140" role="img" aria-label="Completed job volume per day, last 14 days">
    ${daily.map((d, i) => {
      const h = d.gross_cents ? Math.max(2, (d.gross_cents / max) * 112) : 2;
      const x = i * slot + 6;
      const label = new Date(d.day * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' });
      return html`<rect class="${d.gross_cents ? '' : 'zero'}" x="${x}" y="${120 - h}" width="${slot - 12}" height="${h}" rx="2"><title>${label}: ${ui.usd(d.gross_cents)}</title></rect>
        ${i % 2 === 1 ? html`<text x="${x + (slot - 12) / 2}" y="136" text-anchor="middle">${label}</text>` : ''}`;
    })}</svg>`;
}

function platformHtml(pl) {
  const t = pl.totals;
  const row = (label, e) => html`<tr><td class="cell-main">${label}</td>
    <td data-label="Jobs" class="r num">${e.completed_jobs}</td><td data-label="Volume" class="r num">${ui.usd(e.gross_cents)}</td>
    <td data-label="Providers" class="r num">${ui.usd(e.provider_cents)}</td><td data-label="Marketplace" class="r num">${ui.usd(e.platform_cents)}</td>
    <td data-label="GPU time" class="r num">${ui.hours(e.billed_seconds)}</td></tr>`;
  return html`
    <div class="stats cols-5">
      <div class="stat"><div class="label">Completed volume</div><div class="value">${ui.usd(t.gross_cents)}</div><div class="sub">${ui.plural(t.completed_jobs, 'job')}</div></div>
      <div class="stat"><div class="label">Marketplace revenue</div><div class="value">${ui.usd(pl.ledger.platform_revenue_cents)}</div><div class="sub">${ui.pct(pl.fee_bps)} fee, gross</div></div>
      <div class="stat"><div class="label">Provider earnings</div><div class="value">${ui.usd(pl.ledger.provider_earnings_cents)}</div><div class="sub">accrued, unpaid</div></div>
      <div class="stat"><div class="label">GPU time sold</div><div class="value">${ui.hours(t.billed_seconds)}</div><div class="sub">${ui.hours(pl.by_execution.agent.billed_seconds)} measured on agents</div></div>
      <div class="stat"><div class="label">Participants</div><div class="value">${pl.participants.customers + pl.participants.providers}</div><div class="sub">${ui.plural(pl.participants.customers, 'customer')} · ${ui.plural(pl.participants.providers, 'provider')}</div></div>
    </div>
    <div class="grid-2 section-tight">
      <div class="card card-pad">
        <div class="section-head"><div><h2>Daily completed volume</h2><p>Last 14 days, UTC.</p></div></div>
        ${chart(pl.daily_gross)}
      </div>
      <div class="card card-pad">
        <div class="section-head"><div><h2>Ledger</h2><p>Double-entry; every entry sums to zero.</p></div></div>
        <dl class="kv">
          <dt>Demo credits issued</dt><dd>${ui.usd(pl.ledger.demo_credits_issued_cents)}</dd>
          <dt>Customer balances</dt><dd>${ui.usd(pl.ledger.customer_available_cents)}</dd>
          <dt>Held for in-flight jobs</dt><dd>${ui.usd(pl.ledger.customer_held_cents)}</dd>
          <dt>Provider earnings</dt><dd>${ui.usd(pl.ledger.provider_earnings_cents)}</dd>
          <dt>Marketplace revenue</dt><dd>${ui.usd(pl.ledger.platform_revenue_cents)}</dd>
          <dt>Integrity</dt><dd class="integrity ${pl.ledger.balanced ? 'ok' : 'bad'}">${pl.ledger.balanced ? 'Balanced' : 'IMBALANCED — investigate'}</dd>
        </dl>
      </div>
    </div>
    <div class="section-tight table-card"><div class="table-wrap"><table class="table stackable">
      <thead><tr><th>Execution</th><th class="r">Jobs</th><th class="r">Volume</th><th class="r">Providers</th><th class="r">Marketplace</th><th class="r">GPU time</th></tr></thead>
      <tbody>${row(html`Agent-executed <span class="tag tag-agent">AGENT</span>`, pl.by_execution.agent)}${row(html`Simulated <span class="tag tag-sim">SIM</span>`, pl.by_execution.simulated)}</tbody>
    </table></div></div>
    <div class="section">
      <div class="section-head"><div><h2>Completed transactions</h2><p>Most recent 50 settled jobs, across all customers.</p></div></div>
      <div class="table-card">${pl.recent_transactions.length ? html`<div class="table-wrap"><table class="table stackable">
        <thead><tr><th>Job</th><th>GPU</th><th class="r">Billed time</th><th class="r">Charge</th><th class="r">Provider</th><th class="r">Fee</th></tr></thead>
        <tbody>${pl.recent_transactions.map((j) => html`<tr>
          <td class="cell-main"><div class="primary">#${j.id} · ${j.workload_label} ${j.execution === 'agent' ? html`<span class="tag tag-agent">AGENT</span>` : html`<span class="tag tag-sim">SIM</span>`}</div><div class="sub">${ui.date(j.finished_at)}</div></td>
          <td data-label="GPU">${j.model || '—'}</td><td data-label="Billed time" class="r num">${ui.duration(j.billed_seconds)}</td>
          <td data-label="Charge" class="r num"><b>${ui.usd(j.cost_cents)}</b></td><td data-label="Provider" class="r num">${ui.usd(j.provider_cents)}</td>
          <td data-label="Fee" class="r num">${ui.usd(j.platform_cents)}</td></tr>`)}</tbody></table></div>`
        : ui.empty('No completed jobs yet', '')}</div>
    </div>`;
}

function delta(cents) {
  if (!cents) return html`<span class="faint">—</span>`;
  return html`<span class="${cents > 0 ? 'pos' : 'neg'}">${cents > 0 ? '+' : ''}${ui.usd(cents)}</span>`;
}

function ledgerHtml(rows) {
  if (!rows.length) return ui.empty('No ledger activity yet', html`<div>Add demo credits or submit a job.</div>`);
  return html`<div class="table-wrap"><table class="table stackable">
    <thead><tr><th>Entry</th><th>Job</th><th class="r">Available</th><th class="r">Held</th><th class="r">Earnings</th></tr></thead>
    <tbody>${rows.map((r) => html`<tr>
      <td class="cell-main"><div class="primary">${ENTRY_LABEL[r.kind] || r.kind}</div><div class="sub">${ui.date(r.created_at)} · ${r.memo || ''}</div></td>
      <td data-label="Job">${r.job_id ? `#${r.job_id}` : '—'}</td>
      <td data-label="Available" class="r num">${delta(r.available_delta)}</td>
      <td data-label="Held" class="r num">${delta(r.held_delta)}</td>
      <td data-label="Earnings" class="r num">${delta(r.earnings_delta)}</td></tr>`)}</tbody></table></div>`;
}

function render(ctx) {
  if (!data) return;
  const meta = ctx.store.meta;
  const personal = ctx.root.querySelector('#ec-personal');
  if (!personal.contains(document.activeElement)) ui.patch(personal, personalHtml(data.personal, meta));
  ui.patch(ctx.root.querySelector('#ec-platform'), data.platform ? platformHtml(data.platform) : html`
    <div class="notice"><b>Platform-wide figures are visible to operators.</b> They include every customer's transactions, so they are not shown to regular accounts.
    To grant access on this deployment run <code>python -m sharedcompute make-operator ${ctx.store.user.email}</code> and reload.</div>`);
  ui.patch(ctx.root.querySelector('#ec-ledger'), ledgerHtml(data.personal.transactions));
}

async function refresh(ctx) {
  if (!ctx.store.user) return;
  try {
    data = await api.get('/economics');
    render(ctx);
  } catch (err) {
    if (err.status !== 401) ui.toast(err.message, 'error');
  }
}

function mount(ctx) {
  if (!ctx.requireAuth(ctx, 'Usage, spend and earnings', 'Sign in to see your balances, the fee split and your ledger history.')) return null;
  data = null;
  ctx.root.innerHTML = String(html`
    <div class="page-head"><div><h1>Economics</h1>
      <p>Every number here is derived from completed jobs and a double-entry ledger of simulated credits. No real money, payouts or tokens exist in this build.</p></div></div>
    <div id="ec-personal"><div class="loading">Loading…</div></div>
    <div class="section"><div class="section-head"><div><h2>Platform</h2><p>Marketplace-wide volume, revenue and provider earnings.</p></div></div>
      <div id="ec-platform"></div></div>
    <div class="section"><div class="section-head"><div><h2>Your ledger activity</h2><p>How each entry changed your balances.</p></div></div>
      <div class="table-card" id="ec-ledger"></div></div>`);
  const onSubmit = async (event) => {
    if (event.target.id !== 'topup-form') return;
    event.preventDefault();
    const form = event.target;
    try {
      await api.post('/wallet/demo-topup', ui.formData(form));
      ui.toast('Demo credits added.');
      form.querySelector('input').blur();
      await Promise.all([refresh(ctx), ctx.refreshBalances()]);
    } catch (err) {
      ui.showFormError(form, err);
    }
  };
  ctx.root.addEventListener('submit', onSubmit);
  refresh(ctx);
  return () => ctx.root.removeEventListener('submit', onSubmit);
}

export default { title: 'Economics', mount, poll: 6000, refresh: (ctx) => { refresh(ctx); } };
