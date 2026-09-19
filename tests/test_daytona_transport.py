"""Transport proof and idle-recovery checks; fake SDK, no network."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from harness.runtime.daytona import DaytonaScheduleExecutionRuntime, DaytonaRuntimeError
from harness.runtime._remote_entry import BEGIN, END
from harness.weekly import WeeklyValidationError


class DaytonaTransportTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = holder.name

    def runtime(self, payload, exit_code=0):
        remote = SimpleNamespace(
            id='test-sandbox', state='started',
            get_user_root_dir=lambda: '/home/daytona',
            fs=SimpleNamespace(upload_files=Mock()),
            process=SimpleNamespace(exec=Mock(return_value=SimpleNamespace(
                result=BEGIN + '\n' + json.dumps(payload) + '\n' + END,
                exit_code=exit_code))),
        )
        client = SimpleNamespace(get=Mock(return_value=remote))
        runtime = DaytonaScheduleExecutionRuntime(client=client)
        runtime._sandbox = remote
        runtime._uploaded = True
        runtime._python = '/usr/bin/python3'
        return runtime, remote, client

    def test_weekly_request_loads_dataset_remotely_and_returns_proof(self):
        runtime, sandbox, _ = self.runtime({
            'execution_ok': True, 'result': {'plans': [], 'meta': {}},
            'meta': {'jobs_loaded': 600}, 'probe': {'platform': 'Linux'},
        })
        result = runtime.execute_weekly({'profile': {'role': '기타'}})
        upload = sandbox.fs.upload_files.call_args.args[0][0]
        request = json.loads(upload.source)
        self.assertEqual(request['mode'], 'weekly')
        self.assertIsNone(request['jobs'])  # canonical file is read in sandbox
        self.assertEqual(result['meta']['jobs_loaded'], 600)
        self.assertEqual(result['meta']['runtime_provider'], 'daytona')
        self.assertEqual(result['meta']['execution_proof']['exit_code'], 0)
        command = sandbox.process.exec.call_args.args[0]
        self.assertIn('-m harness.runtime._remote_entry request-', command)

    def test_nonzero_exit_cannot_claim_daytona_success(self):
        runtime, _, _ = self.runtime({'execution_ok': True}, exit_code=1)
        with self.assertRaises(DaytonaRuntimeError):
            runtime.execute_weekly({})

    def test_stopped_sandbox_state_is_refreshed_before_next_request(self):
        runtime, old, client = self.runtime({'execution_ok': True})
        fresh = SimpleNamespace(id=old.id, state='stopped', start=Mock())
        client.get.return_value = fresh
        self.assertIs(runtime._acquire_sandbox(), fresh)
        fresh.start.assert_called_once()

    def test_deleted_sandbox_is_replaced_instead_of_failing_every_later_call(self):
        runtime, _, client = self.runtime({'execution_ok': True})
        client.get.side_effect = RuntimeError('404 sandbox not found')
        replacement = SimpleNamespace(id='new-sandbox', state='started')
        client.create = Mock(return_value=replacement)
        runtime._cache_path = Path(self.tmp) / 'daytona-sandbox.json'

        self.assertIs(runtime._acquire_sandbox(), replacement)
        # The package lives on the old filesystem, so it must be re-uploaded.
        self.assertFalse(runtime._uploaded)
        self.assertEqual(runtime._python, '')
        self.assertEqual(
            json.loads(runtime._cache_path.read_text(encoding='utf-8'))['sandbox_id'],
            'new-sandbox',
        )

    def test_remote_refusal_is_re_raised_as_the_same_domain_error(self):
        runtime, _, _ = self.runtime({
            'execution_ok': True,
            'domain_error': {
                'code': 'NO_CANDIDATES', 'status': 422,
                'message': '조건에 맞는 공고를 찾지 못했습니다.',
                'details': {'filteredFrom': 600, 'candidateCount': 0},
            },
            'meta': {'jobs_loaded': 600}, 'probe': {},
        })
        with self.assertRaises(WeeklyValidationError) as caught:
            runtime.execute_weekly({'profile': {}})
        error = caught.exception
        self.assertEqual((error.code, error.status), ('NO_CANDIDATES', 422))
        self.assertEqual(error.details['filteredFrom'], 600)
        # the run itself really happened, so the proof is still recorded
        self.assertEqual(runtime.last_proof['exit_code'], 0)

    def test_reply_with_neither_result_nor_refusal_is_a_transport_failure(self):
        runtime, _, _ = self.runtime({'execution_ok': True, 'meta': {}, 'probe': {}})
        with self.assertRaises(DaytonaRuntimeError):
            runtime.execute_weekly({'profile': {}})

    def test_injected_rows_are_shipped_once(self):
        runtime, sandbox, _ = self.runtime({
            'execution_ok': True, 'candidates': [], 'meta': {}, 'probe': {},
        })
        rows = [{'id': 'job_x'}]
        runtime.execute({'weekday': 'SAT'}, rows)
        request = json.loads(sandbox.fs.upload_files.call_args.args[0][0].source)
        self.assertEqual(request['jobs'], rows)
        self.assertNotIn('mock_jobs', request)
        self.assertEqual(request['mode'], 'daily')


if __name__ == '__main__':
    unittest.main()
