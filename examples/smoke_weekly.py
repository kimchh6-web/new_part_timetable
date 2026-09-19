"""Exercise the running web app, including real Daytona execution.

Usage: python examples/smoke_weekly.py --url http://127.0.0.1:5191
Only the checked-in synthetic demo request is submitted. No API key required.
"""
import argparse
import copy
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:5191')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    payload = json.loads((root / 'examples/weekly_input.json').read_text(encoding='utf-8'))

    def post(value):
        req = Request(args.url.rstrip('/') + '/api/recommendations',
                      data=json.dumps(value, ensure_ascii=False).encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=40)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    started = time.perf_counter()
    status, result = post(payload)
    assert status == 200, (status, result)
    assert result['meta']['runtime_provider'] == 'daytona', result['meta']
    assert result['meta']['execution_ok'] is True
    assert result['meta']['jobs_loaded'] == 600
    assert result['source'] == 'fallback'
    assert 1 <= len(result['plans']) <= 3
    for plan in result['plans']:
        assert len(plan['jobs']) == payload['search']['jobCount']
        assert abs(plan['metrics']['monthlyIncome'] - sum(job['weeklyPay'] for job in plan['jobs']) * 4.3) <= 1
        for job in plan['jobs']:
            assert job['assignedShifts']
            assert all(shift['travel']['departAt'] <= shift['start'] for shift in job['assignedShifts'])
    artifact = root / '.runtime/weekly-smoke-response.json'
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"PASS weekly HTTP 200; Daytona; 600 jobs; {len(result['plans'])} plans; {time.perf_counter()-started:.2f}s")

    regenerated = copy.deepcopy(payload)
    pinned = result['plans'][0]['jobs'][0]['jobId']
    regenerated['regenerate'] = {'pinnedJobIds': [pinned], 'excludedJobIds': [], 'previousPlanHashes': []}
    status, body = post(regenerated)
    assert status == 200, (status, body)
    assert all(pinned in [job['jobId'] for job in plan['jobs']] for plan in body['plans'])
    print('PASS regeneration preserves pinned job')

    status, body = post({})
    assert (status, body['error']['code']) == (400, 'VALIDATION_ERROR')
    print('PASS malformed request returns structured 400')
    print('Response saved:', artifact)


if __name__ == '__main__':
    main()
