#!/usr/bin/env python3
"""SharedCompute: dependency-free, localhost-only simulated GPU marketplace."""
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('SHAREDCOMPUTE_DB', str(ROOT / 'sharedcompute.sqlite3')))
PORT = int(os.environ.get('SHAREDCOMPUTE_PORT', '8765'))
DB_LOCK = RLock()
SIMULATION_SECONDS = 8
FEE_PERCENT = 10
TASK_TYPES = {'image', 'inference', 'fine-tune', 'batch'}

SEEDS = [
    ('Demo Node · Sydney', 'RTX 3060', 12, 'Sydney, AU', '0.24', 98),
    ('Demo Node · Singapore', 'RTX 3090', 24, 'Singapore, SG', '0.44', 97),
    ('Demo Node · Frankfurt', 'RTX 4090', 24, 'Frankfurt, DE', '0.67', 99),
    ('Demo Node · Stockholm', 'RTX 6000 Ada', 48, 'Stockholm, SE', '1.28', 99),
]


def dollars(value):
    """Persist money as integer US cents. Dollar inputs have at most two decimals."""
    try:
        dec = Decimal(str(value))
    except Exception as exc:
        raise ValueError('Enter a valid USD amount') from exc
    if not dec.is_finite() or dec < 0 or dec > 100000:
        raise ValueError('USD amount must be between $0 and $100,000')
    if dec.as_tuple().exponent < -2:
        raise ValueError('Use at most two decimal places for USD')
    return int(dec * 100)


def bill_cents(hourly_cents, minutes):
    return max(1, int((Decimal(hourly_cents * minutes) / Decimal(60)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def provider_share_cents(total):
    return int((Decimal(total * (100 - FEE_PERCENT)) / Decimal(100)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        yield db


def reset_db(db):
    db.executescript('DROP TABLE IF EXISTS jobs; DROP TABLE IF EXISTS providers;')
    db.executescript('''
    CREATE TABLE providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        gpu TEXT NOT NULL,
        vram_gb INTEGER NOT NULL,
        region TEXT NOT NULL,
        hourly_cents INTEGER NOT NULL,
        reliability INTEGER NOT NULL DEFAULT 100,
        online INTEGER NOT NULL DEFAULT 1,
        sample INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        task_type TEXT NOT NULL,
        min_vram_gb INTEGER NOT NULL,
        minutes INTEGER NOT NULL,
        max_budget_cents INTEGER NOT NULL,
        region TEXT NOT NULL DEFAULT 'Any region',
        provider_id INTEGER REFERENCES providers(id),
        status TEXT NOT NULL DEFAULT 'queued',
        cost_cents INTEGER,
        started_at REAL,
        completed_at REAL,
        created_at REAL NOT NULL
    );
    ''')
    for name, gpu, vram, region, rate, reliability in SEEDS:
        db.execute('''INSERT INTO providers(name,gpu,vram_gb,region,hourly_cents,reliability,online,sample)
                      VALUES(?,?,?,?,?,?,1,1)''', (name, gpu, vram, region, dollars(rate), reliability))


def init_db():
    with DB_LOCK, connect() as db:
        found = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='providers'").fetchone()
        if not found:
            reset_db(db)


def schedule(db):
    """Assign jobs to affordable, online, idle providers. No GPU work runs."""
    jobs = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id").fetchall()
    for job in jobs:
        providers = db.execute('''
            SELECT p.* FROM providers p
            WHERE p.online=1 AND p.vram_gb>=?
              AND (?='Any region' OR p.region=?)
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.provider_id=p.id AND j.status='running')
            ORDER BY p.hourly_cents ASC, p.reliability DESC, p.id ASC
        ''', (job['min_vram_gb'], job['region'], job['region'])).fetchall()
        candidate = next((p for p in providers if bill_cents(p['hourly_cents'], job['minutes']) <= job['max_budget_cents']), None)
        if candidate:
            db.execute('''UPDATE jobs SET provider_id=?,status='running',cost_cents=?,started_at=? WHERE id=?''',
                       (candidate['id'], bill_cents(candidate['hourly_cents'], job['minutes']), time.time(), job['id']))


def reconcile(db):
    # Demo-only eight-second automatic completion, priced using requested simulated minutes.
    now = time.time()
    db.execute('''UPDATE jobs SET status='completed', completed_at=?
                  WHERE status='running' AND started_at<=?''', (now, now - SIMULATION_SECONDS))
    schedule(db)


def state():
    with DB_LOCK, connect() as db:
        reconcile(db)
        providers = [dict(row) for row in db.execute('SELECT * FROM providers ORDER BY id')]
        for p in providers:
            completed = db.execute("SELECT cost_cents FROM jobs WHERE provider_id=? AND status='completed'", (p['id'],)).fetchall()
            p['earned_cents'] = sum(provider_share_cents(x['cost_cents']) for x in completed)
            p['completed_jobs'] = len(completed)
            p['busy'] = bool(db.execute("SELECT 1 FROM jobs WHERE provider_id=? AND status='running'", (p['id'],)).fetchone())
        jobs = [dict(row) for row in db.execute('''
            SELECT j.*, p.name provider_name, p.gpu provider_gpu
            FROM jobs j LEFT JOIN providers p ON p.id=j.provider_id ORDER BY j.id DESC''')]
        completed = [j for j in jobs if j['status'] == 'completed']
        totals = {
            'online_providers': sum(p['online'] for p in providers),
            'available_providers': sum(p['online'] and not p['busy'] for p in providers),
            'total_providers': len(providers),
            'completed_jobs': len(completed),
            'running_jobs': sum(j['status'] == 'running' for j in jobs),
            'queued_jobs': sum(j['status'] == 'queued' for j in jobs),
            'gross_cents': sum(j['cost_cents'] for j in completed),
            'provider_cents': sum(provider_share_cents(j['cost_cents']) for j in completed),
            'compute_minutes': sum(j['minutes'] for j in completed),
        }
        totals['platform_cents'] = totals['gross_cents'] - totals['provider_cents']
        return {'demo': True, 'fee_percent': FEE_PERCENT, 'simulation_seconds': SIMULATION_SECONDS,
                'providers': providers, 'jobs': jobs, 'totals': totals}


def clean_text(value, field, min_len=2, max_len=80):
    if not isinstance(value, str):
        raise ValueError(f'{field} is required')
    value = value.strip()
    if not min_len <= len(value) <= max_len or any(ord(c) < 32 for c in value):
        raise ValueError(f'{field} must have {min_len}–{max_len} printable characters')
    return value


def integer(value, field, low, high):
    if isinstance(value, bool):
        raise ValueError(f'{field} must be an integer')
    try:
        i = int(value)
        if str(i) != str(value):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError(f'{field} must be an integer')
    if not low <= i <= high:
        raise ValueError(f'{field} must be between {low} and {high}')
    return i


def create_provider(body):
    name = clean_text(body.get('name'), 'Provider name')
    gpu = clean_text(body.get('gpu'), 'GPU model')
    region = clean_text(body.get('region'), 'Region')
    vram = integer(body.get('vram_gb'), 'VRAM', 2, 192)
    rate = dollars(body.get('hourly_usd'))
    if rate < 1:
        raise ValueError('Rate must be at least $0.01/hour')
    with DB_LOCK, connect() as db:
        cursor = db.execute('''INSERT INTO providers(name,gpu,vram_gb,region,hourly_cents,reliability,online,sample)
                               VALUES(?,?,?,?,?,100,1,0)''', (name, gpu, vram, region, rate))
        schedule(db)
        return cursor.lastrowid


def create_job(body):
    name = clean_text(body.get('name'), 'Job name')
    task = body.get('task_type')
    if task not in TASK_TYPES:
        raise ValueError('Choose a supported workload type')
    vram = integer(body.get('min_vram_gb'), 'Required VRAM', 2, 192)
    minutes = integer(body.get('minutes'), 'Estimated minutes', 1, 10080)
    budget = dollars(body.get('max_budget_usd'))
    if budget < 1:
        raise ValueError('Budget must be at least $0.01')
    region = clean_text(body.get('region', 'Any region'), 'Region')
    with DB_LOCK, connect() as db:
        cursor = db.execute('''INSERT INTO jobs(name,task_type,min_vram_gb,minutes,max_budget_cents,region,created_at)
                               VALUES(?,?,?,?,?,?,?)''', (name, task, vram, minutes, budget, region, time.time()))
        schedule(db)
        return cursor.lastrowid


def toggle_provider(provider_id):
    with DB_LOCK, connect() as db:
        row = db.execute('SELECT online FROM providers WHERE id=?', (provider_id,)).fetchone()
        if row is None:
            raise LookupError('Provider not found')
        online = not row['online']
        db.execute('UPDATE providers SET online=? WHERE id=?', (int(online), provider_id))
        if not online:
            db.execute('''UPDATE jobs SET status='queued', provider_id=NULL, cost_cents=NULL, started_at=NULL
                          WHERE status='running' AND provider_id=?''', (provider_id,))
        schedule(db)
        return online


def complete_job(job_id):
    with DB_LOCK, connect() as db:
        row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('Job not found')
        if row['status'] != 'running':
            raise ValueError('Only running demo jobs can be completed')
        db.execute("UPDATE jobs SET status='completed',completed_at=? WHERE id=?", (time.time(), job_id))
        schedule(db)


class Handler(BaseHTTPRequestHandler):
    server_version = 'SharedComputeDraft/0.1'

    def log_message(self, fmt, *args):
        print('[http]', fmt % args)

    def respond(self, status, obj):
        data = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/state':
            return self.respond(200, state())
        if path == '/api/health':
            return self.respond(200, {'ok': True, 'demo': True})
        assets = {'/': 'index.html', '/index.html': 'index.html', '/styles.css': 'styles.css', '/app.js': 'app.js'}
        name = assets.get(path)
        if name is None:
            return self.respond(404, {'error': 'Not found'})
        asset = ROOT / 'static' / name
        if not asset.is_file():
            return self.respond(404, {'error': 'Asset missing'})
        data = asset.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', {'html':'text/html', 'css':'text/css', 'js':'application/javascript'}[name.rsplit('.',1)[-1]] + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlsplit(self.path).path
        if not (path in ('/api/providers', '/api/jobs', '/api/reset') or re.fullmatch(r'/api/providers/\d+/toggle', path) or re.fullmatch(r'/api/jobs/\d+/complete', path)):
            return self.respond(404, {'error': 'Not found'})
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if length < 0 or length > 16384:
                return self.respond(413, {'error': 'Invalid request size'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Request must be an object')
            if path == '/api/providers':
                result = {'id': create_provider(body)}
            elif path == '/api/jobs':
                result = {'id': create_job(body)}
            elif path == '/api/reset':
                with DB_LOCK, connect() as db:
                    reset_db(db)
                result = {'reset': True}
            elif '/toggle' in path:
                result = {'online': toggle_provider(int(path.split('/')[3]))}
            else:
                complete_job(int(path.split('/')[3]))
                result = {'completed': True}
            self.respond(200, result)
        except LookupError as exc:
            self.respond(404, {'error': str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {'error': str(exc)})
        except Exception:
            self.respond(500, {'error': 'Unexpected server error; see server logs'})
            raise


if __name__ == '__main__':
    init_db()
    print(f'SharedCompute local demo → http://127.0.0.1:{PORT}')
    print('Simulated jobs/credits only. No GPU access, crypto transfers, or authentication.')
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
