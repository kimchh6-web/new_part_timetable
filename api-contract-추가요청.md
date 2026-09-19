# API 계약서 추가 요청 — `PlanJob` 필드 3개

> 구현 상태: **두 경로 모두 반영되었습니다.** 주간 `POST /api/recommendations`
> 응답의 `plans[].jobs[]`가 `timeNegotiable`, `minWeeks`, `benefits`를 담습니다
> ([weekly.v1](examples/WEEKLY_API.md)). 일일 라이브 데모 API는 `PlanJob`이 없으므로
> 같은 세 값을 `schedule`의 job block에 담습니다 ([현재 API 계약](api-contract.md)).

> 대상: `POST /api/recommendations` 응답의 `plans[].jobs[]` (`PlanJob`)
> 요청자: 프론트엔드 / 2026-09-19

## 요청 내용

알바 상세 카드에 아래 배지 3개를 표시해야 하는데, 현재 `PlanJob`에 해당 필드가 없습니다.
`PlanJob`에 다음 3개 필드를 추가해 주세요.

| 필드 | 타입 | 화면 표시 | 비고 |
|---|---|---|---|
| `timeNegotiable` | `boolean` | 배지 `시간 협의 가능` (true일 때만) | 7-1의 `timeNegotiable`과 동일 값 |
| `minWeeks` | `number` | `최소 N주` | 최소 근무 기간(주). 없으면 `null` |
| `benefits` | `string[]` | `식사 제공 · 주휴수당 지급` 형태로 나열 | 빈 배열 허용. 최대 5개면 충분 |

## 예시 (기존 `PlanJob` 예시에 추가된 부분만)

```jsonc
{
  "jobId": "job_at0001",
  "title": "[강남역] 퇴근 후 저녁 홀서빙 (19시~23시)",
  "company": "투썸플레이스 강남역점",
  // ... 기존 필드 동일 ...

  "timeNegotiable": true,
  "minWeeks": 4,
  "benefits": ["식사 제공", "주휴수당 지급"],

  "assignedShifts": [ /* 동일 */ ],
  "weeklyHours": 8,
  "weeklyPay": 104000
}
```

## TypeScript 타입 변경

```ts
interface PlanJob {
  // ... 기존 필드 ...
  timeNegotiable: boolean;
  minWeeks: number | null;
  benefits: string[];
  // ...
}
```

## 이유

- 세 값 모두 공고 원본(`jobs.json`)에 이미 있는 정보라 서버 쪽 추가 비용이 거의 없습니다.
- 7-6(jobs.json 보유 주체)이 **서버 보유**로 가는 전제이므로, 프론트가 `jobId`로 따로 조회할 수단이 없습니다. 응답에 실어 주셔야 합니다.
- 현재 프론트 목데이터는 `negotiable` / `minWeeks` / `benefits`라는 이름을 쓰고 있습니다. 위 표의 이름(`timeNegotiable`)으로 확정되면 프론트에서 맞추겠습니다.

## 그 외

이 3개 외에는 현재 계약서만으로 결과 화면(3안 카드 · 주간 시간표 · 지표 · 상세 · 연락처 · 고정/제외 재생성) 구성이 가능합니다.
