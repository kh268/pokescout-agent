"""The frontend's user-supplied plan boundary uses the real API and Python planner."""
import copy
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.worker import Settings

ROOT = Path(__file__).resolve().parents[1]


class FrontendPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Settings(data_dir=Path(self.temp.name))))
        self.client.__enter__()
        self.csrf = self.client.get('/api/v1/session').json()['data']['csrf_token']
        self.goal = json.loads((ROOT / 'examples/v2/goal.json').read_text())
        self.goal['source_mode'] = 'user_supplied_observations'
        self.rows = json.loads((ROOT / 'examples/v2/listings.json').read_text())
        for row in self.rows:
            row['source_mode'] = 'user_supplied'
            row['source_url'] = 'https://example.org/listing/' + row['id']
            row['observed_at'] = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def post(self, rows=None, key=None):
        return self.client.post('/api/v1/structured-plans', json={'goal': self.goal, 'listings': self.rows if rows is None else rows},
                                headers={'X-CSRF-Token': self.csrf, 'Idempotency-Key': key or str(uuid.uuid4())})

    def test_python_plan_persisted_with_rejections_and_unknown_shipping(self):
        key = str(uuid.uuid4())
        response = self.post(key=key)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()['data']
        self.assertEqual(data['plan']['selected_ids'], ['A2', 'B1'])
        self.assertEqual(data['plan']['cost_jpy'], 8900)
        rejected = {row['id']: row['reasons'] for row in data['plan']['checked']}
        self.assertIn('WRONG_PRINTING_LANGUAGE_OR_VARIANT', rejected['A3'])
        self.assertIn('ACQUISITION_TOTAL_UNKNOWN', rejected['B2'])
        self.assertEqual(self.post(key=key).json()['data']['plan_id'], data['plan_id'])
        self.assertEqual(self.client.get('/api/v1/structured-plans/' + data['plan_id']).json()['data'], data)

    def test_fixture_scheme_and_missing_timestamp_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['source_url'] = 'fixture://A1'
        self.assertEqual(self.post(rows).status_code, 422)
        rows[0]['source_url'] = 'https://example.org/A1'
        rows[0]['observed_at'] = None
        self.assertEqual(self.post(rows).status_code, 422)

    def test_stale_offer_stays_visible_but_cannot_be_selected(self):
        rows = copy.deepcopy(self.rows)
        rows[1]['observed_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        data = self.post(rows).json()['data']['plan']
        self.assertNotIn('A2', data['selected_ids'])
        stale = next(row for row in data['checked'] if row['id'] == 'A2')
        self.assertIn('STALE_OBSERVATION', stale['reasons'])

    def test_demo_mode_is_explicit_and_uses_python_planner(self):
        self.goal['source_mode'] = 'synthetic_frontend_demo'
        rows = copy.deepcopy(self.rows)
        for row in rows:
            row['source_mode'] = 'synthetic_frontend_demo'
            row['source_url'] = 'demo://offer/' + row['id']
        response = self.post(rows)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['data']['mode'], 'synthetic_demo')
        self.assertEqual(response.json()['data']['plan']['cost_jpy'], 8900)
        rows[0]['source_url'] = 'https://example.org/A1'
        self.assertEqual(self.post(rows).status_code, 422)

    def test_frontend_wishlist_budget_demo_flow(self):
        card = {'name': 'ピカチュウ', 'set': 'SV2a', 'number': '173/165',
                'language': 'Japanese', 'variant': 'AR', 'presentation': 'raw',
                'grader': None, 'grade': None, 'grade_text': None, 'cert_number': None,
                'qualifiers': [], 'defects': [], 'uncertainties': []}
        self.goal = {'source_mode': 'synthetic_frontend_demo', 'budget_jpy': 5000,
                     'reserve_jpy': 1000, 'avoid_owned': False,
                     'allowed_conditions': ['near_mint', 'unused'],
                     'targets': [{'card': card, 'quantity': 1}]}
        stamp = datetime.now(timezone.utc).isoformat()
        self.rows = [dict(id='SV2a-173-a', card=card, price_jpy=3900,
                          shipping_jpy=0, purchase_fee_jpy=0, condition='near_mint',
                          availability='available', quantity=1,
                          source_url='demo://offer/SV2a-173-a', observed_at=stamp,
                          source_mode='synthetic_frontend_demo'),
                     dict(id='SV2a-173-c', card={**card, 'variant': 'OTHER_PRINTING'},
                          price_jpy=2000, shipping_jpy=0, purchase_fee_jpy=0,
                          condition='near_mint', availability='available', quantity=1,
                          source_url='demo://offer/SV2a-173-c', observed_at=stamp,
                          source_mode='synthetic_frontend_demo')]
        result = self.post().json()['data']['plan']
        self.assertEqual(result['selected_ids'], ['SV2a-173-a'])
        self.assertEqual(result['cost_jpy'], 3900)
        self.goal['reserve_jpy'] = 1500
        self.assertEqual(self.post().json()['data']['plan']['selected_ids'], [])


if __name__ == '__main__':
    unittest.main()
