# Scheduler Harness — canonical demo dataset

Harness implementation living beside the demo web app; the original repository README is untouched.

## Dataset

`harness/fixtures/jobs.json` is a byte-for-byte copy of the supplied `C:\Users\user\Downloads\jobs (1).json`. It contains 600 synthetic postings from six platforms, not live vacancies. SHA-256: `47c871b33e3a1597bb18c38a08d7d085303e466e5fab3bd2d59f2321e91ca0db`.

All 600 records were inspected: 546 recruiting, 31 closed, 23 paused; seven categories; 1,361 weekday shifts, including 109 overnight shifts. Top-level schema is consistent with no null/missing fields. Nested `payDetail.payDay` is null in 293 rows; some contact links are null. `hourlyWage` is numeric (10,390–19,700 KRW); `dailyPay` is a boolean, never an amount. Qualification licenses, general requirements, and preferences remain distinct. Rich fields and original structured shifts are preserved during normalization.

## Run

For the verified three-case live pitch, run `python pitch_demo.py`. See
[the 3-minute presentation guide](examples/PITCH.md). It uses real Daytona
execution for all three cases and saves full responses under `.runtime/pitch/`.
`pitch_demo.py` is the daily-schedule CLI demo. The weekly HTTP contract
(`POST /api/recommendations`, [weekly.v1](examples/WEEKLY_API.md)) is implemented
as well and runs through the same Daytona sandbox; `python examples/smoke_weekly.py`
exercises it against a running server. The unit suite passes as a whole under
`python -m unittest discover -s tests` (pytest needs `tests/` on `sys.path`
because `test_e2e.py` imports `support` as a top-level module).

Python 3.10+:

```bash
python -m pip install -r requirements.txt
# Set DAYTONA_API_KEY in the process environment.
python demo.py
python demo.py --json
python -m unittest discover -s tests -v
python examples/inspect_dataset.py
```

The default demo executes actual planning in Daytona. `--local` is explicit development mode and reports `runtime_provider: local`; a Daytona failure never silently falls back to local. Optional environment settings are documented in `.env.example`, which is not auto-loaded.

## Backend contract

The live HTTP contract and error/timeout behavior are documented in
[api-contract.md](api-contract.md). Sponsor Q&A grounded in this implementation:
[Daytona pitch defense](examples/DAYTONA_PITCH_DEFENSE.md).

```python
from harness import run_harness

payload = {
    "start_location": "서울 강남",
    "home_location": "서울 용산",
    "availability": {"start": "14:00", "end": "20:00"},
    "weekday": "MON",
    "allow_negotiable_proposals": True,
    "travel_preferences": ["퇴근 경로 인근"],
    "weekly_income_target": 250000,
    "skills": ["POS 경험 6개월", "보건증"],
    "preferred_jobs": ["의류 행사", "매장 정리"],
    "avoid_jobs": ["설거지", "주방 보조"],
}
plan = run_harness(payload)  # Synchronous; returns a JSON-serializable dict.
```

The output has `schedule` (travel/job/travel), `summary`, `recommendation`, and `meta`. Job blocks include the source ID, platform, company, address, hourly wage, income and source URL. Scores are 0–1. Weekly progress means this day's gross estimated earnings divided by the weekly target; prior earnings, taxes, unpaid breaks and allowances are not inferred. For daily-pay postings the available hourly wage is used only as an explicitly disclosed estimate.

An async backend can run this synchronous entrypoint in a thread (`await asyncio.to_thread(run_harness, payload)`). List fields accept simple comma/newline-separated strings. This is not a whole-paragraph NLP parser. Invalid user input raises `ValueError`; invalid job records are skipped. No candidates returns an empty schedule and meaningful metadata.

## Scheduling policy — important for the presentation

Published shifts are fixed by default, including negotiable postings. With the supplied dataset, 14:00–20:00 availability and the demo travel buffers, **there are zero strictly feasible published shifts on every weekday**. The harness must report this honestly.

The primary demo explicitly sets `allow_negotiable_proposals: true`, using the PM-permitted limited adjustment policy: only `scheduleFlexibility.timeNegotiable == true` shifts may be delayed just enough to arrive, by at most 120 minutes. Duration is unchanged, the weekday is unchanged, and the trip home must still fit before 20:00. Original start/end values remain in the output. The resulting schedule is a **proposal requiring employer confirmation**, not a confirmed work shift. Set the flag to false to demonstrate strict empty-result handling.

The target weekday comes from an explicit date or weekday; when absent it uses the single `DEMO_DAY` setting in `harness/config.py`. Same-day planning only; overnight shifts are not fitted into this daytime demo.

Travel is a transparent demonstration estimate: **30 minutes base transit + the posting's `walkMinutes`, per leg**. `travel_estimate_mode: demo_estimator` identifies this assumption. It is not live routing and does not verify commute-route, transfer, or distance preferences. Unknown age, general duties and future multiweek commitments are disclosed rather than invented; missing mandatory licenses are rejected. Explicit supplied constraints are enforced.

## Execution boundary

```text
Daytona = Agent Execution Plane
Nosana  = Optional AI Reasoning / Inference Plane

run_harness(input)                        # one day  -> demo.v1
  -> parse user context
  -> DaytonaScheduleExecutionRuntime.execute()
       -> JsonJobSource loads the canonical jobs.json
       -> normalize and preserve rich source data
       -> recruiting / weekday / shift / travel / qualification / avoid gates
       -> candidate timelines and gross income
  -> preference / capability ranking
  -> SchedulePlan JSON

DaytonaScheduleExecutionRuntime.execute_weekly(request)   # one week -> weekly.v1
  -> JsonJobSource loads the canonical jobs.json
  -> harness.weekly.build_weekly_recommendations(request, rows)
  -> 1-3 plans, available slots, per-shift travel
```

Both calls upload the same stdlib-only `harness/**` package and run it in the
sandbox. A refusal the weekly pipeline raises there (`NO_CANDIDATES`) travels
back as data and is re-raised on the controller as the same
`WeeklyValidationError`, so a domain answer is never confused with a transport
failure - and a transport failure is never dressed up as a local result.

Daytona is where the scheduling agent executes its planning tools. One sandbox is reused through an ignored ID-only `.runtime/` cache or `DAYTONA_SANDBOX_ID`. Execution metadata reports the actual sandbox, process exit code and remote platform. Credentials remain on the controller.

Nosana is optional and cannot alter job facts, schedules, or hard constraints. Missing credentials or inference errors select deterministic weighted ranking. No live scraping, browser, map API, database, persistence system, or multi-agent runtime is implemented.
