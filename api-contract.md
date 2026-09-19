# Live demo API — demo.v1

이 문서는 **현재 구현된 일일 일정 라이브 데모**의 계약이다.
`python web_demo.py` → `http://127.0.0.1:5191/#/live`.

`POST /api/recommendations`의 주간 3안 계약은 아직 구현되지 않았다.
현재 엔드포인트에 주간 `profile/search/regenerate` 요청을 보내지 않는다.
기존 주간 추천 화면은 브라우저의 기존 계산 경로를 사용한다.

## POST /api/demo/schedule

`Content-Type: application/json; charset=utf-8`. 같은 origin의 로컬 웹 앱에서 사용.
인증 키는 서버 환경변수에만 둔다. 본문 최대 16,384 bytes.

Cloudflare Tunnel 공개 시 서버의 `HARNESS_PUBLIC_ORIGIN`에 발급된 HTTPS origin을
설정하고 서버를 재시작한다. 지정된 origin과 localhost만 브라우저 POST를 허용한다.
Quick Tunnel은 PC의 Python 서버에 연결하는 체험용 경로이며 영구 클라우드 호스팅이 아니다.
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

`plans[].jobs[]`는 주간 계약의 경로다. 현재 일일 응답에는 없으며 위 필드들은 `schedule`의 job block에서 읽는다.

후보가 없을 때도 HTTP 200, `schedule=[]`, `daily_income=0`, `score=0`, `reasons=[]`.
`meta.reason="no suitable job"`, 단계별 개수와 거절 이유를 유지한다.

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
| 404 | NOT_FOUND | 미지원 API/경로 (주간 recommendations 포함) |
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
python -m unittest tests.test_live_api -v
python pitch_demo.py
python web_demo.py
```

전체 legacy 테스트가 모두 통과한다는 의미는 아니다. 주간 API와 legacy fixture 테스트 정리는 별도 범위이다.
