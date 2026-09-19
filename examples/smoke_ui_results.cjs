#!/usr/bin/env node
/* =====================================================================
 * examples/smoke_ui_results.cjs — 살아 있는 API → 화면 충실도 프로브
 *
 * 실행 (옵트인. 단위 테스트는 이 파일을 건드리지 않는다):
 *     python web_demo.py           # 다른 창에서 먼저 띄운다
 *     node examples/smoke_ui_results.cjs
 *
 * 무엇을 증명하는가
 * ─────────────────
 * 프로덕션 스크립트(js/data.js · engine.js · api-client.js · app.js ·
 * live-demo.js)를 그대로 node:vm 에 올리고, 실제 사용자 조작 경로(#go /
 * .plan-card / [data-pin] / [data-exclude] / #regen 핸들러)로 돌린 뒤,
 * 화면에 그려진 값이 **그 순간 서버가 보낸 원본 JSON** 과 같은지 본다.
 * 응답은 http://127.0.0.1:5191 의 실제 API 에서 온다 — 주 검증 경로에서는
 * 응답을 지어내지 않는다.
 *
 * 무엇을 증명하지 못하는가
 * ─────────────────────
 * 이것은 **DOM/핸들러 통합 증거이지 브라우저 스크린샷 증거가 아니다.**
 * 실제 브라우저의 CSS 레이아웃·페인팅·클릭 히트테스트는 여기서 보지 않는다.
 * 보는 것은 "앱이 만든 DOM 문자열과 나간 요청 본문" 이다.
 *
 * 로컬 목(mock) 승리 경로 차단 — 두 방향에서 잠근다
 * ─────────────────────────────────────────────
 *   (A) 라이브 구간: vm 안에서 JOBS 를 비우고 generatePlans /
 *       filterCandidates / comboMetrics 를 던지는 함수로 바꾼 뒤 화면을 돌린다.
 *       그래도 결과가 그려지면, 그 값은 로컬 데이터에서 나올 수 없다.
 *   (B) 오프라인 회귀 구간: JOBS 를 **원형 그대로 둔 채** 네트워크만 끊는다.
 *       그래도 화면이 비어 있으면, 실패 시 로컬 공고로 몰래 대체하지 않는다.
 *
 * 예산: POST /api/recommendations 최대 4회(+ /healthz 1회), 전체 45초.
 * 산출물: .runtime/ui-proof/<stamp>/ (gitignore 대상) 에만 쓴다.
 * ===================================================================*/
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createPage } = require('../tests/ui_flow/support/page.js');
const F = require('../tests/ui_flow/support/fixtures.js');

const REPO = path.resolve(__dirname, '..');
const API_BASE = (process.env.UI_PROOF_API || 'http://127.0.0.1:5191').replace(/\/+$/, '');
const MAX_POSTS = 6;                 // 계약상 상한. 이 스크립트는 4회를 쓴다.
const TOTAL_BUDGET_MS = 45000;
const PER_CALL_MS = 20000;
const DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

const t0 = Date.now();
const elapsed = () => Date.now() - t0;
const leftMs = () => TOTAL_BUDGET_MS - elapsed();

/* ---------------------------------------------------------------------
 * 검증 집계 — 어긋나면 출력만 하고 넘어가는 일이 없도록, 실패는 종료 코드 1.
 * -------------------------------------------------------------------*/
const checks = [];
function check(section, name, fn) {
  try {
    fn();
    checks.push({ section, name, ok: true });
  } catch (e) {
    checks.push({ section, name, ok: false, message: e.message });
    console.log(`   ✗ [${section}] ${name}\n     ${String(e.message).split('\n')[0]}`);
  }
}
const failures = () => checks.filter(c => !c.ok);

/* ---------------------------------------------------------------------
 * 서버 계약이 정한 표기. app.js 의 포매터를 가져다 쓰지 않고 따로 적는다 —
 * 같은 함수를 양쪽에서 쓰면 "서버 값을 그렸다"가 아니라 "같은 함수를 썼다"만
 * 증명된다.
 * -------------------------------------------------------------------*/
const won = v => Math.round(v).toLocaleString('ko-KR') + '원';
const pct = v => Math.round(v * 100) + '%';
const hrs = v => v.toFixed(1) + 'h';
const dur = m => (m >= 60 ? `${Math.floor(m / 60)}시간${m % 60 ? ' ' + (m % 60) + '분' : ''}` : `${m}분`);
const toMin = t => { const [h, m] = String(t).split(':').map(Number); return h * 60 + m; };
/* dom.js 는 엔티티를 되돌리지 않는다. 서버 문자열은 앱이 이스케이프한 모양으로 비교한다. */
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ---------------------------------------------------------------------
 * 실제 HTTP 다리. support/page.js 의 fetch 대역은 상대경로만 허용하므로
 * 앱은 여전히 '/api/recommendations' 로만 나가고, 그 호출을 여기서 받아
 * 진짜 서버로 넘긴다. 앱이 만든 본문 바이트를 그대로 보낸다.
 * -------------------------------------------------------------------*/
const api = [];                      // { n, request, status, raw, ms }
function attachLiveApi(page) {
  page.onFetch(async call => {
    if (call.url !== '/api/recommendations') throw new Error('probe: 예상치 못한 경로 → ' + call.url);
    if (api.length >= MAX_POSTS) throw new Error(`probe: API 호출 예산(${MAX_POSTS}회) 초과`);
    if (leftMs() <= 0) throw new Error('probe: 45초 예산 초과');
    const body = call.init && typeof call.init.body === 'string' ? call.init.body : JSON.stringify(call.body);
    const started = Date.now();
    const res = await fetch(API_BASE + call.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body,
      signal: AbortSignal.timeout(Math.max(1000, Math.min(PER_CALL_MS, leftMs()))),
    });
    const text = await res.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch { parsed = null; }
    api.push({ n: api.length + 1, request: call.body, status: res.status, raw: parsed, ms: Date.now() - started });
    return {
      ok: res.ok,
      status: res.status,
      headers: { get: k => res.headers.get(k) },
      json: async () => JSON.parse(text),
      text: async () => text,
    };
  });
}

/* ---------------------------------------------------------------------
 * 화면 띄우기 — (A) 로컬 목 경로를 막은 페이지
 * -------------------------------------------------------------------*/
const POISON = [
  'JOBS.length = 0',
  'generatePlans = function () { throw new Error("LOCAL_MOCK_PATH: generatePlans"); }',
  'filterCandidates = function () { throw new Error("LOCAL_MOCK_PATH: filterCandidates"); }',
  'comboMetrics = function () { throw new Error("LOCAL_MOCK_PATH: comboMetrics"); }',
];

function bootPage({ poison }) {
  const page = createPage();
  page.seed('profile', F.profileFixture());
  if (poison) for (const stmt of POISON) page.evalIn(stmt);
  page.boot();
  return page;
}

/** 손대지 않은 페이지에서 읽어 둔 로컬 목 공고 이름 — 유출 검사의 기준이 된다. */
function localJobCompanies() {
  const pristine = createPage();
  const names = JSON.parse(pristine.evalIn('JSON.stringify(JOBS.map(j => j.company))'));
  return [...new Set(names.filter(Boolean))];
}

/* ---------------------------------------------------------------------
 * 조작 헬퍼 — 전부 실제 핸들러를 통한다
 * -------------------------------------------------------------------*/
const currentSearch = page => page.stored('lastSearch') || { count: 2, categories: [], priority: 'wage' };

function setSearch(page, want) {
  for (const v of [...new Set([...currentSearch(page).categories, ...want.categories])]) {
    if (want.categories.includes(v) === currentSearch(page).categories.includes(v)) continue;
    const chip = page.$$('#cats .chip').find(c => c.dataset.v === v);
    if (!chip) throw new Error('카테고리 칩을 찾지 못했다: ' + v);
    page.clickEl(chip);
  }
  if (currentSearch(page).count !== want.count) {
    page.clickEl(page.$$('#count button').find(b => Number(b.dataset.v) === want.count));
  }
  if (currentSearch(page).priority !== want.priority) {
    page.clickEl(page.$$('#prio button').find(b => b.dataset.v === want.priority));
  }
  const now = currentSearch(page);
  assert.equal(now.count, want.count);
  assert.equal(now.priority, want.priority);
  assert.deepEqual([...now.categories].sort(), [...want.categories].sort());
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(label, pred) {
  while (leftMs() > 0) {
    if (pred()) return;
    await sleep(25);
  }
  throw new Error('probe: 45초 예산 안에 끝나지 않았다 → ' + label);
}
/** 요청 하나가 끝나고 결과/에러 화면이 그려질 때까지 기다린다. */
const settled = page => async n => waitFor(`API #${n} 렌더`, () =>
  api.length >= n && (page.$$('.plan-card').length > 0 || page.$('.api-error')));

/* ---------------------------------------------------------------------
 * 원본 JSON ↔ 그려진 DOM 대조
 * -------------------------------------------------------------------*/
function metricCell(page, label) {
  const cell = page.$$('.metrics-row .metric').find(m => {
    const k = m.querySelector('.k');
    return k && k.textContent.startsWith(label);
  });
  if (!cell) throw new Error('지표 칸을 찾지 못했다: ' + label);
  return cell.querySelector('.v').textContent;
}

/** 3안 카드 — 개수·라벨·서버 지표·구성 공고가 원본과 같은가. */
function verifyCards(page, raw) {
  const cards = page.$$('.plan-card');
  assert.equal(cards.length, raw.plans.length, '카드 수가 응답의 plans 수와 다르다');
  const head = page.$('.page-head').textContent;
  if (typeof raw.candidateCount === 'number') {
    assert.ok(head.includes('후보 ' + raw.candidateCount.toLocaleString('ko-KR') + '건'),
      `후보 수 ${raw.candidateCount} 가 머리말에 없다: ${head}`);
  }
  raw.plans.forEach((p, i) => {
    const txt = cards[i].textContent;
    assert.ok(txt.includes(esc(p.label)), `[${i}] 라벨 "${p.label}" 이 카드에 없다`);
    const m = p.metrics || {};
    if (m.monthlyIncome != null) assert.ok(txt.includes(won(m.monthlyIncome)), `[${i}] 월수입 ${won(m.monthlyIncome)} 이 카드에 없다`);
    if (m.targetAchievementRate != null) assert.ok(txt.includes(pct(m.targetAchievementRate)), `[${i}] 달성률 ${pct(m.targetAchievementRate)} 이 카드에 없다`);
    if (m.weeklyWorkHours != null) assert.ok(txt.includes(hrs(m.weeklyWorkHours)), `[${i}] 주 근무 ${hrs(m.weeklyWorkHours)} 이 카드에 없다`);
    if (m.weeklyTravelMinutes != null) assert.ok(txt.includes(dur(Math.round(m.weeklyTravelMinutes))), `[${i}] 주 이동이 카드에 없다`);
    for (const j of p.jobs) assert.ok(txt.includes(esc(j.company || j.title || j.jobId)), `[${i}] 공고 "${j.company}" 가 카드에 없다`);
  });
  const note = page.text();
  assert.ok(note.includes(raw.requestId), 'requestId 가 화면에 없다: ' + raw.requestId);
  if (raw.generatedAt) assert.ok(note.includes(raw.generatedAt), 'generatedAt 이 화면에 없다');
}

/** 선택된 안의 지표 줄 — 5칸 전부 서버 값 그대로인가. */
function verifyMetrics(page, raw, idx) {
  const m = raw.plans[idx].metrics || {};
  const want = [
    ['예상 월 수입', m.monthlyIncome == null ? '—' : won(m.monthlyIncome)],
    ['목표 달성률', m.targetAchievementRate == null ? '—' : pct(m.targetAchievementRate)],
    ['주 총 근무시간', m.weeklyWorkHours == null ? '—' : hrs(m.weeklyWorkHours)],
    ['주 총 이동시간', m.weeklyTravelMinutes == null ? '—' : dur(Math.round(m.weeklyTravelMinutes))],
    ['실질 시급', m.effectiveHourlyWage == null ? '—' : won(m.effectiveHourlyWage)],
  ];
  for (const [k, v] of want) assert.equal(metricCell(page, k), v, `지표 "${k}" 가 서버 값과 다르다`);
  return want.length;
}

/** 시간표 — 서버 assignedShifts 의 칸과 이동시간만 그려졌는가. */
function verifyTimetable(page, raw, idx) {
  const cols = page.$$('.tt-col');
  assert.equal(cols.length, 7, '요일 열이 7개가 아니다');
  const text = cols.map(c => c.textContent);
  let cells = 0;
  let travelBlocks = 0;
  for (const job of raw.plans[idx].jobs) {
    for (const s of job.assignedShifts) {
      const col = text[DAYS.indexOf(s.day)];
      assert.ok(col.includes(esc(job.company)), `${s.day} 열에 "${job.company}" 가 없다`);
      assert.ok(col.includes(`${s.start}–${s.end}`), `${s.day} 열에 ${s.start}–${s.end} 가 없다`);
      cells++;
      const tv = s.travel;
      const leg = tv && (typeof tv.legMinutes === 'number'
        ? tv.legMinutes
        : (typeof tv.transitMinutes === 'number' || typeof tv.walkMinutes === 'number'
          ? (tv.transitMinutes || 0) + (tv.walkMinutes || 0) : null));
      if (leg && tv.departAt && toMin(s.start) - toMin(tv.departAt) >= 10) {
        assert.ok(col.includes(`이동 ${leg}분`), `${s.day} 열에 서버 이동시간 ${leg}분 블록이 없다`);
        travelBlocks++;
      }
    }
  }
  // 서버가 travel 을 준 칸에 브라우저 추정(별표)이 섞이면 안 된다
  const everyShiftHasTravel = raw.plans[idx].jobs.every(j => j.assignedShifts.every(s => s.travel));
  if (everyShiftHasTravel) {
    assert.ok(!page.html().includes('브라우저 추정 이동시간'), '서버 이동시간이 있는데 브라우저 추정 표기가 붙었다');
  }
  // 서버가 배정하지 않은 요일에 근무 블록을 만들어 내지 않는다
  const assigned = new Set(raw.plans[idx].jobs.flatMap(j => j.assignedShifts.map(s => s.day)));
  const fixedDays = new Set(Object.entries(F.profileFixture().schedule).filter(([, v]) => v.has).map(([d]) => d));
  for (const d of DAYS) {
    if (assigned.has(d) || fixedDays.has(d)) continue;
    assert.equal(text[DAYS.indexOf(d)].trim(), '', `${d} 은 배정이 없는데 블록이 그려졌다`);
  }
  return { cells, travelBlocks };
}

/** 상세 카드 — jobId · 제목 · 시급 · 배정 시간이 원본 그대로인가. */
function verifyDetail(page, raw, idx) {
  const jobs = raw.plans[idx].jobs;
  assert.deepEqual(page.$$('[data-pin]').map(b => b.dataset.pin), jobs.map(j => j.jobId),
    '상세 카드의 jobId 순서가 응답과 다르다');
  const txt = page.$$('.job-item').map(el => el.textContent);
  jobs.forEach((j, i) => {
    assert.ok(txt[i].includes(esc(j.title)), `[${j.jobId}] 제목이 상세에 없다`);
    assert.ok(txt[i].includes(esc(j.company)), `[${j.jobId}] 업체명이 상세에 없다`);
    if (j.hourlyWage != null) assert.ok(txt[i].includes(won(j.hourlyWage)), `[${j.jobId}] 시급이 상세에 없다`);
    if (typeof j.weeklyHours === 'number') assert.ok(txt[i].includes(hrs(j.weeklyHours)), `[${j.jobId}] 주 근무시간이 상세에 없다`);
    if (typeof j.weeklyPay === 'number') assert.ok(txt[i].includes(won(j.weeklyPay)), `[${j.jobId}] 주급이 상세에 없다`);
    for (const s of j.assignedShifts) {
      assert.ok(txt[i].includes(`${s.start} ~ ${s.end}`), `[${j.jobId}] 배정 ${s.day} ${s.start}~${s.end} 가 상세에 없다`);
    }
  });
  const slots = page.$$('.card .slot-list .slot-pill');
  if (Array.isArray(raw.availableSlots) && raw.availableSlots.length) {
    assert.ok(slots.length >= raw.availableSlots.length, '서버가 준 빈 시간 수보다 적게 그려졌다');
  }
  return jobs.length;
}

/** 어댑터가 세션에 남긴 정규화 결과 ↔ 원본 JSON. 깎이거나 바뀐 값이 있으면 실패. */
function verifyAdapterSnapshot(page, raw) {
  const got = page.session().response;
  assert.equal(got.requestId, raw.requestId);
  assert.equal(got.generatedAt, raw.generatedAt);
  assert.equal(got.source, raw.source);
  assert.equal(got.candidateCount, raw.candidateCount);
  assert.equal(got.droppedPlans, 0, '표시하지 못하고 버린 안이 있다');
  assert.equal(got.plans.length, raw.plans.length);
  raw.plans.forEach((p, i) => {
    const g = got.plans[i];
    assert.deepEqual(
      { id: g.id, hash: g.hash, type: g.type, label: g.label, reason: g.reason },
      { id: p.id, hash: p.hash, type: p.type, label: p.label, reason: p.reason },
      `plans[${i}] 식별자/문구가 원본과 다르다`);
    assert.deepEqual(g.metrics, {
      monthlyIncome: p.metrics.monthlyIncome ?? null,
      targetAchievementRate: p.metrics.targetAchievementRate ?? null,
      weeklyWorkHours: p.metrics.weeklyWorkHours ?? null,
      weeklyTravelMinutes: p.metrics.weeklyTravelMinutes ?? null,
      effectiveHourlyWage: p.metrics.effectiveHourlyWage ?? null,
      weeklyHolidayPayIncluded: p.metrics.weeklyHolidayPayIncluded ?? null,
    }, `plans[${i}].metrics 가 원본과 다르다`);
    p.jobs.forEach((j, k) => {
      const gj = g.jobs[k];
      assert.deepEqual(
        { id: gj.jobId, t: gj.title, c: gj.company, w: gj.hourlyWage, h: gj.weeklyHours, p: gj.weeklyPay },
        { id: j.jobId, t: j.title, c: j.company, w: j.hourlyWage, h: j.weeklyHours ?? null, p: j.weeklyPay ?? null },
        `jobs[${k}] 기본 필드가 원본과 다르다`);
      assert.deepEqual(
        gj.assignedShifts.map(s => ({ day: s.day, start: s.start, end: s.end, travel: s.travel })),
        j.assignedShifts.map(s => ({
          day: s.day,
          start: s.start,
          end: s.end,
          travel: s.travel ? {
            fromLocation: s.travel.fromLocation ?? null,
            transitMinutes: s.travel.transitMinutes ?? null,
            walkMinutes: s.travel.walkMinutes ?? null,
            originWalkMinutes: s.travel.originWalkMinutes ?? null,
            legMinutes: s.travel.legMinutes ?? null,
            slackMinutes: s.travel.slackMinutes ?? null,
            bufferMinutes: s.travel.bufferMinutes ?? null,
            departAt: s.travel.departAt ?? null,
          } : null,
        })),
        `jobs[${k}].assignedShifts 가 원본과 다르다`);
    });
  });
}

/** 공고 출처 고지 — 서버 meta 가 밝힌 것만 말하는가. */
function verifySourceNotice(page, raw) {
  const meta = raw.meta || {};
  const html = page.html();
  const txt = page.text();
  const mode = meta.job_source;
  const dataMode = meta.data_mode;
  if (mode === 'demo_json' && (dataMode === 'demo' || dataMode == null)) {
    assert.ok(html.includes('synth-note'), '서버가 demo_json 이라 밝혔는데 합성 데이터 고지가 없다');
    assert.ok(!txt.includes('실제 공개 공고'), '데모 데이터를 실제 공고라고 말했다');
  } else if (mode === 'public_web' && (dataMode === 'live' || dataMode === 'authorized_import')) {
    assert.ok(html.includes('source-note live'), '서버가 실제 공고라 밝혔는데 그 고지가 없다');
    assert.ok(!txt.includes('합성 데모 데이터'), '실제 공고를 합성 데모라고 불렀다');
  } else {
    assert.ok(html.includes('source-note unknown'), '출처가 없는데 단정하는 고지가 붙었다');
  }
  const sourceLabel = { fallback: '서버 규칙 계산', llm: 'LLM' }[raw.source] || '미상';
  assert.ok(txt.includes('출처 ' + sourceLabel), `생성 방식 표기가 응답(source=${raw.source})과 다르다`);
  if (meta.contractVersion) assert.ok(txt.includes(meta.contractVersion), '계약 버전이 화면에 없다');
  const disclosures = Array.isArray(meta.disclosures) ? meta.disclosures : [];
  if (disclosures.length) {
    assert.ok(txt.includes(`서버가 밝힌 계산 전제 ${disclosures.length}건`), '서버 고지 건수가 화면과 다르다');
    for (const d of disclosures) assert.ok(txt.includes(esc(d)), '서버 고지 문구가 화면에 없다: ' + d);
  }
}

/** 경고 — 서버가 준 만큼만, 문구 그대로. */
function verifyWarnings(page, raw, idx) {
  const want = raw.plans[idx].warnings || [];
  const shown = page.$$('.warn-list .alert');
  assert.equal(shown.length, want.length, '경고 개수가 응답과 다르다');
  want.forEach((w, i) => {
    assert.ok(shown[i].textContent.includes(esc(w.message)), `경고 문구가 응답과 다르다: ${w.code}`);
  });
  return want.length;
}

/** 라이브 응답 하나를 화면과 통째로 대조한다. */
function verifyRenderedAgainstRaw(page, raw, idx, section, tag) {
  let metrics = 0, tt = { cells: 0, travelBlocks: 0 }, jobs = 0, warns = 0;
  check(section, `${tag}: 3안 카드가 응답 plans 와 같다`, () => verifyCards(page, raw));
  check(section, `${tag}: 지표 5칸이 서버 metrics 그대로다`, () => { metrics = verifyMetrics(page, raw, idx); });
  check(section, `${tag}: 시간표가 서버 assignedShifts 그대로다`, () => { tt = verifyTimetable(page, raw, idx); });
  check(section, `${tag}: 상세 카드가 서버 공고 그대로다`, () => { jobs = verifyDetail(page, raw, idx); });
  check(section, `${tag}: 경고가 서버가 준 만큼 그대로 뜬다`, () => { warns = verifyWarnings(page, raw, idx); });
  check(section, `${tag}: 공고 출처 고지가 서버 meta 와 일치한다`, () => verifySourceNotice(page, raw));
  check(section, `${tag}: 어댑터 스냅샷이 원본 JSON 과 일치한다`, () => verifyAdapterSnapshot(page, raw));
  return { metrics, ...tt, jobs, warns };
}

function summarize(raw, idx, r) {
  const ids = raw.plans[idx].jobs.map(j => j.jobId).join(', ');
  return `      candidateCount=${raw.candidateCount} · plans=${raw.plans.length} · 선택안 jobIds=[${ids}]\n`
    + `      지표 ${r.metrics}/5 일치 · 시간표 칸 ${r.cells}개 + 이동블록 ${r.travelBlocks}개 일치 · 상세 ${r.jobs}건 · 경고 ${r.warns}건 일치`;
}

/* 응답 자체를 쓸 수 없을 때(비-200 · plans 없음) 라이브 구간만 중단하는 신호. */
class ProbeAbort extends Error {}
function ensureUsable(call, tag) {
  const ok = call.status === 200 && call.raw && Array.isArray(call.raw.plans) && call.raw.plans.length > 0;
  check('A', `${tag}: 서버가 쓸 수 있는 200 응답을 돌려줬다`, () => {
    assert.ok(ok, `status=${call.status} body=${JSON.stringify(call.raw).slice(0, 300)}`);
  });
  if (!ok) throw new ProbeAbort(`${tag} 응답을 쓸 수 없어 라이브 구간을 여기서 접습니다 (FAIL)`);
}

/* ---------------------------------------------------------------------
 * A 구간 본체. 응답을 쓸 수 없으면 ProbeAbort 로 라이브 구간만 접고,
 * 지금까지의 검사 결과는 그대로 보고한다.
 * -------------------------------------------------------------------*/
async function runLive(page, mockCompanies) {
  /* ===================================================================
   * A. 라이브 — 로컬 목 경로를 막아 둔 채 실제 응답을 그린다
   * =================================================================*/
  const ready = settled(page);

  check('A', '가드: 로컬 목 경로가 실제로 죽어 있다', () => {
    assert.equal(page.evalIn('JOBS.length'), 0, 'JOBS 를 비우지 못했다');
    for (const fn of ['generatePlans', 'filterCandidates', 'comboMetrics']) {
      assert.throws(() => page.evalIn(`${fn}()`), /LOCAL_MOCK_PATH/, `${fn} 이 여전히 살아 있다`);
    }
  });

  /* --- A1. 첫 탐색 --------------------------------------------------*/
  const searchA = { count: 2, categories: ['카페·음식점'], priority: 'wage' };
  setSearch(page, searchA);
  page.click('#go');
  await ready(1);

  const callA = api[0];
  const rawA = callA.raw;
  console.log(`  [A1] POST /api/recommendations → ${callA.status} (${callA.ms}ms)`);
  ensureUsable(callA, 'A1');

  check('A', 'A1: 고른 조건이 그대로 계약 Request 로 나갔다', () => {
    assert.equal(callA.status, 200, `서버가 ${callA.status} 를 돌려줬다: ${JSON.stringify(callA.raw).slice(0, 300)}`);
    assert.deepEqual(callA.request.search, { jobCount: 2, categories: ['카페·음식점'], priority: 'wage' });
    assert.equal(callA.request.profile.home, '사당');
    assert.equal(callA.request.profile.targetAmount, 600000);
    assert.deepEqual(callA.request.profile.fixedSchedules.map(f => f.day), ['MON', 'TUE', 'THU']);
    assert.deepEqual(callA.request.profile.constraints, { minBlockHours: 3, allowNight: true, wantWeeklyHolidayPay: true, age: 26 });
    assert.equal(callA.request.regenerate, undefined, '첫 요청에 regenerate 가 붙었다');
    assert.equal(page.calls[0].url, '/api/recommendations', '앱이 상대경로 외의 주소로 나갔다');
  });
  check('A', 'A1: 서버가 실제 조합을 돌려줬다', () => {
    assert.ok(Array.isArray(rawA.plans) && rawA.plans.length > 0, 'plans 가 비어 있다');
    assert.ok(rawA.plans.every(p => p.jobs.length > 0), '공고 없는 안이 있다');
  });
  const rA = verifyRenderedAgainstRaw(page, rawA, 0, 'A', 'A1');
  console.log(summarize(rawA, 0, rA));

  check('A', 'A1: 죽은 로컬 목 경로를 거치지 않고 그려졌다', () => {
    assert.equal(page.evalIn('JOBS.length'), 0);
    const html = page.html();
    const leaked = mockCompanies.filter(c => html.includes(c) && !JSON.stringify(rawA).includes(c));
    assert.deepEqual(leaked, [], '로컬 목 공고 이름이 화면에 새어 나왔다');
  });

  /* --- A2. 다른 안 선택 (네트워크 없이 응답 안에서만 움직인다) -------*/
  if (rawA.plans.length > 1) {
    page.clickEl(page.$$('.plan-card')[1]);
    const r2 = verifyRenderedAgainstRaw(page, rawA, 1, 'A', 'A2(카드 전환)');
    check('A', 'A2: 카드 전환에 추가 네트워크가 필요 없다', () => assert.equal(api.length, 1));
    console.log(`  [A2] 2번 카드 선택 — 같은 응답 안에서 전환`);
    console.log(summarize(rawA, 1, r2));
    page.clickEl(page.$$('.plan-card')[0]);
  }

  /* --- A3. 조건을 바꾸면 요청도 화면도 바뀐다 -----------------------*/
  page.go('#/');
  const searchB = { count: 3, categories: ['편의점'], priority: 'distance' };
  setSearch(page, searchB);
  page.click('#go');
  await ready(2);

  const callB = api[1];
  const rawB = callB.raw;
  console.log(`  [A3] 조건 변경 후 POST → ${callB.status} (${callB.ms}ms)`);
  ensureUsable(callB, 'A3');

  check('A', 'A3: 바뀐 조건이 그대로 요청에 실렸다', () => {
    assert.equal(callB.status, 200, `서버가 ${callB.status} 를 돌려줬다`);
    assert.deepEqual(callB.request.search, { jobCount: 3, categories: ['편의점'], priority: 'distance' });
    assert.notDeepEqual(callB.request.search, callA.request.search);
    assert.deepEqual(callB.request.profile, callA.request.profile, '조건만 바꿨는데 프로필이 달라졌다');
  });
  check('A', 'A3: 서버가 실제로 다른 결과를 돌려줬다', () => {
    assert.notEqual(rawB.requestId, rawA.requestId);
    const idsA = new Set(rawA.plans.flatMap(p => p.jobs.map(j => j.jobId)));
    const idsB = new Set(rawB.plans.flatMap(p => p.jobs.map(j => j.jobId)));
    assert.notDeepEqual([...idsB].sort(), [...idsA].sort(), '조건을 바꿨는데 같은 공고가 돌아왔다');
    const cats = new Set(rawB.plans.flatMap(p => p.jobs.map(j => j.category)));
    assert.deepEqual([...cats], ['편의점'], '요청한 카테고리 밖의 공고가 섞였다: ' + [...cats].join(', '));
  });
  const rB = verifyRenderedAgainstRaw(page, rawB, 0, 'A', 'A3');
  console.log(summarize(rawB, 0, rB));

  check('A', 'A3: 화면이 새 응답을 그리고 옛 응답을 남기지 않았다', () => {
    const shown = new Set(page.$$('[data-pin]').map(b => b.dataset.pin));
    const idsB = new Set(rawB.plans.flatMap(p => p.jobs.map(j => j.jobId)));
    const staleA = [...new Set(rawA.plans.flatMap(p => p.jobs.map(j => j.jobId)))].filter(id => !idsB.has(id));
    assert.deepEqual(staleA.filter(id => shown.has(id)), [], '이전 응답의 공고가 화면에 남아 있다');
    const txt = page.text();
    assert.ok(txt.includes(rawB.requestId), '새 requestId 가 화면에 없다');
    assert.ok(!txt.includes(rawA.requestId), '옛 requestId 가 화면에 남아 있다');
  });

  /* --- A4. 고정(📌) 후 재생성 ---------------------------------------*/
  const pinId = page.$$('[data-pin]')[0].dataset.pin;
  page.clickEl(page.$$('[data-pin]')[0]);
  check('A', 'A4: 고정 상태가 세션과 화면에 반영된다', () => {
    assert.deepEqual(page.session().pinned, [pinId]);
    assert.ok(page.text().includes('고정 1 · 제외 0'), '고정 배지가 없다');
  });
  page.click('#regen');
  await ready(3);

  const callC = api[2];
  const rawC = callC.raw;
  console.log(`  [A4] 📌 ${pinId} 고정 후 재생성 → ${callC.status} (${callC.ms}ms)`);
  ensureUsable(callC, 'A4');

  check('A', 'A4: 고정 id 와 이미 본 조합 해시가 정확히 실렸다', () => {
    assert.equal(callC.status, 200, `서버가 ${callC.status} 를 돌려줬다`);
    assert.deepEqual(callC.request.regenerate.pinnedJobIds, [pinId]);
    assert.deepEqual(callC.request.regenerate.excludedJobIds, []);
    assert.deepEqual([...callC.request.regenerate.previousPlanHashes].sort(),
      rawB.plans.map(p => p.hash).sort(), '직전 응답의 plan hash 가 그대로 실려야 한다');
    assert.deepEqual(callC.request.search, callB.request.search, '재생성이 탐색 조건을 바꿨다');
  });
  check('A', 'A4: 서버가 고정을 지켰고 화면이 그 사실을 표시한다', () => {
    assert.ok(rawC.plans[0].jobs.some(j => j.jobId === pinId), `서버 응답의 1안에 고정 공고(${pinId})가 없다`);
    const item = page.$$('.job-item').find(el => el.textContent.includes('고정됨'));
    assert.ok(item, '📌 고정됨 배지가 화면에 없다');
  });
  const rC = verifyRenderedAgainstRaw(page, rawC, 0, 'A', 'A4');
  console.log(summarize(rawC, 0, rC));

  /* --- A5. 제외(✕) 후 재생성 ----------------------------------------*/
  const excludeIdx = page.$$('[data-exclude]').length > 1 ? 1 : 0;
  const exId = page.$$('[data-exclude]')[excludeIdx].dataset.exclude;
  page.clickEl(page.$$('[data-exclude]')[excludeIdx]);
  await ready(4);

  const callD = api[3];
  const rawD = callD.raw;
  console.log(`  [A5] ✕ ${exId} 제외 후 재생성 → ${callD.status} (${callD.ms}ms)`);
  ensureUsable(callD, 'A5');

  check('A', 'A5: 제외 id 가 실리고 고정은 규칙대로 정리됐다', () => {
    assert.equal(callD.status, 200, `서버가 ${callD.status} 를 돌려줬다`);
    assert.deepEqual(callD.request.regenerate.excludedJobIds, [exId]);
    assert.deepEqual(callD.request.regenerate.pinnedJobIds, exId === pinId ? [] : [pinId],
      '제외한 공고는 고정 목록에서 빠지고, 다른 공고의 고정은 유지돼야 한다');
  });
  check('A', 'A5: 서버가 제외를 지켰고 화면에도 그 공고가 없다', () => {
    const idsD = rawD.plans.flatMap(p => p.jobs.map(j => j.jobId));
    assert.ok(!idsD.includes(exId), `제외한 ${exId} 가 응답에 다시 들어왔다`);
    assert.ok(!page.$$('[data-pin]').map(b => b.dataset.pin).includes(exId), `제외한 ${exId} 가 화면에 남아 있다`);
  });
  const rD = verifyRenderedAgainstRaw(page, rawD, 0, 'A', 'A5');
  console.log(summarize(rawD, 0, rD));

  check('A', '라이브 구간 내내 로컬 목 경로는 죽어 있었다', () => {
    assert.equal(page.evalIn('JOBS.length'), 0);
    assert.throws(() => page.evalIn('generatePlans()'), /LOCAL_MOCK_PATH/);
  });
  check('A', `라이브 API 호출이 예산(${MAX_POSTS}회) 안이다`, () => {
    assert.ok(api.length <= MAX_POSTS, `${api.length}회 호출됨`);
    assert.equal(page.calls.length, api.length, '앱이 보낸 호출 수와 서버에 간 호출 수가 다르다');
    assert.deepEqual([...new Set(page.calls.map(c => c.url))], ['/api/recommendations']);
  });

  return rawD;
}

/* =====================================================================
 * 본체
 * ===================================================================*/
async function main() {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const artDir = path.join(REPO, '.runtime', 'ui-proof', stamp);

  console.log('live API → UI 충실도 프로브');
  console.log(`  API   ${API_BASE}   예산 POST ${MAX_POSTS}회 / ${TOTAL_BUDGET_MS / 1000}초`);
  console.log('  형태  DOM/핸들러 통합 증거 (브라우저 스크린샷 증거 아님)\n');

  /* 0. 서버 확인 (/healthz GET 1회) */
  let health;
  try {
    const res = await fetch(API_BASE + '/healthz', { signal: AbortSignal.timeout(3000) });
    health = await res.json();
    assert.equal(health.status, 'ok');
  } catch (e) {
    console.error(`서버에 닿지 못했습니다 (${API_BASE}/healthz): ${e.message}`);
    console.error('먼저 다른 창에서 `python web_demo.py` 로 띄운 뒤 다시 실행해 주세요.');
    process.exitCode = 2;
    return;
  }
  console.log(`  GET /healthz → ${JSON.stringify(health)}\n`);
  fs.mkdirSync(artDir, { recursive: true });   // 서버가 살아 있을 때만 산출물 폴더를 만든다

  const mockCompanies = localJobCompanies();

  /* ===================================================================
   * A. 라이브 — 로컬 목 경로를 막아 둔 채 실제 응답을 그린다
   * =================================================================*/
  const page = bootPage({ poison: true });
  attachLiveApi(page);
  let rawLast = null;
  try {
    rawLast = await runLive(page, mockCompanies);
  } catch (e) {
    if (!(e instanceof ProbeAbort)) throw e;
    console.log(`
  ⚠ ${e.message}`);
  }

  /* ===================================================================
   * B. 오프라인 회귀 — 여기서만 응답을 지어낸다(또는 끊는다).
   *    주 증명(A)과 성격이 다르므로 구간을 나눠 표시한다.
   * =================================================================*/
  console.log('\n  [B] 오프라인 회귀 (API 호출 0회 · 여기서만 응답을 지어낸다)');

  // B1: JOBS 를 원형 그대로 둔 채 네트워크만 끊는다 → 로컬 공고로 대체하지 않는가
  const offline = bootPage({ poison: false });
  offline.queueFailure(new TypeError('fetch failed'));
  offline.click('#go');
  await waitFor('B1 에러 렌더', () => !!offline.$('.api-error'));

  check('B', 'B1: 네트워크가 끊기면 로컬 JOBS 로 결과를 지어내지 않는다', () => {
    assert.ok(offline.evalIn('JOBS.length') > 0, '이 검사는 JOBS 가 살아 있어야 뜻이 있다');
    assert.equal(offline.session().error.code, 'NETWORK');
    assert.equal(offline.$$('.plan-card').length, 0, '실패했는데 추천 카드가 그려졌다');
    const html = offline.html();
    assert.deepEqual(mockCompanies.filter(c => html.includes(c)), [], '로컬 목 공고가 화면에 나왔다');
    assert.ok(offline.$('#retry'), '재시도 경로가 없다');
    assert.equal(offline.stored('history'), null, '실패한 요청이 기록에 남았다');
  });

  // B2: 검사 자체가 이빨이 있는가 — 원본을 한 글자 비틀면 반드시 실패해야 한다
  //     (라이브 응답이 없으면 비틀 원본도 없으므로 건너뛴다)
  if (rawLast) {
    check('B', 'B2: 지표가 원본과 어긋나면 검사가 실패한다 (음성 대조)', () => {
      const tampered = JSON.parse(JSON.stringify(rawLast));
      tampered.plans[0].metrics.monthlyIncome = (tampered.plans[0].metrics.monthlyIncome || 0) + 1;
      assert.throws(() => verifyMetrics(page, tampered, 0), /지표/, '지표를 비틀었는데 검사가 통과했다');
    });
    check('B', 'B2: 배정 시간이 원본과 어긋나면 검사가 실패한다 (음성 대조)', () => {
      const tampered = JSON.parse(JSON.stringify(rawLast));
      const s = tampered.plans[0].jobs[0].assignedShifts[0];
      s.start = s.start === '06:00' ? '07:00' : '06:00';
      assert.throws(() => verifyTimetable(page, tampered, 0), /열에/, '배정 시간을 비틀었는데 검사가 통과했다');
    });
    check('B', 'B2: 출처 고지가 응답과 어긋나면 검사가 실패한다 (음성 대조)', () => {
      const tampered = JSON.parse(JSON.stringify(rawLast));
      tampered.meta.job_source = 'public_web';
      tampered.meta.data_mode = 'live';
      assert.throws(() => verifySourceNotice(page, tampered), /고지/, '출처를 비틀었는데 검사가 통과했다');
    });
    check('B', 'B2: 어댑터 스냅샷이 어긋나면 검사가 실패한다 (음성 대조)', () => {
      const tampered = JSON.parse(JSON.stringify(rawLast));
      tampered.plans[0].jobs[0].title += ' (변조)';
      assert.throws(() => verifyAdapterSnapshot(page, tampered), /원본과 다르다/, '제목을 비틀었는데 검사가 통과했다');
    });
  } else {
    console.log('   · B2 음성 대조는 라이브 응답이 없어 건너뜁니다');
  }

  /* ---------------- 산출물 ---------------- */
  const summary = {
    stamp,
    apiBase: API_BASE,
    kind: 'DOM/handler integration proof (not a browser screenshot proof)',
    posts: api.length,
    healthzCalls: 1,
    elapsedMs: elapsed(),
    checks: checks.length,
    failed: failures().length,
    calls: api.map(c => ({
      n: c.n,
      status: c.status,
      ms: c.ms,
      search: c.request.search,
      regenerate: c.request.regenerate || null,
      requestId: c.raw && c.raw.requestId,
      candidateCount: c.raw && c.raw.candidateCount,
      planIds: c.raw && (c.raw.plans || []).map(p => p.id),
      jobIds: c.raw && (c.raw.plans || []).map(p => p.jobs.map(j => j.jobId)),
    })),
    results: checks,
  };
  for (const c of api) {
    fs.writeFileSync(path.join(artDir, `call${c.n}-request.json`), JSON.stringify(c.request, null, 2));
    fs.writeFileSync(path.join(artDir, `call${c.n}-response.json`), JSON.stringify(c.raw, null, 2));
  }
  fs.writeFileSync(path.join(artDir, 'rendered-final.html'), page.bodyHtml());
  fs.writeFileSync(path.join(artDir, 'summary.json'), JSON.stringify(summary, null, 2));

  /* ---------------- 보고 ---------------- */
  const bad = failures();
  console.log('\n  ───────────────────────────────────────────────');
  console.log(`  POST /api/recommendations ${api.length}회 (+ /healthz 1회) · ${(elapsed() / 1000).toFixed(1)}초 소요`);
  console.log(`  검사 ${checks.length}건 · 통과 ${checks.length - bad.length} · 실패 ${bad.length}`);
  console.log(`  산출물 ${path.relative(REPO, artDir)}`);
  if (bad.length) {
    console.log('\n  실패 목록');
    for (const f of bad) console.log(`   ✗ [${f.section}] ${f.name}\n     ${f.message}`);
  }
  console.log(bad.length ? '\n  결과: FAIL' : '\n  결과: PASS — 화면 값이 그 순간 서버가 보낸 원본 JSON 과 일치');
  process.exitCode = bad.length ? 1 : 0;
}

const watchdog = setTimeout(() => {
  console.error(`\nprobe: ${TOTAL_BUDGET_MS / 1000}초 예산을 넘겨 중단합니다.`);
  process.exit(2);
}, TOTAL_BUDGET_MS + 2000);

main()
  .catch(e => {
    console.error('\nprobe 중단: ' + (e && e.stack ? e.stack : e));
    process.exitCode = 2;
  })
  .finally(() => clearTimeout(watchdog));
