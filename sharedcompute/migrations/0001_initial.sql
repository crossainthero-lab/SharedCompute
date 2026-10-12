-- SharedCompute schema, migration 0001 (v0.2 baseline). Never edit an applied migration; add a new one.
-- Portable SQL where practical so the move to PostgreSQL stays mechanical.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL,
    password_hash TEXT,                       -- NULL: account cannot sign in (system accounts)
    is_operator   INTEGER NOT NULL DEFAULT 0,
    is_system     INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT    PRIMARY KEY,           -- sha256 of the bearer token; raw token never stored
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);

-- A node is one machine. 'simulated' nodes are self-reported listings executed by the
-- server-side simulator; 'agent' nodes run the provider agent and report real hardware.
CREATE TABLE nodes (
    id                   INTEGER PRIMARY KEY,
    owner_id             INTEGER NOT NULL REFERENCES users(id),
    name                 TEXT    NOT NULL,
    region               TEXT    NOT NULL,
    kind                 TEXT    NOT NULL CHECK (kind IN ('simulated', 'agent')),
    enabled              INTEGER NOT NULL DEFAULT 1,
    retired              INTEGER NOT NULL DEFAULT 0,
    token_hash           TEXT    UNIQUE,      -- agent credential (sha256)
    default_hourly_cents INTEGER NOT NULL,
    max_concurrency      INTEGER NOT NULL DEFAULT 1,
    agent_version        TEXT,
    platform             TEXT,
    capabilities         TEXT    NOT NULL DEFAULT '[]',  -- JSON array of workload ids
    last_seen_at         REAL,
    created_at           REAL    NOT NULL
);
CREATE INDEX nodes_owner ON nodes(owner_id);

CREATE TABLE gpus (
    id                 INTEGER PRIMARY KEY,
    node_id            INTEGER NOT NULL REFERENCES nodes(id),
    slot               INTEGER NOT NULL,     -- device index on the node
    fingerprint        TEXT,                 -- hashed hardware UUID reported by the agent
    vendor             TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    vram_mb            INTEGER NOT NULL,
    cuda               INTEGER NOT NULL DEFAULT 0,
    compute_capability TEXT,
    driver_version     TEXT,
    integrated         INTEGER NOT NULL DEFAULT 0,
    detection          TEXT    NOT NULL,     -- self_reported | nvml | nvidia-smi | system_profiler
    hourly_cents       INTEGER NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    present            INTEGER NOT NULL DEFAULT 1,  -- still reported by the agent
    util_percent       REAL,
    mem_used_mb        INTEGER,
    temperature_c      REAL,
    metrics_at         REAL,
    created_at         REAL    NOT NULL
);
CREATE INDEX gpus_node ON gpus(node_id);

CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    owner_id         INTEGER NOT NULL REFERENCES users(id),
    name             TEXT    NOT NULL,
    workload         TEXT    NOT NULL,
    execution        TEXT    NOT NULL CHECK (execution IN ('simulated', 'agent')),
    min_vram_mb      INTEGER NOT NULL,
    est_minutes      INTEGER NOT NULL,
    max_budget_cents INTEGER NOT NULL,       -- also the amount held from the customer balance
    region           TEXT    NOT NULL,       -- region code or 'any'
    status           TEXT    NOT NULL CHECK (status IN
                         ('pending', 'assigned', 'running', 'completed', 'failed', 'cancelled')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    -- Set on assignment (snapshot of the match).
    gpu_id           INTEGER REFERENCES gpus(id),
    node_id          INTEGER REFERENCES nodes(id),
    provider_id      INTEGER REFERENCES users(id),
    hourly_cents     INTEGER,
    quote_cents      INTEGER,
    fee_bps          INTEGER,
    params           TEXT,                   -- JSON workload parameters issued to the executor
    lease_expires_at REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    -- Set on completion.
    billed_seconds   REAL,
    cost_cents       INTEGER,
    provider_cents   INTEGER,
    platform_cents   INTEGER,
    result           TEXT,                   -- JSON
    verified         INTEGER,                -- NULL: not applicable, 1: passed, 0: failed
    failure_reason   TEXT,
    created_at       REAL    NOT NULL,
    assigned_at      REAL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX jobs_status ON jobs(status, id);
CREATE INDEX jobs_owner ON jobs(owner_id, id);
CREATE INDEX jobs_gpu ON jobs(gpu_id, status);
CREATE INDEX jobs_provider ON jobs(provider_id, status);

CREATE TABLE job_events (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES jobs(id),
    node_id INTEGER REFERENCES nodes(id),      -- node involved, for reliability statistics
    at      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    detail  TEXT
);
CREATE INDEX job_events_job ON job_events(job_id, id);
CREATE INDEX job_events_node ON job_events(node_id, kind);

-- Double-entry ledger. Every entry's postings sum to zero. Balances are sums of postings.
CREATE TABLE ledger_accounts (
    id      INTEGER PRIMARY KEY,
    code    TEXT    NOT NULL UNIQUE,         -- e.g. user:7:available, platform:revenue
    kind    TEXT    NOT NULL,                -- available | held | earnings | revenue | demo_funding
    user_id INTEGER REFERENCES users(id)
);

CREATE TABLE ledger_entries (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,             -- demo_topup | hold | release | charge
    job_id     INTEGER REFERENCES jobs(id),
    user_id    INTEGER REFERENCES users(id), -- the customer the entry concerns
    memo       TEXT,
    created_at REAL    NOT NULL
);
CREATE INDEX ledger_entries_job ON ledger_entries(job_id);
CREATE INDEX ledger_entries_user ON ledger_entries(user_id, id);

CREATE TABLE ledger_postings (
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER NOT NULL REFERENCES ledger_entries(id),
    account_id   INTEGER NOT NULL REFERENCES ledger_accounts(id),
    amount_cents INTEGER NOT NULL
);
CREATE INDEX ledger_postings_account ON ledger_postings(account_id);
CREATE INDEX ledger_postings_entry ON ledger_postings(entry_id);
