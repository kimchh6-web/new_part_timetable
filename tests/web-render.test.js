/* =====================================================================
 * tests/web-render.test.js
 *
 * 실행: node --test tests/web-render.test.js
 *
 * 대상: js/app.js 의 렌더 함수들.
 *
 * 한 가지만 본다 — **화면에 나오는 값이 전부 서버 응답에서 왔는가.**
 * js/data.js 의 손으로 만든 JOBS 72건은 이 저장소에 그대로 있고 브라우저에도
 * 로드되지만, 결과 화면은 jobId 로 그 목록을 뒤지면 안 된다. 그래서 이 파일의
 * 시험 데이터는 JOBS 에 실제로 있는 jobId 를 쓰되 내용은 전부 다르게 둔다.
 * 화면에 JOBS 쪽 값이 한 글자라도 새어 나오면 실패한다.
 *
 * DOM 이 필요 없는 함수만 다룬다(전부 HTML 문자열을 돌려준다).
 * 브라우저 확인은 이 파일의 범위가 아니다.
 * ===================================================================*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

/* 브라우저용 스크립트 3개를 그대로(번들러 없이) 올린다. index.html 과 같은 순서. */
const ROOT = path.join(__dirname, '..');
for (const file of ['js/data.js', 'js/engine.js', 'js/app.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(ROOT, file), 'utf8'), { filename: file });
}
const app = vm.runInThisContext(`({
  toViewJob, toViewJobLegacy, toStorageJob, localWeekly, renderTimetable, jobItem,
  warningBlock, metricsRowServer, slotsCard, buildDayTimeline, syntheticNotice,
  disclosureBlock, shiftTravelMinutes, safeProfile, JOBS,
})`);

/* ---------- 손으로 만든 목록에 실제로 있는 id 를 빌려 쓴다 ---------- */
const HANDMADE = app.JOBS[0];
assert.ok(HANDMADE && HANDMADE.id, 'js/data.js 의 JOBS 가 비어 있으면 이 파일의 전제가 깨진다');

const profile = {
  role: '직장인', home: '사당', minBlock: 3, night: true, want15: false, goal: 600000, age: null,
  schedule: {
    MON: { has: true, start: '09:00', end: '18:00', place: '강남역' },
    TUE: { has: false, start: '', end: '', place: '' },
    WED: { has: false, start: '', end: '', place: '' },
    THU: { has: false, start: '', end: '', place: '' },
    FRI: { has: false, start: '', end: '', place: '' },
    SAT: { has: false, start: '', end: '', place: '' },
    SUN: { has: false, start: '', end: '', place: '' },
  },
};

/** 서버가 내려보낸 PlanJob (api-client 가 정규화한 뒤의 모양). */
function planJob(over = {}) {
  return {
    jobId: HANDMADE.id,                       // 같은 id, 완전히 다른 내용
    pinned: false,
    title: '서버가 배정한 주말 근무',
    company: '서버측 상호',
    platform: '알바몬',
    category: '물류·배송',
    location: '노원',
    address: '서울 노원구 노해로 1',
    hourlyWage: 14000,
    rating: 4.9,
    reviewCount: 3,
    thumbnail: null,
    descriptionSnippet: '서버가 준 설명',
    sourceUrl: null,
    contact: { manager: '서버 담당자', phone: '010-0000-0000', kakao: null, applyUrl: null, preferred: '문자' },
    timeNegotiable: true,
    minWeeks: 8,
    benefits: ['교통비 지원'],
    assignedShifts: [{
      day: 'SAT', start: '10:00', end: '14:00',
      travel: { fromLocation: '사당', transitMinutes: 30, walkMinutes: 6, originWalkMinutes: 4, legMinutes: 40, slackMinutes: 5, bufferMinutes: 15, departAt: '09:05' },
    }],
    weeklyHours: 4,
    weeklyPay: 56000,
    holidayPay: null,
    unverifiedQualifications: [],
    ...over,
  };
}

const metrics = {
  monthlyIncome: 688000, targetAchievementRate: 1.15, weeklyWorkHours: 16,
  weeklyTravelMinutes: 180, effectiveHourlyWage: 10720, weeklyHolidayPayIncluded: false,
};

/* =====================================================================
 * 1. 손으로 만든 JOBS 로부터의 독립
 * ===================================================================*/
test('toViewJob: jobId 가 같아도 값은 서버 것만 쓴다', () => {
  const j = app.toViewJob(planJob());
  assert.equal(j.id, HANDMADE.id);
  assert.equal(j.company, '서버측 상호');
  assert.equal(j.location, '노원');
  assert.equal(j.category, '물류·배송');
  assert.equal(j.hourlyWage, 14000);
  assert.notEqual(j.company, HANDMADE.company);
  assert.deepEqual(j.shifts, [{
    day: 'SAT', start: '10:00', end: '14:00',
    travel: { fromLocation: '사당', transitMinutes: 30, walkMinutes: 6, originWalkMinutes: 4, legMinutes: 40, slackMinutes: 5, bufferMinutes: 15, departAt: '09:05' },
  }]);
});

test('renderTimetable: 손으로 만든 공고의 상호·근무시간이 화면에 새어 나오지 않는다', () => {
  const html = app.renderTimetable([app.toViewJob(planJob())], profile);

  assert.match(html, /서버측 상호/);
  assert.match(html, /10:00–14:00/, '배정 근무는 assignedShifts 그대로여야 한다');
  assert.ok(!html.includes(HANDMADE.company), `손으로 만든 상호(${HANDMADE.company})가 보이면 안 된다`);
  for (const sh of HANDMADE.shifts) {
    assert.ok(!html.includes(`${sh.start}–${sh.end}`), '공고 원본 시간표를 그리면 안 된다');
  }
});

test('jobItem: 상세도 서버 값만 쓴다', () => {
  const html = app.jobItem(app.toViewJob(planJob()), profile, { controls: 'result' });
  assert.match(html, /서버가 배정한 주말 근무/);
  assert.match(html, /서버측 상호/);
  assert.match(html, /14,000원/);
  assert.match(html, /8주/);
  assert.match(html, /서버 담당자/);
  assert.ok(!html.includes(HANDMADE.company));
  assert.ok(!html.includes(HANDMADE.contact.manager));
});

/* =====================================================================
 * 2. 이동시간 — 서버 값이 있으면 서버 값, 없으면 추정이라고 밝힌다
 * ===================================================================*/
test('shiftTravelMinutes: legMinutes(= door-to-door 편도)를 그대로 쓴다', () => {
  assert.equal(app.shiftTravelMinutes({ travel: { legMinutes: 40, transitMinutes: 30, walkMinutes: 6 } }), 40);
  assert.equal(app.shiftTravelMinutes({ travel: { transitMinutes: 30, walkMinutes: 6 } }), 36, 'legMinutes 가 없을 때만 더한다');
  assert.equal(app.shiftTravelMinutes({ travel: null }), null);
  assert.equal(app.shiftTravelMinutes({ travel: {} }), null, '없는 값을 0 으로 만들지 않는다');
});

test('renderTimetable: 서버 이동시간에는 추정 표시를 붙이지 않는다', () => {
  const html = app.renderTimetable([app.toViewJob(planJob())], profile);
  assert.match(html, /이동 40분/);
  assert.ok(!html.includes('이동 40분*'));
  assert.ok(!html.includes('브라우저 추정 이동시간</span>'), '범례에 추정 각주가 붙으면 안 된다');
  assert.match(html, /서버 계산 이동시간/);
});

test('renderTimetable: 서버가 이동을 안 줬으면 추정임을 밝힌다', () => {
  const noTravel = planJob();
  noTravel.assignedShifts[0].travel = null;
  const html = app.renderTimetable([app.toViewJob(noTravel)], profile);
  assert.match(html, /이동 \d+분\*/, '추정값에는 * 를 붙인다');
  assert.match(html, /브라우저 추정 이동시간/);
});

/* =====================================================================
 * 3. 지표 — 다시 계산하지 않는다
 * ===================================================================*/
test('metricsRowServer: 서버 숫자를 그대로 내보낸다', () => {
  const html = app.metricsRowServer(metrics, profile);
  assert.match(html, /688,000원/);
  assert.match(html, /115%/);
  assert.match(html, /16\.0h/);
  assert.match(html, /3시간/, '180분은 3시간으로 표기');
  assert.match(html, /10,720원/);
  assert.match(html, /주휴수당 미포함/);
});

test('metricsRowServer: 없는 지표는 — 로 두고 채워 넣지 않는다', () => {
  const html = app.metricsRowServer({
    monthlyIncome: null, targetAchievementRate: null, weeklyWorkHours: null,
    weeklyTravelMinutes: null, effectiveHourlyWage: null, weeklyHolidayPayIncluded: null,
  }, profile);
  assert.equal((html.match(/—/g) || []).length, 5, '다섯 칸 모두 — 여야 한다');
  assert.match(html, /주휴수당 여부 미확인/);
  assert.ok(!/"v">0(원|%|h)/.test(html), '모르는 값을 0 으로 적지 않는다');
});

test('jobItem: 주급은 서버 값이 없으면 곱해서 만들지 않는다', () => {
  const withServer = app.jobItem(app.toViewJob(planJob()), profile, {});
  assert.match(withServer, /주 <b>4\.0h<\/b>/);
  assert.match(withServer, /56,000원/);

  const without = app.jobItem(app.toViewJob(planJob({ weeklyHours: null, weeklyPay: null })), profile, {});
  assert.match(without, /주 <b>4\.0h<span title=/, '배정 시간 합계는 보여주되 서버 값이 아님을 밝힌다');
  assert.match(without, /주급 <b>—<\/b>/, '시급 × 시간을 대신 계산하지 않는다');
  assert.ok(!without.includes('56,000원'));
});

/* =====================================================================
 * 4. 합성 데이터 공시
 * ===================================================================*/
test('syntheticNotice: 실제 공고가 아님을 매 화면에서 밝힌다', () => {
  const html = app.syntheticNotice();
  assert.match(html, /합성 데모 데이터/);
  assert.match(html, /실제 채용 공고가 아니/);
  assert.match(html, /연락처·링크도 데모용/);
});

test('jobItem: 연락처에도 데모임을 붙인다', () => {
  const html = app.jobItem(app.toViewJob(planJob()), profile, {});
  assert.match(html, /데모 연락처/);
  assert.match(html, /실제로 연결되지 않습니다/);
});

test('disclosureBlock: 서버가 밝힌 전제를 그대로 싣고, 없으면 아무것도 그리지 않는다', () => {
  assert.equal(app.disclosureBlock([]), '');
  assert.equal(app.disclosureBlock(null), '');
  const html = app.disclosureBlock(['이동 시간은 추정값입니다.', '공고 데이터는 데모용 합성 데이터셋(600건)입니다.']);
  assert.match(html, /서버가 밝힌 계산 전제 2건/);
  assert.match(html, /합성 데이터셋\(600건\)/);
});

/* =====================================================================
 * 5. 경고 · 이스케이프
 * ===================================================================*/
test('warningBlock: 모르는 코드도 서버 문구는 그대로 보여준다', () => {
  const html = app.warningBlock([
    { code: 'LONG_TRAVEL', message: '토요일 편도 45분입니다.', jobId: null },
    { code: 'HOLIDAY_PAY_NOT_INCLUDED', message: '주휴수당은 넣지 않았습니다.', jobId: null },
    { code: 'FUTURE_CODE', message: '앞으로 생길 경고.', jobId: null },
  ], profile);
  assert.match(html, /이동 시간이 깁니다/);
  assert.match(html, /주휴수당은 수입에 넣지 않았습니다/);
  assert.match(html, /FUTURE_CODE/, '모르는 코드는 코드 그대로 보여준다');
  assert.match(html, /앞으로 생길 경고/);
});

test('warningBlock: 나이를 입력했으면 AGE_UNVERIFIED 안내 링크를 걸지 않는다', () => {
  const w = [{ code: 'AGE_UNVERIFIED', message: '연령 조건을 확인하지 못했습니다.', jobId: null }];
  assert.match(app.warningBlock(w, profile), /내 정보에서 나이 입력/);
  assert.ok(!app.warningBlock(w, { ...profile, age: 26 }).includes('내 정보에서 나이 입력'));
  assert.equal(app.warningBlock([], profile), '');
});

test('서버 문자열은 HTML 로 해석되지 않는다', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const j = app.toViewJob(planJob({ company: evil, title: evil }));
  for (const html of [app.renderTimetable([j], profile), app.jobItem(j, profile, {}),
                      app.warningBlock([{ code: 'X', message: evil }], profile),
                      app.disclosureBlock([evil]),
                      app.slotsCard([{ day: 'SAT', from: '10:00', to: '14:00', fromLocation: evil }])]) {
    assert.ok(!html.includes('<img'), '원본 태그가 그대로 들어가면 안 된다');
    assert.match(html, /&lt;img/);
  }
});

/* =====================================================================
 * 6. 서버가 계산한 빈 시간 / 요일 타임라인
 * ===================================================================*/
test('slotsCard: 서버가 준 빈 시간을 그대로 그린다', () => {
  const html = app.slotsCard([{ day: 'SAT', from: '08:00', to: '24:00', fromLocation: '사당' }]);
  assert.match(html, /토/);
  assert.match(html, /08:00 ~ 24:00/);
  assert.match(html, /사당 출발/);
  assert.equal(app.slotsCard([]), '');
});

test('buildDayTimeline: 고정 일정과 배정 근무가 시간순으로 한 줄에 선다', () => {
  const j = app.toViewJob(planJob({ assignedShifts: [{ day: 'MON', start: '19:00', end: '22:00', travel: null }] }));
  const tl = app.buildDayTimeline([j], profile, 'MON');
  assert.equal(tl.length, 2);
  assert.equal(tl[0].type, 'fixed');
  assert.equal(tl[0].loc, '강남역');
  assert.equal(tl[1].type, 'job');
  assert.equal(tl[1].start, 19 * 60);
});

/* =====================================================================
 * 7. 옛 저장 데이터 보존
 * ===================================================================*/
test('toViewJobLegacy: 옛 형식(shifts)을 읽되 새 값을 지어내지 않는다', () => {
  const old = {
    id: 'job_003', title: '옛 알바', company: '옛 상호', category: '편의점', location: '신촌',
    address: '서울 서대문구', hourlyWage: 10000, rating: 4.1, reviewCount: 10,
    negotiable: true, minWeeks: 4, benefits: ['식사 제공'],
    contact: { manager: '옛 담당자', phone: '010-1111-2222' },
    shifts: [{ day: 'TUE', start: '19:00', end: '23:00' }],
  };
  const j = app.toViewJobLegacy(old);
  assert.equal(j.legacy, true);
  assert.equal(j.company, '옛 상호');
  assert.equal(j.timeNegotiable, true);
  assert.equal(j.weeklyHours, 4);
  assert.deepEqual(j.shifts, [{ day: 'TUE', start: '19:00', end: '23:00', travel: null }]);
  assert.equal(j.shifts[0].travel, null, '옛 데이터에는 서버 이동정보가 없다');
});

test('toStorageJob: 저장 형식을 바꾸지 않는다 (옛 것은 shifts, 새 것은 assignedShifts)', () => {
  const old = { id: 'job_003', company: '옛 상호', benefits: ['식사 제공'], contact: { manager: '옛 담당자' }, shifts: [{ day: 'TUE', start: '19:00', end: '23:00' }] };
  const view = app.toViewJobLegacy(old);
  view.shifts[0].start = '20:00';
  const back = app.toStorageJob(view, true);
  assert.deepEqual(back.shifts, [{ day: 'TUE', start: '20:00', end: '23:00' }]);
  assert.equal(back.company, '옛 상호', '건드리지 않은 필드는 그대로 남는다');
  assert.deepEqual(back.contact, { manager: '옛 담당자' });
  assert.equal('assignedShifts' in back, false, '옛 기록을 새 형식으로 덮어쓰지 않는다');

  const fresh = app.toViewJob(planJob());
  const saved = app.toStorageJob(fresh, false);
  assert.equal(saved.assignedShifts.length, 1);
  assert.equal(saved.jobId, HANDMADE.id);
  assert.equal('shifts' in saved, false);
});

test('safeProfile: 프로필을 잃은 저장 데이터도 화면을 무너뜨리지 않는다', () => {
  const p = app.safeProfile(undefined);
  assert.deepEqual(p.schedule, {});
  assert.equal(p.home, '');
  assert.doesNotThrow(() => app.renderTimetable([app.toViewJob(planJob())], p));
  assert.equal(app.safeProfile({ home: '사당' }).home, '사당');
});
