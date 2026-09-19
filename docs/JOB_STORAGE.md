# 수집한 공고를 어디에 쌓는가 — `harness/storage`

수집 레인이 만든 **정규화된 공고 행**을 SQLite 파일 하나에 보관한다. 표준 라이브러리
`sqlite3` 외에 의존성이 없고, 서버도 별도 저장소도 없다.

* **코드는 git 에, 데이터는 git 밖에.** 기본 경로는 `.runtime/jobs.sqlite3` 이고
  `.runtime/` 은 `.gitignore` 에 있다. 체크아웃이 남의 수집 결과를 들고 다니지 않고,
  수집을 돌려도 워킹 트리가 더러워지지 않는다.
* **인터페이스는 하나.** `JobStore(path)` 에 `ingest(envelope)` 와 `snapshot()`.
  수명주기 정리용으로 `prune_stale()` 과 `reconcile_missing()` 이 더 있다(§5).

```python
from harness.storage import JobStore

with JobStore() as store:                      # .runtime/jobs.sqlite3
    summary  = store.ingest(envelope)          # 수집기 envelope 그대로
    envelope = store.snapshot(max_age_hours=24)  # 모집중 + 신선한 행만
```

입력 envelope 는 수집 레인이 쓰는 그 모양이다(`docs/PUBLIC_JOB_SOURCES.md` §3):
`{"jobs": [...], "meta": {"job_source", "data_mode", ...}}`. 공개 수집기
(`collect_public_jobs`), 승인 임포트 씨앗(`import_authorized_jobs`), 그리고 나중에
생길 API 어댑터가 모두 같은 봉투를 쓴다.

---

## 1. 이 저장소가 **아닌** 것

| 아닌 것 | 왜 |
|---|---|
| 스케줄링 검증기 | 행은 온 그대로 보존한다. `scheduling_eligible` 는 계산하지 않고 그대로 옮긴다. 편성 가능 여부는 importer 가 다시 판정한다. |
| 데이터 웨어하우스 | 이력 테이블도, 필드 단위 diff 도, 질의 언어도 없다. 공고 하나당 행 하나, 가장 최근 관측. |
| 세정기(sanitizer) | **정규화된 행만** 받는다. 원문 body·크리덴셜·연락처가 남아 있으면 씻지 않고 **거부**한다(§4). |

## 2. 동일성과 우선순위

* 공고의 신원은 **복합키 `(provenance.provider, id)`**. 같은 공고를 두 번 넣으면
  행 하나가 갱신될 뿐, 쌓이지 않는다.
* `platform` 은 `provider` 와 일치해야 한다. 아는 제공자는
  `harness/sources/public_jobs/policy.py` 의 표시명을, 모르는 제공자는 이 저장소가
  그 provider 에 대해 **처음 본 platform** 을 기준으로 한다. 어긋나면 조용히
  고치지 않고 건너뛴다(`provider_platform_mismatch`).
* **새 관측만 이긴다.** 우선순위는 적재 순서가 아니라 관측된
  `provenance.fetched_at` 이 정한다. 더 새롭지 않은 관측은 내용도 신선도도 건드리지
  못한다. 어제 산출물을 다시 넣어도 그 사이 닫힌 공고를 되살릴 수 없고, 오래된
  행을 신선하게 만들 수도 없다.
* 더 새로운 `closed` / `paused` / `expired` / `unknown` 관측은 저장된 `recruiting`
  을 대체하고, 그 공고는 즉시 스냅샷에서 빠진다. **`unknown` 은 "아직 모집 중"으로
  읽지 않는다.**

### `first_seen` / `last_seen`

둘 다 **적재 시각이 아니라 관측 시각**이다.

* `last_seen` — 지금 저장된 내용을 만든 관측의 `fetched_at`(가장 최신). 신선도는
  여기서 잰다.
* `first_seen` — 이 키에 대해 관측된 가장 이른 `fetched_at`. 오래된 관측이 뒤늦게
  들어오면 `first_seen` 은 뒤로 물러날 수 있다(처음 본 시점의 교정). 저장된 내용과
  `last_seen` 은 그대로다.

## 3. 스냅샷이 말하는 것과 말하지 않는 것

`snapshot(max_age_hours=24, now=None)` 은 **모집중(`recruiting`)이고 신선한** 행만,
수집기와 같은 봉투 모양으로 돌려준다.

* 행은 **저장된 그대로** 나온다. 각 행의 `provenance.data_mode` 는 절대 고쳐 쓰지
  않는다 — live 로 수집한 행과 승인 임포트 행이 섞여 있으면 행마다 자기 모드를
  그대로 들고 나온다.
* `meta.job_source` / `meta.data_mode` 는 돌려주는 행들이 한 값으로 합의할 때만 그
  값이고, 섞였으면 **`"mixed"`**, 행이 없으면 `null` 이다. 하나를 골라주지 않는다.
  `meta.sources` / `meta.data_modes` 에 값별 개수가 그대로 있다.
* `meta.excluded` 가 왜 빠졌는지(`not_recruiting`, `stale`) 개수로 말한다.
* 스냅샷에 있다 = **최근에 모집중으로 관측됐다**. 편성 가능(schedule eligible)이라는
  뜻은 아니다. 시급·요일·위치 재검사는 importer 의 일이고 여기서 하지 않는다.
* 행에는 이 저장소가 아는 것만 담은 `store` 블록(`first_seen`/`last_seen`/
  `observations`)이 붙는다. 제공자가 게시한 값이 아니라 저장소가 아는 값이다.

### importer 로 자동 승격되지 않는다

이 스냅샷은 **자동으로 운영 importer 에 들어가지 않는다.** 현재 importer
(`harness/sources/imported_jobs.py`)가 받는 것은 `www.alba.co.kr` 과
`www.albamon.com` 의 HTTPS 공고 경로뿐이다(`ALLOWED_SOURCE_PATHS`). 고용24(work24)
같은 API 출처를 승격하려면 스키마·증거 검토를 거쳐야 하고, **그것을 건너뛰는
플래그는 없다.** 이 레인은 저장과 스냅샷까지이고, 활성화는 별도 판단이다.

> 2026-09-20 기준 고용24 API 키는 아직 없다. 어댑터와 저장소는 오프라인 합성
> 봉투로 독립 검증돼 있고, 라이브 전환은 이번 작업에 포함되지 않는다.

## 4. 정규화된 입력의 경계

정규화된 JSON 공고 행만 저장한다. 다음을 들고 있는 레코드는 씻지 않고 건너뛴다
(이유별 개수는 `ingest` 요약에 나온다):

| 거부 사유 | 무엇 |
|---|---|
| `raw_body_rejected` | `html`, `body`, `raw`, `content` … 원문 페이지 덤프 |
| `credential_rejected` | `authKey`/`serviceKey`/`token`/`secret` 류 키, 또는 그런 질의 파라미터를 달고 있는 URL |
| `contact_data_rejected` | `phone`, `email`, `manager`, `kakao` … 사람에게 닿는 정보 |
| `unsafe_source_url` | HTTPS 가 아닌 스킴(`javascript:`, `data:` …), `user@host` 권한부 |
| `record_too_large` | 64 KiB 초과 — 행이 아니라 덤프 |
| `bad_fetched_at`, `fetched_at_in_future` | 오프셋 없는/파싱 불가 스탬프, 시계 오차(5분) 넘게 미래인 스탬프 |

envelope 의 `meta` 는 통째로 저장하지 않는다. `job_source`, `data_mode`,
`attempted`, `collected` 와 오류 **개수**만 남는다. 운영 메모나 티켓 번호, 어쩌다
`meta` 에 들어간 키는 DB 에 닿지 않는다.

레코드 하나가 망가진 것은 **건너뛰고 세는** 일이고, **봉투가 망가진 것은 실패**다
(`JobStoreError`, 메시지에 경로·행 내용이 들어가지 않는다). 적재는 트랜잭션 하나로
끝난다: 받아들인 행은 전부 들어가거나 전부 안 들어간다.

## 5. 수명주기 — 행은 어떻게 사라지는가

스냅샷은 애초에 `recruiting` + 신선한 행만 보여주므로, 닫히거나 멈췄거나 모르는
상태거나 오래된 공고는 **관측되는 즉시 / 나이를 먹는 즉시** 추천에서 빠진다.
물리 삭제는 그와 별개이고, 삭제 방법마다 요구하는 증거가 다르다.

| 방법 | 증거 | 하는 일 |
|---|---|---|
| **적재 중 명시 삭제** | 저장된 것보다 **새로운** 관측이 `closed`/`gone`/`expired`/`paused` | 행을 물리 삭제하고 `removed_explicit` 로 센다 |
| **봉투의 removal 통지** | 봉투 최상위 `removals[]` 항목이 종결 `kind` 이고, 저장된 행과 삭제 워터마크 **둘 다보다** 새로움 | 행을 물리 삭제하고 `removals_applied` 로 센다(§5.1) |
| `prune_stale(retention_hours)` | 마지막 **검증된 관측**이 보존 기간보다 오래됨 | 행을 물리 삭제. 보존 기간은 스냅샷 신선도 창보다 **반드시 길어야** 한다(짧으면 `retention_too_short` 로 거부) |
| `reconcile_missing(provider, seen_ids, …)` | **성공적으로 완료된 전체 동기화** | 그 동기화가 못 본 해당 provider 행만 삭제 |

`unknown` 은 삭제하지 않는다. 가져오기 자체가 불확실했다는 뜻일 수 있고, 불확실은
멀쩡한 행을 지울 근거가 못 된다 — 숨기되 남긴다.

### 5.1 봉투의 removal 통지 (work24 연동 seam)

work24 어댑터는 사라진 공고를 "행 없이" 알려준다. 봉투 최상위에 이런 항목이 온다:

```json
{
  "jobs": [],
  "removals": [
    {"provider": "work24", "platform": "work24", "id": "work24_<auth>",
     "kind": "closed", "observed_at": "2026-09-20T11:00:00+09:00",
     "source": "work24_api", "evidence": "..."}
  ],
  "meta": {"job_source": "public_api", "data_mode": "live", "complete_sync": false}
}
```

* 통지는 **공고가 아니라 공고에 대한 증거**다. 그래서 `provider`, `id`, 종결 `kind`,
  오프셋 있는 `observed_at` 만 요구한다 — 공고 행도, `source_url` 도 필요 없다.
* 받아들이는 `kind` 는 `closed` `gone` `not_found` `expired` `paused` 뿐이다.
  전송 실패(5xx)는 여기 오지 않으며, 모르는 `kind` 는 **아무것도 지우지 않는다**
  (`removal_kind_unknown`).
* 저장된 행과 삭제 워터마크 **둘 다보다 새로울 때만** 적용된다. 오래된 removal
  파일을 다시 넣어도, 그 사이 다시 모집중으로 관측된 공고를 지우지 못한다
  (`removal_not_newer`).
* 보관 중이 아니던 공고에 대한 통지도 워터마크는 남긴다. 그래야 그보다 오래된
  산출물이 나중에 그 공고를 다시 집어넣지 못한다.
* 적용은 같은 트랜잭션 안에서 행 적재와 함께 끝난다. 거부 사유는 행 거부와 **따로**
  센다(`removal_skipped_reasons` — `removal_malformed`, `removal_kind_unknown`,
  `removal_bad_observed_at`, `removal_observed_in_future`, `credential_rejected`,
  `removal_not_newer`). 통지의 `source`/`evidence` 는 검사만 하고 저장하지 않는다;
  남는 것은 워터마크의 시각 하나뿐이다.
* `removals` 가 리스트가 아니면 봉투 자체가 실패한다(`malformed_envelope`).
* 이 통지들의 `complete_sync` 는 항상 `false` 이고, 이 저장소는 **그 값을 삭제
  근거로 쓰지 않는다.** 부재로 지우는 경로는 아래 `reconcile_missing` 하나뿐이다.

### 부분 동기화로는 지울 수 없다

```python
store.reconcile_missing(
    "alba", seen_ids,
    full_sync_completed=True,      # 필수 키워드, 기본값 없음
    sync_token="nightly-2026-09-20",
)
```

`full_sync_completed` 와 `sync_token` 은 **기본값 없는 필수 키워드 인자**다. 부분
배치·실패한 가져오기·검색 결과 일부에서 플래그를 깜빡해서 이 경로에 도달할 수 없다.
`seen_ids` 가 비어 있으면 거부한다(`empty_full_sync`) — 아무것도 못 본 "완료된"
동기화는 망가진 동기화와 구분되지 않기 때문이다. 다른 provider 의 행은 건드리지
않는다. 동기화 기록은 공고 행이 아니라 별도 테이블(`provider_syncs`)에 남는다.

**부재는 그 자체로 폐쇄가 아니다.** 적재는 넣거나, 갱신하거나, 명시적 증거로
지우기만 한다. 봉투에 없는 공고는 그대로 둔다 — 실패하거나 부분적인 가져오기
(`collected: 0` + errors)는 "전부 한꺼번에 닫혔다"와 겉모습이 같기 때문이다.

### 지운 공고를 되살리지 않기 위한 최소 기억

명시 삭제 한 건마다 `(provider, job_id, 관측시각)` 한 줄만 남긴다(`removals`).
상태도 내용도 이력도 없다. 하는 일은 하나다: **닫힘 관측보다 오래된 산출물이
그 공고를 다시 집어넣지 못하게** 한다. 진짜로 다시 모집한다는 **더 새로운** 관측이
오면 그 줄은 지워지고 공고는 정상적으로 돌아온다. 툼스톤 아카이브도 감사 로그도
아니다.

## 6. CLI

```
python examples/manage_job_store.py ingest ARTIFACT.json [--db PATH]
python examples/manage_job_store.py status [--db PATH] [--max-age-hours N]
python examples/manage_job_store.py prune --retention-hours N [--db PATH]
python examples/manage_job_store.py export --out FILE [--db PATH] [--max-age-hours N]
```

`reconcile` 서브커맨드는 **없다.** 부재를 삭제로 바꾸려면 "전체 동기화가 끝났다"는
증언이 필요한데, 명령줄은 그것을 신뢰성 있게 나를 수 없다 — 중간에 끊긴 수집과
끝까지 간 수집이 같은 파일을 만든다. 그 경로는 API 로 남겨 두고 위에 문서화했다.

### 어디에 쓸 수 있는가

`--db` 와 `--out` 은 `.runtime/` 아래, 또는 `HARNESS_JOB_STORE_DIR` 이 가리키는
디렉터리 아래여야 한다. `harness/fixtures/`(정본 600행 데이터셋)와 소스 파일
확장자(`.py`, `.md`, `.js` …)는 무조건 거부한다. 오타 하나로 정본이나 코드를
덮어쓰는 일이 없게 하려는 것이고, 그 이상으로 넓히지 않았다.

실패는 코드와 고정된 문장 한 줄로 끝난다. **입력 경로도, 행 내용도, 비밀도 출력에
나오지 않는다.**

```
$ python examples/manage_job_store.py ingest run.json --db harness/fixtures/jobs.sqlite3
unsafe_path: harness/fixtures is the canonical dataset and is never written here (allowed: .runtime/ or HARNESS_JOB_STORE_DIR)
```

## 7. 테스트

`tests/test_job_store.py` — 48개, 네트워크 없음, 전부 임시 디렉터리에서 돈다.
재시작 후 지속성, 중복 upsert, 오래된 관측이 되살리지도 신선도를 갱신하지도 못함,
`closed`/`paused`/`expired`/`gone` 물리 삭제, `unknown` 은 숨기되 보존, 실패·부분
가져오기가 아무것도 지우지 않음, 완료된 전체 동기화만 부재 행을 지움, 봉투 removal
통지(유효/오래됨/망가짐/모르는 kind/빈 봉투), 신선도 경계,
망가진 입력의 사유별 집계, 경로 정책, 그리고 임시 데이터로 도는 `ingest → export`
CLI 증명까지 덮는다. 정본 600행 픽스처는 이 레인에서 읽기만 한다.
