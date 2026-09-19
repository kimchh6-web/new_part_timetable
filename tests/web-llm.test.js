/* =====================================================================
 * tests/web-llm.test.js
 *
 * 실행: node --test tests/web-llm.test.js
 *
 * 대상: 주간 추천을 **무엇이 만들었는가**의 표시.
 *       js/api-client.js 의 llm metadata 해석과 js/app.js 의
 *       llmNotice() · reasonOriginLabel() 만 본다.
 *
 * 화면이 저지를 수 있는 거짓말은 다섯 가지이고, 아래 시험이 하나씩 막는다.
 *   1) 결정론적 fallback 을 "AI가 평가했다"고 부르는 것,
 *   2) 서버가 source=llm 이라고만 하고 근거(llmUsed·llmStatus)를 안 줬는데
 *      검증된 AI 평가인 것처럼 부르는 것,
 *   3) fallback 사유를 모르면서 그럴듯한 이유를 지어내는 것,
 *   4) 제공자 오류 원문을 그대로 화면에 그리는 것,
 *   5) 옛 응답(metadata 없음)을 새 판정으로 덧칠하는 것.
 *
 * 공고 데이터의 출처(meta.job_source)는 다른 축이며 tests/web-source.test.js 가 본다.
 * 여기서는 한쪽이 다른 쪽을 바꾸지 않는지만 확인한다.
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
const app = vm.runInThisContext('({ llmNotice, llmState, reasonOriginLabel, sourceNotice })');

/* "AI가 평가했다"고 단정하는 문구. */
const CLAIMS_AI = /<b>AI가 적합도와 추천 사유를 평가했습니다\.<\/b>/;
const GENERIC_FALLBACK = /LLM 대신 서버 계산 규칙으로 만든 조합입니다/;

function responseWith(source, meta) {
  return {
    requestId: 'req_TEST',
    generatedAt: '2026-09-19T14:32:10+09:00',
    source,
    availableSlots: [],
    candidateCount: 1,
    plans: [{
      id: 'plan_balanced',
      type: 'balanced',
      label: '밸런스안',
      reason: '이동이 적고 목표에 가깝습니다.',
      metrics: { monthlyIncome: 500000, targetAchievementRate: 0.8, weeklyWorkHours: 12, weeklyTravelMinutes: 60, effectiveHourlyWage: 11000, weeklyHolidayPayIncluded: false },
      jobs: [{
        jobId: 'albamon_1001',
        title: '카페 홀 서빙',
        company: '테스트 카페',
        hourlyWage: 11000,
        assignedShifts: [{ day: 'SAT', start: '10:00', end: '14:00', travel: { fromLocation: '사당', transitMinutes: 0, walkMinutes: 6, bufferMinutes: 15, departAt: '09:39' } }],
        weeklyHours: 4,
        weeklyPay: 44000,
      }],
      warnings: [],
    }],
    meta,
  };
}

const SUCCESS_META = {
  contractVersion: 'weekly.v1',
  engine: 'hybrid',
  llmUsed: true,
  llmProvider: 'nosana',
  llmModel: 'qwen2.5-7b-instruct',
  llmLatencyMs: 1840,
  llmStatus: 'success',
};

/* =====================================================================
 * 1. 서버 metadata → 화면이 쓰는 판정
 * ===================================================================*/
test('adaptResponse: 검증된 LLM 성공을 그대로 옮긴다', () => {
  const r = api.adaptResponse(responseWith('llm', SUCCESS_META));
  assert.equal(r.source, 'llm');
  assert.equal(r.llm.verified, true);
  assert.equal(r.llm.claimed, true);
  assert.equal(r.llm.engine, 'hybrid');
  assert.equal(r.llm.used, true);
  assert.equal(r.llm.provider, 'nosana');
  assert.equal(r.llm.model, 'qwen2.5-7b-instruct');
  assert.equal(r.llm.latencyMs, 1840);
  assert.equal(r.llm.status, 'success');
});

test('adaptResponse: fallback 사유는 계약 enum 만 받는다', () => {
  for (const status of ['not_configured', 'timeout', 'provider_error', 'invalid_response', 'budget_exhausted']) {
    const r = api.adaptResponse(responseWith('fallback', { engine: 'deterministic', llmUsed: false, llmStatus: status }));
    assert.equal(r.llm.status, status);
    assert.equal(r.llm.engine, 'deterministic');
    assert.equal(r.llm.used, false);
    assert.equal(r.llm.verified, false);
  }
});

test('adaptResponse: 모르는 값은 채우지 않고 null 로 둔다', () => {
  const r = api.adaptResponse(responseWith('fallback', {
    engine: 'magic', llmUsed: 'yes', llmProvider: 'unlisted_provider',
    llmModel: 42, llmLatencyMs: -1, llmStatus: 'exploded',
  }));
  assert.deepEqual(
    { ...r.llm },
    { engine: null, used: null, provider: null, model: null, latencyMs: null, status: null, claimed: false, verified: false },
  );
});

test('adaptResponse: metadata 가 없는 옛 응답은 전부 미상으로 남는다', () => {
  const r = api.adaptResponse(responseWith('fallback', undefined));
  assert.equal(r.llm.status, null);
  assert.equal(r.llm.used, null);
  assert.equal(r.llm.verified, false);
});

test('adaptResponse: source=llm 이라도 근거가 없으면 verified 가 아니다', () => {
  for (const meta of [
    undefined,
    { engine: 'hybrid' },                                             // llmUsed·llmStatus 없음
    { llmUsed: true },                                                // 상태 없음
    { llmUsed: false, llmStatus: 'success' },                         // 서로 어긋남
    { llmUsed: true, llmStatus: 'timeout' },                          // 성공이 아님
    { llmUsed: true, llmStatus: 'success', engine: 'deterministic' }, // 엔진과 어긋남
  ]) {
    const r = api.adaptResponse(responseWith('llm', meta));
    assert.equal(r.llm.claimed, true);
    assert.equal(r.llm.verified, false, JSON.stringify(meta));
  }
});

test('adaptResponse: source 가 fallback 이면 llmUsed=true 라도 verified 가 아니다', () => {
  const r = api.adaptResponse(responseWith('fallback', { llmUsed: true, llmStatus: 'success', engine: 'hybrid' }));
  assert.equal(r.llm.verified, false);
  assert.equal(r.llm.claimed, false);
});

test('adaptResponse: 모델 이름은 60자까지만 옮긴다', () => {
  const r = api.adaptResponse(responseWith('llm', { ...SUCCESS_META, llmModel: 'm'.repeat(200) }));
  assert.equal(r.llm.model.length, 60);
});

test('adaptResponse: 생성 방식은 공고 출처와 다른 축이다', () => {
  const r = api.adaptResponse(responseWith('llm', { ...SUCCESS_META, job_source: 'demo_json', data_mode: 'demo' }));
  assert.equal(r.llm.verified, true);
  assert.equal(r.jobSource.mode, 'demo_json');   // AI가 골랐어도 데이터는 합성 데모 그대로
  assert.match(app.sourceNotice(r.jobSource), /<b>합성 데모 데이터<\/b>/);
});

test('adaptResponse: meta 원본과 응답 순서는 그대로 남는다', () => {
  const raw = responseWith('llm', SUCCESS_META);
  raw.plans.push({ ...raw.plans[0], id: 'plan_income', type: 'maxIncome', label: '수입 최대안' });
  const r = api.adaptResponse(raw);
  assert.deepEqual(r.plans.map(p => p.id), ['plan_balanced', 'plan_income']);
  assert.equal(r.meta.llmStatus, 'success');
  assert.equal(r.meta.engine, 'hybrid');
});

/* =====================================================================
 * 2. 문구 — 다섯 가지 거짓말 막기
 * ===================================================================*/
test('llmNotice: 검증된 성공만 AI 평가라고 말한다', () => {
  const html = app.llmNotice('llm', api.adaptResponse(responseWith('llm', SUCCESS_META)).llm);
  assert.match(html, CLAIMS_AI);
  assert.match(html, /시간표·이동·수입은 서버 코드가 검증한 값/);
  assert.match(html, /제공자 nosana/);
  assert.match(html, /모델 qwen2\.5-7b-instruct/);
  assert.match(html, /응답 1,840ms/);
});

test('llmNotice: 서버가 준 만큼만 쓴다 — 없는 모델·지연은 비운다', () => {
  const html = app.llmNotice('llm', { used: true, status: 'success', engine: 'hybrid', provider: null, model: null, latencyMs: null });
  assert.match(html, CLAIMS_AI);
  assert.doesNotMatch(html, /제공자|모델|응답 /);
});

test('llmNotice: 근거 없는 source=llm 은 AI 평가라고 부르지 않는다', () => {
  for (const meta of [undefined, { llmUsed: true }, { llmUsed: false, llmStatus: 'success' }]) {
    const html = app.llmNotice('llm', api.adaptResponse(responseWith('llm', meta)).llm);
    assert.match(html, /AI 사용 여부를 확인하지 못했습니다/);
    assert.doesNotMatch(html, CLAIMS_AI);
  }
});

test('llmNotice: fallback 사유를 아는 만큼만 구분해 말한다', () => {
  const cases = {
    not_configured: /LLM 모델이 설정되어 있지 않아 AI 평가를 생략했습니다/,
    timeout: /제한 시간 안에 답하지 않아/,
    provider_error: /LLM 제공자에 연결하지 못해/,
    invalid_response: /형식 검증을 통과하지 못해/,
    /* 요청에 남은 처리 시간 예산이다. 크레딧·토큰 할당량이라고 말하면 거짓말이 된다. */
    budget_exhausted: /남은 처리 시간이 부족해 AI 평가를 생략했습니다/,
  };
  for (const [status, re] of Object.entries(cases)) {
    const html = app.llmNotice('fallback', api.adaptResponse(responseWith('fallback', { engine: 'deterministic', llmUsed: false, llmStatus: status })).llm);
    assert.match(html, re);
    assert.match(html, /추천 사유는 코드가 만든 고정 문구/);
    assert.doesNotMatch(html, CLAIMS_AI);
    for (const [other, otherRe] of Object.entries(cases)) {
      if (other !== status) assert.doesNotMatch(html, otherRe, `${status} 문구에 ${other} 사유가 섞였다`);
    }
  }
});

test('llmNotice: 사유를 모르는 옛 fallback 은 예전 문구 그대로다', () => {
  for (const llm of [null, undefined, api.adaptResponse(responseWith('fallback', undefined)).llm]) {
    const html = app.llmNotice('fallback', llm);
    assert.match(html, GENERIC_FALLBACK);
    assert.doesNotMatch(html, /설정되어 있지 않아|제한 시간|연결하지 못해|처리 시간이 부족/);
  }
});

test('llmNotice: fallback 인데 llmUsed=true 면 사유를 믿지 않는다', () => {
  const html = app.llmNotice('fallback', { used: true, status: 'timeout' });
  assert.match(html, GENERIC_FALLBACK);
  assert.doesNotMatch(html, /제한 시간/);
});

test('llmNotice: 제공자 오류 원문은 어떤 경우에도 그리지 않는다', () => {
  const secret = 'Error: provider.local 401 sk-live-DO-NOT-SHOW';
  for (const source of ['llm', 'fallback', null]) {
    const html = app.llmNotice(source, api.adaptResponse(responseWith(source, {
      llmUsed: false, llmStatus: 'provider_error', llmError: secret, message: secret,
    })).llm);
    assert.doesNotMatch(html, /sk-live/);
    assert.doesNotMatch(html, /401/);
  }
});

test('llmNotice: source 를 모르면 생성 방식을 단정하지 않는다', () => {
  const html = app.llmNotice(null, api.adaptResponse(responseWith(undefined, SUCCESS_META)).llm);
  assert.match(html, /생성 방식\(source\)을 밝히지 않았습니다/);
  assert.doesNotMatch(html, CLAIMS_AI);
  assert.doesNotMatch(html, GENERIC_FALLBACK);
});

/* =====================================================================
 * 3. 이름표 — 바닥글과 카드의 사유 출처
 * ===================================================================*/
test('reasonOriginLabel: 네 가지 상태를 섞지 않는다', () => {
  const verified = api.adaptResponse(responseWith('llm', SUCCESS_META)).llm;
  assert.equal(app.reasonOriginLabel('llm', verified), 'AI 사유');
  assert.equal(app.reasonOriginLabel('llm', api.adaptResponse(responseWith('llm', {})).llm), '사유 출처 확인 불가');
  assert.equal(app.reasonOriginLabel('fallback', null), '규칙 사유');
  assert.equal(app.reasonOriginLabel(null, null), '사유 출처 미상');
});

/* =====================================================================
 * 4. 저장물 — 저장한 스냅샷도 같은 말을 한다
 * ===================================================================*/
test('llmState: 저장된 스냅샷은 기록된 플래그가 아니라 원래 값으로 다시 판정한다', () => {
  // 저장물에 verified:true 가 박혀 있어도 근거가 없으면 인정하지 않는다.
  const forged = { verified: true, claimed: true, used: null, status: null, engine: null };
  assert.equal(app.llmState('llm', forged).verified, false);
  assert.match(app.llmNotice('llm', forged), /확인하지 못했습니다/);

  // llm 필드가 없는 옛 저장물은 source 만으로 정직하게 읽힌다.
  assert.equal(app.llmState('fallback', undefined).verified, false);
  assert.match(app.llmNotice('fallback', undefined), GENERIC_FALLBACK);
});

/* =====================================================================
 * 5. 화면 흐름 — 실제 app.js 를 올려 결과·저장 화면을 그린다
 *
 * ui_flow 하네스(tests/ui_flow/support)는 읽기만 하고 고치지 않는다.
 * 단위 시험이 문구를 보는 동안, 여기서는 그 문구가 **실제로 결과 화면과
 * 저장물에 실려 나오는지**만 본다.
 * ===================================================================*/
const { createPage } = require('./ui_flow/support/page.js');
const F = require('./ui_flow/support/fixtures.js');

const flush = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

async function resultPage(over) {
  const page = createPage();
  page.seed('profile', F.profileFixture());
  page.boot();
  page.queueJson(F.responseFixture(over));
  page.click('#go');
  await flush();
  return page;
}

test('결과 화면: 검증된 LLM 성공이면 AI 평가 고지와 사유 이름표가 함께 나온다', async () => {
  const page = await resultPage({ source: 'llm', meta: { contractVersion: 'weekly.v1', ...SUCCESS_META } });
  const html = page.html();
  assert.match(html, /llm-note verified/);
  assert.match(html, CLAIMS_AI);
  assert.match(html, /제공자 nosana/);
  assert.ok(page.$$('.plan-card .reason .hint').every(el => el.textContent === 'AI 사유'));
  assert.match(page.text(), /출처 LLM/, '바닥글의 기존 출처 표기는 그대로 둔다');
});

test('결과 화면: 결정론적 fallback 은 사유를 구분해 밝히고 AI 평가라고 하지 않는다', async () => {
  const page = await resultPage({
    source: 'fallback',
    meta: { contractVersion: 'weekly.v1', engine: 'deterministic', llmUsed: false, llmStatus: 'timeout' },
  });
  const html = page.html();
  assert.match(html, /llm-note fallback/);
  assert.match(html, /제한 시간 안에 답하지 않아/);
  assert.doesNotMatch(html, CLAIMS_AI);
  assert.ok(page.$$('.plan-card .reason .hint').every(el => el.textContent === '규칙 사유'));
  assert.match(page.text(), /출처 서버 규칙 계산/);
});

test('결과 화면: metadata 없는 옛 응답은 예전 문구 그대로 나온다', async () => {
  const page = await resultPage({ source: 'fallback', meta: { contractVersion: 'weekly.v1' } });
  assert.match(page.html(), GENERIC_FALLBACK);
  assert.doesNotMatch(page.html(), /제한 시간|설정되어 있지 않아|처리 시간이 부족/);
});

test('저장물: 생성 방식 표시를 저장하고, 다시 열어도 같은 말을 한다', async () => {
  const page = await resultPage({ source: 'llm', meta: { contractVersion: 'weekly.v1', ...SUCCESS_META } });
  page.click('#save');
  page.click('#mok');
  await flush();

  const saved = page.stored('schedules')[0];
  assert.equal(saved.source, 'llm');
  assert.deepEqual(saved.llm, {
    engine: 'hybrid', used: true, provider: 'nosana', model: 'qwen2.5-7b-instruct',
    latencyMs: 1840, status: 'success', claimed: true, verified: true,
  });

  const reopened = createPage();
  reopened.seed('profile', F.profileFixture());
  reopened.seed('schedules', [saved]);
  reopened.location._hash = '#/schedule/' + saved.id;
  reopened.boot();
  assert.match(reopened.html(), CLAIMS_AI);
  assert.equal(reopened.calls.length, 0, '저장물을 여는 데 네트워크가 필요하면 안 된다');
});

test('저장물: llm 필드가 없는 옛 저장물은 AI 평가라고 말하지 않는다', () => {
  const page = createPage();
  page.seed('profile', F.profileFixture());
  page.seed('schedules', [{
    id: 's_old', schemaVersion: 2, title: '옛 저장물', createdAt: '2026-09-01T00:00:00+09:00',
    planId: 'p1', planType: 'balanced', planLabel: '밸런스안',
    profile: F.profileFixture(), source: 'fallback',
    metrics: F.responseFixture().plans[0].metrics, warnings: [], jobs: [F.jobA()],
  }]);
  page.location._hash = '#/schedule/s_old';
  page.boot();
  assert.match(page.html(), GENERIC_FALLBACK);
  assert.doesNotMatch(page.html(), CLAIMS_AI);
});
