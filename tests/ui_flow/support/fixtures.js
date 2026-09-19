/* =====================================================================
 * tests/ui_flow/support/fixtures.js
 *
 * 합성 픽스처. harness/fixtures/jobs.json 은 읽지도 고치지도 않는다.
 * 실제로 잡아낸 주간 응답(.runtime/weekly-smoke-response.json)의 모양만
 * 본떠 손으로 쓴 값이며, 연락처·링크는 전부 example.invalid 다.
 *
 * 핵심 장치: 서버 metrics 를 근무시간 산술과 일부러 어긋나게 둔다.
 *   배정된 근무 합계는 주 11시간인데 서버는 weeklyWorkHours 28 을 준다.
 *   화면이 28.0h 를 그리면 "서버 값을 그대로 쓴다"가 증명되고,
 *   11.0h 를 그리면 프론트가 몰래 다시 계산한 것이다.
 * ===================================================================*/
'use strict';

const DAY_KEYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

function profileFixture(over = {}) {
  const schedule = {};
  for (const d of DAY_KEYS) schedule[d] = { has: false, start: '', end: '', place: '' };
  schedule.MON = { has: true, start: '09:00', end: '18:00', place: '강남역' };
  schedule.TUE = { has: true, start: '09:00', end: '18:00', place: '강남역' };
  schedule.THU = { has: true, start: '09:00', end: '18:00', place: '강남역' };
  return {
    role: '직장인',
    sameDaily: false,
    schedule,
    home: '사당',
    minBlock: 3,
    night: true,
    want15: true,
    goal: 600000,
    age: 26,
    ...over,
  };
}

const searchFixture = (over = {}) => ({ count: 2, categories: ['카페·음식점'], priority: 'flex', ...over });

function travel(over = {}) {
  return {
    fromLocation: '강남역',
    transitMinutes: 30,
    walkMinutes: 13,
    bufferMinutes: 15,
    departAt: '18:02',
    ...over,
  };
}

/* 잡 A — 배정 근무 주 6시간 (MON/THU 19:00-22:00) */
function jobA(over = {}) {
  return {
    jobId: 'job_cp0095',
    pinned: false,
    title: '[노원] 저녁 홀서빙',
    company: '치킨하우스 반반 노원점',
    platform: '알바천국',
    category: '카페·음식점',
    location: '노원',
    address: '서울 노원구 노해로 480',
    hourlyWage: 11500,
    rating: 4.2,
    reviewCount: 38,
    thumbnail: null,
    descriptionSnippet: '홀 주문과 서빙을 맡습니다',
    sourceUrl: 'https://example.invalid/job/cp0095',
    contact: { manager: '김지현 점장', phone: '010-0000-1111', kakao: null, applyUrl: null, preferred: '전화' },
    timeNegotiable: true,
    minWeeks: 4,
    benefits: ['식사 제공', '주휴수당 지급'],
    assignedShifts: [
      { day: 'MON', start: '19:00', end: '22:00', travel: travel() },
      { day: 'THU', start: '19:00', end: '22:00', travel: travel() },
    ],
    weeklyHours: 6,
    weeklyPay: 69000,
    ...over,
  };
}

/* 잡 B — 배정 근무 주 5시간 (SAT 10:00-15:00) */
function jobB(over = {}) {
  return {
    jobId: 'job_st0042',
    pinned: false,
    title: '[여의도] 주말 매장 정리',
    company: '올리브영 여의도점',
    platform: '알바몬',
    category: '매장판매',
    location: '여의도',
    address: '서울 영등포구 여의대로 108',
    hourlyWage: 10800,
    rating: 4.6,
    reviewCount: 121,
    thumbnail: null,
    descriptionSnippet: '진열과 재고 정리를 맡습니다',
    sourceUrl: 'https://example.invalid/job/st0042',
    contact: { manager: '박서준 매니저', phone: '010-0000-2222', kakao: null, applyUrl: null, preferred: '문자' },
    timeNegotiable: false,
    minWeeks: 8,
    benefits: ['교통비 지원'],
    assignedShifts: [
      { day: 'SAT', start: '10:00', end: '15:00', travel: travel({ fromLocation: '사당', transitMinutes: 22, walkMinutes: 8, departAt: '09:15' }) },
    ],
    weeklyHours: 5,
    weeklyPay: 54000,
    ...over,
  };
}

/* 잡 C — 3안 전용 (SUN 13:00-18:00) */
function jobC(over = {}) {
  return {
    jobId: 'job_dl0007',
    pinned: false,
    title: '[성수] 주말 물류 보조',
    company: '쿠팡 성수캠프',
    platform: '알바천국',
    category: '물류·배송',
    location: '성수',
    address: '서울 성동구 아차산로 100',
    hourlyWage: 12000,
    rating: 3.9,
    reviewCount: 64,
    thumbnail: null,
    descriptionSnippet: '분류와 상차를 맡습니다',
    sourceUrl: 'https://example.invalid/job/dl0007',
    contact: null,
    timeNegotiable: null,
    minWeeks: null,
    benefits: [],
    assignedShifts: [
      { day: 'SUN', start: '13:00', end: '18:00', travel: travel({ fromLocation: '사당', transitMinutes: 35, walkMinutes: 6, departAt: '12:14' }) },
    ],
    weeklyHours: 5,
    weeklyPay: 60000,
    ...over,
  };
}

/**
 * 3안 응답. 지표는 배정 근무 산술과 어긋나게 두었다 (위 주석 참고).
 * 3안(balanced)은 effectiveHourlyWage / weeklyHolidayPayIncluded 가 없어서
 * 화면이 '—' 와 '주휴수당 여부 미확인' 을 그려야 한다.
 */
function responseFixture(over = {}) {
  return {
    requestId: 'req_UIFLOW_0001',
    generatedAt: '2026-09-19T14:32:10+09:00',
    source: 'llm',
    candidateCount: 187,
    availableSlots: [
      { day: 'MON', from: '18:00', to: '24:00', fromLocation: '강남역' },
      { day: 'SAT', from: '07:00', to: '24:00', fromLocation: '사당' },
    ],
    plans: [
      {
        id: 'plan_max_income_66489241',
        type: 'maxIncome',
        label: '수입 최대안',
        reason: '시급이 가장 높은 조합입니다.',
        metrics: {
          monthlyIncome: 1734792,
          targetAchievementRate: 2.89,
          weeklyWorkHours: 28,
          weeklyTravelMinutes: 528,
          effectiveHourlyWage: 10963,
          weeklyHolidayPayIncluded: false,
        },
        jobs: [jobA(), jobB()],
        warnings: [{ code: 'LONG_TRAVEL', message: '노원까지 편도 43분이 걸립니다.', jobId: 'job_cp0095' }],
      },
      {
        id: 'plan_min_travel_1197a3c0',
        type: 'minTravel',
        label: '이동 최소안',
        reason: '이동 시간이 가장 짧은 조합입니다.',
        metrics: {
          monthlyIncome: 934110,
          targetAchievementRate: 1.55,
          weeklyWorkHours: 15,
          weeklyTravelMinutes: 96,
          effectiveHourlyWage: 9812,
          weeklyHolidayPayIncluded: true,
        },
        jobs: [jobB(), jobC()],
        warnings: [],
      },
      {
        id: 'plan_balanced_5d20e4b1',
        type: 'balanced',
        label: '밸런스안',
        reason: '수입과 이동이 고르게 섞인 조합입니다.',
        metrics: {
          monthlyIncome: 1210500,
          targetAchievementRate: 2.01,
          weeklyWorkHours: 20,
          weeklyTravelMinutes: 240,
        },
        jobs: [jobA(), jobC()],
        warnings: [],
      },
    ],
    meta: { model: 'synthetic-fixture', jobsLoaded: 600 },
    ...over,
  };
}

/** 계약 에러 본문 (HTTP 상태와 함께 준다) */
function errorBody(code, message, details) {
  return { error: { code, message, details: details || null } };
}

/* ---------- 옛 형식(schemaVersion 1) 저장물 ---------- */
function legacySchedule(over = {}) {
  return {
    id: 's_legacy_1',
    title: '5월 수입 최대안',
    createdAt: '2026-05-02T09:00:00.000Z',
    kind: 'income',
    profile: profileFixture(),
    jobs: [
      {
        id: 'old_job_1',
        title: '[신촌] 카페 마감',
        company: '옛날카페 신촌점',
        category: '카페·음식점',
        location: '신촌',
        address: '서울 서대문구 연세로 1',
        hourlyWage: 10320,
        rating: 4.0,
        reviewCount: 12,
        negotiable: true,
        minWeeks: 4,
        benefits: ['식사 제공'],
        contact: { manager: '이수민 점장', phone: '010-0000-9999' },
        description: '마감 청소와 정산',
        shifts: [
          { day: 'WED', start: '18:00', end: '22:00' },
          { day: 'FRI', start: '18:00', end: '22:00' },
        ],
      },
    ],
    ...over,
  };
}

function legacyHistoryEntry(over = {}) {
  return {
    id: 'h_legacy_1',
    createdAt: '2026-05-02T08:00:00.000Z',
    search: { count: 2, categories: [], priority: 'wage' },
    pinned: [],
    excluded: [],
    combos: [{ kind: 'income', jobs: [{ id: 'old_job_1' }] }],
    ...over,
  };
}

module.exports = {
  DAY_KEYS,
  profileFixture,
  searchFixture,
  responseFixture,
  errorBody,
  jobA,
  jobB,
  jobC,
  legacySchedule,
  legacyHistoryEntry,
};
