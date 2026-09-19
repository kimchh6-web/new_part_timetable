"""Transport proof and idle-recovery checks; fake SDK, no network."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from harness.runtime.daytona import DaytonaScheduleExecutionRuntime, DaytonaRuntimeError
from harness.runtime._remote_entry import BEGIN, END


class DaytonaTransportTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
