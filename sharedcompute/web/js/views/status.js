// What this build really does. Kept next to the UI so the product never overstates itself.
import { html } from '../ui.js';

const REAL = html`<span class="cap real">WORKS</span>`;
const PARTIAL = html`<span class="cap partial">PARTIAL</span>`;
const SIM = html`<span class="cap sim">SIMULATED</span>`;
const NONE = html`<span class="cap none">NOT BUILT</span>`;

const SECTIONS = [
  ['Marketplace & scheduling', [
    ['Accounts and sessions', REAL, 'Email + password (PBKDF2), HttpOnly session cookie, CSRF header, login rate limit. No email verification or password reset yet.'],
    ['GPU listings and availability', REAL, 'Live status from provider settings, heartbeats and active jobs.'],
    ['Matching scheduler', REAL, 'Cheapest compatible GPU within budget; VRAM, region, workload support, CUDA and availability checks; FIFO queue.'],
    ['Job lifecycle and retries', REAL, 'Pending → assigned → running → completed / failed / cancelled. Interrupted attempts requeue up to the attempt limit.'],
    ['Sample network listings', SIM, 'Four seeded listings owned by a system account. Their hardware does not exist.'],
    ['Simulated workloads', SIM, 'Image, inference, fine-tune and batch jobs are timed by the server and billed for their estimated minutes. Nothing executes.'],
  ]],
  ['Provider agent', [
    ['Hardware discovery', PARTIAL, 'NVML, nvidia-smi and macOS reports. NVIDIA paths are unit-tested against synthetic output in the documented formats; they have not yet run on a real NVIDIA machine. The macOS path is verified on real hardware.'],
    ['Heartbeats, liveness and leases', REAL, 'Stale nodes and unclaimed jobs are detected and their work requeued.'],
    ['Pipeline-check workload', REAL, 'Executes on the provider machine (CPU) and is verified by the server before billing. Proves the end-to-end path, not GPU throughput.'],
    ['CUDA matmul benchmark', PARTIAL, 'Implemented with an exact, server-verified checksum; requires PyTorch + CUDA and has not yet been executed on NVIDIA hardware.'],
    ['Customer containers / arbitrary code', NONE, 'Deliberately absent until sandboxing (gVisor/Kata/VM) and artifact storage exist.'],
    ['Hardware attestation', NONE, 'Agent reports are self-attested by the provider.'],
  ]],
  ['Money', [
    ['Double-entry ledger', REAL, 'Holds, charges, refunds and the 10% / 90% split are ledger entries that always balance.'],
    ['Balances and payments', SIM, 'Demo credits only. No payment processor, deposits, payouts, invoices or tax handling.'],
    ['Crypto settlement', NONE, 'No token, wallet or chain integration. Settlement design is documented for later.'],
  ]],
];

function mount(ctx) {
  ctx.root.innerHTML = String(html`
    <div class="page-head"><div><h1>Build status</h1>
      <p>What is real in this build, what is simulated, and what has not been built. See <span class="inline-code">docs/ARCHITECTURE.md</span> in the repository for the path to real GPU execution.</p></div></div>
    ${SECTIONS.map(([title, rows]) => html`
      <div class="section"><div class="section-head"><h2>${title}</h2></div>
        <div class="table-card"><div class="table-wrap"><table class="table stackable cap-table"><tbody>
          ${rows.map(([name, cap, note]) => html`<tr><td class="cell-main"><div class="primary">${name}</div></td><td data-label="State">${cap}</td><td data-label="Notes" class="muted">${note}</td></tr>`)}
        </tbody></table></div></div></div>`)}
  `);
  return null;
}

export default { title: 'Build status', mount };
