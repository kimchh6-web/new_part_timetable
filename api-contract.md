# Web API — weekly.v1 / demo.v1

`python web_demo.py` → `http://127.0.0.1:5191/`.
기본 웹 흐름은 [주간 추천 계약](examples/WEEKLY_API.md)의
`POST /api/recommendations`를 사용한다. 아래 일일 데모 계약은 `/#/live`에서 유지한다.

기본 공고 데이터는 제공된 600개 합성 공고다. 실제 구인 공고나 확정된 근무 배정이 아니다.
일일 데모 계약은 언제나 이 합성 데이터만 쓴다. 주간 추천만 서버 환경변수
`HARNESS_JOB_SOURCE=public_web`으로 opt-in 하면, 검토를 마친 로컬 아티팩트의 공개 공고를
대신 읽는다 — 설정과 검증 규칙은 [공개 채용 공고 연동](docs/PUBLIC_JOB_INTEGRATION.md).
공개 제공자 수집 자체는 여전히 기본 비활성(`permission_required`)이며, opt-in 은
라이브 수집이 돌고 있다는 뜻이 아니다.
`source`(`llm|fallback`)는 **추천을 만든 방식**이고, `meta.job_source`/`meta.data_mode`는
**공고 데이터의 출처**다. 두 값은 서로 독립이다.
주간 추천의 `source=llm`은 검증을 통과한 실제 LLM 호출에만 붙으며, 그 근거
(`meta.engine`/`llmUsed`/`llmProvider`/`llmModel`/`llmLatencyMs`/`llmStatus`)와
대체 사유 목록은 [주간 추천 계약](examples/WEEKLY_API.md)의 *생성 방식 공시*에 있다.
일일 데모 계약은 이 필드들을 쓰지 않는다.
일일 `availability` 입력과 주간 `profile/search/regenerate` 입력은 서로 대체할 수 없다.

## POST /api/demo/schedule

`Content-Type: application/json; charset=utf-8`. 같은 origin의 로컬 웹 앱에서 사용.
인증 키는 서버 환경변수에만 둔다. 본문 최대 16,384 bytes.

Cloudflare Tunnel 공개 시 서버의 `HARNESS_PUBLIC_ORIGIN`에 발급된 HTTPS origin을
설정하고 서버를 재시작한다. 지정된 origin과 localhost만 브라우저 POST를 허용한다.
현재 공개 주소는 계정 소유 Named Tunnel의 `https://timetable.shinick.dev`다.
터널은 PC의 Python 서버에 연결하며 영구 클라우드 호스팅이 아니다.
PC, Python 서버, cloudflared 프로세스를 유지해야 한다. 동시 계획 요청은 BUSY로 응답할 수 있다.

```json
{
  "start_location": "서울 강남",
  "home_location": "서울 용산",
  "availability": {"start": "14:00", "end": "20:00"},
  "weekday": "SAT",
  "weekly_income_target": 250000,
  "skills": ["POS 경험 6개월", "보건증"],
  "preferred_jobs": ["매장 정리"],
  "avoid_jobs": ["설거지", "주방 보조"],
  "allow_negotiable_proposals": true
}
```

| 입력 | 규칙 |
|---|---|
| start_location, home_location | 필수, 비어 있지 않은 문자열 |
| availability.start/end | 필수, 같은 날 HH:mm, start < end; 24:00 입력 미지원 |
| weekday | MON/TUE/WED/THU/FRI/SAT/SUN; 날짜와 요일 모두 없으면 config의 MON |
| date | 선택, YYYY-MM-DD; weekday와 함께 주면 일치해야 함 |
| weekly_income_target | 선택, 0 이상의 유한 숫자; null/0이면 기여율 null |
| skills, preferred_jobs, avoid_jobs, travel_preferences | 선택, 문자열 배열 또는 쉼표/줄바꿈으로 나눈 문자열 |
| age, commitment_weeks | 선택, 0 이상의 정수; 미입력은 미확인 상태로 표시 |
| allow_negotiable_proposals | 선택 boolean, 기본 false |

이동은 **편도 30분 + 공고 walkMinutes** 데모 추정치이다. 실시간 지도 계산이 아니다.
시간 협의 opt-in 시에만 timeNegotiable 공고를 최대 120분 늦추며 기간과 요일은 유지한다.
고용주 합의 전에는 확정 근무가 아니다. 이 API는 주간 최소 배정 일수를 검증하는 주간 계획 API가 아니다.

## 성공 — HTTP 200

최상위: `requestId`, `generatedAt`(KST ISO timestamp), `source`(`llm|fallback`),
`schedule`, `summary`, `recommendation`, `meta`.
`source=fallback`은 결정론적 추천을 의미하며 오류가 아니다.

| 필드 | 타입 / 의미 |
|---|---|
| schedule | travel → job → travel 배열. 후보가 없으면 [] |
| summary.daily_income | number, 시간 × 시급으로 계산한 하루 세전 추정 수입 |
| summary.weekly_target | number 또는 null |
| summary.target_progress_percent | number 또는 null, 100이 100%; 하루 수입/주간 목표 ×100 |
| recommendation.score | 0~1 number |
| recommendation.reasons | string[] |
| meta.runtime_provider | daytona |
| meta.execution_ok / sandbox_id | boolean / string, 실제 원격 실행 근거 |
| meta.job_source | demo_json |
| meta.ranking_provider | deterministic 또는 nosana |
| meta.travel_estimate_mode | demo_estimator |
| meta.jobs_loaded / jobs_recruiting / jobs_schedule_compatible / jobs_final_candidates | 각 단계 공고 개수 |
| meta.contractVersion | demo.v1 |
| meta.totalLatencyMs | 전체 서버 처리 시간(ms) |
| meta.requires_employer_confirmation | boolean |
| meta.warnings / trace | string[] |

Travel block: `type=travel`, `start`, `end`, `from`, `to`, `duration_min`.

Job block: `type=job`, `start`, `end`, `duration_min`, `job_id`, `platform`, `title`,
`company`, `location`, `address`, `category`, `hourly_wage`, `estimated_income`, `source_url`.
`hourly_pay`는 이전 코드 호환 alias. 필드명은 변경하지 않는다.

시간 제안: `schedule_status=published|proposed`, `requires_confirmation`,
`published_start`, `published_end`, `adjustment_minutes`, `day`.

프론트 추가 요청 배지 3개는 **job block에 추가**한다:

| 필드 | 타입 | 원본 |
|---|---|---|
| timeNegotiable | boolean | scheduleFlexibility.timeNegotiable (coarse negotiable 아님) |
| minWeeks | number 또는 null | minWeeks; 누락 시 null |
| benefits | string[] | benefits 앞 최대 5개; 누락 시 [] |

`plans[].jobs[]`는 주간 계약([weekly.v1](examples/WEEKLY_API.md))의 경로이며 거기서도 같은 세 필드를
제공한다. 일일 응답에는 `plans`가 없으므로 위 필드들은 `schedule`의 job block에서 읽는다.

후보가 없을 때도 HTTP 200, `schedule=[]`, `daily_income=0`, `score=0`, `reasons=[]`.
`meta.reason="no suitable job"`, 단계별 개수와 거절 이유를 유지한다.

## GET /healthz

`{"status": "ok"}`를 200으로 돌려주는 서버 생존 확인용 경로다. 인증·본문·origin 검사가 없고
Daytona를 호출하지 않으므로 sandbox 잠금이나 원격 상태를 보장하지 않는다.

## 오류

```json
{
  "requestId": "req_...",
  "error": {"code": "VALIDATION_ERROR", "message": "availability.start must ...", "details": {}}
}
```

| HTTP | code | 의미 |
|---|---|---|
| 400 | VALIDATION_ERROR | JSON, 필수값, 시간, 필드 타입 오류 |
| 403 | ORIGIN_NOT_ALLOWED | 다른 origin 요청 |
| 404 | NOT_FOUND | 미지원 API/경로 |
| 405 | METHOD_NOT_ALLOWED | 미지원 HEAD 요청 |
| 408 | REQUEST_TIMEOUT | 본문 수신 15초 초과 |
| 415 | UNSUPPORTED_MEDIA_TYPE | JSON Content-Type 아님 |
| 503 | BUSY | sandbox에서 이전 요청이 실행 중 |
| 503 | DAYTONA_UNAVAILABLE | 실제 원격 실행 실패; 로컬 추천으로 대체하지 않음 |
| 504 | TIMEOUT | 계획 응답 대기 25초 초과 |

브라우저 타임아웃은 30초. 서버 25초는 JSON 본문 수신/검증 이후 계획 실행 대기 한도다.
원격 SDK 호출은 즉시 취소되지 않을 수 있으므로 타임아웃 이후에도 실제 종료까지
sandbox 잠금을 유지한다. 그동안 새 요청은 BUSY이며 중복 실행하지 않는다.
Nosana가 없거나 실패하면 deterministic 순위로 HTTP 200을 반환한다.
API 키, 원격 내부 예외, 토큰은 오류 응답에 포함하지 않는다.

## 확인 명령

```bash
python -m unittest tests.test_live_api tests.test_http_weekly tests.test_daytona_transport -v
python -m unittest discover -s tests          # 전체 스위트
python web_demo.py                            # 기본 127.0.0.1:5191
python examples/smoke_weekly.py --url http://127.0.0.1:5191   # 실제 Daytona 왕복
python pitch_demo.py
```

`tests/`가 `sys.path`에 있어야 `test_e2e.py`가 `support`를 import할 수 있으므로 전체 스위트는
`python -m unittest discover -s tests`로 돌린다. `smoke_weekly.py`는 서버가 떠 있어야 하고
컨트롤러 프로세스에 `DAYTONA_API_KEY`가 있어야 한다.
