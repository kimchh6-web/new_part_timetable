"""Bounded HTTP contract checks, independent of the legacy fixture suite."""
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
from harness import run_harness
from harness.runtime import LocalScheduleExecutionRuntime


class LiveApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(Path('examples/primary_input.json').read_text(encoding='utf-8'))
        cls.payload['weekday'] = 'SAT'
        cls.local_result = run_harness(cls.payload, runtime=LocalScheduleExecutionRuntime())
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), web_demo.Handler)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/api/demo/schedule'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, payload=None, raw=None, content_type='application/json', origin=None):
        data = raw if raw is not None else json.dumps(self.payload if payload is None else payload).encode()
        req = Request(self.url, data=data, headers={'Content-Type': content_type})
        if origin:
            req.add_header('Origin', origin)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_success_envelope_and_badges_from_canonical_dataset(self):
        with patch.object(web_demo, 'run_harness', return_value=copy.deepcopy(self.local_result)):
            status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['source'], 'fallback')
        self.assertTrue(body['requestId'].startswith('req_'))
        self.assertTrue(body['generatedAt'].endswith('+09:00'))
        self.assertEqual(body['meta']['contractVersion'], 'demo.v1')
        self.assertGreaterEqual(body['meta']['totalLatencyMs'], 0)
        job = body['schedule'][1]
        rows = json.loads(Path('harness/fixtures/jobs.json').read_text(encoding='utf-8'))
        original = next(row for row in rows if row['id'] == job['job_id'])
        self.assertEqual(job['timeNegotiable'], original['scheduleFlexibility']['timeNegotiable'])
        self.assertEqual(job['minWeeks'], original['minWeeks'])
        self.assertEqual(job['benefits'], original['benefits'][:5])

    def test_invalid_json(self):
        status, body = self.request(raw=b'{')
        self.assertEqual((status, body['error']['code']), (400, 'VALIDATION_ERROR'))

    def test_missing_required_fields(self):
        with patch.object(web_demo, 'run_harness') as run:
            status, body = self.request(payload={})
            run.assert_not_called()
        self.assertEqual((status, body['error']['code']), (400, 'VALIDATION_ERROR'))

    def test_wrong_media_type(self):
        status, body = self.request(content_type='text/plain')
        self.assertEqual((status, body['error']['code']), (415, 'UNSUPPORTED_MEDIA_TYPE'))

    def test_configured_public_origin_allowed(self):
        origin = 'https://configured-demo.trycloudflare.com'
        with patch.object(web_demo, 'ALLOWED_ORIGINS', {origin}), patch.object(web_demo, 'run_harness', return_value=copy.deepcopy(self.local_result)):
            status, body = self.request(origin=origin)
        self.assertEqual(status, 200)

    def test_unknown_origin_rejected(self):
        with patch.object(web_demo, 'run_harness') as run:
            status, body = self.request(origin='https://unconfigured.example')
            run.assert_not_called()
        self.assertEqual((status, body['error']['code']), (403, 'ORIGIN_NOT_ALLOWED'))

    def test_nonfinite_json_rejected(self):
        status, body = self.request(raw=b'{"weekly_income_target":NaN}')
        self.assertEqual((status, body['error']['code']), (400, 'VALIDATION_ERROR'))

    def test_runtime_failure_does_not_leak_internal_error(self):
        with patch.object(web_demo, 'run_harness', side_effect=RuntimeError('secret-internal-value')):
            status, body = self.request()
        self.assertEqual((status, body['error']['code']), (503, 'DAYTONA_UNAVAILABLE'))
        self.assertNotIn('secret-internal-value', json.dumps(body))

    def test_empty_schedule_is_success(self):
        payload = dict(self.payload, allow_negotiable_proposals=False)
        result = run_harness(payload, runtime=LocalScheduleExecutionRuntime())
        with patch.object(web_demo, 'run_harness', return_value=result):
            status, body = self.request(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body['schedule'], [])
        self.assertEqual(body['meta']['jobs_loaded'], 600)

    def test_timeout_retains_lock_until_execution_ends(self):
        release = threading.Event()
        def blocked(*args, **kwargs):
            release.wait(2)
            return copy.deepcopy(self.local_result)
        try:
            with patch.object(web_demo, 'RESPONSE_TIMEOUT_SECONDS', .02), patch.object(web_demo, 'run_harness', side_effect=blocked):
                status, body = self.request()
                self.assertEqual((status, body['error']['code']), (504, 'TIMEOUT'))
                status, body = self.request()
                self.assertEqual((status, body['error']['code']), (503, 'BUSY'))
        finally:
            release.set()
            self.assertTrue(web_demo.LOCK.acquire(timeout=2))
            web_demo.LOCK.release()


if __name__ == '__main__':
    unittest.main()
