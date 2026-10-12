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
    ['Device opt-in', REAL, 'Nothing is offered unless the provider names a GPU (--gpu) or the CPU (--offer-cpu) on the machine. GPUs that drive a display are refused unless explicitly allowed.'],
    ['Hardware discovery', PARTIAL, 'NVML, nvidia-smi, the CUDA driver (ctypes, matched by PCI bus) and macOS. The macOS path runs on real hardware; the NVIDIA paths are tested only against synthetic output and a fake driver, not yet on an NVIDIA GPU.'],
    ['Compatibility checks', REAL, 'Server and agent both check compute capability (≥ 3.0), CUDA driver (≥ 5.5), VRAM and CUDA usability; incompatible jobs are never matched, and late incompatibilities are declined without costing the customer an attempt.'],
    ['Heartbeats, liveness and leases', REAL, 'Stale nodes and unclaimed jobs are detected and their work requeued.'],
    ['Pipeline-check workload (CPU)', REAL, 'Executes on the provider CPU and is verified by the server before billing. Proves the end-to-end path, not GPU compute.'],
    ['CUDA integer matmul (GPU)', PARTIAL, 'LLVM-generated PTX (Kepler sm_30+) loaded through the driver, verified by exact checksum plus Merkle-committed row challenges. Exercised only with a fake driver so far; not yet executed on an NVIDIA GPU.'],
    ['Where a job ran', PARTIAL, 'The server records GPU vs CPU, verifies results, and cross-checks device identity and timing. A GPU claim is attested by the agent, not cryptographically proven. CPU fallback is never billed as GPU.'],
    ['TLS for remote agents', REAL, 'Agents refuse plain HTTP to remote hosts and verify certificates (system store or a private CA). Tested on loopback with a private CA; not yet across two networks.'],
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
