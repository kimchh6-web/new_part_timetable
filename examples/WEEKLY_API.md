# 주간 추천 API — weekly.v1

`POST /api/recommendations`, JSON, KST. 실제 요청 예시는 `weekly_input.json`.
단일 Python 웹 서버가 요청을 검증한 후 **Daytona 안에서** 원본 600개 JSON 로딩,
공고 필터링, 배정 가능 시간, 조합과 수입 계산을 실행한다.
브라우저는 서버의 배정 시간과 지표를 그대로 표시한다.

조합·시간표·수입은 언제나 Daytona 안의 결정론적 코드가 만든다. 그 뒤에 LLM 단계를
붙이면 `source="llm"`, 붙이지 않거나 실패하면 `source="fallback"`이다(아래 *생성 방식 공시*).
Daytona 실패는 503이며 로컬 추천으로 위장하지 않는다.
기존 일일 데모(`/api/demo/schedule`, `/#/live`)는 별도 입력·출력 계약을 유지한다.

## 요청

| 경로 | 값 |
|---|---|
| profile.role | 학생 / 직장인 / 기타 |
| profile.home | 아래 지역 enum |
| profile.fixedSchedules | `{day,start,end,endLocation}[]`, 빈 배열 허용, 같은 요일 중복 불가 |
| profile.constraints.minBlockHours | 2 / 3 / 4 |
| profile.constraints.allowNight | boolean, 22시 이후 근무 허용 여부 |
| profile.constraints.wantWeeklyHolidayPay | boolean, 주휴수당 관심 신호 |
| profile.constraints.age | 선택 정수, 미입력은 미확인 경고 |
| profile.targetAmount | 월 목표 금액 |
| search.jobCount | 1 / 2 / 3 |
| search.categories | 아래 카테고리 배열, 빈 배열이면 전체 |
| search.priority | wage / distance / rating / flexibility |
| regenerate | 선택 또는 null |
| regenerate.pinnedJobIds | 유지할 공고 ID 배열, jobCount 이하 |
| regenerate.excludedJobIds | 제외할 공고 ID 배열 |
| regenerate.previousPlanHashes | 이전 응답의 plan.id 배열, 같은 조합 재출력 제외 |

요일: MON/TUE/WED/THU/FRI/SAT/SUN. 시각: HH:mm, 일 종료 경계 24:00 허용.
지역: 강남역, 신촌, 홍대입구, 잠실, 건대입구, 성수, 종로3가, 여의도, 사당,
왕십리, 노원, 신림, 영등포, 수유, 가산디지털단지.
카테고리: 카페·음식점, 편의점, 물류·배송, 과외·교육, 사무보조, 행사·단기, 매장판매.

## 성공 — HTTP 200

최상위: `requestId`, `generatedAt`, `source`, `availableSlots`, `candidateCount`, `plans`, `meta`.

- availableSlots: `{day,from,to,fromLocation}[]`, 서버가 계산한 빈 시간.
- plans: 서로 다른 조합 1~3개. 각 안은 요청한 jobCount를 충족한다.
- plan: `id`, `type`(maxIncome/minTravel/balanced), `label`, `reason`, `metrics`, `jobs`, `warnings`.
  `fitScore`(0~1 number)는 LLM 단계가 붙을 때만 오는 선택 필드다. 없으면 없는 것이고,
  화면은 이 값을 지어내지 않는다.
- jobs: `jobId`, `pinned`, `title`, `company`, `platform`, `category`, `location`,
  `address`, `hourlyWage`, `rating`, `reviewCount`, `thumbnail`, `descriptionSnippet`,
  `sourceUrl`, `contact`, `assignedShifts`, `weeklyHours`, `weeklyPay`,
  `timeNegotiable`, `minWeeks`, `benefits`.
- assignedShifts: `{day,start,end,travel}`. travel은 `fromLocation`, `transitMinutes`,
  `walkMinutes`, `bufferMinutes`, `departAt`을 담는다.
- meta: `runtime_provider=daytona`, `execution_ok`, `jobs_loaded`, `job_source=demo_json`,
  단계별 개수, `contractVersion=weekly.v1`, `totalLatencyMs`.
- meta.execution_proof: `sandbox_id`, `exit_code`(0만 성공으로 인정), `platform`, `python`,
  `cwd`, `command`, `remote_seconds`, `controller_seconds`. meta.trace는 같은 내용을 사람이
  읽는 순서로 남긴다. 이 값들은 sandbox가 보고한 관측값이며 컨트롤러가 채우지 않는다.

## 생성 방식 공시 — `source` 와 `meta` 의 LLM 필드

기존 필드는 그대로 두고 `meta`에만 값을 더한다(가산적). 화면은 여기 있는 값만 읽고,
없으면 "확인하지 못함"으로 표시한다 — 빠진 값을 추측해 채우지 않는다.

| 경우 | `source` | `meta.engine` | `meta.llmUsed` | `meta.llmStatus` |
|---|---|---|---|---|
| LLM 성공(검증 통과) | `llm` | `hybrid` | `true` | `success` |
| 결정론적 대체 | `fallback` | `deterministic` | `false` | 아래 사유 중 하나 |

`meta.llmProvider`(`nosana` \| `openai`; 현재 구현·검증된 것은 Nosana 하나다)와
`meta.llmModel`(모델 식별자 문자열)은 **성공일 때만** 싣는다.
`meta.llmLatencyMs`(0 이상의 정수)는 **두 경우 모두** 싣는다 — 대체된 경우에도 실제로
LLM 을 기다린 시간이며, 단계를 아예 건너뛰었으면 0 이다.

`meta.llmStatus` 대체 사유:

| 값 | 뜻 |
|---|---|
| `not_configured` | 이 서버에 LLM 모델 설정이 없다 |
| `timeout` | LLM이 제한 시간 안에 답하지 않았다 |
| `provider_error` | 제공자 호출이 실패했다 (원문 오류 메시지는 응답에 싣지 않는다) |
| `invalid_response` | LLM 출력이 형식 검증을 통과하지 못했다 |
| `budget_exhausted` | 요청에 남은 **처리 시간 예산**이 부족해 LLM 단계를 건너뛰었다 (크레딧·토큰 할당량이 아니다) |

LLM 이 하는 일은 **결정론적 단계가 이미 만든 후보와 근거 중에서 적합도를 평가하고
추천 사유 문장을 고르는 것**뿐이다. 자유 작문을 싣지 않고, 입력에 없는 자격·경력·스킬을
추론하지 않는다.

LLM 단계가 바꿀 수 있는 것은 셋뿐이다: `plans` 의 **순서**, 각 안의 **`reason`**,
그리고 선택 필드 **`fitScore`** 의 추가. 각 안의 `jobs`·`metrics`·`assignedShifts` —
곧 어떤 공고를 언제 일하고 얼마를 버는가 — 는 결정론적 단계가 계산한 값 그대로이며
LLM 이 만들거나 바꾸지 않는다. 브라우저는 서버가 돌려준 `plans` 순서를 그대로 그린다.

화면(`js/app.js`)은 `source=llm` + `llmUsed=true` + `llmStatus=success` 가 모두 맞고
`engine` 이 `deterministic` 이 아닐 때만 "AI가 평가했다"고 말한다. 하나라도 빠지거나
어긋나면 "확인하지 못했다"로 표시하고, `source=fallback` 은 아는 사유가 있을 때만
그 사유를 밝힌다. 오래된 응답(이 필드들이 없는 응답)은 예전 문구 그대로 읽힌다.
이 축은 공고 데이터의 출처(`meta.job_source` / `meta.data_mode`)와 독립이다.

`monthlyIncome = 주급 합계 × 4.3`. `targetAchievementRate`는 비율(1 = 100%).
`weeklyWorkHours`는 배정된 근무시간, `weeklyTravelMinutes`는 도보 포함 이동시간,
`effectiveHourlyWage`는 주급 / (근무시간 + 이동시간)이다.
수입은 세전 추정치이며 주휴수당을 추가하지 않는다(`weeklyHolidayPayIncluded=false`).
실제 지급·자격 충족을 보장하지 않는다.

## 계산 범위

- 원본 recruiting 공고만 사용하며 published start/end를 바꾸지 않는다.
- daysNegotiable 공고는 minDaysPerWeek를 만족하는 요일 부분집합을 허용한다.
- timeNegotiable은 안내·선호 신호이며 근무시간을 임의로 바꾸지 않는다.
- 이동은 데모 추정치: 다른 지역 30분, 같은 지역 0분에 도보를 더하고 출근 전 15분 여유를 둔다.
- 하루 08:00~24:00 범위. 자정을 넘는 근무는 이번 버전에서 제외한다.
- 주간 최대 40시간은 이 데모의 제품 정책이다. 법적 근무 가능성 판정이 아니다.
- 제한된 후보 조합을 탐색하므로 전체 조합의 수학적 최적해를 보장하지 않는다.
- 입력에 없는 자격·면허·경력 충족은 미확인으로 안내한다.

## 오류

`{requestId,error:{code,message,details}}`. 입력은 유지하고 수정 또는 재시도한다.

| HTTP | code | 의미 |
|---|---|---|
| 400 | VALIDATION_ERROR | 형식·필수값·enum 오류 |
| 400 | SCHEDULE_CONFLICT | 고정 일정 요일 중복 |
| 400 | PINNED_EXCEEDS_COUNT | 고정 공고 수 초과 |
| 422 | NO_CANDIDATES | 요청 조합을 만들 수 없음; details에 필터 단계 수 포함 |
| 503 | BUSY | 이전 원격 요청이 아직 처리 중 |
| 503 | DAYTONA_UNAVAILABLE | 원격 실행 실패 |
| 504 | TIMEOUT | 응답 대기 25초 초과 |

공통 본문 한도·origin·미지원 경로 오류는 상위 `api-contract.md` 참조.
`GET /healthz`는 서버 생존 확인만 수행하며 Daytona 상태를 보장하지 않는다.
sandbox 실행은 한 번에 하나다: 앞선 요청이 끝날 때까지 새 요청은 BUSY이고, 25초 TIMEOUT
이후에도 원격 호출이 실제로 끝날 때까지 잠금을 유지한다.
선택 엔드포인트 `/api/meta`, `/api/schedules/validate`는 제공하지 않는다.

실행 검증: `python examples/smoke_weekly.py --url http://127.0.0.1:5191`.
서버 프로세스에 `DAYTONA_API_KEY`가 있어야 한다. sandbox는 `.runtime/daytona-sandbox.json`
(ID만 저장) 또는 `DAYTONA_SANDBOX_ID`로 재사용하고, 없으면 새로 만든다. 멈춘 sandbox는
재사용 전에 상태를 다시 읽어 다시 시작하며, 사라진 sandbox는 새로 만들어 패키지를 다시 올린다.
