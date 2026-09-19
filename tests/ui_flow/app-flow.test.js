/* =====================================================================
 * tests/ui_flow/app-flow.test.js
 *
 * 실행: node --test tests/ui_flow/*.test.js
 *
 * 대상: js/app.js — 화면 흐름(라우팅 → 요청 → 렌더 → 저장 → 재생성 → 실패).
 * tests/web-api-client.test.js 가 덮는 어댑터 단위 규칙은 여기서 다시
 * 검증하지 않는다. 여기서 보는 것은 "앱이 그 어댑터를 어떻게 쓰는가" 다.
 *
 * 브라우저 커넥터를 쓸 수 없는 환경이라 렌더된 GUI 증거(스크린샷)는 없다.
 * 대신 실제 app.js 를 node:vm 에 올려 실제 DOM 문자열과 실제 핸들러를
 * 실행하고, 그 결과 HTML 과 나간 요청 본문을 검증한다.
 *
 * 네트워크: support/page.js 의 fetch 대역이 상대경로 외의 주소를 막고,
 * 테스트가 응답을 큐에 넣지 않으면 호출 자체를 실패시킨다.
 * ===================================================================*/
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createPage, jsonResponse } = require('./support/page.js');
const F = require('./support/fixtures.js');

/** app.js 의 비동기 run() 이 끝날 때까지 마이크로태스크를 흘린다. */
const flush = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

/** 프로필이 저장된 상태로 홈까지 띄운다. */
function bootHome(extraSeed = {}) {
  const page = createPage();
  page.seed('profile', F.profileFixture());
  for (const [k, v] of Object.entries(extraSeed)) page.seed(k, v);
  page.boot();
  return page;
}

/** 결과/저장 화면의 지표 줄에서 한 칸의 값을 읽는다. */
function metricValue(page, label) {
  const cell = page.$$('.metrics-row .metric').find(m => {
    const k = m.querySelector('.k');
    return k && k.textContent.startsWith(label);
  });
  assert.ok(cell, '지표 칸을 찾지 못했다: ' + label);
  return cell.querySelector('.v').textContent;
}

/** 홈에서 조건을 고르고 추천을 생성한다. */
async function generate(page, response) {
  if (response !== undefined) page.queueJson(response);
  page.click('#go');
  await flush();
}

/* =====================================================================
 * 1. 성공 응답 — 서버가 준 지표와 배정 근무를 그대로 그린다
 * ===================================================================*/
test('성공 흐름: 홈에서 고른 조건이 계약 Request 로 나가고 상대경로로만 호출한다', async () => {
  const page = bootHome();

  page.clickEl(page.$$('#cats .chip')[0]);      // 카페·음식점
  page.clickEl(page.$$('#prio button')[1]);     // 거리
  page.clickEl(page.$$('#count button')[2]);    // 3개

  await generate(page, F.responseFixture());

  assert.equal(page.calls.length, 1);
  const call = page.calls[0];
  assert.equal(call.url, page.evalIn('WeeklyApi.ENDPOINT'));
  assert.equal(call.url, '/api/recommendations');
  assert.equal(call.method, 'POST');
  assert.deepEqual(call.body.search, { jobCount: 3, categories: ['카페·음식점'], priority: 'distance' });
  assert.equal(call.body.profile.targetAmount, 600000);
  assert.deepEqual(call.body.profile.fixedSchedules.map(f => f.day), ['MON', 'TUE', 'THU']);
  assert.equal(call.body.regenerate, undefined, '첫 요청에는 regenerate 가 붙지 않아야 한다');

  assert.equal(page.hash(), '#/result');
  assert.equal(page.$$('.plan-card').length, 3);
});

test('성공 흐름: 월 수입·달성률·주 근무는 서버 값 그대로다 (프론트가 다시 계산하지 않는다)', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());
  const html = page.html();

  // 픽스처의 배정 근무 합계는 주 11시간이지만 서버는 28 을 줬다.
  assert.match(html, /1,734,792원/);
  assert.match(html, /289%/);
  assert.match(html, /28\.0h/);
  assert.ok(!html.includes('11.0h'), '배정 근무 산술(11h)로 서버 지표를 덮어쓰면 안 된다');
  assert.match(html, /8시간 48분/);                 // weeklyTravelMinutes 528
  assert.match(html, /10,963원/);                   // effectiveHourlyWage
  assert.match(html, /주휴수당 미포함/);            // weeklyHolidayPayIncluded false
  assert.match(html, /후보 187건/);                 // candidateCount
  assert.match(html, /req_UIFLOW_0001/);
});

test('성공 흐름: 시간표는 서버 assignedShifts 와 서버 이동시간만 쓴다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  const cols = page.$$('.tt-col');
  assert.equal(cols.length, 7);
  const [mon, tue, wed, thu, fri, sat, sun] = cols.map(c => c.textContent);

  assert.ok(mon.includes('치킨하우스 반반 노원점') && mon.includes('19:00–22:00'));
  assert.ok(thu.includes('치킨하우스 반반 노원점'));
  assert.ok(sat.includes('올리브영 여의도점') && sat.includes('10:00–15:00'));

  // 서버가 배정하지 않은 요일에 근무를 만들어 내지 않는다 (고정 일정만 남는다)
  assert.ok(!tue.includes('치킨하우스') && !tue.includes('올리브영'));
  for (const empty of [wed, fri, sun]) assert.equal(empty.trim(), '');

  // 이동시간은 서버 travel(30+13분) 을 쓰고, 브라우저 추정으로 넘어가지 않는다
  const html = page.html();
  assert.match(html, /이동 43분/);
  assert.match(html, /서버 계산 이동시간/);
  assert.ok(!html.includes('브라우저 추정 이동시간'), '서버가 travel 을 준 경우 추정 표기가 붙으면 안 된다');
  assert.match(html, /18:02 출발/);                 // departAt
  assert.match(html, /서버가 계산한 빈 시간/);      // availableSlots
});

test('성공 흐름: 서버가 주지 않은 지표는 만들어 내지 않고 —  로 둔다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  page.clickEl(page.$$('.plan-card')[2]);           // balanced — effectiveHourlyWage 없음
  assert.match(page.html(), /밸런스안 주간 시간표/);

  assert.equal(metricValue(page, '예상 월 수입'), '1,210,500원');
  assert.equal(metricValue(page, '목표 달성률'), '201%');
  assert.equal(metricValue(page, '주 총 근무시간'), '20.0h');
  assert.equal(metricValue(page, '실질 시급'), '—', '서버가 안 준 실질 시급을 계산해 채우면 안 된다');
  assert.match(page.html(), /주휴수당 여부 미확인/);
});

/* =====================================================================
 * 2. 저장 → 재열람 — 서버 plan 이 보존된다
 * ===================================================================*/
test('저장 흐름: 저장하면 서버 plan 이 schemaVersion 2 로 그대로 보관된다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  page.click('#save');
  assert.ok(page.$('#modalbg'), '저장 모달이 떠야 한다');
  page.click('#mok');
  await flush();

  const saved = page.stored('schedules');
  assert.equal(saved.length, 1);
  const entry = saved[0];
  assert.equal(entry.schemaVersion, 2);
  assert.equal(entry.planId, 'plan_max_income_66489241');
  assert.equal(entry.planType, 'maxIncome');
  assert.equal(entry.requestId, 'req_UIFLOW_0001');
  assert.equal(entry.source, 'llm');
  assert.deepEqual(entry.metrics, F.responseFixture().plans[0].metrics);
  assert.deepEqual(entry.jobs.map(j => j.jobId), ['job_cp0095', 'job_st0042']);
  assert.deepEqual(entry.jobs[0].assignedShifts, F.jobA().assignedShifts);
  // 응답에만 있고 화면 계산에는 쓰이지 않는 travel 선택 필드도 깎이지 않는다
  const savedTravel = entry.jobs[0].assignedShifts[0].travel;
  assert.equal(savedTravel.legMinutes, 43);
  assert.equal(savedTravel.originWalkMinutes, 0);
  assert.equal(savedTravel.slackMinutes, 2);
  assert.equal(page.hash(), '#/schedule/' + entry.id);
  assert.ok(!page.$('#modalbg'), '저장 후 모달이 닫혀야 한다');
});

test('저장 흐름: 새 세션에서 다시 열어도 서버 지표와 배정 시간이 그대로다', async () => {
  const first = bootHome();
  await generate(first, F.responseFixture());
  first.click('#save');
  first.click('#mok');
  await flush();
  const saved = first.stored('schedules');

  // 같은 저장물을 가진 완전히 새 세션 (sessionStorage 는 비어 있다)
  const reopened = createPage();
  reopened.seed('profile', F.profileFixture());
  reopened.seed('schedules', saved);
  reopened.location._hash = '#/schedule/' + saved[0].id;
  reopened.boot();

  const html = reopened.html();
  assert.match(html, /1,734,792원/);
  assert.match(html, /289%/);
  assert.match(html, /28\.0h/);
  assert.match(html, /치킨하우스 반반 노원점/);
  assert.match(html, /19:00 ~ 22:00/);
  assert.match(html, /18:02 출발/);
  // 저장·재열람을 거쳐도 이동 블록은 서버 값으로 남는다 (leg 43 = 30+13 이라 합산 경로와 숫자는 같고,
  // 필드 자체가 보존되는지는 아래 저장물 검사가 본다)
  assert.match(html, /이동 43분/);
  assert.ok(!html.includes('브라우저 추정 이동시간'));
  const reopenedTravel = reopened.stored('schedules')[0].jobs[0].assignedShifts[0].travel;
  assert.deepEqual(
    { leg: reopenedTravel.legMinutes, originWalk: reopenedTravel.originWalkMinutes, slack: reopenedTravel.slackMinutes },
    { leg: 43, originWalk: 0, slack: 2 },
    '새 세션에서 읽어도 travel 선택 필드 3개가 그대로 남아야 한다',
  );
  assert.ok(!html.includes('이전 버전에서 저장된'), '서버 기반 저장물을 옛 형식으로 취급하면 안 된다');
  assert.equal(reopened.calls.length, 0, '저장된 시간표를 여는 데 네트워크가 필요하면 안 된다');
});

/* =====================================================================
 * 3. 고정·제외 재생성 — 정확한 id 가 실린다
 * ===================================================================*/
test('재생성 흐름: 고정한 알바 id 와 이미 본 plan id 가 정확히 실린다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  const pinButtons = page.$$('[data-pin]');
  assert.deepEqual(pinButtons.map(b => b.dataset.pin), ['job_cp0095', 'job_st0042']);
  page.clickEl(pinButtons[0]);
  assert.match(page.html(), /고정 1 · 제외 0/);

  page.queueJson(F.responseFixture({ requestId: 'req_UIFLOW_0002' }));
  page.click('#regen');
  await flush();

  assert.equal(page.calls.length, 2);
  assert.deepEqual(page.calls[1].body.regenerate, {
    pinnedJobIds: ['job_cp0095'],
    excludedJobIds: [],
    previousPlanHashes: ['plan_max_income_66489241', 'plan_min_travel_1197a3c0', 'plan_balanced_5d20e4b1'],
  });
  assert.deepEqual(page.calls[1].body.search, page.calls[0].body.search, '재생성은 탐색 조건을 바꾸지 않는다');
});

test('재생성 흐름: 제외는 고정에서 빼고 excludedJobIds 로 나간다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  page.clickEl(page.$$('[data-pin]')[0]);                      // job_cp0095 고정
  page.queueJson(F.responseFixture({ requestId: 'req_UIFLOW_0002' }));
  page.click('#regen');
  await flush();

  page.queueJson(F.responseFixture({ requestId: 'req_UIFLOW_0003' }));
  page.clickEl(page.$$('[data-exclude]')[0]);                  // job_cp0095 제외
  await flush();

  assert.equal(page.calls.length, 3);
  const regen = page.calls[2].body.regenerate;
  assert.deepEqual(regen.excludedJobIds, ['job_cp0095']);
  assert.deepEqual(regen.pinnedJobIds, [], '제외한 알바는 고정 목록에서 빠져야 한다');
  assert.ok(regen.previousPlanHashes.includes('plan_max_income_66489241'));
});

/* =====================================================================
 * 4. 실패 — 입력을 지우지 않고, 로컬 JOBS 로 몰래 대체하지 않는다
 * ===================================================================*/
test('실패 흐름: HTTP 5xx 는 입력을 유지하고 재시도를 제공하며 로컬 공고로 대체하지 않는다', async () => {
  const page = bootHome();
  page.clickEl(page.$$('#cats .chip')[0]);
  page.clickEl(page.$$('#count button')[2]);
  const chosen = page.stored('lastSearch');

  page.queueJson(F.errorBody('SERVER_ERROR', '일시적인 서버 오류입니다.'), { status: 503 });
  page.click('#go');
  await flush();

  const html = page.html();
  assert.match(html, /추천을 받지 못했습니다/);
  assert.match(html, /SERVER_ERROR/);
  assert.ok(page.$('#retry'), '재시도 버튼이 있어야 한다');
  assert.equal(page.$$('.plan-card').length, 0);

  // js/data.js 의 손으로 만든 JOBS 가 화면에 새어 나오면 안 된다
  const localCompanies = [...new Set(page.evalIn('JOBS.map(j => j.company)'))];
  const leaked = localCompanies.filter(c => html.includes(c));
  assert.deepEqual(leaked, [], '서버 실패 시 로컬 JOBS 로 결과를 지어내면 안 된다');

  // 입력은 그대로 남는다
  assert.deepEqual(page.stored('lastSearch'), chosen);
  assert.deepEqual(page.session().search, chosen);
  assert.equal(page.session().error.code, 'SERVER_ERROR');
  assert.deepEqual(page.stored('history'), null, '실패한 요청은 기록에 남기지 않는다');
});

test('실패 흐름: 네트워크 자체가 끊겨도 같은 자리를 지킨다', async () => {
  const page = bootHome();
  page.queueFailure(new TypeError('fetch failed'));
  page.click('#go');
  await flush();

  const html = page.html();
  assert.equal(page.session().error.code, 'NETWORK');
  assert.match(html, /로컬 예시 데이터로 결과를 지어내지 않기/);
  assert.match(html, /\/api\/recommendations/);
  assert.equal(page.$$('.plan-card').length, 0);
  assert.ok(page.$('#retry'));
});

test('실패 흐름: 재시도는 같은 조건을 그대로 다시 보낸다', async () => {
  const page = bootHome();
  page.clickEl(page.$$('#prio button')[2]);        // 평점
  page.queueJson(F.errorBody('SERVER_ERROR', '일시적인 서버 오류입니다.'), { status: 503 });
  page.click('#go');
  await flush();

  page.queueJson(F.responseFixture());
  page.click('#retry');
  await flush();

  assert.equal(page.calls.length, 2);
  assert.deepEqual(page.calls[1].body, page.calls[0].body);
  assert.match(page.html(), /1,734,792원/);
  assert.equal(page.$$('.plan-card').length, 3);
});

/* =====================================================================
 * 5. 옛 저장물 — 조용히 부수지 않는다
 * ===================================================================*/
test('옛 저장물: schemaVersion 1 스케줄은 경고와 함께 원본 그대로 열린다', () => {
  const page = bootHome({ schedules: [F.legacySchedule()] });

  page.go('#/my');
  assert.match(page.html(), /이전 형식/);
  assert.match(page.html(), /5월 수입 최대안/);

  page.go('#/schedule/s_legacy_1');
  const html = page.html();
  assert.match(html, /이전 버전에서 저장된 시간표입니다/);
  assert.match(html, /옛날카페 신촌점/);
  assert.match(html, /18:00 ~ 22:00/);
  assert.match(html, /010-0000-9999/);
  assert.deepEqual(page.stored('schedules'), [F.legacySchedule()], '열어 보기만 해도 저장물이 바뀌면 안 된다');
});

test('옛 저장물: 새로 추천받고 저장해도 옛 기록·스케줄이 살아 있다', async () => {
  const page = bootHome({ schedules: [F.legacySchedule()], history: [F.legacyHistoryEntry()] });

  // 쓸 수 있는 기록이 없으면 결과 화면은 홈으로 되돌린다 (옛 기록을 억지로 열지 않는다)
  page.go('#/result');
  assert.equal(page.hash(), '#/');

  await generate(page, F.responseFixture());

  // 사이드바에서 옛 기록은 "다시 열 수 없음" 으로만 표시되고 사라지지 않는다
  assert.equal(page.$$('[data-hlegacy]').length, 1);
  assert.equal(page.$$('[data-hid]').length, 1);
  page.clickEl(page.$('[data-hlegacy]'));
  assert.equal(page.toasts.at(-1), '예전 형식 기록입니다. 조건을 확인하고 새로 추천받아 주세요.');

  page.click('#save');
  page.click('#mok');
  await flush();

  const schedules = page.stored('schedules');
  assert.equal(schedules.length, 2);
  assert.deepEqual(schedules.find(s => s.id === 's_legacy_1'), F.legacySchedule());

  const history = page.stored('history');
  assert.equal(history.length, 2);
  // 옛 기록에는 schemaVersion 표식만 붙고 내용은 손대지 않는다
  assert.deepEqual(history.find(e => e.id === 'h_legacy_1'), { ...F.legacyHistoryEntry(), schemaVersion: 1 });
});

test('옛 저장물: 옛 형식 스케줄을 수정해 저장해도 옛 형식 그대로 남는다', () => {
  const page = bootHome({ schedules: [F.legacySchedule()] });
  page.go('#/schedule/s_legacy_1');

  page.click('#edit');
  const inputs = page.$$('input[data-shift]');
  assert.deepEqual(inputs.map(i => i.dataset.shift), [
    'old_job_1:0:start', 'old_job_1:0:end', 'old_job_1:1:start', 'old_job_1:1:end',
  ]);
  page.change(inputs[1], '23:00');                  // 수 종료 22:00 → 23:00
  assert.ok(page.$('#savechg') && !page.$('#savechg').disabled);
  page.click('#savechg');

  const saved = page.stored('schedules')[0];
  assert.equal(saved.schemaVersion, undefined, '옛 형식을 v2 로 승격해 버리면 안 된다');
  assert.ok(!('assignedShifts' in saved.jobs[0]), '옛 jobs 에 서버 필드를 끼워 넣지 않는다');
  assert.deepEqual(saved.jobs[0].shifts, [
    { day: 'WED', start: '18:00', end: '23:00' },
    { day: 'FRI', start: '18:00', end: '22:00' },
  ]);
  assert.deepEqual(saved.jobs[0].contact, F.legacySchedule().jobs[0].contact);
  assert.equal(saved.jobs[0].description, F.legacySchedule().jobs[0].description);
  assert.equal(page.toasts.at(-1), '변경사항을 저장했습니다.');
});

/* =====================================================================
 * 6. /live 라우트 위임
 * ===================================================================*/
test('/live: 프로필이 없어도 온보딩으로 가로채지 않고 라이브 화면에 위임한다', () => {
  const page = createPage();
  page.location._hash = '#/live';
  page.boot();

  assert.equal(page.stored('profile'), null);
  const html = page.html();
  assert.match(html, /Daytona Execution Plane/);
  assert.ok(page.$('#live-run'));
  assert.ok(!html.includes('Onboarding'), '/live 는 온보딩 게이트보다 먼저 처리되어야 한다');
  assert.ok(!html.includes('추천 조합 생성하기'));
  assert.equal(page.calls.length, 0, '화면을 여는 것만으로 네트워크에 나가면 안 된다');
});

test('/live: 주간 추천 화면을 대체하지 않는다 (해시를 되돌리면 홈으로 복귀)', () => {
  const page = bootHome();
  assert.match(page.html(), /추천 조합 생성하기/);

  page.go('#/live');
  assert.match(page.html(), /Daytona Execution Plane/);
  assert.ok(!page.html().includes('추천 조합 생성하기'));

  page.go('#/');
  assert.match(page.html(), /추천 조합 생성하기/);
  assert.ok(!page.html().includes('Daytona Execution Plane'));
});

/* =====================================================================
 * 7. 늦게 도착한 응답의 소유권
 *
 * 요청 하나는 최대 30초다. 그 사이 사용자는 조건을 바꿔 다시 누르거나,
 * 옆의 기록을 열거나, 저장 정보를 초기화할 수 있다. 그때 먼저 나간 요청이
 * 늦게 도착해 화면·Session·기록을 덮어쓰면, 화면은 지금 조건이 아닌 옛 조건의
 * 결과를 "지금 결과"라고 말하게 된다. 아래 네 개가 그 경계다.
 * ===================================================================*/

/** 응답 시점을 테스트가 직접 잡는다. */
function deferredNetwork(page) {
  const waiting = [];
  page.onFetch(() => new Promise((resolve, reject) => waiting.push({ resolve, reject })));
  return {
    inFlight: () => waiting.length,
    resolve: async (i, body, opts) => { waiting[i].resolve(jsonResponse(body, opts)); await flush(8); },
    fail: async (i, err) => { waiting[i].reject(err); await flush(8); },
  };
}

test('늦게 온 응답: 조건을 바꿔 다시 받은 결과를 옛 요청이 덮어쓰지 않는다', async () => {
  const page = bootHome();
  const net = deferredNetwork(page);

  page.click('#go');                                   // 요청 A (2개)
  await flush();
  assert.equal(page.calls[0].body.search.jobCount, 2);

  page.go('#/');                                       // 홈으로 돌아가 조건 변경
  page.clickEl(page.$$('#count button')[0]);           // 1개
  page.click('#go');                                   // 요청 B
  await flush();
  assert.equal(net.inFlight(), 2);
  assert.equal(page.calls[1].body.search.jobCount, 1);

  await net.resolve(1, F.responseFixture({ requestId: 'req_NEW_B' }));
  await net.resolve(0, F.responseFixture({ requestId: 'req_OLD_A' }));   // A 가 늦게 도착

  assert.match(page.html(), /req_NEW_B/);
  assert.ok(!page.html().includes('req_OLD_A'), '늦게 온 옛 응답이 화면을 덮으면 안 된다');
  assert.equal(page.session().response.requestId, 'req_NEW_B');
  assert.equal(page.session().search.count, 1);
  assert.deepEqual(page.stored('history').map(e => e.response.requestId), ['req_NEW_B'],
    '버려진 요청의 응답은 기록에도 남기지 않는다');
});

test('늦게 온 실패: 옛 요청의 에러가 지금 보이는 결과를 에러 화면으로 바꾸지 않는다', async () => {
  const page = bootHome();
  const net = deferredNetwork(page);

  page.click('#go');                                   // 요청 A
  await flush();
  page.go('#/');
  page.click('#go');                                   // 요청 B
  await flush();

  await net.resolve(1, F.responseFixture({ requestId: 'req_NEW_B' }));
  await net.fail(0, new TypeError('fetch failed'));    // A 가 늦게 실패

  assert.equal(page.$$('.plan-card').length, 3);
  assert.match(page.html(), /req_NEW_B/);
  assert.ok(!page.html().includes('추천을 받지 못했습니다'));
  assert.equal(page.session().error, null);
});

test('늦게 온 응답: 초기화(#/reset) 로 지운 결과를 되살리지 않는다', async () => {
  const page = bootHome();
  const net = deferredNetwork(page);

  page.click('#go');
  await flush();

  page.setConfirm(true);
  page.go('#/reset');                                  // localStorage · sessionStorage 를 비운다
  assert.equal(page.hash(), '#/onboarding');
  assert.equal(page.stored('profile'), null);

  await net.resolve(0, F.responseFixture({ requestId: 'req_AFTER_RESET' }));

  assert.equal(page.session(), null, '초기화 뒤 도착한 응답이 세션을 되살리면 안 된다');
  assert.equal(page.stored('history'), null, '초기화 뒤 도착한 응답이 기록을 되살리면 안 된다');
  assert.ok(!page.html().includes('req_AFTER_RESET'));
});

test('늦게 온 응답: 대기 중에 연 옛 기록을 재생성 응답이 밀어내지 않는다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture({ requestId: 'req_FIRST' }));
  const firstHistoryId = page.stored('history')[0].id;

  const net = deferredNetwork(page);
  page.click('#regen');                                 // 재생성 요청이 날아가 있는 상태
  await flush();
  assert.equal(net.inFlight(), 1);

  page.clickEl(page.$$('.hist-item[data-hid]')[0]);     // 그 사이 기록 #1 을 연다
  assert.match(page.html(), /req_FIRST/);

  await net.resolve(0, F.responseFixture({ requestId: 'req_LATE_REGEN' }));

  assert.match(page.html(), /req_FIRST/);
  assert.ok(!page.html().includes('req_LATE_REGEN'), '버린 재생성 응답이 열어 둔 기록을 밀어내면 안 된다');
  assert.equal(page.session().response.requestId, 'req_FIRST');
  assert.equal(page.session().historyId, firstHistoryId);
  assert.deepEqual(page.stored('history').map(e => e.response.requestId), ['req_FIRST']);
});

/* =====================================================================
 * 8. 기록은 "그때의 내 정보" 와 함께 남는다
 *
 * 추천은 요청 당시의 고정 일정·목표 금액에 대한 답이다. 내 정보를 바꾼 뒤
 * 옛 결과를 열면서 지금의 고정 일정 위에 그 배정을 겹쳐 그리면, 화면은
 * 서버가 한 번도 확인한 적 없는 시간표를 보여 주게 된다.
 * ===================================================================*/
test('기록: 요청 당시 내 정보가 함께 저장된다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  const entry = page.stored('history')[0];
  assert.deepEqual(entry.profile.schedule.MON, { has: true, start: '09:00', end: '18:00', place: '강남역' });
  assert.equal(entry.profile.goal, 600000);
  assert.deepEqual(page.session().profile, entry.profile);
});

test('기록: 내 정보를 바꾼 뒤 옛 결과를 열면 당시 고정 일정으로 그리고 그 사실을 밝힌다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  // 내 정보를 바꾼다: 고정 일정을 수요일 신촌으로, 목표를 120만원으로
  const changed = F.profileFixture({ goal: 1200000 });
  for (const d of F.DAY_KEYS) changed.schedule[d] = { has: false, start: '', end: '', place: '' };
  changed.schedule.WED = { has: true, start: '09:00', end: '18:00', place: '신촌' };
  page.seed('profile', changed);
  page.sessionStorage.removeItem('result');
  page.go('#/result');                                  // 기록에서 복원

  const html = page.html();
  assert.match(html, /지금의 내 정보와 다른 조건으로 받은 결과입니다/);
  assert.ok(!html.includes('신촌'), '바뀐 고정 일정을 옛 결과의 시간표에 겹쳐 그리면 안 된다');
  assert.match(html, /목표 600,000원/);
  assert.ok(!html.includes('1,200,000원'), '지표 줄의 목표는 그 결과를 받을 당시 값이어야 한다');

  page.click('#save');
  page.click('#mok');
  const saved = page.stored('schedules')[0];
  assert.equal(saved.profile.goal, 600000);
  assert.equal(saved.profile.schedule.WED.has, false, '저장물도 당시 내 정보로 남아야 한다');
  assert.equal(saved.profile.schedule.MON.place, '강남역');
});

test('기록: 당시 내 정보가 없는 옛 기록은 지금 고정 일정을 겹쳐 그리지 않는다', async () => {
  const seedPage = bootHome();
  await generate(seedPage, F.responseFixture());
  const oldEntry = seedPage.stored('history')[0];
  delete oldEntry.profile;                              // 이 필드가 없던 시절의 기록

  const page = createPage();
  const now = F.profileFixture();
  for (const d of F.DAY_KEYS) now.schedule[d] = { has: false, start: '', end: '', place: '' };
  now.schedule.WED = { has: true, start: '09:00', end: '18:00', place: '신촌' };
  page.seed('profile', now);
  page.seed('history', [oldEntry]);
  page.location._hash = '#/result';
  page.boot();

  const html = page.html();
  assert.match(html, /요청 당시 내 정보가 남아 있지 않습니다/);
  assert.ok(!html.includes('신촌'), '모르는 고정 일정 자리에 지금 값을 채워 넣으면 안 된다');
  assert.equal(page.$$('.tt-block.fixed').length, 0);
  assert.match(html, /치킨하우스 반반 노원점/, '서버가 배정한 근무 자체는 그대로 보인다');
  assert.equal(page.calls.length, 0);
});

test('기록: 기록을 지워도 모르는 고정 일정을 지금 값으로 메우지 않는다', async () => {
  const seedPage = bootHome();
  await generate(seedPage, F.responseFixture());
  const oldEntry = seedPage.stored('history')[0];
  delete oldEntry.profile;

  const page = createPage();
  const now = F.profileFixture();
  for (const d of F.DAY_KEYS) now.schedule[d] = { has: false, start: '', end: '', place: '' };
  now.schedule.WED = { has: true, start: '09:00', end: '18:00', place: '신촌' };
  page.seed('profile', now);
  page.seed('history', [oldEntry]);
  page.location._hash = '#/result';
  page.boot();

  page.click('#hist-clear');                            // 기록만 비운다 (보고 있던 결과는 남는다)

  const html = page.html();
  assert.match(html, /요청 당시 내 정보가 남아 있지 않습니다/);
  assert.ok(!html.includes('신촌'));
  assert.equal(page.$$('.tt-block.fixed').length, 0);
});

/* =====================================================================
 * 9. 수정한 저장 시간표 — 지표가 어느 시점 값인지 계속 밝힌다
 * ===================================================================*/
test('저장물 수정: 저장 후 다시 열어도 지표가 추천 당시 값임을 밝힌다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());
  page.click('#save');
  page.click('#mok');
  await flush();
  const id = page.stored('schedules')[0].id;

  page.click('#edit');
  page.change('input[data-shift="job_cp0095:0:end"]', '21:00');   // 월 19:00–22:00 → 21:00
  assert.match(page.html(), /근무 시간을 수정한 시간표입니다/);
  page.click('#savechg');
  assert.equal(page.stored('schedules')[0].edited, true);

  // 새 세션에서 다시 연다 — dirty 는 없지만 수정된 시간표라는 사실은 남아 있다
  const reopened = createPage();
  reopened.seed('profile', F.profileFixture());
  reopened.seed('schedules', page.stored('schedules'));
  reopened.location._hash = '#/schedule/' + id;
  reopened.boot();

  const html = reopened.html();
  assert.match(html, /19:00 ~ 21:00/, '수정한 시간이 저장돼 있어야 한다');
  assert.match(html, /1,734,792원/, '서버 지표 자체는 지어내지 않고 그대로 둔다');
  assert.match(html, /추천 당시 서버 계산값/);
  assert.match(html, /수정 내용 저장됨/);
  assert.match(html, /예상 월 수입 \(수정 전\)/);
});

/* =====================================================================
 * 10. 당시 내 정보를 모르는 결과 — 지금 값으로 메우지 않는다
 *
 * 기록만이 아니라 세션에도 스냅샷이 없는 상태가 있다. 이 필드가 생기기 전에
 * 열려 있던 탭의 sessionStorage 가 그렇다. 응답은 있는데 그 응답이 어떤 내
 * 정보로 받아진 것인지 모르는 결과를 "지금 내 정보의 답"인 양 그리면, 화면은
 * 서버가 본 적 없는 고정 일정·목표 금액을 그 결과의 일부라고 말하게 된다.
 * ===================================================================*/

/** 지금 내 정보와 확실히 구별되는 프로필 — 무엇이 새어 들어왔는지 이름으로 드러난다. */
function changedProfile() {
  const p = F.profileFixture({ goal: 1200000, home: '홍대' });
  for (const d of F.DAY_KEYS) p.schedule[d] = { has: false, start: '', end: '', place: '' };
  p.schedule.WED = { has: true, start: '09:00', end: '18:00', place: '신촌' };
  return p;
}

test('옛 세션: 스냅샷 없는 세션의 결과를 바뀐 내 정보로 그리지 않는다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  // 이 필드들이 없던 때 남아 있던 세션 — 응답만 있고 당시 내 정보가 없다
  const st = page.session();
  delete st.profile;
  delete st.profileUnknown;
  page.sessionStorage.setItem('result', JSON.stringify(st));

  page.seed('profile', changedProfile());
  page.go('#/result');

  const html = page.html();
  assert.match(html, /요청 당시 내 정보가 남아 있지 않습니다/);
  assert.equal(page.$$('.profile-drift').length, 1, '모른다는 사실을 화면이 밝혀야 한다');
  assert.ok(!html.includes('신촌'), '지금의 고정 일정을 옛 결과의 시간표에 그리면 안 된다');
  assert.equal(page.$$('.tt-block.fixed').length, 0);
  assert.ok(!html.includes('홍대'), '지금의 집 위치를 당시 조건인 양 쓰면 안 된다');
  assert.ok(!html.includes('1,200,000원'), '지금의 목표 금액을 그 결과의 목표로 쓰면 안 된다');
  assert.match(html, /목표 알 수 없음/);
  assert.match(html, /치킨하우스 반반 노원점/, '서버가 배정한 근무 자체는 그대로 보인다');
  assert.equal(page.calls.length, 1, '있는 응답을 두고 다시 요청하지 않는다');

  // 모른다는 사실은 세션에 남아 다시 열어도 같은 말을 한다
  assert.equal(page.session().profileUnknown, true);
  assert.equal(page.session().profile, undefined);
  page.go('#/');
  page.go('#/result');
  assert.match(page.html(), /요청 당시 내 정보가 남아 있지 않습니다/);
  assert.equal(page.calls.length, 1);
});

test('옛 세션: 스냅샷이 있는 결과와 갓 받은 결과는 그대로 둔다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());

  // 갓 받은 결과 — 모름 안내도, 조건이 다르다는 안내도 붙지 않는다
  let html = page.html();
  assert.equal(page.$$('.profile-drift').length, 0);
  assert.match(html, /목표 600,000원/);
  assert.equal(page.$$('.tt-block.fixed').length, 3, '당시 고정 일정(월·화·목)은 그대로 그린다');
  assert.equal(page.session().profileUnknown, false);

  // 화면을 다시 열어도 스냅샷이 있는 한 "모름" 으로 떨어지지 않는다
  page.go('#/');
  page.go('#/result');
  html = page.html();
  assert.ok(!html.includes('요청 당시 내 정보가 남아 있지 않습니다'));
  assert.equal(page.session().profileUnknown, false);
  assert.deepEqual(page.session().profile.schedule.MON, { has: true, start: '09:00', end: '18:00', place: '강남역' });
});

test('모르는 결과의 저장물: 지금 내 정보가 섞이지 않고, 다시 열어도 모름을 밝힌다', async () => {
  const seedPage = bootHome();
  await generate(seedPage, F.responseFixture());
  const oldEntry = seedPage.stored('history')[0];
  delete oldEntry.profile;                              // 이 필드가 없던 시절의 기록

  const page = createPage();
  page.seed('profile', changedProfile());
  page.seed('history', [oldEntry]);
  page.location._hash = '#/result';
  page.boot();
  assert.match(page.html(), /요청 당시 내 정보가 남아 있지 않습니다/);

  page.click('#save');
  page.click('#mok');

  const saved = page.stored('schedules')[0];
  assert.equal(saved.profile, null, '모르는 내 정보 자리에 지금 값을 저장하면 안 된다');
  assert.equal(saved.profileUnknown, true);

  // 다른 브라우저 세션처럼 새로 열어도 (그 사이 내 정보가 또 바뀌어도) 같은 말을 한다
  const reopened = createPage();
  reopened.seed('profile', F.profileFixture({ goal: 9990000, home: '수원' }));
  reopened.seed('schedules', page.stored('schedules'));
  reopened.location._hash = '#/schedule/' + saved.id;
  reopened.boot();

  const html = reopened.html();
  assert.match(html, /추천 당시 내 정보가 남아 있지 않습니다/);
  assert.match(html, /요청 당시 내 정보 없음/);
  assert.ok(!html.includes('신촌'), '저장 시점의 고정 일정도, 다시 열 때의 고정 일정도 그리지 않는다');
  assert.ok(!html.includes('수원'));
  assert.ok(!html.includes('9,990,000원'));
  assert.equal(reopened.$$('.tt-block.fixed').length, 0);
  assert.match(html, /목표 알 수 없음/);
  assert.match(html, /1,734,792원/, '서버가 계산한 지표 자체는 그대로 보인다');
  assert.match(html, /치킨하우스 반반 노원점/);
  assert.equal(reopened.calls.length, 0);
});

test('스냅샷이 있는 결과의 저장물은 당시 내 정보를 그대로 남긴다', async () => {
  const page = bootHome();
  await generate(page, F.responseFixture());
  page.click('#save');
  page.click('#mok');

  const saved = page.stored('schedules')[0];
  assert.equal(saved.profileUnknown, false);
  assert.equal(saved.profile.goal, 600000);
  assert.equal(saved.profile.home, '사당');
  assert.match(page.html(), /집 사당/);
  assert.ok(!page.html().includes('내 정보가 남아 있지 않습니다'));
});
