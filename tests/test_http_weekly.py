"""Weekly HTTP boundary checks against the canonical dataset, without network SDK."""
import copy
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import web_demo
from harness.weekly import build_weekly_recommendations, WeeklyValidationError


class WeeklyHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(Path('examples/weekly_input.json').read_text(encoding='utf-8'))
        cls.rows = json.loads(Path('harness/fixtures/jobs.json').read_text(encoding='utf-8'))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), web_demo.Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, payload=None):
        req = Request(self.base + '/api/recommendations',
                      data=json.dumps(self.payload if payload is None else payload).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_canonical_plans_and_fallback_envelope(self):
        def execute(payload):
            # Explicit test seam; this does not claim to prove Daytona execution.
            result = build_weekly_recommendations(payload, self.rows)
            result['meta']['runtime_provider'] = 'local_test'
            return result
        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=execute) as run:
            status, body = self.request()
        self.assertEqual(status, 200)
        run.assert_called_once()
        self.assertEqual(body['source'], 'fallback')
        self.assertTrue(body['requestId'].startswith('req_'))
        self.assertTrue(body['generatedAt'].endswith('+09:00'))
        self.assertEqual(body['meta']['contractVersion'], 'weekly.v1')
        self.assertTrue(body['availableSlots'])
        self.assertGreater(body['candidateCount'], 0)
        self.assertTrue(body['plans'])
        self.assertNotIn('schedule', body)  # weekly contract, not daily alias
        for plan in body['plans']:
            for job in plan['jobs']:
                self.assertTrue(job['assignedShifts'])
                self.assertIn('departAt', job['assignedShifts'][0]['travel'])

    def test_invalid_request_rejected_before_remote_execution(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly') as run:
            status, body = self.request({})
        run.assert_not_called()
        self.assertEqual((status, body['error']['code']), (400, 'VALIDATION_ERROR'))

    def test_remote_no_candidates_keeps_stage_counts(self):
        error = WeeklyValidationError('NO_CANDIDATES', '조건에 맞는 공고가 없습니다.',
                                      status=422, details={'stageCounts': {'loaded': 600, 'final': 0}})
        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=error):
            status, body = self.request()
        self.assertEqual((status, body['error']['code']), (422, 'NO_CANDIDATES'))
        self.assertEqual(body['error']['details']['stageCounts']['loaded'], 600)

    def test_runtime_outage_not_mislabelled_as_local_success(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=RuntimeError('private-key-marker')):
            status, body = self.request()
        self.assertEqual((status, body['error']['code']), (503, 'DAYTONA_UNAVAILABLE'))
        self.assertNotIn('private-key-marker', json.dumps(body))

    def test_health_check_is_lightweight(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly') as run:
            with urlopen(self.base + '/healthz') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {'status': 'ok'})
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
