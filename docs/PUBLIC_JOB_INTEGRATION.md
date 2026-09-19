# 공개 채용 공고 연동 (public job integration)

수집 담당 레인이 만든 **로컬 아티팩트 한 개**를 읽어, 실제 공개 채용 공고로 주간
추천을 만드는 경로다. 이 문서의 범위는 `아티팩트 → 검증 → Daytona 전송 → 응답·화면
표시`까지이고, 수집(fetch) 자체는 여기서 하지 않는다. 이 레인의 어떤 코드도 네트워크를
열지 않는다.

기본값은 바뀌지 않았다. **아무것도 설정하지 않으면 지금까지와 똑같이 합성 데모
600건**이 Daytona 안에서 로드된다.

---

## 1. 켜는 방법 (opt-in, 기본 꺼짐)

서버 환경변수 두 개뿐이다. HTTP 요청 본문으로는 경로도 URL도 받지 않는다.

| 변수 | 값 | 뜻 |
|---|---|---|
| `HARNESS_JOB_SOURCE` | `demo_json` (기본) | 합성 데모 600건. 지금까지와 동일 |
| | `public_web` | 아래 아티팩트를 읽는다 |
| | `job_store` | 영속 저장소를 읽는다 (`docs/JOB_STORAGE.md` §3.1) |
| `HARNESS_PUBLIC_JOBS_PATH` | 절대 경로 | 검토를 마친 아티팩트 JSON 파일 |
| `HARNESS_JOB_STORE_PATH` | 절대 경로 | `job_store` 모드가 읽을 `.sqlite3` 파일 |

`public_web`/`job_store`인데 그 모드의 경로가 없거나 상대경로면 실행을 거부한다
(데모로 조용히 되돌아가지 않는다). 모드마다 경로 변수가 따로이므로 한쪽에 설정한
경로를 다른 쪽이 읽는 일은 없다. 경로만 설정하고 `HARNESS_JOB_SOURCE`를 바꾸지
않으면 **아무 일도 일어나지 않는다** — 활성화는 언제나 명시적 선택이다.

플랫폼의 이용약관상 재사용 권한은 운영자가 판단·확보할 사항이다. 이 코드는 권한이
있는지 확인하지 않고, 있다고 주장하지도 않는다. `public_web`으로 바꾸는 행위는
"운영자가 이 아티팩트를 검토했고 사용해도 된다고 판단했다"는 뜻으로만 해석된다.

---

## 2. 아티팩트 계약 (수집 레인과 고정된 seam)

```jsonc
{
  "jobs": [ { /* 아래 행 스키마 */ } ],
  "meta": {
    "job_source": "public_web",
    "data_mode": "live" | "authorized_import",
    "attempted": 5, "collected": 3, "errors": [],
    "source_permission": { /* 선택. 운영자 기록 */ }
  }
}
```

* 파일 5MB 이하, 행 1000개 이하, UTF-8 JSON, `NaN`/`Infinity` 금지(전부 유한수).
* `data_mode` 두 가지는 **끝까지 구분한다.**
  * `live` — 수집 레인이 직접 가져온 공고,
  * `authorized_import` — 권한 있는 경로로 제공받은 공고(실시간 크롤링이 아님).
* 행의 `provenance.data_mode`는 상단 `meta.data_mode`와 같아야 한다. 다르면 그 행은
  `PROVENANCE_MODE_MISMATCH`로 제외한다.

### 행 스키마

기존 데이터셋과 같은 camelCase를 그대로 쓴다.

```
id, platform, status, title, company, category, location, address,
hourlyWage, shifts[{day,start,end}], qualifications, scheduleFlexibility, sourceUrl
+ provenance {provider, source_url, fetched_at, data_mode}
+ missing_fields[], scheduling_eligible
```

모르는 값은 `null`/빈 값으로 둔다. 지어내지 않는다.

---

## 3. 검증 — 아티팩트는 입력이지 증언이 아니다

`scheduling_eligible: true`는 **필요조건일 뿐**이다. 아래를 여기서 독립적으로 다시
본다. 하나라도 어긋나면 그 행은 이유와 함께 제외되고, 이유는 `meta.source_counts.
rejection_reasons`에 집계된다.

| 제외 이유 | 언제 |
|---|---|
| `MISSING_REQUIRED_FIELDS` | id/platform/status/title/company/location/address/sourceUrl 중 하나라도 비었을 때 |
| `NOT_RECRUITING` | `status != "recruiting"` — 마감·종료·일시중지·불명 전부 포함(불명을 모집중으로 읽지 않는다) |
| `EXPIRED_POSTING` | 공고가 밝힌 마감 날짜(`validThrough`/`expiresAt`/`closingDate`/`deadline`)가 이미 지났을 때. 상태가 `recruiting`이어도 **날짜가 이긴다**. 날짜가 아닌 자유 문구("채용시 마감")는 판정하지 않고 모르는 채로 공시한다 |
| `STALE` | `provenance.fetched_at`이 TTL(기본 24시간)보다 오래됐을 때 |
| `PROVENANCE_INVALID` | `fetched_at`이 파싱되지 않거나, 타임존이 없거나, 미래일 때 |
| `PROVENANCE_MISSING` / `PROVENANCE_MODE_*` | provider/source_url/fetched_at 누락, data_mode 불일치 |
| `NOT_SCHEDULING_ELIGIBLE` | 수집 레인이 스스로 부적격으로 표시한 행 |
| `PAY_NOT_HOURLY` | 시급이 없거나 0 이하이거나 숫자가 아니거나, `payDetail.payType`이 hourly가 아닐 때 |
| `NO_EXPLICIT_SHIFTS` | 요일·시작·종료가 명시되지 않았거나, 자정을 넘기는 근무일 때 |
| `ONE_OFF_POSTING` / `DATE_SPECIFIC_POSTING` / `UNVERIFIED_RECURRENCE` | 아래 4절 |
| `UNSUPPORTED_LOCATION` | `location`이 이 데모 추정기가 아는 15개 지역이 아닐 때 |
| `ADDRESS_NOT_SEOUL` | 주소에 서울이 없을 때. 같은 이름의 다른 도시 장소를 서울로 간주하지 않는다 |
| `SOURCE_URL_NOT_ALLOWED` | 아래 5절 |
| `HTML_IN_TEXT_FIELD` | 제목·회사·주소 등에 마크업이 남아 있을 때(파싱 실패로 본다) |
| `DUPLICATE_ID` | 같은 id가 두 번 |

`category`는 **제외 사유가 아니다.** 카테고리는 근무 가능 여부를 결정하지 않는다 —
시급·명시적 요일 근무·반복·지역·출처만이 결정한다. 수집 레인은 공고가 모집직종을
적었을 때만 `category`를 채우고 아니면 `None`으로 둔다. 그래서 알 수 없거나 데모 7종에
매핑되지 않는 카테고리는 **모르는 채로 통과**하고, 여기서 분류를 지어내지 않는다.
문자열이 아닌 `category`만 파싱 실패로 보아 `MISSING_REQUIRED_FIELDS`가 된다.
대신 주간 요청이 `search.categories`로 **명시한** 분류만 원하면, 카테고리를 모르는 행은
그 필터(`afterCategoryFilter`)에서 걸러진다. 주의: 현재 `search.categories: []`는
"전체"가 아니라 데모 7종 전체로 해석되므로, 카테고리가 `None`인 행은 그 단계에서도
빠진다. 이 문서 범위(importer) 밖의 동작이고, 필요하면 주간 레인에서 별도로 다뤄야 한다.

문서 자체가 잘못된 경우(없음·5MB 초과·JSON 오류·스키마 불일치·1000행 초과)는 행이
아니라 **호출 전체가 실패**하고, 코드만 담긴 `DaytonaRuntimeError`가 된다. 메시지에는
경로·환경변수·비밀이 들어가지 않는다.

---

## 4. 주간 반복과 월 수입 추정

주간 파이프라인은 배정된 근무를 **매주 반복한다고 보고 4.3을 곱해** 월 수입을
추정한다. 그래서 반복이 공개적으로 확인되지 않는 공고는 받지 않는다.

* 하루·당일·단발·원데이·행사 문구가 있으면 `ONE_OFF_POSTING`,
* 근무 날짜가 지정된 공고(`workDates` 등, shift에 `date`)는 `DATE_SPECIFIC_POSTING`,
* `workPeriod`/`shiftPattern`에 반복(매주·주 N일·요일 고정·상시·N개월 이상 등)이
  적혀 있지도 않고 `minWeeks >= 2`도 아니면 `UNVERIFIED_RECURRENCE`.

"아마 반복되겠지"는 추론하지 않는다. 그리고 응답에는 monthlyIncome이 **보장된 수입이
아니라 추정치**이며 근무 기간이 정해진 공고라면 그 기간까지만 유효하다는 공시가 붙는다.

---

## 5. 링크·개인정보

`sourceUrl`과 `provenance.source_url`은 둘 다

* `https`여야 하고 (포트는 443 또는 생략),
* 호스트가 `www.alba.co.kr` 또는 `www.albamon.com`이어야 하며,
* 경로가 그 제공자의 공개 공고 경로(`/job/`, `/jobs/`, `/recruit/`, `/contract/`)여야 하고,
* `user@host` 형태의 userinfo가 없어야 하며,
* 두 URL의 호스트가 서로 같아야 한다.

`javascript:`, `data:`, 커스텀 스킴, 다른 호스트는 전부 거부한다.

전송 전에 다음은 **행에서 제거된다**: `contact`, `phone`, `manager`, `kakao`,
`applyUrl`, `thumbnail`(검토되지 않은 제3자 이미지 호스트). 알려지지 않은 추가 필드는
평범한 스칼라면 그대로 보존하고, 마크업이나 연락처 형태를 담고 있으면 버린다(버린
키는 행의 `dropped_fields`에 남는다). `title`/`company`/`description` 같은 자유 문구에
전화번호·이메일이 섞여 있으면 그 부분만 `[연락처 제거]`로 치환한다.

운영자의 권한 기록(`meta.source_permission`)은 **공개되지 않는다.** 응답에 나가는 것은
`{"recorded": true, "providers": ["albamon"]}` 뿐이고, 계약 번호·담당자·내부 메모는
로더의 반환값(`permission_record`)에만 남아 서버 밖으로 나가지 않는다.

---

## 6. 도보 시간 — 유일한 예외

공개 공고는 역에서 근무지까지의 도보 시간을 싣지 않는다. 값이 없는 행은 데모 추정치
**15분**(`DEMO_WALK_MINUTES`)으로 계산한다.

이것은 **추정치이지 상한이 아니다.** 실제 도보 시간은 더 길 수도 짧을 수도 있고, 경로를
조회한 값이 아니다. 해당 행은 `walkMinutesEstimated: true`와 `missing_fields`에
표시되고, 응답 공시와 화면 배지(`도보 시간 데모 추정 N건`)에도 그대로 나온다. 이동
추정기 자체는 넓히지 않았다 — 지역 15개·고정 환승 시간 그대로다.

---

## 7. 실행 경로와 응답

1. 컨트롤러(`harness/runtime/daytona.py`)가 환경변수를 읽고 아티팩트를 검증한다.
2. 통과한 행 + 출처 서술자(`source`)를 **기존 request 파일**에 담아 업로드한다.
   사용자 payload에서 오는 값은 하나도 들어가지 않는다.
3. 정규화·필터·후보 생성·조합 탐색은 지금처럼 **Daytona 안에서** 일어난다
   (`harness/runtime/_remote_entry.py` → `build_weekly_recommendations`).
4. 응답 meta에 출처가 붙고, 합성 데이터셋 공시는 실제 공고용 공시로 교체된다
   (`harness/weekly/response.py: apply_job_source`).

적격한 행이 하나도 없으면 `NO_CANDIDATES`(422, `reason: NO_ELIGIBLE_IMPORTED_JOBS`)로
거절한다. **데모 600건으로 대체하지 않는다.**

### 응답 meta

| 필드 | 값 |
|---|---|
| `meta.job_source` | `demo_json` \| `public_web` |
| `meta.data_mode` | `demo` \| `live` \| `authorized_import` \| `unknown` |
| `meta.source_counts` | attempted/collected/received/accepted/rejected/walk_estimated/rejection_reasons/providers |
| `meta.source_permission` | `{recorded, providers}` 또는 없음 |

`response.source`는 **건드리지 않는다.** 그 필드는 "순위를 무엇이 만들었는가"(`fallback`
= 결정론적 서버 규칙)이고, 데이터 출처와는 다른 축이다. 둘을 섞지 않는다.

### 엔드포인트 경계

* `POST /api/recommendations` (주간) — 위 설정이 적용되는 유일한 경로다.
* `POST /api/demo/schedule` (일간 데모) — **언제나 데모 전용**이다. `public_web`을
  켜도 이 경로는 canonical 600건을 그대로 쓴다.

---

## 8. 화면 표시

`js/api-client.js`가 `meta.job_source`/`meta.data_mode`를 `response.jobSource`로
옮기고, `js/app.js: sourceNotice()`가 그 값으로만 문구를 고른다.

| 상태 | 문구 |
|---|---|
| 서버 응답 전 · 출처 미기록 저장본 | **공고 출처 미표시** — 어느 쪽도 단정하지 않는다 |
| `demo_json` + `demo` | **합성 데모 데이터** (기존 문구 그대로) |
| `public_web` + `live` | **실제 공개 공고** |
| `public_web` + `authorized_import` | **제공된 실제 공고** (실시간 크롤링 아님) |

실제 공고를 합성 데모라고 부르지 않고, 데모를 실제라고 부르지 않는다. 저장한 시간표에는
`jobSource`가 함께 저장되어 나중에 열어도 출처가 유지된다. 시각 디자인은 바꾸지 않았다.

---

## 9. 실제 가동에 남은 조건

이 레인이 끝나도 아래가 충족되기 전에는 실제 공고가 화면에 나오지 않는다.

1. **수집 레인의 실제 산출물.** 약관상 자동 수집이 허용되지 않으면 `live`는 켜지지
   않는다. 그 경우 권한 있는 경로로 받은 `authorized_import` 아티팩트만 쓸 수 있다.
2. **운영자의 권한 판단과 기록.** 이 코드는 권한 여부를 확인하지 않는다.
3. **행의 적격성.** 수집된 공고가 서울·지원 지역·시급·명시적 요일 근무·반복 근무를
   모두 만족해야 한다. 공개 공고 상당수는 이 중 하나가 없어 제외될 수 있고, 그것은
   실패가 아니라 정직한 결과다. 적격 행이 0이면 `NO_CANDIDATES`가 맞는 답이다.
4. **카테고리 표시.** 수집 레인이 데모 7종 분류에 맞춘 `category`를 채우면 카테고리
   필터와 화면 표기가 의미를 갖는다. 채우지 못해도 그 행은 제외되지 않고 카테고리만
   모르는 채로 남는다 — 여기서 임의로 분류하지 않는다.
5. **자격 요건.** 면허·경력 요구가 있는 공고는 이 요청으로 확인할 수 없으므로 "지원
   가능"이라고 말하지 않고 확인 필요 항목으로만 표시된다.
6. **검증된 사실.** 수집 레인의 원시 산출물이 이 계약을 그대로 만족한다는 보장은 없다.
   `모집직종`처럼 공고가 안 적으면 비는 필드가 있고, 필수 사실(시급·요일·주소·반복)이
   비면 그 행은 여기서 제외된다. 아티팩트를 붙이기 전에 **어떤 필수 사실이 비는지 실제
   산출물로 확인**해야 한다.
7. **파일이 읽혔다는 것은 가동이 아니다.** `load_imported_jobs`가 성공했다는 것은 파일이
   계약에 맞았다는 뜻일 뿐, 공고가 실재하고 모집 중이며 화면까지 나왔다는 증거가 아니다.
   이 레인의 어떤 테스트도 실제 공고를 가져온 적이 없다. 실가동 판정은 실제 아티팩트로
   `jobs`가 남고 주간 응답이 나오는 것을 본 뒤에만 할 수 있다.
