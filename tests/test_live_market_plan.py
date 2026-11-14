"""Live marketplace path preserves evidence and never invents a buyable total."""
import unittest
from unittest.mock import patch
import tempfile
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.worker import Settings

from live_market_plan import research_and_plan


CARD = {'name': 'ピカチュウ', 'set': 'SV2a', 'number': '173/165',
        'language': 'Japanese', 'variant': 'AR', 'presentation': 'raw',
        'qualifiers': [], 'defects': [], 'uncertainties': []}
GOAL = {'source_mode': 'tavily_live', 'budget_jpy': 10000, 'reserve_jpy': 1000,
        'targets': [{'card': CARD, 'quantity': 1}]}
URL = 'https://jp.mercari.com/item/m12345678901'


class FakeTavily:
    def __init__(self, emit): self.emit = emit
    def search(self, query):
        self.emit('Tavily', 'search.completed', urls=[URL])
        return [URL]
    def extract(self, urls):
        self.emit('Tavily', 'extract.completed', returned=1)
        return [(URL, 'ピカチュウ 173/165 AR SV2a ¥4,200 送料込み 購入する')]


class FakeNosana:
    def __init__(self, emit): self.emit = emit
    def extract(self, text, url):
        return {'url': url, 'title': 'ピカチュウ 173/165 AR SV2a',
                'price': 4200, 'shipping_jpy': 0, 'condition': '新品、未使用',
                'availability': 'available', 'observed_at': '2026-11-14T12:00:00+09:00',
                'source_sha256': 'a' * 64,
                'evidence': {'title': 'ピカチュウ 173/165 AR SV2a', 'price': '¥4,200',
                             'shipping': '送料込み', 'availability': '購入する'}}


class LivePlanTests(unittest.TestCase):
    def test_tavily_evidence_reaches_python_planner_without_invented_fee(self):
        result = research_and_plan(GOAL, tavily_factory=FakeTavily, nosana_factory=FakeNosana)
        self.assertEqual(result['mode'], 'tavily_live')
        self.assertEqual(result['observations'][0]['source_url'], URL)
        self.assertEqual(result['observations'][0]['observed_at'], '2026-11-14T12:00:00+09:00')
        self.assertEqual(result['observations'][0]['evidence']['price'], '¥4,200')
        self.assertEqual(result['plan']['selected_ids'], [])
        self.assertIn('ACQUISITION_TOTAL_UNKNOWN', result['plan']['checked'][0]['reasons'])

    def test_existing_job_api_returns_source_backed_live_plan(self):
        result = research_and_plan(GOAL, tavily_factory=FakeTavily, nosana_factory=FakeNosana)
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {'TAVILY_API_KEY': 'test', 'NOSANA_API_KEY': 'test'}), patch('backend.worker.research_and_plan', return_value=result):
            with TestClient(create_app(Settings(data_dir=Path(temp)))) as client:
                csrf = client.get('/api/v1/session').json()['data']['csrf_token']
                response = client.post('/api/v1/jobs', json={'type': 'tavily_plan', 'input': {'goal': GOAL}},
                    headers={'X-CSRF-Token': csrf, 'Idempotency-Key': str(uuid.uuid4())})
                self.assertEqual(response.status_code, 202, response.text)
                key = response.json()['data']['job_id']
                for _ in range(100):
                    job = client.get('/api/v1/jobs/' + key).json()['data']
                    if job['state'] in {'succeeded', 'failed'}: break
                    time.sleep(.01)
                self.assertEqual(job['state'], 'succeeded', job)
                saved = client.get(job['result']['url']).json()['data']
                self.assertEqual(saved['mode'], 'tavily_live')
                self.assertEqual(saved['observations'][0]['source_url'], URL)

    def test_missing_keys_report_unavailable_without_fake_success(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {'TAVILY_API_KEY': '', 'NOSANA_API_KEY': ''}):
            with TestClient(create_app(Settings(data_dir=Path(temp)))) as client:
                csrf = client.get('/api/v1/session').json()['data']['csrf_token']
                response = client.post('/api/v1/jobs', json={'type': 'tavily_plan', 'input': {'goal': GOAL}},
                    headers={'X-CSRF-Token': csrf, 'Idempotency-Key': str(uuid.uuid4())})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['error']['code'], 'FEATURE_UNAVAILABLE')


if __name__ == '__main__': unittest.main()
