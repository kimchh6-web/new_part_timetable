"""Weekly HTTP boundary checks against the canonical dataset, without network SDK.

The semantic layer is exercised here through the handler, not around it: the
provider is configured and its transport is mocked, so what these tests prove is
that a request to ``/api/recommendations`` really does reach the enrichment and
really does come back enriched — and that when inference fails, the same request
still answers 200 with the three deterministic plans.

Every test states what it wants of the LLM layer. None of them may depend on
whether this machine happens to carry ``NOSANA_API_KEY`` in its environment.
"""
import contextlib
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
from harness.weekly import semantic
from harness.weekly.semantic import SemanticConfig, SemanticError

TEST_CONFIG = SemanticConfig(
    "nosana", "test-key-not-a-real-secret", "https://inference.example.invalid/v1", "test/model-1"
)


@contextlib.contextmanager
def llm_disabled():
    """No provider configured, whatever this machine's environment says."""
    with patch.object(semantic, "load_provider_config", return_value=None):
        yield


@contextlib.contextmanager
def llm_configured(invoke):
    """A configured provider whose one HTTP call is ``invoke``."""
    with patch.object(semantic, "load_provider_config", return_value=TEST_CONFIG):
        with patch.object(semantic, "_http_invoke", side_effect=invoke) as call:
            yield call


def selector(scores):
    """Answer the prompt the handler actually sent, reading ids out of it.

    Building the reply from the prompt is deliberate: if the handler never sent
    the evidence bank, this cannot produce a valid answer at all.
    """

    def invoke(config, messages, deadline, clock=None):
        body = json.loads(messages[-1]["content"])
        entries = [
            {
                "planId": plan["planId"],
                "fitScore": scores[index],
                "reasonEvidenceIds": [plan["evidence"][1]["id"]],
            }
            for index, plan in enumerate(body["plans"])
        ]
        return json.dumps({"plans": entries}, ensure_ascii=False), "test/model-1"

    return invoke


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

    def execute(self, payload):
        # Explicit test seam; this does not claim to prove Daytona execution.
        result = build_weekly_recommendations(payload, self.rows)
        result['meta']['runtime_provider'] = 'local_test'
        return result

    def deterministic_body(self):
        return build_weekly_recommendations(copy.deepcopy(self.payload), self.rows)

    def test_canonical_plans_and_fallback_envelope(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=self.execute) as run:
            with llm_disabled():
                status, body = self.request()
        self.assertEqual(status, 200)
        run.assert_called_once()
        self.assertEqual(body['source'], 'fallback')
        self.assertTrue(body['requestId'].startswith('req_'))
        self.assertTrue(body['generatedAt'].endswith('+09:00'))
        self.assertEqual(body['meta']['contractVersion'], 'weekly.v1')
        self.assertEqual(body['meta']['llmStatus'], 'not_configured')
        self.assertIs(body['meta']['llmUsed'], False)
        self.assertEqual(body['meta']['engine'], 'deterministic')
        self.assertTrue(body['availableSlots'])
        self.assertGreater(body['candidateCount'], 0)
        self.assertTrue(body['plans'])
        self.assertNotIn('schedule', body)  # weekly contract, not daily alias
        for plan in body['plans']:
            for job in plan['jobs']:
                self.assertTrue(job['assignedShifts'])
                self.assertIn('departAt', job['assignedShifts'][0]['travel'])

    def test_invalid_request_rejected_before_remote_execution(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly') as run, llm_disabled():
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

    def test_handler_enriches_the_weekly_answer_with_a_verified_ordering(self):
        plain = self.deterministic_body()
        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=self.execute):
            with llm_configured(selector([0.4, 0.9, 0.6])) as call:
                status, body = self.request()

        self.assertEqual(status, 200)
        call.assert_called_once()
        self.assertEqual(body['source'], 'llm')
        meta = body['meta']
        self.assertEqual(meta['engine'], 'hybrid')
        self.assertIs(meta['llmUsed'], True)
        self.assertEqual(meta['llmProvider'], 'nosana')
        self.assertEqual(meta['llmModel'], 'test/model-1')
        self.assertEqual(meta['llmStatus'], 'success')
        self.assertIsInstance(meta['llmLatencyMs'], int)
        self.assertEqual(meta['contractVersion'], 'weekly.v1')

        plain_ids = [plan['id'] for plan in plain['plans']]
        self.assertEqual([plan['id'] for plan in body['plans']], plain_ids[1:] + plain_ids[:1])
        self.assertEqual([plan['fitScore'] for plan in body['plans']], [0.9, 0.6, 0.4])

        # Everything except order, reason and fitScore is the deterministic answer.
        by_id = {plan['id']: plan for plan in plain['plans']}
        for plan in body['plans']:
            original = by_id[plan['id']]
            for key in ('hash', 'type', 'label', 'metrics', 'jobs', 'warnings'):
                self.assertEqual(plan[key], original[key], key)
            self.assertTrue(plan['reason'])
        self.assertEqual(body['candidateCount'], plain['candidateCount'])
        self.assertEqual(body['availableSlots'], plain['availableSlots'])

    def test_inference_failure_still_answers_200_with_the_deterministic_plans(self):
        plain = self.deterministic_body()

        def broken(config, messages, deadline, clock=None):
            raise SemanticError('provider_error', 'HTTP 503 from node-xyz')

        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=self.execute):
            with llm_configured(broken):
                status, body = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(body['source'], 'fallback')
        self.assertEqual(body['meta']['llmStatus'], 'provider_error')
        self.assertIs(body['meta']['llmUsed'], False)
        self.assertEqual(body['meta']['engine'], 'deterministic')
        self.assertNotIn('node-xyz', json.dumps(body))
        self.assertEqual(len(body['plans']), 3)
        self.assertEqual(
            [plan['id'] for plan in body['plans']], [plan['id'] for plan in plain['plans']]
        )
        for plan, original in zip(body['plans'], plain['plans']):
            self.assertEqual(plan['reason'], original['reason'])
            self.assertNotIn('fitScore', plan)

    def test_unverifiable_model_answer_is_never_rendered(self):
        def invents(config, messages, deadline, clock=None):
            body = json.loads(messages[-1]['content'])
            entries = [
                {
                    'planId': plan['planId'],
                    'fitScore': 0.9,
                    'reasonEvidenceIds': ['당신의 경력과 잘 맞는 조합입니다'],
                }
                for plan in body['plans']
            ]
            return json.dumps({'plans': entries}, ensure_ascii=False), 'test/model-1'

        with patch.object(web_demo.RUNTIME, 'execute_weekly', side_effect=self.execute):
            with llm_configured(invents):
                status, body = self.request()

        self.assertEqual(status, 200)
        self.assertEqual(body['meta']['llmStatus'], 'invalid_response')
        # '바리스타' itself is a real job title here; what must not survive is
        # the sentence the model made up.
        self.assertNotIn('경력과 잘 맞는', json.dumps(body, ensure_ascii=False))

    def test_the_daily_path_never_reaches_the_weekly_layer(self):
        daily = json.loads(Path('examples/primary_input.json').read_text(encoding='utf-8'))
        with patch.object(semantic, '_http_invoke') as call:
            with patch.object(web_demo, 'run_harness', return_value={'meta': {}, 'schedule': []}) as run:
                request = Request(self.base + '/api/demo/schedule',
                                  data=json.dumps(daily).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    body = json.load(response)
        run.assert_called_once()
        call.assert_not_called()
        self.assertEqual(body['source'], 'fallback')
        self.assertEqual(body['meta']['contractVersion'], 'demo.v1')
        self.assertNotIn('llmStatus', body['meta'])

    def test_health_check_is_lightweight(self):
        with patch.object(web_demo.RUNTIME, 'execute_weekly') as run:
            with urlopen(self.base + '/healthz') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {'status': 'ok'})
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
