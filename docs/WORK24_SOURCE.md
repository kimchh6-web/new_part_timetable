# Work24 (고용24) 채용정보 Open API source

A narrow, official-API-first collector for 고용노동부 고용24 job postings.

- module: `harness/sources/work24.py`
- CLI: `examples/collect_work24_jobs.py`
- tests: `tests/test_work24_source.py` (42 tests, no socket)

**Status: implementation-ready, not release-ready.** No 인증키 is issued to
this project, so nothing here has ever run against the live service. Every
claim below about the contract comes from the official documentation; every
claim about behaviour comes from mocked tests.

---

## 1. Why the API, and not a scraper

고용24 publishes an official 채용정보 Open API. The documentation was read on
2026-09-20 at:

```
https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do?fullApiSvcId=000000000000000000000000000000
```

The 채용정보상세 contract is the second tab of the same page, served under the
tab's own id chain:

```
https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do?fullApiSvcId=000000000000000000000000000000%5E000000000000000000000000000001%5E000000000000000000000000000003
```

Because an official API exists and covers this data, **this collector never
falls back to scraping work24.go.kr.** A missing key produces an
`api_key_missing` error and no request at all. There is no scraping branch to
enable, and adding one would be the wrong fix for a credential problem.

## 2. Exact usage

### Check the prerequisite (works with no key)

```
python examples/collect_work24_jobs.py --status
```

Prints the env var name, a **boolean** for whether a key is configured, and
the two endpoints. It never prints the key. Exit code `0`.

### Collect (needs a key)

```
# PowerShell
$env:WORK24_API_KEY = "<issued key>"
# bash
export WORK24_API_KEY='<issued key>'

python examples/collect_work24_jobs.py --region 11000 --work-hr-cd 2 --display 50
python examples/collect_work24_jobs.py --emp-tp-gb 2 --detail --out C:/Users/you/review/work24.json
```

The key is taken from `WORK24_API_KEY` **only** — never a CLI flag, never a
file — so it cannot land in shell history or a process listing.

| flag | meaning |
|---|---|
| `--region` | 근무지역코드; `a\|b` for several |
| `--emp-tp` | 고용형태, default `11\|21` (the 시간(선택)제 contracts); `any` for no filter |
| `--emp-tp-gb` | `1` 상용직, `2` 일용직. Unset searches 상용직, the service's own default |
| `--work-hr-cd` | 근무시간 search band (`1`–`9`, `99`) |
| `--keyword` | 키워드검색 |
| `--display` | rows per page, max 100 (service maximum) |
| `--start-page` | 검색 시작위치, max 1000 (service maximum) |
| `--pages` | pages to read, max 5 (our cap, not the service's) |
| `--detail` | also call 채용정보상세 per row, max 20 calls |
| `--out` | write the envelope as JSON; refuses `harness/fixtures/` |

Exit codes: `0` collected ≥ 1 row (or `--status`), `1` reached the service and
collected nothing, `2` a configuration problem such as a missing key.

### Library

```python
from harness.sources.work24 import collect_work24_jobs

envelope = collect_work24_jobs(region="11000", work_hr_cd="2", display=50)
```

## 3. The endpoints, as documented

**목록** `GET https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210L01.do`
with `authKey`, `callTp=L`, `returnType=XML`, `startPage` (≤1000),
`display` (≤100), plus optional `region`, `empTp`, `empTpGb`, `workHrCd`,
`keyword` and the other documented filters. Response root `<wantedRoot>` with
`<total>`, `<startPage>`, `<display>` and repeated `<wanted>`.

**상세** `GET https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo210D01.do`
with `authKey`, `callTp=D`, `returnType=XML`, `wantedAuthNo`, `infoSvc=VALIDATION`.
Response root `<wantedDtl>` with `<corpInfo>`, `<wantedInfo>` and
`<empchargeInfo>`.

Codes used here, all from the documentation:

- `empTp` / `empTpCd` — `10`/`11`/`20`/`21` (+ request-only `4`, `Y`).
  `11`/`21` are 시간(선택)제 and are the default filter.
- `empTpGb` — `1` 상용직, `2` 일용직; unset ⇒ 상용직.
- `workHrCd` — `1` 오전(06–12), `2` 오후(12–18), `3` 저녁(18–24), `4` 새벽(00–06),
  `5`–`8` spans, `9` 종일(09–18), `99` 시간협의/무관.
- `salTpCd` — `D` 일급, `H` 시급, `M` 월급, `Y` 연봉.

The documentation names the public posting page integrators must link to, and
this collector uses it as `sourceUrl`:

```
https://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do?wantedAuthNo=<no>&infoTypeCd=VALIDATION&infoTypeGroup=tb_workinfoworknet
```

It also requires the attribution line *본 자료는 고용노동부 고용24에서 제공된
정보이며, 무단복제 및 배포를 금지합니다.* on any detail page built from this
data. **That obligation belongs to whoever renders these rows; this collector
cannot discharge it.**

## 4. Output envelope

```jsonc
{
  "jobs": [ <row>, … ],
  "removals": [ <tombstone>, … ],
  "meta": {
    "job_source": "work24_api",
    "data_mode": "live",
    "attempted": 1, "collected": 1, "pages_read": 1, "total": 1,
    "detail_requested": false, "detail_calls": 0,
    "removals_observed": 0,
    "complete_sync": false,
    "sync_scope": { "kind": "bounded_slice", "note": "…" },
    "key_configured": true, "key_env_var": "WORK24_API_KEY",
    "endpoint": "…210L01.do",
    "query": { "callTp": "L", "returnType": "XML", "startPage": 1, "display": 20, "empTp": "11|21" },
    "errors": [ { "stage", "reason", "detail", "http_status", … } ],
    "schedule_coverage": { "shifts_published": 0, "note": "…" },
    "limits": { … }
  }
}
```

`data_mode` is always `"live"` — there is no mock branch. A failure is zero
rows plus an error, never a stand-in row. `meta.query` never contains the key,
and neither does any error detail.

### Row

Names follow the existing canonical rows (`harness/sources/public_jobs`):

`id` (`work24_<wantedAuthNo>`), `platform` (`"work24"`), `status`,
`statusEvidence`, `title`, `company`, `location`, `address`, `hourlyWage`,
`shifts`, `workSchedule`, `shiftPattern`, `category`, `employmentType`,
`sourceUrl`, `wagePublished`, `schedulePublished`, `raw`, `provenance`,
`missing_fields`, `scheduling_eligible`.

`provenance` is `{provider: "work24", source_url, fetched_at, data_mode:
"live", api: "list" | "list+detail"}`.

Rules that hold for every row:

- **Published or absent.** A field is filled only from an element the API
  returned; anything else is `null`/`[]` and named in `missing_fields`.
- **Pay unit is never converted.** `hourlyWage` is an integer only when the
  posting's own 임금형태 is 시급 (list `salTpNm`) or `salTpCd == "H"`
  (detail). 일급/월급/연봉 leave it `null` and keep the figures verbatim in
  `wagePublished`. If list and detail disagree on the unit, `hourlyWage` is
  cleared and `payUnitConflict` records both readings.
- **Status needs a date.** `recruiting` only when a published closing date is
  still in the future (KST, end of day); `closed` when past; `unknown` when
  there is no parseable date. Being returned by a search is not evidence.
- **No contact or person data.** `empchargeInfo` (전화번호/휴대전화/팩스/
  E-Mail) and `corpInfo/reperNm` (대표자명) are dropped at the parse boundary.
- **`sourceUrl` is a link we would publish.** The API's `wantedInfoUrl` is
  used only if it is HTTPS, on `www.work24.go.kr`/`m.work24.go.kr`, free of
  userinfo, and free of anything resembling a credential; otherwise the
  documented canonical URL is built from `wantedAuthNo`.

### Schedule: always unknown today

`shifts` is **always `[]`** and `scheduling_eligible` **always `false`**.

- The **list** response publishes no weekday-and-time pair at all. `holidayTpNm`
  (`주 5일 근무`) is a count, not a timetable, and is kept verbatim in
  `shiftPattern`.
- The **detail** response has `workdayWorkhrCont` (근무시간/형태), but it is
  free text whose live formatting has never been observed from here. It is
  preserved verbatim in `workSchedule` and
  `schedulePublished.workdayWorkhrText` **as evidence, and is not parsed.**
- `workHrCd` is a *search band the caller chose*. A posting matched by band `2`
  has not said it runs 12:00–18:00. It is echoed in `meta.query` and never
  reaches a row.

Consequence for the planner: **no Work24 row is schedulable today.** These rows
are browsable and rankable by wage/location/status, but the weekly planner must
skip them until `scheduling_eligible` is true.

### Lifecycle: `removals`

A tombstone carries provider and id only — never a fabricated posting:

```jsonc
{ "provider": "work24", "platform": "work24", "id": "work24_K1…",
  "wantedAuthNo": "K1…", "kind": "gone" | "not_found" | "closed",
  "evidence": "http_410" | "http_404" | "published_closing_date",
  "published": "20260901", "observed_at": "…", "source": "detail" | "list" }
```

| observation | result |
|---|---|
| detail answers **404** | tombstone `not_found`/`http_404`; the row is dropped |
| detail answers **410** | tombstone `gone`/`http_410`; the row is dropped |
| published closing date is past | row kept with `status: "closed"` **and** a tombstone `closed` |
| **5xx**, **429**, timeout, network error, malformed XML | `meta.errors` entry; **no tombstone**, row kept |

The last line is the important one: a failed request is *not* a disappearance.
A bad afternoon at work24.go.kr must never be read downstream as every posting
vanishing at once.

### `complete_sync` is always `false`

This collector reads a **bounded slice** — a filter, a page window, at most 5
pages — never the whole 채용정보 corpus. `meta.complete_sync` is therefore a
constant `false`, and `meta.sync_scope.kind` is `"bounded_slice"`.

**A store must not reconcile absence.** Rows it holds that this envelope did
not mention may simply be outside the slice. The only deletion signals are the
entries in `removals`. Setting `complete_sync: true` would require a full
traversal, which this module does not implement — so nothing here can set it.

### Recommendation refresh behaviour

Given the above, a consumer refreshing recommendations from this source should:

1. **Upsert** every row in `jobs`, keyed on `id`.
2. **Delete or hide** exactly the ids in `removals`: `not_found`/`gone` are
   gone from the service; `closed` has published a closing date now past and
   should stop being recommended.
3. **Leave everything else alone.** Never expire a stored row because this
   envelope omitted it — `complete_sync` is `false`.
4. **Treat an errored run as no information.** When `meta.errors` is
   non-empty, the rows that did arrive are still good, but the run says
   nothing about the ones that did not.
5. **Keep Work24 rows out of weekly planning** while `scheduling_eligible` is
   `false`; surface `workSchedule` as text for a human to read instead.
6. Re-running only refreshes the slice you asked for. Widen `--region` /
   `--pages` rather than assuming a narrow run covered the city.

## 5. Security properties

- **Credential.** `WORK24_API_KEY` only. Held by `Work24Transport`; `fetch()`
  takes the query *without* `authKey` and appends it internally, so no caller
  — and no test double — is ever handed it. `sanitize()` scrubs both the
  literal key and any `authKey=…` run out of every error detail. A test
  serializes a whole envelope and asserts neither the key nor the string
  `authKey` appears.
- **Fixed destination.** Only the two documented HTTPS endpoints are callable;
  anything else returns `endpoint_not_allowed` without a request.
- **No redirects.** Every redirect is refused (`redirect_not_followed`) — this
  request carries a credential, and there is no destination worth handing it
  to automatically. The response's final URL is re-checked against the
  requested host *and* path (`redirect_off_endpoint`).
- **Bounded.** 10 s timeout, 2 MB response cap enforced while reading, ≤5 list
  pages, ≤20 detail calls, ≥1 s between requests, **0 retries** — one failure
  stops the run rather than starting a storm.
- **XML.** A body declaring a `<!DOCTYPE` or `<!ENTITY` is refused outright
  (`xml_doctype_refused`); external entities and entity expansion never run.
  Any root other than the documented one is `unexpected_response` rather than
  decoded against a guessed error schema.
- **No credential fallback to scraping.** Stated once more because it is a
  design rule, not an oversight.

## 6. Gaps, and the one prerequisite

**Approved key prerequisite.** A 인증키 must be issued for this project at
`https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do` and exported as
`WORK24_API_KEY`. Until then:

| gap | what is missing | what closes it |
|---|---|---|
| No live verification | nothing here has touched the service | one authenticated list call |
| Error envelope unknown | the docs define none; we report `unexpected_response` | one observed failure response |
| `workdayWorkhrCont` format unseen | so it is preserved, not parsed ⇒ no `shifts` | a handful of real detail responses to confirm the format before writing a narrow parser |
| Hourly amount in detail | `salTpNm` there is free text (임금조건) | a real sample; the list's numeric `minSal` is used meanwhile |
| Rate limits unknown | docs state none; we self-limit to 1 req/s | the terms that come with the key |
| `complete_sync` always false | no full traversal implemented | out of scope for this lane |

Also out of scope here, by instruction: fixtures, the public-web collectors,
the importer, runtime, frontend and server settings are untouched, and nothing
registers this collector as a task or a production source.
