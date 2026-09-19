/* =====================================================================
 * tests/web-flow.test.js
 *
 * 실행: node --test tests/web-flow.test.js
 *
 * 대상: 온보딩 저장값 → 추천 요청 → 결과 → 📌고정 / ✕제외 → 재생성 까지,
 *       화면 한 바퀴를 실제 코드로 돌린다.
 *
 * 브라우저를 띄우지 않는다. viewResult 안의 run() 이 하는 일은
 *   regeneratePayload → WeeklyApi.requestRecommendations → mergeSeenPlanIds
 * 세 줄이고, 이 파일은 그 세 줄을 fetch 만 가짜로 끼워 그대로 호출한다.
 * 클릭에 해당하는 상태 전이도 화면이 쓰는 togglePin / excludeJob 을 그대로 쓴다.
 * ===================================================================*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WeeklyApi = require('../js/api-client.js');

const ROOT = path.join(__dirname, '..');
for (const file of ['js/data.js', 'js/engine.js', 'js/app.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(ROOT, file), 'utf8'), { filename: file });
}
const app = vm.runInThisContext(`({
  regeneratePayload, togglePin, excludeJob, mergeSeenPlanIds, MAX_SEEN_PLAN_IDS,
  toViewJob, renderTimetable, jobItem, metricsRowServer,
})`);

/* ---------- 온보딩이 localStorage 에 남기는 모양 그대로 ---------- */
const profile = {
  role: '학생', home: '홍대입구', minBlock: 3, night: true, want15: true, goal: 600000, age: 22,
  sameDaily: false,
  schedule: {
    MON: { has: true, start: '10:00', end: '16:00', place: '신촌' },
    TUE: { has: true, start: '13:00', end: '18:00', place: '신촌' },
    WED: { has: true, start: '10:00', end: '16:00', place: '신촌' },
    THU: { has: true, start: '13:00', end: '18:00', place: '신촌' },
    FRI: { has: false, start: '', end: '', place: '' },
    SAT: { has: false, start: '', end: '', place: '' },
    SUN: { has: false, start: '', end: '', place: '' },
  },
};
const search = { count: 2, categories: ['카페·음식점'], priority: 'flex' };

/* ---------- 서버 응답 만들기 ---------- */
function job(id, over = {}) {
  return {
    jobId: id, pinned: false,
    title: id + ' 근무', company: id + ' 상호', platform: '알바천국',
    category: '카페·음식점', location: '신촌', address: '서울 서대문구 신촌로 1',
    hourlyWage: 12000, rating: 4.2, reviewCount: 11, thumbnail: null,
    descriptionSnippet: '설명', sourceUrl: null,
    contact: { manager: '담당자', phone: '010-0000-0000', kakao: null, applyUrl: null, preferred: '전화' },
    timeNegotiable: false, minWeeks: 4, benefits: [],
    assignedShifts: [{
      day: 'FRI', start: '18:00', end: '22:00',
      travel: { fromLocation: '홍대입구', transitMinutes: 30, walkMinutes: 5, originWalkMinutes: 0, legMinutes: 35, slackMinutes: 20, bufferMinutes: 15, departAt: '17:10' },
    }],
    weeklyHours: 4, weeklyPay: 48000,
    ...over,
  };
}

function planFor(hash, jobIds, over = {}) {
  return {
    id: 'plan_income_' + hash, hash, type: 'maxIncome', label: '수입 최대안',
    reason: '탐색한 조합 중 주급이 가장 높습니다.',
    metrics: { monthlyIncome: 412800, targetAchievementRate: 0.69, weeklyWorkHours: 8, weeklyTravelMinutes: 140, effectiveHourlyWage: 8300, weeklyHolidayPayIncluded: false },
    jobs: jobIds.map(id => job(id)),
    warnings: [],
    ...over,
  };
}

function responseFor(hash, jobIds) {
  return {
    requestId: 'req_' + hash, generatedAt: '2026-09-19T14:32:10+09:00', source: 'fallback',
    availableSlots: [{ day: 'FRI', from: '08:00', to: '24:00', fromLocation: '홍대입구' }],
    candidateCount: 18,
    plans: [planFor(hash, jobIds)],
    meta: { contractVersion: 'weekly.v1', engine: 'deterministic', totalLatencyMs: 900, disclosures: ['공고 데이터는 데모용 합성 데이터셋(600건)입니다.'] },
  };
}

/** viewResult 의 run() 과 같은 순서로 한 번 돌린다. */
async function run(st, fetchImpl, opts = {}) {
  const { request, response } = await WeeklyApi.requestRecommendations(
    profile, st.search, app.regeneratePayload(st, opts), { fetch: fetchImpl });
  st.request = request;
  st.response = response;
  st.seenPlanIds = app.mergeSeenPlanIds(st.seenPlanIds, WeeklyApi.planIds(response));
  st.selected = 0;
  return response;
}

const newState = () => ({ search, pinned: [], excluded: [], seenPlanIds: [], selected: 0, response: null, error: null });
const ok = body => ({ ok: true, status: 200, json: async () => body });

/* =====================================================================
 * 1. 한 바퀴: 첫 추천 → 고정 → 재생성 → 제외 → 재생성
 * ===================================================================*/
test('전체 흐름: 고정은 유지되고 제외는 빠지며 이미 본 조합이 쌓인다', async () => {
  const sent = [];
  let turn = 0;
  const fetchImpl = async (url, init) => {
    sent.push(JSON.parse(init.body));
    turn++;
    if (turn === 1) return ok(responseFor('aaaa1111', ['job_a', 'job_b']));
    if (turn === 2) return ok(responseFor('bbbb2222', ['job_a', 'job_c']));
    return ok(responseFor('cccc3333', ['job_a', 'job_d']));
  };

  const st = newState();

  /* --- 1) 홈에서 "추천 조합 생성하기" --- */
  await run(st, fetchImpl);
  assert.equal(sent[0].profile.home, '홍대입구');
  assert.equal(sent[0].profile.fixedSchedules.length, 4, '온보딩 고정 일정이 그대로 실린다');
  assert.equal(sent[0].profile.constraints.age, 22);
  assert.equal(sent[0].search.priority, 'flexibility', "화면의 'flex' 를 계약 enum 으로 옮긴다");
  assert.equal('regenerate' in sent[0], false, '첫 호출에는 regenerate 가 없다');
  assert.deepEqual(st.seenPlanIds, ['aaaa1111']);

  /* --- 2) job_a 를 📌 고정 --- */
  const pinned = app.togglePin(st, 'job_a');
  assert.equal(pinned.ok, true);
  st.pinned = pinned.pinned; st.excluded = pinned.excluded;

  /* --- 3) 🔄 재생성 --- */
  await run(st, fetchImpl, { regenerate: true });
  assert.deepEqual(sent[1].regenerate, {
    pinnedJobIds: ['job_a'],
    excludedJobIds: [],
    previousPlanHashes: ['aaaa1111'],
  }, '고정한 알바와 이미 본 조합을 함께 보낸다');
  assert.deepEqual(st.seenPlanIds, ['aaaa1111', 'bbbb2222']);

  /* --- 4) job_c 를 ✕ 제외 (제외는 즉시 재생성) --- */
  const excluded = app.excludeJob(st, 'job_c');
  st.pinned = excluded.pinned; st.excluded = excluded.excluded;
  await run(st, fetchImpl, { regenerate: true });

  assert.deepEqual(sent[2].regenerate, {
    pinnedJobIds: ['job_a'],
    excludedJobIds: ['job_c'],
    previousPlanHashes: ['aaaa1111', 'bbbb2222'],
  });
  assert.deepEqual(st.seenPlanIds, ['aaaa1111', 'bbbb2222', 'cccc3333']);

  /* --- 5) 화면은 마지막 응답만 그린다 --- */
  const jobs = st.response.plans[0].jobs.map(app.toViewJob);
  const html = app.renderTimetable(jobs, profile);
  assert.match(html, /job_a 상호/);
  assert.match(html, /job_d 상호/);
  assert.ok(!html.includes('job_c 상호'), '제외한 공고가 남아 있으면 안 된다');
  assert.equal(st.response.contractVersion, 'weekly.v1');
  assert.equal(st.response.source, 'fallback');
});

/* =====================================================================
 * 2. 고정 · 제외 상태 전이
 * ===================================================================*/
test('togglePin: 추천 개수를 넘겨 고정할 수 없다', () => {
  let st = { ...newState(), search: { ...search, count: 2 } };
  st = { ...st, ...app.togglePin(st, 'job_a') };
  st = { ...st, ...app.togglePin(st, 'job_b') };
  assert.deepEqual(st.pinned, ['job_a', 'job_b']);

  const third = app.togglePin(st, 'job_c');
  assert.equal(third.ok, false);
  assert.equal(third.limit, 2);
  assert.deepEqual(third.pinned, ['job_a', 'job_b'], '거부했으면 목록이 그대로여야 한다');
});

test('togglePin: 다시 누르면 풀리고, 고정하면 제외에서 빠진다', () => {
  const st = { ...newState(), pinned: ['job_a'], excluded: ['job_b'] };
  const off = app.togglePin(st, 'job_a');
  assert.equal(off.ok, true);
  assert.equal(off.pinnedNow, false);
  assert.deepEqual(off.pinned, []);

  const on = app.togglePin({ ...st, pinned: [] }, 'job_b');
  assert.deepEqual(on.pinned, ['job_b']);
  assert.deepEqual(on.excluded, [], '한 공고가 고정과 제외에 동시에 있을 수 없다');
});

test('excludeJob: 고정돼 있던 공고를 제외하면 고정이 풀린다 (중복 없이)', () => {
  const st = { ...newState(), pinned: ['job_a'], excluded: ['job_x'] };
  const next = app.excludeJob(st, 'job_a');
  assert.deepEqual(next.pinned, []);
  assert.deepEqual(next.excluded, ['job_x', 'job_a']);
  assert.deepEqual(app.excludeJob(next, 'job_a').excluded, ['job_x', 'job_a'], '같은 id 를 두 번 넣지 않는다');
});

test('regeneratePayload: fresh 면 이미 본 조합을 비우고 고정·제외는 지킨다', () => {
  const st = { ...newState(), pinned: ['job_a'], excluded: ['job_b'], seenPlanIds: ['h1', 'h2'] };
  assert.deepEqual(app.regeneratePayload(st), { pinned: ['job_a'], excluded: ['job_b'], previousPlanHashes: ['h1', 'h2'] });
  assert.deepEqual(app.regeneratePayload(st, { fresh: true }), { pinned: ['job_a'], excluded: ['job_b'], previousPlanHashes: [] });

  const payload = app.regeneratePayload(st);
  payload.pinned.push('job_z');
  assert.deepEqual(st.pinned, ['job_a'], '페이로드를 건드려도 화면 상태는 그대로다');
});

test('mergeSeenPlanIds: 중복 없이 쌓되 최근 것만 남긴다', () => {
  assert.deepEqual(app.mergeSeenPlanIds(['h1'], ['h1', 'h2']), ['h1', 'h2']);
  assert.deepEqual(app.mergeSeenPlanIds(null, null), []);
  const many = Array.from({ length: 40 }, (_, i) => 'h' + i);
  const kept = app.mergeSeenPlanIds([], many);
  assert.equal(kept.length, app.MAX_SEEN_PLAN_IDS);
  assert.equal(kept[kept.length - 1], 'h39', '가장 최근에 본 조합이 남는다');
});

/* =====================================================================
 * 3. 실패 — 지어내지 않고 멈춘다
 * ===================================================================*/
test('고정이 추천 개수보다 많으면 요청을 보내지 않는다', async () => {
  const st = { ...newState(), search: { ...search, count: 1 }, pinned: ['job_a', 'job_b'] };
  let called = 0;
  const err = await run(st, async () => { called++; return ok(responseFor('x', ['job_a'])); }).then(() => null, e => e);
  assert.equal(err.code, 'PINNED_EXCEEDS_COUNT');
  assert.equal(called, 0);
  assert.equal(st.response, null, '실패했는데 예전 결과를 남겨 두지 않는다');
});

test('켜져 있는 고정 일정에 시간이 없으면 추천을 보내지 않고 요일을 짚어 준다', async () => {
  const broken = JSON.parse(JSON.stringify(profile));
  broken.schedule.FRI = { has: true, start: '', end: '', place: '신촌' };
  let called = 0;
  const err = await WeeklyApi.requestRecommendations(broken, search, null, {
    fetch: async () => { called++; return ok(responseFor('x', ['job_a'])); },
  }).then(() => null, e => e);

  assert.equal(called, 0, '제약이 빠진 요청이 나가면 안 된다');
  assert.equal(err.code, 'VALIDATION_ERROR');
  assert.match(err.message, /금요일/);
  assert.ok(err.details.problems.some(p => p.day === 'FRI'));
});

test('서버가 후보를 못 찾으면 제안까지 받아 화면에 넘긴다', async () => {
  const st = newState();
  const err = await run(st, async () => ({
    ok: false, status: 422,
    json: async () => ({ error: { code: 'NO_CANDIDATES', message: '조건에 맞는 공고를 찾지 못했습니다.', details: {
      filteredFrom: 600, afterTimeFilter: 8, afterCategoryFilter: 0,
      suggestions: [{ type: 'expandCategory', label: '카테고리 넓히기' }],
    } } }),
  })).then(() => null, e => e);

  assert.equal(err.code, 'NO_CANDIDATES');
  assert.equal(err.retryable, false, '같은 조건으로 다시 눌러도 결과가 같다');
  assert.equal(err.details.afterCategoryFilter, 0);
  assert.equal(err.suggestions[0].type, 'expandCategory');
  assert.equal(st.response, null);
});

test('서버가 잠시 바쁘면(503 BUSY) 조건 그대로 다시 시도해 성공한다', async () => {
  const st = { ...newState(), pinned: ['job_a'] };
  let turn = 0;
  const fetchImpl = async () => {
    turn++;
    if (turn === 1) return { ok: false, status: 503, json: async () => ({ error: { code: 'BUSY', message: '앞선 추천을 처리 중입니다.' } }) };
    return ok(responseFor('dddd4444', ['job_a', 'job_b']));
  };

  const err = await run(st, fetchImpl, { regenerate: true }).then(() => null, e => e);
  assert.equal(err.code, 'BUSY');
  assert.equal(err.retryable, true);
  assert.deepEqual(st.pinned, ['job_a'], '실패해도 고정은 유지된다');

  const response = await run(st, fetchImpl, { regenerate: true });
  assert.equal(response.plans.length, 1);
  assert.deepEqual(st.seenPlanIds, ['dddd4444']);
});

test('지표와 시간표가 어긋난 응답은 아예 그리지 않는다', async () => {
  const bad = responseFor('eeee5555', ['job_a', 'job_b']);
  bad.plans[0].jobs[1].assignedShifts = [{ day: 'FRI', start: '18:00', end: 'NaN' }];
  const err = await run(newState(), async () => ok(bad)).then(() => null, e => e);
  assert.equal(err.code, 'MALFORMED_RESPONSE');
  assert.equal(err.retryable, true);
});
