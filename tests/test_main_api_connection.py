"""API-to-main-agent boundary tests without provider calls."""
import unittest

import test_api
from test_api import CARD, research


class MainConnectionTests(unittest.TestCase):
    setUp = test_api.APITests.setUp
    tearDown = test_api.APITests.tearDown
    headers = test_api.APITests.headers
    post = test_api.APITests.post
    wait = test_api.APITests.wait

    def test_real_decision_path_calls_teammate_planner(self):
        self.settings.fixture_listings = []
        response = self.post('/jobs', {'type': 'decision', 'input': {'goal': {
            'intent': 'buy', 'message': 'Research this card', 'budget_jpy': 5000,
            'allowed_conditions': ['near_mint'], 'targets': [{'card': CARD, 'quantity': 1}]}}})
        self.assertEqual(response.status_code, 202, response.text)
        done = self.wait(response.json()['data'])
        self.assertEqual(done['state'], 'succeeded', done)
        decision = self.client.get(done['result']['url']).json()['data']
        self.assertEqual(decision['rule_version'], 'main-agent-1.0/planner-0.2')
        self.assertEqual(decision['recommendation'], 'insufficient_evidence')
        self.assertEqual(len(decision['research_refs']), 1)
        self.assertEqual(decision['calculations']['purchase_total_jpy'], 0)
        events = self.client.get('/api/v1/activity', params={'job_id': done['job_id']}).json()['data']['items']
        self.assertEqual(events[-1]['type'], 'job.succeeded')

    def test_failed_research_retains_report_and_fails_job(self):
        async def failed(request):
            report = await research(request)
            report['status'] = 'failed'
            return report
        self.settings.research = failed
        response = self.post('/jobs', {'type': 'research', 'input': {'request': {
            'confirmed_target': {'name_ja': 'ミュウツーV', 'number': '273/S-P'}}}})
        done = self.wait(response.json()['data'])
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['error']['code'], 'RESEARCH_FAILED')
        with self.app.state.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM api_records WHERE kind='research_report'").fetchone()[0], 1)
