"""API-to-main-agent boundary tests without provider calls."""
import unittest

import test_api
from test_api import CARD, LISTINGS, research
from backend.store import now
from fastapi.testclient import TestClient


class MainConnectionTests(unittest.TestCase):
    setUp = test_api.APITests.setUp
    tearDown = test_api.APITests.tearDown
    headers = test_api.APITests.headers
    post = test_api.APITests.post
    wait = test_api.APITests.wait
    upload = test_api.APITests.upload
    candidate = test_api.APITests.candidate

    def test_protected_sale_clarifies_then_resumes(self):
        self.settings.fixture_listings = []
        candidate = self.candidate()
        copy = self.post('/copies/confirm', {'candidate_id': candidate['candidate_id'],
            'confirmed_card': CARD, 'confirm_owned': True, 'allow_sale': False}).json()['data']
        job = self.post('/jobs', {'type': 'decision', 'input': {'goal': {
            'intent': 'sell', 'message': 'Sell this copy', 'copy_ids': [copy['id']]}}}).json()['data']
        waiting = self.wait(job)
        self.assertEqual(waiting['state'], 'awaiting_input', waiting)
        self.assertEqual(waiting['questions'][0]['field'], 'copy_id')
        enabled = self.client.patch('/api/v1/copies/' + copy['id'],
            json={'allow_sale': True}, headers=self.headers(rev=copy['revision']))
        self.assertEqual(enabled.status_code, 200, enabled.text)
        resumed = self.post('/jobs/' + job['job_id'] + '/answers', {'answers': [
            {'question_id': waiting['questions'][0]['question_id'], 'value': copy['id']}]}, rev=waiting['revision'])
        self.assertEqual(resumed.status_code, 202, resumed.text)
        done = self.wait(resumed.json()['data'])
        self.assertEqual(done['state'], 'succeeded', done)
        result = self.client.get(done['result']['url']).json()['data']
        self.assertEqual(result['sale_candidates'][0]['copy_id'], copy['id'])

    def test_listing_confirmation_connects_evidence_to_decision(self):
        self.settings.fixture_listings = []
        card = LISTINGS[0]['card']
        async def observed(request):
            import hashlib
            report = await research(request)
            report['started_at'] = report['completed_at'] = now()
            report['listings'] = [{'listing_id': 's1', 'url': 'https://jp.mercari.com/item/m12345678901',
                'title': card['name'], 'price': 1000, 'shipping': 0, 'currency': 'JPY',
                'condition': 'near_mint', 'availability': 'available', 'verification': 'verified_listing',
                'observed_at': now(), 'evidence': [{'kind': 'direct_page',
                    'source_url': 'https://jp.mercari.com/item/m12345678901', 'observed_at': now(),
                    'content': 'Scripted provider test: 1000 JPY, near_mint, available.',
                    'sha256': hashlib.sha256(b'Scripted provider test: 1000 JPY, near_mint, available.').hexdigest()}],
                'field_evidence': {'price': '1000 JPY', 'availability': 'available'}}]
            return report
        self.settings.research = observed
        response = self.post('/jobs', {'type': 'research', 'input': {'request': {
            'confirmed_target': {'name_ja': card['name'], 'number': card['number']}}}})
        done = self.wait(response.json()['data'])
        report = self.client.get(done['result']['url']).json()['data']
        decision_input = {'type': 'decision', 'input': {'goal': {'intent': 'buy', 'message': 'Buy this printing',
            'budget_jpy': 5000, 'allowed_conditions': ['near_mint'], 'targets': [{'card': card, 'quantity': 1}]},
            'research_run_ids': [report['run_id']]}}
        first = self.wait(self.post('/jobs', decision_input).json()['data'])
        self.assertEqual(self.client.get(first['result']['url']).json()['data']['recommendation'], 'insufficient_evidence')
        path = '/research-reports/' + report['run_id'] + '/listing-confirmations'
        confirmation = {'listing_id': 's1', 'observed_at': report['listings'][0]['observed_at'], 'confirmed': True,
            'facts': {'card': card, 'quantity': 1, 'purchase_fee_jpy': 0}}
        with TestClient(self.app) as stranger:
            csrf = stranger.get('/api/v1/session').json()['data']['csrf_token']
            import uuid
            denied = stranger.post('/api/v1' + path, json=confirmation,
                headers={'X-CSRF-Token': csrf, 'Idempotency-Key': str(uuid.uuid4())})
            self.assertEqual(denied.status_code, 404)
        confirmed = self.post(path, confirmation)
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        confirmation['facts']['purchase_fee_jpy'] = 1
        self.assertEqual(self.post(path, confirmation).status_code, 409)
        final = self.wait(self.post('/jobs', decision_input).json()['data'])
        self.assertEqual(final['state'], 'succeeded', final)
        result = self.client.get(final['result']['url']).json()['data']
        self.assertEqual(result['recommendation'], 'buy', result)
        self.assertEqual(result['calculations']['purchase_total_jpy'], 1000)

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
