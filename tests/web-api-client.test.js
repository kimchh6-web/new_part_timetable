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

/* 회귀: 켜져 있는 고정 일정은 값이 잘못돼 있어도 조용히 빠지지 않는다.
 * 빠지면 그 시간이 비어 있는 것으로 요청이 나가고, 수업·근무 시간에 겹치는
 * 알바를 추천받게 된다. 제약을 지우는 대신 요일을 짚어 막아야 한다. */
test('buildRequest: 시간이 비어 있는 고정 일정을 지우지 않고 남겨 막는다', () => {
  const p = profileFixture();
  p.schedule.WED = { has: true, start: '', end: '', place: '신촌' };
  const req = api.buildRequest(p, searchFixture());

  assert.deepEqual(req.profile.fixedSchedules.map(f => f.day), ['MON', 'TUE', 'WED'], '행이 사라지면 안 된다');
  assert.equal(req.profile.fixedSchedules[2].start, null);

  const problems = api.validateRequest(req);
  const blocked = problems.find(x => x.field === 'fixedSchedules' && x.day === 'WED');
  assert.ok(blocked, '막는 문제로 잡혀야 한다');
  assert.equal(blocked.code, 'VALIDATION_ERROR');
  assert.match(blocked.message, /수요일/, '어느 요일인지 화면에 보여야 한다');
});

test('buildRequest: 시각이 형식에 맞지 않는 고정 일정도 남겨 막는다', () => {
  const p = profileFixture();
  p.schedule.THU = { has: true, start: '25:00', end: '26:30', place: '신촌' };
  const req = api.buildRequest(p, searchFixture());
  assert.deepEqual(req.profile.fixedSchedules.map(f => f.day), ['MON', 'TUE', 'THU']);
  assert.deepEqual([req.profile.fixedSchedules[2].start, req.profile.fixedSchedules[2].end], [null, null]);
  assert.ok(api.validateRequest(req).some(x => x.day === 'THU' && x.code === 'VALIDATION_ERROR'));
});

test('buildRequest: 켜져 있지만 장소를 안 고른 행도 막힌다', () => {
  const p = profileFixture();
  p.schedule.FRI = { has: true, start: '09:00', end: '18:00', place: '' };
  const problems = api.validateRequest(api.buildRequest(p, searchFixture()));
  assert.ok(problems.some(x => x.day === 'FRI' && /장소/.test(x.message)));
});

/* 회귀: 24시는 하루의 끝(24:00)만 뜻이 있다. 24:30 같은 값은 시각이 아니다. */
test('normTime: 24:00 만 허용하고 24:01~24:59 는 거부한다', () => {
  const { normTime } = api._internals;
  assert.equal(normTime('24:00'), '24:00');
  assert.equal(normTime('23:59'), '23:59');
  assert.equal(normTime('9:05'), '09:05');
  assert.equal(normTime('24:01'), null);
  assert.equal(normTime('24:30'), null);
  assert.equal(normTime('24:59'), null);
  assert.equal(normTime('25:00'), null);
  assert.equal(normTime(''), null);
  assert.equal(normTime(null), null);
});

test('normTime: 응답 쪽에서도 24:30 은 근무로 읽지 않는다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs[0].assignedShifts = [{ day: 'TUE', start: '19:00', end: '24:30' }];
  assert.throws(() => api.adaptResponse(raw), e => e.code === 'MALFORMED_RESPONSE');
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
    travel: {
      fromLocation: '강남역', transitMinutes: 5, walkMinutes: 6,
      originWalkMinutes: null, legMinutes: null, slackMinutes: null,
      bufferMinutes: 15, departAt: '18:34',
    },
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

/* 근무 한 칸이 깨졌다고 그 칸만 빼고 그리면, 시간표는 줄었는데 monthlyIncome 은
 * 원래 조합 기준 그대로 남는다. 화면이 "이 시간표로 이 돈"이라는 거짓말을 하게 되므로
 * 안 전체를 버리고, 몇 개를 버렸는지 화면에 알린다. */
test('adaptResponse: 근무 한 칸이 깨지면 그 안을 통째로 버린다 (부분 표시 금지)', () => {
  const raw = responseFixture();
  const good = JSON.parse(JSON.stringify(raw.plans[0]));
  good.id = 'plan_ok';
  raw.plans[0].jobs[0].assignedShifts = [
    { day: 'TUE', start: '19:00', end: '23:00' },
    { day: 'XXX', start: '19:00', end: '23:00' },
  ];
  raw.plans.push(good);
  const out = api.adaptResponse(raw);
  assert.equal(out.plans.length, 1);
  assert.equal(out.plans[0].id, 'plan_ok');
  assert.equal(out.droppedPlans, 1);
});

test('adaptResponse: 공고 한 건이 깨지면 나머지 공고까지 같이 버린다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs.push({ noJobId: true });
  assert.throws(() => api.adaptResponse(raw), e => e.code === 'MALFORMED_RESPONSE');
});

test('adaptResponse: 배정 근무가 아예 없는 공고도 표시하지 않는다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs[0].assignedShifts = [];
  assert.throws(() => api.adaptResponse(raw), e => e.code === 'MALFORMED_RESPONSE');
});

test('adaptResponse: 끝이 시작보다 빠른 근무는 읽지 않는다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs[0].assignedShifts = [{ day: 'TUE', start: '19:00', end: '19:00' }];
  assert.throws(() => api.adaptResponse(raw), e => e.code === 'MALFORMED_RESPONSE');
});

test('adaptResponse: 쓸 수 있는 안이 하나도 없으면 던진다', () => {
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

/* =====================================================================
 * 5. 타임아웃 — 헤더가 아니라 "본문을 다 읽을 때까지"
 *
 * 계약 7-3: 서버 25초 / 클라이언트 30초. 헤더만 오고 본문이 끊기는 응답에서
 * 타이머를 먼저 꺼 버리면 화면이 영원히 로딩에 머문다.
 * ===================================================================*/
function timerSpy() {
  const realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout;
  const log = {
    created: 0, cleared: 0, clearedAt: null, mark: 'before-body',
    restore() { globalThis.setTimeout = realSet; globalThis.clearTimeout = realClear; },
  };
  globalThis.setTimeout = (fn, ms) => { log.created++; return realSet(fn, ms); };
  globalThis.clearTimeout = h => { log.cleared++; if (log.clearedAt === null) log.clearedAt = log.mark; return realClear(h); };
  return log;
}

test('postRecommendations: 본문을 읽는 동안에도 타임아웃 타이머가 살아 있다', async () => {
  const spy = timerSpy();
  try {
    await api.postRecommendations({}, {
      fetch: async () => ({
        ok: true, status: 200,
        json: async () => { await new Promise(r => setImmediate(r)); spy.mark = 'after-body'; return responseFixture(); },
      }),
    });
  } finally { spy.restore(); }

  assert.equal(spy.created, 1, '타임아웃 타이머는 한 번 건다');
  assert.equal(spy.clearedAt, 'after-body', 'res.json() 이 끝나기 전에 타이머를 끄면 안 된다');
  assert.equal(spy.cleared, 1, '성공한 뒤에는 반드시 해제한다 (타이머를 남기지 않는다)');
});

test('postRecommendations: 본문을 읽다가 시간이 넘으면 CLIENT_TIMEOUT 이다', async () => {
  const err = await api.postRecommendations({}, {
    timeoutMs: 20,
    fetch: async (url, init) => ({
      ok: true, status: 200,
      // 헤더는 왔지만 본문이 오지 않는 응답. abort 로만 끝난다.
      json: () => new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      }),
    }),
  }).then(() => null, e => e);

  assert.ok(err, '응답 없이 매달려 있으면 안 된다');
  assert.equal(err.code, 'CLIENT_TIMEOUT');
  assert.equal(err.retryable, true, '다시 시도 버튼이 보여야 한다');
});

test('postRecommendations: 연결 단계에서 시간이 넘어도 CLIENT_TIMEOUT 이다', async () => {
  await assert.rejects(
    api.postRecommendations({}, {
      timeoutMs: 20,
      fetch: (url, init) => new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      }),
    }),
    e => e.code === 'CLIENT_TIMEOUT');
});

test('postRecommendations: 타임아웃으로 끝나도 타이머를 남기지 않는다', async () => {
  const spy = timerSpy();
  try {
    await api.postRecommendations({}, {
      timeoutMs: 20,
      fetch: (url, init) => new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      }),
    }).catch(() => {});
  } finally { spy.restore(); }
  assert.equal(spy.cleared, 1);
});

/* =====================================================================
 * 6. 에러 · 재시도 — 서버(web_demo.py)가 실제로 내려보내는 것들
 * ===================================================================*/
test('adaptError: 503 BUSY 는 조건을 바꿀 필요 없이 다시 시도하면 되는 에러다', () => {
  const err = api.adaptError({ status: 503, body: { error: { code: 'BUSY', message: '앞선 추천을 처리 중입니다. 잠시 후 다시 시도해 주세요.' } } });
  assert.equal(err.code, 'BUSY');
  assert.equal(err.status, 503);
  assert.equal(err.retryable, true);
  assert.equal(err.message, '앞선 추천을 처리 중입니다. 잠시 후 다시 시도해 주세요.', '서버 문구를 그대로 보여준다');
});

test('adaptError: 503 DAYTONA_UNAVAILABLE / 504 TIMEOUT 도 재시도 대상이다', () => {
  assert.equal(api.adaptError({ status: 503, body: { error: { code: 'DAYTONA_UNAVAILABLE', message: '실행 실패' } } }).retryable, true);
  assert.equal(api.adaptError({ status: 504, body: { error: { code: 'TIMEOUT', message: '지연' } } }).retryable, true);
});

test('adaptError: 입력을 고쳐야 하는 에러는 재시도 대상이 아니다', () => {
  for (const code of ['VALIDATION_ERROR', 'SCHEDULE_CONFLICT', 'PINNED_EXCEEDS_COUNT', 'NO_CANDIDATES']) {
    assert.equal(api.adaptError({ status: 400, body: { error: { code, message: 'x' } } }).retryable, false, code);
  }
});

test('requestRecommendations: 재시도하면 같은 요청을 다시 보내고 성공을 돌려준다', async () => {
  const sent = [];
  let attempt = 0;
  const fetchImpl = async (url, init) => {
    sent.push(JSON.parse(init.body));
    attempt++;
    if (attempt === 1) return jsonResponse(503, { error: { code: 'BUSY', message: '처리 중입니다.' } });
    return jsonResponse(200, responseFixture());
  };
  const regen = { pinned: ['job_at0001'], excluded: ['job_cp0043'], previousPlanHashes: ['h1'] };

  const failed = await api.requestRecommendations(profileFixture(), searchFixture(), regen, { fetch: fetchImpl }).then(() => null, e => e);
  assert.equal(failed.code, 'BUSY');
  assert.equal(failed.retryable, true);

  const { response } = await api.requestRecommendations(profileFixture(), searchFixture(), regen, { fetch: fetchImpl });
  assert.equal(response.plans.length, 1);
  assert.equal(attempt, 2);
  assert.deepEqual(sent[0], sent[1], '재시도는 조건을 바꾸지 않는다 — 고정·제외가 그대로 남아야 한다');
  assert.deepEqual(sent[1].regenerate, { pinnedJobIds: ['job_at0001'], excludedJobIds: ['job_cp0043'], previousPlanHashes: ['h1'] });
});

/* =====================================================================
 * 7. 출처 · 계약 버전 · 서버 공시
 * ===================================================================*/
test('adaptResponse: source 를 모르면 llm 이라고 지어내지 않는다', () => {
  assert.equal(api.adaptResponse(responseFixture({ source: undefined })).source, null);
  assert.equal(api.adaptResponse(responseFixture({ source: 'magic' })).source, null);
  assert.equal(api.adaptResponse(responseFixture({ source: 'fallback' })).source, 'fallback');
  assert.equal(api.adaptResponse(responseFixture({ source: 'llm' })).source, 'llm');
});

test('adaptResponse: weekly.v1 응답의 계약 버전과 공시 문장을 옮긴다', () => {
  const out = api.adaptResponse(responseFixture({
    source: 'fallback',
    meta: { contractVersion: 'weekly.v1', engine: 'deterministic', disclosures: ['이동 시간은 추정값입니다.', '공고 데이터는 데모용 합성 데이터셋(600건)입니다.'] },
  }));
  assert.equal(out.contractVersion, 'weekly.v1');
  assert.deepEqual(out.disclosures, ['이동 시간은 추정값입니다.', '공고 데이터는 데모용 합성 데이터셋(600건)입니다.']);
  assert.equal(out.meta.engine, 'deterministic', 'meta 원본은 그대로 남긴다');
});

test('adaptResponse: meta 가 없어도 계약 버전은 null, 공시는 빈 배열', () => {
  const out = api.adaptResponse(responseFixture({ meta: undefined }));
  assert.equal(out.contractVersion, null);
  assert.deepEqual(out.disclosures, []);
  assert.equal(out.meta, null);
});

test('adaptResponse: 서버가 계산한 편도 이동(legMinutes)과 여유를 옮긴다', () => {
  const raw = responseFixture();
  raw.plans[0].jobs[0].assignedShifts = [{
    day: 'TUE', start: '19:00', end: '23:00',
    travel: { fromLocation: '강남역', transitMinutes: 30, walkMinutes: 6, originWalkMinutes: 4, bufferMinutes: 15, departAt: '18:05', legMinutes: 40, slackMinutes: 5 },
  }];
  const t = api.adaptResponse(raw).plans[0].jobs[0].assignedShifts[0].travel;
  assert.equal(t.legMinutes, 40);
  assert.equal(t.slackMinutes, 5);
  assert.equal(t.originWalkMinutes, 4);
});

test('planIds: 조합 hash 가 있으면 hash 를, 없으면 plan id 를 보낸다', () => {
  const withHash = api.adaptResponse(responseFixture({
    plans: [{ ...responseFixture().plans[0], id: 'plan_income_ab12cd34', hash: 'ab12cd34' }],
  }));
  assert.deepEqual(api.planIds(withHash), ['ab12cd34'], '같은 조합이 다른 이름표로 다시 오는 것까지 막는다');
  assert.deepEqual(api.planIds(api.adaptResponse(responseFixture())), ['plan_max_income']);
  assert.deepEqual(api.planIds(null), []);
});

/* =====================================================================
 * 8. 실제 서버 응답 모양 (weekly.v1)
 *
 * 아래 안 하나는 `python -c "from harness.weekly import ..."` 로 정본 600건
 * 데이터셋에 examples/weekly_input.json 을 넣어 받은 응답에서 그대로 떠 온 것이다.
 * 계약서 예시가 아니라 서버가 실제로 보내는 모양이라, 계약에 없는 필드
 * (daysNegotiable / payType / assignment / holidayPay / unverifiedQualifications)와
 * 계약 밖 경고 코드가 함께 들어 있다. 그것들 때문에 안이 버려지면 안 된다.
 * ===================================================================*/
const REAL_PLAN = {
  id: 'plan_max_income_66489241',
  hash: '66489241',
  type: 'maxIncome',
  label: '수입 최대안',
  reason: '탐색한 조합 중 주급이 가장 높습니다. 주 6일(MON, TUE, THU, FRI, SAT, SUN) 근무, 주 28시간, 이동 528분. 목표 600,000원의 289% 수준입니다.',
  metrics: { monthlyIncome: 1734792, targetAchievementRate: 2.89, weeklyWorkHours: 28, weeklyTravelMinutes: 528, effectiveHourlyWage: 10963, weeklyHolidayPayIncluded: false },
  jobs: [{
    jobId: 'job_cp0095',
    pinned: false,
    title: '[노원] 바리스타 구합니다',
    company: '치킨하우스 반반 노원점',
    platform: '쿠펀치',
    category: '카페·음식점',
    location: '노원',
    address: '서울 노원구 상계로 201',
    hourlyWage: 13900,
    rating: 3.2,
    reviewCount: 78,
    thumbnail: 'https://picsum.photos/seed/job_cp0095/480/320',
    descriptionSnippet: '음료 제조와 카운터 응대를 맡습니다.',
    sourceUrl: 'https://www.coupunch.com/jobs/cp0095',
    contact: { manager: '남태민 담당자', phone: '010-0000-5642', kakao: null, applyUrl: 'https://www.coupunch.com/jobs/cp0095', preferred: '온라인 지원' },
    timeNegotiable: false,
    daysNegotiable: false,
    minWeeks: 1,
    benefits: ['교통비 지원', '당일지급', '식사 제공', '인센티브 있음'],
    assignedShifts: [
      { day: 'MON', start: '19:00', end: '22:00', travel: { fromLocation: '강남역', transitMinutes: 30, walkMinutes: 13, originWalkMinutes: 0, bufferMinutes: 15, departAt: '18:02', legMinutes: 43, slackMinutes: 2 } },
      { day: 'TUE', start: '19:00', end: '22:00', travel: { fromLocation: '강남역', transitMinutes: 30, walkMinutes: 13, originWalkMinutes: 0, bufferMinutes: 15, departAt: '18:02', legMinutes: 43, slackMinutes: 2 } },
    ],
    weeklyHours: 12,
    weeklyPay: 166800,
    payType: 'hourly',
    assignment: { publishedShiftCount: 4, assignedShiftCount: 4, droppedShifts: [], minDaysPerWeek: 4, maxDaysPerWeek: 4 },
    holidayPay: { includedInIncome: false, employerHoursThresholdMet: false, postingClaimsWeeklyHolidayPay: false },
    unverifiedQualifications: ['요구사항: 위생 관리 준수', '우대: 카페 경험자'],
  }],
  warnings: [
    { code: 'LONG_TRAVEL', message: 'MON, TUE 편도 이동이 최대 43분입니다.', jobId: 'job_cp0095' },
    { code: 'TIGHT_TRANSFER', message: 'MON 도착 여유가 2분입니다.', jobId: 'job_cp0095' },
    { code: 'QUALIFICATIONS_UNVERIFIED', message: '자격 요건을 확인하지 못했습니다.', jobId: 'job_cp0095' },
    { code: 'HOLIDAY_PAY_NOT_INCLUDED', message: '주휴수당은 monthlyIncome 에 넣지 않았습니다.' },
  ],
};

test('adaptResponse: 실제 weekly.v1 응답을 그대로 읽는다 (계약 밖 필드가 있어도)', () => {
  const out = api.adaptResponse({
    requestId: 'req_5A5433C33F',
    generatedAt: '2026-09-19T14:32:10+09:00',
    source: 'fallback',
    availableSlots: [{ day: 'MON', from: '18:00', to: '24:00', fromLocation: '강남역' }],
    candidateCount: 62,
    plans: [REAL_PLAN],
    meta: { llmLatencyMs: 0, totalLatencyMs: 12, filteredFrom: 600, engine: 'deterministic', llmUsed: false, contractVersion: 'weekly.v1', disclosures: ['이동 시간은 추정값입니다.'] },
  });

  assert.equal(out.droppedPlans, 0, '계약 밖 필드 때문에 안을 버리면 안 된다');
  assert.equal(out.contractVersion, 'weekly.v1');
  assert.equal(out.source, 'fallback');
  assert.equal(out.candidateCount, 62);

  const plan = out.plans[0];
  assert.equal(plan.hash, '66489241');
  assert.deepEqual(api.planIds(out), ['66489241']);
  assert.equal(plan.metrics.weeklyHolidayPayIncluded, false);
  assert.equal(plan.warnings.length, 4, '계약 밖 경고 코드도 그대로 넘긴다');
  assert.equal(plan.warnings[3].jobId, null);

  const job = plan.jobs[0];
  assert.equal(job.weeklyHours, 12);
  assert.equal(job.weeklyPay, 166800);
  assert.equal(job.timeNegotiable, false);
  assert.equal(job.assignedShifts.length, 2);
  assert.equal(job.assignedShifts[0].travel.legMinutes, 43, '서버가 계산한 편도 이동');
  assert.equal(job.assignedShifts[0].travel.slackMinutes, 2);
  assert.equal(job.assignedShifts[0].travel.departAt, '18:02');
  assert.equal(job.holidayPay.employerHoursThresholdMet, false);
  assert.deepEqual(job.unverifiedQualifications, ['요구사항: 위생 관리 준수', '우대: 카페 경험자']);
});
