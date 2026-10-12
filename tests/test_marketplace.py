import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.temp.name) / 'test.sqlite3'
        server.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **overrides):
        base = {'name': 'Test batch', 'task_type': 'image', 'min_vram_gb': 12,
                'minutes': 60, 'max_budget_usd': '2.00', 'region': 'Any region'}
        base.update(overrides)
        return server.create_job(base)

    def test_seed_inventory(self):
        data = server.state()
        self.assertEqual(data['totals']['total_providers'], 4)
        self.assertEqual(data['totals']['available_providers'], 4)
        self.assertTrue(all(p['sample'] for p in data['providers']))

    def test_cheapest_gpu_selected_and_paid_only_on_completion(self):
        self.job(min_vram_gb=24)
        data = server.state()
        job = data['jobs'][0]
        self.assertEqual(job['provider_gpu'], 'RTX 3090')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['cost_cents'], 44)
        self.assertEqual(data['totals']['gross_cents'], 0)
        server.complete_job(job['id'])
        total = server.state()['totals']
        self.assertEqual(total['gross_cents'], 44)
        self.assertEqual(total['provider_cents'], 40)
        self.assertEqual(total['platform_cents'], 4)
        with self.assertRaisesRegex(ValueError, 'Only running'):
            server.complete_job(job['id'])
        self.assertEqual(server.state()['totals']['gross_cents'], 44)

    def test_queue_then_activate_when_affordable_provider_joins(self):
        job_id = self.job(min_vram_gb=48, max_budget_usd='0.30')
        self.assertEqual(server.state()['jobs'][0]['status'], 'queued')
        server.create_provider({'name': 'Founder PC', 'gpu': 'RTX Pro 48GB', 'vram_gb': 48,
                                'region': 'Sydney, AU', 'hourly_usd': '0.25'})
        job = server.state()['jobs'][0]
        self.assertEqual(job['id'], job_id)
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['provider_name'], 'Founder PC')

    def test_offline_requeues_region_bound_job(self):
        self.job(region='Frankfurt, DE', min_vram_gb=24)
        job = server.state()['jobs'][0]
        self.assertEqual(job['provider_gpu'], 'RTX 4090')
        self.assertFalse(server.toggle_provider(job['provider_id']))
        self.assertEqual(server.state()['jobs'][0]['status'], 'queued')
        self.assertTrue(server.toggle_provider(job['provider_id']))
        self.assertEqual(server.state()['jobs'][0]['status'], 'running')

    def test_busy_gpu_cannot_take_second_job(self):
        self.job(region='Sydney, AU')
        self.job(region='Sydney, AU')
        data = server.state()['jobs']
        self.assertEqual(sorted(x['status'] for x in data), ['queued', 'running'])
        active = next(x for x in data if x['status'] == 'running')
        server.complete_job(active['id'])
        new = server.state()['jobs']
        self.assertEqual(sorted(x['status'] for x in new), ['completed', 'running'])

    def test_invalid_requests(self):
        with self.assertRaises(ValueError):
            self.job(max_budget_usd='-1')
        with self.assertRaises(ValueError):
            self.job(task_type='execute_arbitrary_shell')
        with self.assertRaises(ValueError):
            self.job(minutes=0)
        with self.assertRaises(ValueError):
            server.create_provider({'name': '<script>alert(1)</script>', 'gpu': 'RTX',
                                   'vram_gb': 0, 'region': 'Sydney, AU', 'hourly_usd': '1'})


if __name__ == '__main__':
    unittest.main()
