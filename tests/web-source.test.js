/* =====================================================================
 * tests/web-source.test.js
 *
 * 실행: node --test tests/web-source.test.js
 *
 * 대상: 공고 데이터의 **출처 표시**. js/api-client.js 의 meta 해석과
 *       js/app.js 의 sourceNotice() 두 군데만 본다.
 *
 * 화면이 저지를 수 있는 거짓말은 네 가지뿐이고, 아래 시험은 그 네 가지를
 * 하나씩 막는다.
 *   1) 실제 공고를 "합성 데모 데이터"라고 부르는 것,
 *   2) 데모 데이터를 "실제 공고"라고 부르는 것,
 *   3) 제공받은 공고(authorized_import)를 "실시간 수집"이라고 부르는 것,
 *   4) 서버가 출처를 밝히기도 전에 둘 중 하나로 단정하는 것.
 *
 * 순위를 만든 방식(source=fallback)과 데이터 출처(meta.job_source)는 서로 다른
 * 축이다. 한쪽이 다른 쪽을 바꾸지 않는지도 여기서 본다.
 * ===================================================================*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const api = require('../js/api-client.js');

const ROOT = path.join(__dirname, '..');
for (const file of ['js/data.js', 'js/engine.js', 'js/app.js']) {
  vm.runInThisContext(fs.readFileSync(path.join(ROOT, file), 'utf8'), { filename: file });
}
const app = vm.runInThisContext('({ sourceNotice, syntheticNotice })');

const SYNTHETIC = /<b>합성 데모 데이터<\/b>/;
/* "실제 공고다"라고 단정하는 문구. 중립 문구의 "실제 공고인지 데모인지"는 단정이 아니다. */
const REAL = /<b>(실제 공개 공고|제공된 실제 공고)<\/b>/;

function responseWith(meta) {
  return {
    requestId: 'req_TEST',
    generatedAt: '2026-09-19T14:32:10+09:00',
    source: 'fallback',
    availableSlots: [],
    candidateCount: 1,
    plans: [{
      id: 'plan_balanced',
      type: 'balanced',
      label: '밸런스안',
      reason: '이유',
      metrics: { monthlyIncome: 500000, targetAchievementRate: 0.8, weeklyWorkHours: 12, weeklyTravelMinutes: 60, effectiveHourlyWage: 11000, weeklyHolidayPayIncluded: false },
      jobs: [{
        jobId: 'albamon_1001',
        pinned: false,
        title: '카페 홀 서빙',
        company: '테스트 카페',
        platform: '알바몬',
        category: '카페·음식점',
        location: '사당',
        address: '서울 동작구 사당로 1',
        hourlyWage: 11000,
        sourceUrl: 'https://www.albamon.com/jobs/detail/1001',
        assignedShifts: [{ day: 'SAT', start: '10:00', end: '14:00', travel: { fromLocation: '사당', transitMinutes: 0, walkMinutes: 6, bufferMinutes: 15, departAt: '09:39' } }],
        weeklyHours: 4,
        weeklyPay: 44000,
      }],
      warnings: [],
    }],
    meta,
  };
}

/* =====================================================================
 * 1. 서버 meta → 화면이 쓰는 출처 객체
 * ===================================================================*/
test('adaptResponse: 서버가 밝힌 출처를 그대로 옮긴다', () => {
  const r = api.adaptResponse(responseWith({
    job_source: 'public_web',
    data_mode: 'live',
    source_counts: { attempted: 5, collected: 3, accepted: 2, rejected: 1, walk_estimated: 1, providers: ['albamon'] },
  }));
  assert.equal(r.jobSource.mode, 'public_web');
  assert.equal(r.jobSource.dataMode, 'live');
  assert.equal(r.jobSource.counts.accepted, 2);
  assert.equal(r.jobSource.counts.rejected, 1);
  assert.equal(r.jobSource.counts.walkEstimated, 1);
  assert.deepEqual(r.jobSource.counts.providers, ['albamon']);
});

test('adaptResponse: 출처가 없거나 모르는 값이면 채워 넣지 않는다', () => {
  for (const meta of [{}, { job_source: 'scraped_somehow', data_mode: '실시간' }, { job_source: null }]) {
    const r = api.adaptResponse(responseWith(meta));
    assert.equal(r.jobSource.mode, null);
    assert.equal(r.jobSource.dataMode, null);
    assert.equal(r.jobSource.counts, null);
  }
});

test('adaptResponse: 데이터 출처는 순위 방식(source)과 다른 축이다', () => {
  const r = api.adaptResponse(responseWith({ job_source: 'public_web', data_mode: 'live' }));
  assert.equal(r.source, 'fallback');          // 순위: 서버 규칙 계산
  assert.equal(r.jobSource.mode, 'public_web'); // 데이터: 실제 공고
});

test('adaptResponse: 데모 응답은 demo_json/demo 로 온다', () => {
  const r = api.adaptResponse(responseWith({ job_source: 'demo_json', data_mode: 'demo' }));
  assert.equal(r.jobSource.mode, 'demo_json');
  assert.equal(r.jobSource.dataMode, 'demo');
});

/* =====================================================================
 * 2. 출처 문구 — 네 가지 거짓말 막기
 * ===================================================================*/
test('sourceNotice: 서버가 밝히기 전에는 어느 쪽도 단정하지 않는다', () => {
  for (const value of [null, undefined, {}, { mode: null, dataMode: null }]) {
    const html = app.sourceNotice(value);
    assert.match(html, /출처 미표시/);
    assert.doesNotMatch(html, SYNTHETIC);
    assert.doesNotMatch(html, REAL);
  }
});

test('sourceNotice: 데모 데이터는 예전 그대로 합성 데이터라고 밝힌다', () => {
  const html = app.sourceNotice({ mode: 'demo_json', dataMode: 'demo' });
  assert.match(html, SYNTHETIC);
  assert.equal(html, app.syntheticNotice());
});

test('sourceNotice: 실제 수집 공고를 합성 데모라고 부르지 않는다', () => {
  const html = app.sourceNotice({ mode: 'public_web', dataMode: 'live', counts: null });
  assert.match(html, REAL);
  assert.match(html, /실제 공개 공고/);
  assert.doesNotMatch(html, SYNTHETIC);
  assert.match(html, /원문 공고에서 확인/);
});

test('sourceNotice: 제공받은 공고를 실시간 수집이라고 부르지 않는다', () => {
  const html = app.sourceNotice({ mode: 'public_web', dataMode: 'authorized_import', counts: null });
  assert.match(html, REAL);
  assert.match(html, /제공된 실제 공고/);
  assert.match(html, /실시간 크롤링 아님/);
  assert.doesNotMatch(html, SYNTHETIC);
});

test('sourceNotice: 건수와 추정 적용 건수는 서버가 준 만큼만 쓴다', () => {
  const html = app.sourceNotice({
    mode: 'public_web',
    dataMode: 'live',
    counts: { accepted: 2, rejected: 3, walkEstimated: 1, providers: ['albamon', 'alba'] },
  });
  assert.match(html, /검증 통과 2건/);
  assert.match(html, /제외 3건/);
  assert.match(html, /도보 시간 데모 추정 1건/);
  assert.match(html, /출처 albamon, alba/);

  const bare = app.sourceNotice({ mode: 'public_web', dataMode: 'live', counts: null });
  assert.doesNotMatch(bare, /검증 통과/);
});

test('sourceNotice: 반쪽짜리 출처 정보는 실제 공고로 승격되지 않는다', () => {
  for (const value of [
    { mode: 'public_web', dataMode: null },
    { mode: 'public_web', dataMode: 'demo' },
    { mode: null, dataMode: 'live' },
  ]) {
    const html = app.sourceNotice(value);
    assert.match(html, /출처 미표시/);
    assert.doesNotMatch(html, REAL);
  }
});
