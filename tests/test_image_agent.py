"""Offline image-agent contract checks; no provider credentials or network calls."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cards import DEFAULT_GEMINI_MODEL, identify
from image_agent import recognize_card

ROOT = Path(__file__).resolve().parents[1]
CARD = json.loads((ROOT / 'examples/v2/owned-card.json').read_text())


class ImageAgentTests(unittest.TestCase):
    def candidate(self, card=None):
        return {'card': copy.deepcopy(card or CARD), 'image_sha256': ['a' * 64],
                'provider': 'Gemini', 'configured_model': DEFAULT_GEMINI_MODEL,
                'returned_model': DEFAULT_GEMINI_MODEL, 'request_id': 'test-only',
                'status': 'candidate; user confirmation required',
                'physical_authenticity': 'not established'}

    def test_candidate_keeps_existing_add_record_shape(self):
        with patch('image_agent.identify', return_value=self.candidate()) as call:
            result = recognize_card(['front.png'], model='chosen-model')
        call.assert_called_once_with(['front.png'], model='chosen-model')
        self.assertTrue(result['ok'])
        self.assertTrue(result['candidate_complete'])
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(result['card'], CARD)
        self.assertEqual(result['physical_authenticity'], 'not established')

    def test_unreadable_identity_remains_a_candidate(self):
        card = copy.deepcopy(CARD)
        card.update(number=None, uncertainties=['Collector number unreadable.'])
        with patch('image_agent.identify', return_value=self.candidate(card)):
            result = recognize_card(['front.png'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['candidate_complete'])
        self.assertIn('number', result['missing_fields'])
        self.assertEqual(result['card']['uncertainties'], card['uncertainties'])

    def test_slab_requires_grader_and_grade_for_complete_candidate(self):
        card = copy.deepcopy(CARD)
        card.update(presentation='slabbed')
        with patch('image_agent.identify', return_value=self.candidate(card)):
            result = recognize_card(['front.png'])
        self.assertFalse(result['candidate_complete'])
        self.assertIn('grader', result['missing_fields'])
        self.assertIn('grade_or_grade_text', result['missing_fields'])

    def test_provider_failures_are_safe_and_do_not_retry(self):
        for status, expected in [(503, 'PROVIDER_UNAVAILABLE'), (429, 'RATE_LIMITED'),
                                 (403, 'AUTHENTICATION_FAILED'), (404, 'MODEL_UNAVAILABLE'),
                                 (400, 'PROVIDER_REJECTED_REQUEST')]:
            error = RuntimeError('secret-key and private request body')
            error.code = status
            with patch('image_agent.identify', side_effect=error) as call:
                result = recognize_card(['front.png'])
            self.assertEqual(call.call_count, 1)
            self.assertFalse(result['ok'])
            self.assertNotIn('card', result)
            self.assertEqual(result['error']['code'], expected)
            self.assertEqual(result['error']['retryable'], status in (503, 429))
            self.assertNotIn('secret-key', json.dumps(result))

    def test_timeout_and_missing_key(self):
        for error, code in [(TimeoutError('secret'), 'PROVIDER_TIMEOUT'),
                            (ValueError('GEMINI_API_KEY is not configured.'), 'MISSING_API_KEY')]:
            with patch('image_agent.identify', side_effect=error):
                result = recognize_card(['front.png'])
            self.assertEqual(result['error']['code'], code)

    def test_cli_writes_failure_json_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'failure.json'
            env = os.environ.copy()
            env.pop('GEMINI_API_KEY', None)
            result = subprocess.run([sys.executable, str(ROOT / 'pokescout.py'), 'identify',
                                     'not-read.png', '--out', str(out)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(out.read_text())['error']['code'], 'MISSING_API_KEY')
            self.assertNotIn('Traceback', result.stderr)

    def sdk(self, response_text):
        response = types.SimpleNamespace(text=response_text, model_version='returned-model', response_id='test-id')
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.models.generate_content.return_value = response
        constructor = Mock(return_value=client)
        google = types.ModuleType('google')
        genai = types.ModuleType('google.genai')
        genai_types = types.ModuleType('google.genai.types')
        genai_types.Part = types.SimpleNamespace(from_bytes=lambda **kwargs: kwargs)
        genai.Client = constructor
        genai.types = genai_types
        google.genai = genai
        pil = types.ModuleType('PIL')
        image = Mock()
        image.__enter__ = Mock(return_value=image)
        image.__exit__ = Mock(return_value=False)
        image.format = 'PNG'
        pil.Image = types.SimpleNamespace(open=lambda path: image, MIME={'PNG': 'image/png'})
        modules = {'google': google, 'google.genai': genai, 'google.genai.types': genai_types, 'PIL': pil}
        return modules, constructor, client

    def invoke(self, response_text, model=None, env_model=None):
        modules, constructor, client = self.sdk(response_text)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'front.png'
            image.write_bytes(b'fixture-image')
            env = {'GEMINI_API_KEY': 'unit-test-only'}
            if env_model:
                env['GEMINI_MODEL'] = env_model
            with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
                result = identify([image], model=model)
        return result, constructor, client

    def test_model_selection_and_no_hidden_sdk_retries(self):
        for model, env_model, expected in [(None, None, DEFAULT_GEMINI_MODEL),
                                            (None, 'environment-model', 'environment-model'),
                                            ('explicit-model', 'environment-model', 'explicit-model')]:
            result, constructor, client = self.invoke(json.dumps(CARD), model, env_model)
            self.assertEqual(client.models.generate_content.call_args.kwargs['model'], expected)
            self.assertEqual(result['configured_model'], expected)
            self.assertEqual(result['returned_model'], 'returned-model')
            self.assertEqual(constructor.call_args.kwargs['http_options']['retry_options']['attempts'], 1)

    def test_response_validation_rejects_missing_fields_bad_json_and_raw_grade(self):
        missing = copy.deepcopy(CARD)
        del missing['language']
        raw_grade = dict(CARD, grade='10')
        for response in ['not JSON', json.dumps(missing), json.dumps(raw_grade), '[]']:
            with self.assertRaisesRegex(ValueError, 'Gemini response'):
                self.invoke(response)

    def test_oversized_image_stops_before_provider_call(self):
        modules, constructor, client = self.sdk(json.dumps(CARD))
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'oversized.png'
            with image.open('wb') as stream:
                stream.truncate(12_000_001)
            with patch.dict(sys.modules, modules), patch.dict(os.environ, {'GEMINI_API_KEY': 'unit-test-only'}):
                with self.assertRaisesRegex(ValueError, '12 MB'):
                    identify([image])
        constructor.assert_not_called()

    def test_invalid_image_container_stops_before_sdk(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'unit-test-only'}):
            for images in ['front.png', [], ['a'] * 5]:
                with self.assertRaisesRegex(ValueError, '1–4'):
                    identify(images)


if __name__ == '__main__':
    unittest.main()
