# 공개 채용공고 수집 — 알바천국 / 알바몬

`harness.sources.public_jobs` 는 이 harness 에서 **공개 웹의 채용공고를 직접
읽는 유일한 경로**다. 2026-09-19 기준 상태는 다음 한 줄로 요약된다.

> 파서·수집 한도·권한 경계는 실제 페이지 모양에 맞춰 구현하고 오프라인으로
> 검증했다. **두 제공자 모두 기본값은 비활성(permission_required)** 이며, 권한이
> 기록될 때까지 공고 본문을 가져오지 않는다.

라이브 수집 PASS 를 주장하지 않는다. 아래 근거 문서는 직접 받아서 읽은 것만
인용하고, 받은 문서의 회수 정보(URL·상태·바이트수)를 함께 남겼다.

---

## 1. 근거 — robots.txt

둘 다 직접 받아 확인했다(2026-09-19).

| 제공자 | robots.txt | 우리에게 적용되는 그룹 | 요점 |
|---|---|---|---|
| 알바천국 | <https://www.alba.co.kr/robots.txt> | `User-agent: *` | `Disallow: /` + 명시적 `Allow` 목록. `/job/` `/recruit/` `/search/` `/contract/` (및 대문자 쌍) 허용. 별도 `User-agent: Yeti` 그룹은 전체 허용 — **우리 것이 아니다.** |
| 알바몬 | <https://www.albamon.com/robots.txt> | `User-agent: *` | `/jobs` 계열은 대체로 허용, 단 `/jobs/detail-content` `/jobs/detail/content` `/jobs/detail/manager` `/jobs/detail/print` `/jobs/detail/photos` `/jobs/detail/*?*keyword` `/jobs/apply/` `/jobs/town/apply/` `/alba-contract` `/personal` 금지. `ClaudeBot`/`GPTBot`/`PerplexityBot` 등 별도 그룹 존재 — **우리 것이 아니다.** |

구현상 주의점 두 가지:

* **longest-match.** `Disallow: /` 뒤에 `Allow: /job/` 가 오는 구조를
  `urllib.robotparser` 는 first-match 로 읽어 "전부 금지"로 판정한다. 그래서
  `robots.py` 를 직접 구현했다 — 가장 긴 규칙이 이기고, 길이가 같으면 `Allow`
  가 이긴다. 같은 user-agent 그룹이 여러 번 나오면 규칙을 합친다. 지원 문법은
  `User-agent` / `Allow` / `Disallow` / `*` / `$` / `#` 로 한정했고
  `Crawl-delay` 는 표준 밖 확장이라 **지원하지 않는다** — 값을 읽지 않는다. 지금
  강제되는 것은 `MIN_INTERVAL_SECONDS = 1.0` 의 최소 간격뿐이므로, 제공자가 1초보다
  긴 값을 게시했다면 그것을 지키지 못한다. 활성화 전에 현재 robots.txt 를 사람이
  검토하고, 더 긴 제공자 한도가 있으면 그 값을 지키도록 정해야 한다.
* **user-agent 를 빌리지 않는다.** `USER_AGENT` 는
  `timetable-harness-public-sources/0.1 …` 이고, Googlebot·Yeti·ClaudeBot 등
  어떤 토큰도 포함하지 않는다. 그 토큰들은 두 사이트에서 *우리 것이 아닌 다른* 그룹을
  선택하게 만들므로, 빌려 쓰는 것은 받지 않은 허가를 주장하는 일이다. 테스트가
  이를 기계적으로 막는다(`FORBIDDEN_USER_AGENT_TOKENS`).

**robots 는 fail-closed 다.** 받지 못했거나, HTML/에러 페이지거나, `User-agent`
그룹이 없으면 아무것도 허용하지 않는다(HTTP 200 으로 오는 에러 페이지 포함).

## 2. 근거 — 이용약관

크롤 접근 허용은 재사용 허락이 아니다. 두 제공자의 회원 이용약관을 직접 받아
읽었고, 아래는 **받은 문서에서 그대로 인용**한 것이다. 법적 판단이 아니라,
사람이 판단할 근거로 기록한다.

### 알바천국 — (주)미디어윌네트웍스

* 문서: <https://sign.alba.co.kr/policy/agreement.asp?site=WWW>
* 회수 정보: GET 2026-09-19, HTTP 200, `text/html;charset=UTF-8`, 106,494 bytes,
  추출 텍스트 36,617자. 문서 제목 `미디어윌네트웍스 회원 이용약관`, 부칙
  "2025년 12월 18일부터 적용", 푸터 `ⓒ (주)미디어윌네트웍스`.
* **제18조 ②**(원문): "회원은 서비스를 이용함으로써 얻은 정보를 채용 이외의
  목적으로 회사의 사전 승낙 없이 복제, 송신, 출판, 전송, 배포, 방송, 기타 방법에
  의하여 영리목적으로 이용하거나 제3자에게 이용하게 하여서는 안됩니다."
* 자동 수집 자체를 금지하는 문구는 **찾지 못했다** — 이 약관에는
  크롤/스크래핑/로봇/자동 수집/무단 수집 표현이 없다. 걸리는 것은 **사후 재사용**
  (복제·전송·배포·영리목적·제3자 이용)이고, 그것이 사전 승낙 조건이다.

> ⚠️ 이 호스트는 같은 URL 에 대해 **HTTP 200 으로 3~4 KB 짜리 "요청하신 페이지에
> 일시적인 장애가 발생하였습니다" 페이지**를 돌려주는 경우가 있다(다른 검증자가
> 19:47 에 관측). 그런 응답에는 제18조가 없으므로 그것은 실패한 읽기이지 약관이
> 아니다. `fetch._ERROR_PAGE_MARKERS` 가 이 형태를 성공으로 세지 않는다. 위
> 인용은 106 KB 본문을 실제로 받은 회수에서 뽑은 것이다.

### 알바몬 — 웍스피어 유한책임회사

* 문서: <https://www.albamon.com/service-center/terms/member> (개인회원 이용약관)
* 회수 정보: GET 2026-09-19, HTTP 200, `text/html; charset=utf-8`, 67,968 bytes,
  추출 텍스트 12,144자. 제1조가 운영 주체를 `웍스피어 유한책임회사` 로 명시.
* **제18조 ④**(원문): "회원은 서비스를 이용하여 얻은 정보를 회사의 사전동의 없이
  복사, 복제, 번역, 출판, 방송 기타의 방법으로 사용하거나 이를 타인에게 제공할
  수 없다."
* **제18조 ⑤-8**: 금지행위로 "사이트의 정보 및 서비스를 이용한 영리 행위".
  같은 항 7호는 "서비스의 안정적인 운영에 지장을 주거나 줄 우려가 있다고
  판단되는 행위" 를 금지한다.
* 참고로 푸터/공지의 무단 복제·가공 금지 문구도 같은 방향을 가리킨다
  (<https://www.albamon.com/service-center/notice/search>).

### 그래서 어떤 상태인가

제18조 ④ 는 "사용" 과 "타인 제공" 을 사전동의 조건으로 걸고, 자동 판독기에 대한
예외를 두지 않는다. 알바천국 제18조 ② 도 채용 목적 밖의 재사용과 제3자 이용을
사전 승낙 조건으로 건다. harness 에 공고를 적재하는 것은 한 사람이 지원하려고
읽는 것을 넘어서므로, **두 제공자 모두 `collection_enabled: False`,
`status: permission_required`** 로 둔다. 권한이 생기면 코드 변경 없이 열 수 있다
(§4).

공식 대안(다음 단계 문서용 포인터, 이번 레인에서 구현하지 않음):
고용24 공공 채용정보 오픈 API —
<https://www.work24.go.kr/cm/e/a/0110/selectOpenApiSvcInfo.do?fullApiSvcId=000000000000000000000000000000>
(키 발급형 공식 엔드포인트 `210L01`).

## 3. 고정된 연동 계약 (frozen)

```python
from harness.sources.public_jobs import collect_public_jobs

envelope = collect_public_jobs(["https://www.alba.co.kr/job/Detail?adid=..."])
```

```python
{
  "jobs": [ <row>, ... ],
  "meta": {
    "job_source": "public_web",
    "data_mode": "live",
    "attempted": int,
    "collected": int,
    "errors": [ {"url", "provider", "reason", "detail", ...}, ... ],
    "providers": [ ... ],   # 정보성(추가 필드)
    "limits":   { ... },    # 정보성(추가 필드)
  },
}
```

`data_mode` 는 항상 `"live"` 다. 이 수집기에는 mock 분기가 아예 없기 때문이다 —
게이트에 막히거나 요청이 실패하면 **행 0개 + 에러**를 돌려주고, 대체 행을
만들지 않는다. 그래서 `collected == 0` 과 `errors` 가 "못 가져왔다" 의 정직한
표현이고, 호출자는 행이 있다고 가정하지 말고 `collected`/`errors` 를 읽어야 한다.

### 행(row) 필드

데이터셋과 같은 camelCase 를 쓰고, 공개되지 않은 값은 `null`/`[]` 로 두고
`missing_fields` 에 이름을 남긴다.

| 필드 | 의미 |
|---|---|
| `id` | `"<provider>_<공고번호>"`. 공고번호가 없으면 `"<provider>_h<sha256(source_url)[:12]>"` — 같은 URL 이면 항상 같은 id |
| `platform` | `알바천국` / `알바몬` |
| `status` | `recruiting` / `closed` / `unknown` |
| `statusEvidence` | 그 판정의 근거 문장 |
| `title` `company` `location` `address` | 공개된 값 그대로 |
| `hourlyWage` | **명시적으로 시급(KRW)** 일 때만 정수. 월급·일급·연봉은 `null` |
| `wagePublished` | `{amount, maxAmount, currency, unit}` — 발표된 급여 원형 |
| `shifts` | `[{day, start, end}]`. 요일과 시간이 **둘 다** 명시된 경우에만 |
| `schedulePublished` | `{daysText, hoursText, workHoursText}` — 원문 보존 |
| `qualifications` | `{licenses, requirements, preferred}` |
| `scheduleFlexibility` | 구조화된 협의 정보가 공개되지 않으므로 `{}` |
| `walkMinutes` | 공고에 "도보 N분" 이 있을 때만 **키 자체가 존재** |
| `employmentType` `postedAt` `validThrough` `description` | 공개된 값 그대로 |
| `provenance` | 정확히 `{provider, source_url, fetched_at, data_mode}` |
| `scheduling_eligible` | `bool(shifts) and status == "recruiting"` |
| `missing_fields` | 채우지 못한 추적 필드 목록 |

`provenance.fetched_at` 은 항상 오프셋이 붙은 ISO-8601 이다.

### 절대 하지 않는 추론

* `주5일` → 월~금 5일로 펼치지 않는다(요일이 아니라 개수다).
* `요일 협의` / `시간 협의` / `주말` / `월~금 중 3일` → shift 를 만들지 않는다.
* 영업시간을 근무시간으로 쓰지 않는다.
* 월급/연봉을 시급으로 환산하지 않는다. 시급 범위(min~max)도 `hourlyWage` 로
  접지 않는다.
* `validThrough` 가 없으면 `unknown` 이다 — **절대 `recruiting` 이 아니다.**
* 담당자명·전화번호·이메일은 필드로 만들지 않고, 보존하는 본문에서도
  `[연락처 미수집]` 로 지운다.

## 4. 권한이 생겼을 때 — 두 가지 경로

### (a) 라이브 수집을 여는 경로

```python
collect_public_jobs(
    urls,
    authorizations=[{
        "provider": "alba",
        "granted_by": "누가 허락했는지",
        "reference": "그 근거가 어디에 기록되어 있는지",
        "scope": "service_ingestion",
    }],
)
```

`granted_by` 와 `reference` 가 **둘 다** 있어야 게이트가 열린다. 없으면 요청은
와이어에 나가지도 않는다.

### (b) 다른 곳에서 권한 아래 수집한 행을 받는 경로

```python
from harness.sources.public_jobs import import_authorized_jobs
envelope = import_authorized_jobs(records, authorization)
```

`records` 는 `{provider, source_url, fetched_at, html}` 이거나 이미 파싱된 행이다.

* `meta.data_mode` 와 행의 `provenance.data_mode` 는 `"authorized_import"` —
  봉투 모양은 같지만 **live 와 구분된다.** 임포트된 행이 "라이브 수집이 된다" 는
  증거로 읽히면 안 된다.
* `provenance.fetched_at` 은 **원래 수집 시각 그대로** 보존한다. 임포트 시각은
  별도로 `imported_at` 에 기록한다. 오프셋 없는 naive 시각은 거부한다(수집기의
  타임존을 우리가 추측하지 않는다).
* 14일(`STALE_AFTER_DAYS`)보다 오래된 행은 `stale: True` 와 `age_days` 로
  **표시**한다. 시각을 갱신해 신선하게 보이게 만들지 않는다.

## 5. 요청 한도 (코드로 강제)

| 항목 | 값 |
|---|---|
| 한 번에 읽는 공고 | 최대 5개 (`MAX_JOBS_PER_REQUEST`) |
| 요청 간격 | 순차, 최소 1.0초 |
| 타임아웃 | 10초 |
| 응답 본문 상한 | 2 MB (읽는 중에 차단) |
| 스킴/포트 | HTTPS, 443 만 |
| 호스트 | `www.alba.co.kr`, `www.albamon.com` 만 |
| 경로 | 제공자별 경로 allowlist + robots 판정 |
| 리다이렉트 | **따라가지 않는다** (`redirect_not_followed`) |
| URL userinfo | 거부 (`userinfo_not_allowed`) |
| 깨진 URL | 예외가 아니라 `malformed_url` 결과 |

리다이렉트를 따라가지 않는 이유: 같은 제공자 안이라도 목적지가
robots-disallowed 경로일 수 있고(알바천국은 일부 공고 URL 을
`/error/error_msg.asp` 로 돌린다), 제공자가 바뀌면 한쪽 권한이 다른 쪽으로
새어 나간다. 필요하면 호출자가 새 URL 을 **명시적으로** 요청하면 되고, 그러면
게이트 전체가 다시 돈다.

쿠키·세션·자격증명·헤드리스 브라우저·안티봇 우회는 쓰지 않는다. 봇 차단을
뚫어야만 읽히는 페이지는 그 사실을 보고하고 멈춘다. 임의 URL 을 받는 공개 HTTP
엔드포인트도 만들지 않는다 — 호출자가 코드에서 seed URL 을 명시한다.

## 6. CLI

```bash
python examples/collect_public_jobs.py --status        # 게이트 상태와 약관 근거
python examples/collect_public_jobs.py <posting-url>   # 게이트에 막히면 이유를 출력
python examples/collect_public_jobs.py \
    --authorize alba --granted-by "..." --reference "TICKET-123" \
    --out <경로>.json <posting-url>
python examples/collect_public_jobs.py \
    --authorize alba --granted-by "..." --reference "TICKET-123" \
    --import <records>.json
```

요약 한 줄 + 봉투 JSON 을 출력하고, `--out` 을 주면 그 경로에 쓴다.
`harness/fixtures/` 아래로는 쓰지 않는다 — canonical 600행과 demo fixture 는
이 명령이 건드리지 않는다.

## 7. 테스트

* `tests/test_public_jobs.py` — 오프라인 65개. 소켓을 열지 않는다. 안의 HTML 은
  전부 **이 파일용 합성 fixture** 이고(실제 캡처가 아니며 데이터로 배포되지도
  않는다), 구조만 실제 페이지에서 확인한 모양을 따른다. robots longest-match /
  그룹 병합 / fail-closed, 리다이렉트·userinfo·깨진 URL, HTTP 200 에러 페이지,
  급여 단위, 마감/미상, 협의·주5일·영업시간, 연락처 비수집, 게이트, 봉투 모양,
  authorized import 의 시각 보존까지 덮는다.
* `tests/test_public_jobs_online.py` — opt-in 라이브 smoke. `PUBLIC_JOBS_ONLINE_SMOKE=1`
  과 `PUBLIC_JOBS_AUTHORIZATION_REFERENCE` 가 **둘 다** 있어야 돈다. 제공자가
  막으면 **skip(이유 포함)** 으로 보고한다 — blocked 와 pass 를 구분한다.

## 8. 남은 연동 요구사항

1. **권한.** 두 제공자 중 하나라도 실제로 열려면 `granted_by`/`reference` 로
   기록된 동의가 필요하다. 그때까지 `collection_enabled` 는 `False` 로 둔다.
2. **normalizer 연결.** 이 행들은 아직 `harness.normalizer` 를 통과하지 않는다.
   `hourlyWage`/`shifts` 가 `null`/`[]` 인 행이 planner 로 들어가면 어떻게
   다뤄질지는 normalizer 쪽 결정이다. `scheduling_eligible: False` 인 행은
   스케줄 후보가 아니다.
3. **status 매핑.** 데이터셋은 `recruiting`/`closed`/`paused` 를 쓰고 여기서는
   `unknown` 이 추가된다. 소비하는 쪽이 `unknown` 을 recruiting 으로 취급하지
   않아야 한다.
4. **`walkMinutes` 부재.** 데이터셋 행에는 거의 항상 있지만 여기서는 공개되지
   않으면 키가 없다. 이동시간 추정이 이 필드를 전제하면 그 경로를 따로 다뤄야
   한다.
5. **naive 시각.** `validThrough`/`datePosted` 는 오프셋 없이 발표되므로 KST
   (+09:00) 로 읽는다(한국은 1988년 이후 DST 없음). 다른 타임존 가정이 필요하면
   `parse.KST` 한 곳만 바꾸면 된다.
