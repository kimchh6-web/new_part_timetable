/* =====================================================================
 * tests/web-api-client.test.js
 *
 * 실행: node --test tests/web-api-client.test.js
 *
 * 대상: js/api-client.js — 요청 어댑터 / 응답 어댑터 / 에러 어댑터 / 호출.
 * 브라우저 없이 순수 함수만 검증한다.
 * ===================================================================*/
const test = require('node:test');
const assert = require('node:assert/strict');
const api = require('../js/api-client.js');

/* ---------- 화면이 저장하는 프로필·탐색 조건 형태 ---------- */
function profileFixture(over = {}) {
  const schedule = {
    MON: { has: true, start: '09:00', end: '18:00', place: '강남역' },
    TUE: { has: true, start: '09:00', end: '18:00', place: '강남역' },
    WED: { has: false, start: '', end: '', place: '' },
    THU: { has: false, start: '', end: '', place: '' },
    FRI: { has: false, start: '', end: '', place: '' },
    SAT: { has: false, start: '', end: '', place: '' },
    SUN: { has: false, start: '', end: '', place: '' },
  };
  return { role: '직장인', home: '사당', schedule, minBlock: 3, night: false, want15: true, goal: 600000, ...over };
}
const searchFixture = (over = {}) => ({ count: 2, categories: ['카페·음식점'], priority: 'flex', ...over });

function planJobFixture(over = {}) {
  return {
    jobId: 'job_at0001',
    pinned: true,
    title: '[강남역] 퇴근 후 저녁 홀서빙',
    company: '투썸플레이스 강남역점',
    platform: '알바천국',
    category: '카페·음식점',
    location: '강남역',
    address: '서울 강남구 테헤란로 88',
    hourlyWage: 13000,
    rating: 4.3,
    reviewCount: 57,
    thumbnail: 'https://example.invalid/t.png',
    descriptionSnippet: '주문 받고 음식 서빙',
    sourceUrl: 'https://example.invalid/job/at0001',
    contact: { manager: '김지현 점장', phone: '010-0000-5382', kakao: null, applyUrl: null, preferred: '전화' },
    timeNegotiable: true,
    minWeeks: 4,
    benefits: ['식사 제공', '주휴수당 지급'],
    assignedShifts: [
      { day: 'TUE', start: '19:00', end: '23:00', travel: { fromLocation: '강남역', transitMinutes: 5, walkMinutes: 6, bufferMinutes: 15, departAt: '18:34' } },
    ],
    weeklyHours: 4,
    weeklyPay: 52000,
    ...over,
  };
}

function responseFixture(over = {}) {
  return {
    requestId: 'req_01HX8F2K9M',
    generatedAt: '2026-09-19T14:32:10+09:00',
    source: 'llm',
    availableSlots: [{ day: 'MON', from: '18:00', to: '24:00', fromLocation: '강남역' }],
    candidateCount: 23,
    plans: [{
      id: 'plan_max_income',
      type: 'maxIncome',
      label: '수입 최대안',
      reason: '목표 금액을 넘기는 조합입니다.',
      metrics: { monthlyIncome: 688000, targetAchievementRate: 1.15, weeklyWorkHours: 16, weeklyTravelMinutes: 180, effectiveHourlyWage: 10720, weeklyHolidayPayIncluded: true },
      jobs: [planJobFixture()],
      warnings: [{ code: 'LONG_TRAVEL', message: '토요일 이동이 편도 45분입니다.', jobId: 'job_cp0001' }],
    }],
    meta: { llmLatencyMs: 6240, totalLatencyMs: 6890, filteredFrom: 546 },
    ...over,
  };
}

/* =====================================================================
 * 1. 요청 어댑터
 * ===================================================================*/
test('buildRequest: 화면 입력을 계약 Request 로 옮긴다', () => {
  const req = api.buildRequest(profileFixture(), searchFixture());

  assert.deepEqual(req.profile.fixedSchedules, [
    { day: 'MON', start: '09:00', end: '18:00', endLocation: '강남역' },
    { day: 'TUE', start: '09:00', end: '18:00', endLocation: '강남역' },
  ]);
  assert.equal(req.profile.role, '직장인');
  assert.equal(req.profile.home, '사당');
  assert.equal(req.profile.targetAmount, 600000);
  assert.deepEqual(req.profile.constraints, { minBlockHours: 3, allowNight: false, wantWeeklyHolidayPay: true });
  assert.deepEqual(req.search, { jobCount: 2, categories: ['카페·음식점'], priority: 'flexibility' });
  assert.equal('regenerate' in req, false, '최초 호출에는 regenerate 를 보내지 않는다');
});

test('buildRequest: 나이는 입력했을 때만 싣는다', () => {
  assert.equal('age' in api.buildRequest(profileFixture(), searchFixture()).profile.constraints, false);
  assert.equal('age' in api.buildRequest(profileFixture({ age: null }), searchFixture()).profile.constraints, false);
  assert.equal('age' in api.buildRequest(profileFixture({ age: 0 }), searchFixture()).profile.constraints, false);
  assert.equal(api.buildRequest(profileFixture({ age: 26 }), searchFixture()).profile.constraints.age, 26);
  assert.equal(api.buildRequest(profileFixture({ age: '26' }), searchFixture()).profile.constraints.age, 26);
});

test('buildRequest: 값을 지어내지 않고 계약 범위로만 맞춘다', () => {
  const req = api.buildRequest(profileFixture({ minBlock: 7 }), searchFixture({ count: 9, priority: '??', categories: ['카페·음식점', '카페·음식점', ''] }));
  assert.equal(req.profile.constraints.minBlockHours, 2);
  assert.equal(req.search.jobCount, 3);
  assert.equal(req.search.priority, 'wage');
  assert.deepEqual(req.search.categories, ['카페·음식점']);
});

test('buildRequest: 시간이 비어 있는 고정 일정 행은 보내지 않는다', () => {
  const p = profileFixture();
  p.schedule.WED = { has: true, start: '', end: '', place: '신촌' };
  const req = api.buildRequest(p, searchFixture());
  assert.deepEqual(req.profile.fixedSchedules.map(f => f.day), ['MON', 'TUE']);
});

test('buildRequest: 고정/제외/이미 본 조합이 regenerate 로 실린다 (pin·exclude 페이로드)', () => {
  const req = api.buildRequest(profileFixture(), searchFixture(), {
    pinned: ['job_at0001', 'job_at0001'],
    excluded: ['job_cp0043'],
    previousPlanHashes: ['plan_max_income', 'plan_balanced'],
  });
  assert.deepEqual(req.regenerate, {
    pinnedJobIds: ['job_at0001'],
    excludedJobIds: ['job_cp0043'],
    previousPlanHashes: ['plan_max_income', 'plan_balanced'],
  });
});

test('buildRequest: 계약 필드명(pinnedJobIds 등)으로 줘도 받는다', () => {
  const req = api.buildRequest(profileFixture(), searchFixture(), { pinnedJobIds: ['a'], excludedJobIds: ['b'] });
  assert.deepEqual(req.regenerate.pinnedJobIds, ['a']);
  assert.deepEqual(req.regenerate.excludedJobIds, ['b']);
  assert.deepEqual(req.regenerate.previousPlanHashes, []);
});

test('validateRequest: 서버에 보내기 전에 잡히는 규칙', () => {
  assert.deepEqual(api.validateRequest(api.buildRequest(profileFixture(), searchFixture())), []);

  const dup = api.buildRequest(profileFixture(), searchFixture());
  dup.profile.fixedSchedules.push({ day: 'MON', start: '10:00', end: '12:00', endLocation: '신촌' });
  assert.ok(api.validateRequest(dup).some(p => p.code === 'SCHEDULE_CONFLICT'));

  const rev = api.buildRequest(profileFixture(), searchFixture());
  rev.profile.fixedSchedules[0].end = '08:00';
  assert.ok(api.validateRequest(rev).some(p => p.code === 'VALIDATION_ERROR' && p.field === 'fixedSchedules'));

  const over = api.buildRequest(profileFixture({ goal: 20000000 }), searchFixture());
  assert.ok(api.validateRequest(over).some(p => p.field === 'targetAmount'));

  const zero = api.buildRequest(profileFixture({ goal: 0 }), searchFixture());
  assert.ok(api.validateRequest(zero).some(p => p.field === 'targetAmount'));

  const pins = api.buildRequest(profileFixture(), searchFixture({ count: 1 }), { pinned: ['a', 'b'] });
  assert.ok(api.validateRequest(pins).some(p => p.code === 'PINNED_EXCEEDS_COUNT'));
});

/* =====================================================================
 * 2. 응답 어댑터
 * ===================================================================*/
test('adaptResponse: 서버 값을 그대로 옮긴다 (재계산 없음)', () => {
  const out = api.adaptResponse(responseFixture());
  assert.equal(out.source, 'llm');
  assert.equal(out.candidateCount, 23);
  assert.equal(out.plans.length, 1);

  const plan = out.plans[0];
  assert.equal(plan.id, 'plan_max_income');
  assert.equal(plan.type, 'maxIncome');
  assert.deepEqual(plan.metrics, {
    monthlyIncome: 688000, targetAchievementRate: 1.15, weeklyWorkHours: 16,
    weeklyTravelMinutes: 180, effectiveHourlyWage: 10720, weeklyHolidayPayIncluded: true,
  });
  assert.deepEqual(plan.warnings, [{ code: 'LONG_TRAVEL', message: '토요일 이동이 편도 45분입니다.', jobId: 'job_cp0001' }]);

  const job = plan.jobs[0];
  assert.equal(job.jobId, 'job_at0001');
  assert.equal(job.pinned, true);
  assert.equal(job.timeNegotiable, true);
  assert.equal(job.minWeeks, 4);
  assert.deepEqual(job.benefits, ['식사 제공', '주휴수당 지급']);
  assert.equal(job.weeklyHours, 4);
  assert.deepEqual(job.assignedShifts[0], {
    day: 'TUE', start: '19:00', end: '23:00',
    travel: { fromLocation: '강남역', transitMinutes: 5, walkMinutes: 6, bufferMinutes: 15, departAt: '18:34' },
  });
  assert.deepEqual(out.availableSlots, [{ day: 'MON', from: '18:00', to: '24:00', fromLocation: '강남역' }]);
});

test('adaptResponse: 없는 선택 필드는 null 로 두고 만들어 내지 않는다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs = [{ jobId: 'job_x', assignedShifts: [{ day: 'SAT', start: '9:00', end: '15:00' }] }];
  raw.plans[0].metrics = { monthlyIncome: 400000 };
  const job = api.adaptResponse(raw).plans[0].jobs[0];

  assert.equal(job.timeNegotiable, null, '미지정이면 false 가 아니라 null');
  assert.equal(job.minWeeks, null);
  assert.deepEqual(job.benefits, []);
  assert.equal(job.hourlyWage, null);
  assert.equal(job.rating, null);
  assert.equal(job.weeklyPay, null);
  assert.equal(job.contact, null);
  assert.equal(job.pinned, false);
  assert.deepEqual(job.assignedShifts[0], { day: 'SAT', start: '09:00', end: '15:00', travel: null });

  const m = api.adaptResponse(raw).plans[0].metrics;
  assert.equal(m.monthlyIncome, 400000);
  assert.equal(m.weeklyWorkHours, null);
  assert.equal(m.weeklyHolidayPayIncluded, null);
});

test('adaptResponse: 형식이 깨진 근무/안은 버리고, 남은 게 없으면 던진다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs[0].assignedShifts = [
    { day: 'TUE', start: '19:00', end: '23:00' },
    { day: 'XXX', start: '19:00', end: '23:00' },
    { day: 'WED', start: 'abc', end: '23:00' },
    null,
  ];
  raw.plans.push({ id: 'plan_broken', jobs: [{ noJobId: true }] });
  const out = api.adaptResponse(raw);
  assert.equal(out.plans.length, 1);
  assert.equal(out.droppedPlans, 1);
  assert.equal(out.plans[0].jobs[0].assignedShifts.length, 1);

  assert.throws(() => api.adaptResponse(responseFixture({ plans: [] })), e => e.code === 'MALFORMED_RESPONSE');
  assert.throws(() => api.adaptResponse(responseFixture({ plans: [{ id: 'p', jobs: [] }] })), e => e.code === 'MALFORMED_RESPONSE');
  assert.throws(() => api.adaptResponse({ requestId: 'r' }), e => e.code === 'MALFORMED_RESPONSE');
  assert.throws(() => api.adaptResponse('not json object'), e => e.code === 'MALFORMED_RESPONSE');
  assert.throws(() => api.adaptResponse(null), e => e.code === 'MALFORMED_RESPONSE');
});

test('adaptResponse: source 가 fallback 이어도 에러가 아니다', () => {
  const out = api.adaptResponse(responseFixture({ source: 'fallback' }));
  assert.equal(out.source, 'fallback');
  assert.equal(out.plans.length, 1);
});

test('adaptResponse: 200 본문에 error 가 실려 있으면 에러로 처리한다', () => {
  assert.throws(
    () => api.adaptResponse({ error: { code: 'NO_CANDIDATES', message: '없습니다.' } }),
    e => e.code === 'NO_CANDIDATES');
});

/* =====================================================================
 * 3. 에러 어댑터
 * ===================================================================*/
test('adaptError: 계약 에러 본문을 코드·제안까지 옮긴다', () => {
  const err = api.adaptError({
    status: 422,
    body: { error: { code: 'NO_CANDIDATES', message: '조건에 맞는 공고를 찾지 못했습니다.', details: {
      filteredFrom: 546, afterTimeFilter: 12, afterCategoryFilter: 0,
      suggestions: [{ type: 'expandCategory', label: '카테고리 넓히기' }, { type: 'lowerMinBlock', label: '2시간으로', value: 2 }],
    } } },
  });
  assert.equal(err.code, 'NO_CANDIDATES');
  assert.equal(err.status, 422);
  assert.equal(err.details.afterCategoryFilter, 0);
  assert.equal(err.suggestions.length, 2);
  assert.equal(err.retryable, false, '조건을 바꿔야 하는 에러는 단순 재시도 대상이 아니다');
});

test('adaptError: 타임아웃·네트워크·5xx 는 재시도 가능으로 표시한다', () => {
  assert.equal(api.adaptError({ status: 504, body: { error: { code: 'TIMEOUT', message: '지연' } } }).retryable, true);
  assert.equal(api.adaptError({ status: 500, body: null }).code, 'SERVER_ERROR');
  assert.equal(api.adaptError({ status: 500, body: null }).retryable, true);

  const aborted = api.adaptError({ cause: Object.assign(new Error('aborted'), { name: 'AbortError' }) });
  assert.equal(aborted.code, 'CLIENT_TIMEOUT');
  assert.equal(aborted.retryable, true);

  const offline = api.adaptError({ cause: new TypeError('Failed to fetch') });
  assert.equal(offline.code, 'NETWORK');
  assert.equal(offline.retryable, true);

  assert.equal(api.adaptError({ status: 400, body: null }).retryable, false);
});

/* =====================================================================
 * 4. 호출 (fetch 주입)
 * ===================================================================*/
const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test('postRecommendations: 성공하면 정규화된 응답을 돌려준다', async () => {
  let seen = null;
  const out = await api.postRecommendations({ hello: 'world' }, {
    fetch: async (url, init) => { seen = { url, init }; return jsonResponse(200, responseFixture()); },
  });
  assert.equal(seen.url, '/api/recommendations');
  assert.equal(seen.init.method, 'POST');
  assert.deepEqual(JSON.parse(seen.init.body), { hello: 'world' });
  assert.equal(out.plans[0].jobs[0].jobId, 'job_at0001');
});

test('postRecommendations: 에러 응답과 JSON 이 아닌 본문을 구분해서 던진다', async () => {
  await assert.rejects(
    api.postRecommendations({}, { fetch: async () => jsonResponse(422, { error: { code: 'NO_CANDIDATES', message: '없음' } }) }),
    e => e.code === 'NO_CANDIDATES');

  await assert.rejects(
    api.postRecommendations({}, { fetch: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('bad json'); } }) }),
    e => e.code === 'MALFORMED_RESPONSE');

  await assert.rejects(
    api.postRecommendations({}, { fetch: async () => { throw new TypeError('Failed to fetch'); } }),
    e => e.code === 'NETWORK');
});

test('requestRecommendations: 로컬 검증에서 걸리면 요청을 보내지 않는다', async () => {
  let called = 0;
  await assert.rejects(
    api.requestRecommendations(profileFixture({ goal: 0 }), searchFixture(), null, { fetch: async () => { called++; return jsonResponse(200, responseFixture()); } }),
    e => e.code === 'VALIDATION_ERROR');
  assert.equal(called, 0);
});

test('requestRecommendations: 고정·제외를 실어 보내고 응답을 정규화한다', async () => {
  let sent = null;
  const { request, response } = await api.requestRecommendations(
    profileFixture(), searchFixture(), { pinned: ['job_at0001'], excluded: ['job_cp0043'], previousPlanHashes: ['plan_old'] },
    { fetch: async (url, init) => { sent = JSON.parse(init.body); return jsonResponse(200, responseFixture()); } });

  assert.deepEqual(sent.regenerate, { pinnedJobIds: ['job_at0001'], excludedJobIds: ['job_cp0043'], previousPlanHashes: ['plan_old'] });
  assert.deepEqual(request.regenerate, sent.regenerate);
  assert.deepEqual(api.planIds(response), ['plan_max_income']);
});
