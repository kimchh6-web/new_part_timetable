/* =====================================================================
 * app.js — 해시 라우터 + 화면 (온보딩 / 홈 / 결과 / 스케줄 / 내 스케줄)
 *
 * 추천 결과는 전부 서버(POST /api/recommendations)가 만든다.
 * 이 파일은 응답의 plans / assignedShifts / metrics / availableSlots 를
 * 그대로 그린다. 지표를 다시 계산하지 않고, 네트워크가 실패해도
 * 로컬 공고 데이터로 몰래 대체하지 않는다 (에러 + 재시도를 보여준다).
 * ===================================================================*/

/* ---------- 저장소 ---------- */
const Store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
const $ = sel => document.querySelector(sel);
const app = () => $('#app');

let toastTimer;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

/* ---------- 세션 상태 (결과 페이지용) ---------- */
const Session = {
  get() { try { return JSON.parse(sessionStorage.getItem('result')) || null; } catch { return null; } },
  set(v) { try { sessionStorage.setItem('result', JSON.stringify(v)); } catch {} },
  clear() { try { sessionStorage.removeItem('result'); } catch {} },
};

/* ---------- 저장 포맷 버전 ----------
 * 1 (또는 없음) = 프론트가 로컬에서 만든 옛 결과. jobs[].shifts 를 가짐.
 * 2            = 서버 응답 기반. jobs[].assignedShifts + 서버 metrics 를 가짐.
 * 옛 데이터는 그대로 두고 읽을 때만 변환한다. 덮어쓰지 않는다.
 * -------------------------------------------------------------------*/
const SCHEMA_VERSION = 2;

const PLAN_TYPE_META = {
  maxIncome: { cls: 'income',  fallbackLabel: '수입 최대안' },
  minTravel: { cls: 'ease',    fallbackLabel: '이동 최소안' },
  balanced:  { cls: 'balance', fallbackLabel: '밸런스안' },
};
const LEGACY_KIND_LABEL = { income: '수입 최대안', ease: '여유 우선안', balance: '밸런스안' };
const PRIORITY_LABEL = { wage: '시급', distance: '거리', rating: '평점', flex: '시간 유연성', flexibility: '시간 유연성' };
const WARNING_LABEL = {
  LONG_TRAVEL: '이동 시간이 깁니다',
  TIGHT_TRANSFER: '도착 여유가 빠듯합니다',
  BELOW_TARGET: '목표 금액에 못 미칩니다',
  SHORT_PERIOD: '단기 공고입니다',
  AGE_UNVERIFIED: '연령 조건을 확인하지 못했습니다',
  // 계약 5종 밖이지만 서버(harness/weekly)가 실제로 내려보내는 코드들
  HOLIDAY_PAY_NOT_INCLUDED: '주휴수당은 수입에 넣지 않았습니다',
  QUALIFICATIONS_UNVERIFIED: '자격 요건을 확인하지 못했습니다',
  NON_HOURLY_PAY: '시급제가 아닌 급여입니다',
};

/* =====================================================================
 * 고정·제외·재생성 상태 전이
 *
 * 화면에서 떼어내 순수 함수로 둔다. 이 네 개가 "고정한 알바는 유지하고
 * 나머지만 다시" 를 실제로 지키는 부분이라, 브라우저 없이도 검증할 수 있어야 한다.
 * ===================================================================*/
const MAX_SEEN_PLAN_IDS = 30;

/** 요청에 실을 재생성 페이로드. fresh 면 "이미 본 조합" 을 비워 처음부터 다시 뽑는다. */
function regeneratePayload(st, opts = {}) {
  return {
    pinned: [...(st.pinned || [])],
    excluded: [...(st.excluded || [])],
    previousPlanHashes: opts.fresh ? [] : [...(st.seenPlanIds || [])],
  };
}

/**
 * 📌 토글. 추천 개수를 넘겨 고정하려 하면 거부한다(서버의 PINNED_EXCEEDS_COUNT 와 같은 규칙).
 * 고정한 알바는 제외 목록에서 빠진다 — 한 공고가 두 목록에 동시에 있을 수 없다.
 */
function togglePin(st, id) {
  const pinned = st.pinned || [];
  const excluded = st.excluded || [];
  if (pinned.includes(id)) {
    return { ok: true, pinnedNow: false, pinned: pinned.filter(x => x !== id), excluded: [...excluded] };
  }
  const limit = Math.max(1, Math.min(3, Number(st.search && st.search.count) || 1));
  if (pinned.length >= limit) {
    return { ok: false, limit, pinnedNow: false, pinned: [...pinned], excluded: [...excluded] };
  }
  return { ok: true, pinnedNow: true, pinned: [...pinned, id], excluded: excluded.filter(x => x !== id) };
}

/** ✕ 제외. 고정돼 있었다면 고정이 풀린다. */
function excludeJob(st, id) {
  return {
    pinned: (st.pinned || []).filter(x => x !== id),
    excluded: [...new Set([...(st.excluded || []), id])],
  };
}

/** 이미 본 조합 누적 — 중복 없이, 최근 것부터 MAX_SEEN_PLAN_IDS 개까지. */
function mergeSeenPlanIds(seen, ids) {
  return [...new Set([...(seen || []), ...(ids || [])])].slice(-MAX_SEEN_PLAN_IDS);
}

/* 저장 데이터가 프로필을 잃었어도 목록 화면 전체가 죽지 않게 한다. */
function safeProfile(p) {
  const base = { role: '', home: '', minBlock: 2, night: true, want15: false, goal: 0, age: null, schedule: {} };
  const out = { ...base, ...(p && typeof p === 'object' ? p : {}) };
  if (!out.schedule || typeof out.schedule !== 'object') out.schedule = {};
  return out;
}

/**
 * 요청 결과를 좌우하는 프로필 필드만 뽑은 지문.
 * 화면 표시용 필드(sameDaily 등)는 보지 않는다. 결과를 받은 뒤 내 정보를 바꿨는지
 * 판단하는 데만 쓴다 — 바뀌었으면 그 결과는 지금 조건의 답이 아니다.
 */
function profileSignature(p) {
  const s = safeProfile(p);
  const days = DAYS.map(d => {
    const row = s.schedule[d];
    return row && row.has ? `${d}:${row.start}-${row.end}@${row.place}` : `${d}:-`;
  }).join('|');
  return JSON.stringify([String(s.role ?? ''), String(s.home ?? ''), Number(s.minBlock) || 0, !!s.night, !!s.want15, Number(s.goal) || 0, s.age == null ? null : Number(s.age), days]);
}

/* ---------- 라우터 ---------- */
const routes = {
  '/onboarding': viewOnboarding,
  '/': viewHome,
  '/result': viewResult,
  '/my': viewMy,
};
function navigate(path) { location.hash = '#' + path; }
function router() {
  const hash = location.hash.replace(/^#/, '') || '/';
  const profile = Store.get('profile');
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + hash.split('/').slice(0, 2).join('/')));
  window.scrollTo(0, 0);
  if (hash === '/live') return viewLiveDemo();
  if (hash === '/reset') { if (confirm('저장된 내 정보와 스케줄을 모두 지우고 처음부터 시작할까요?')) { localStorage.clear(); sessionStorage.clear(); } navigate('/onboarding'); return; }
  const m = hash.match(/^\/schedule\/(.+)$/);
  if (m) return viewSchedule(decodeURIComponent(m[1]));
  if (!profile && hash !== '/onboarding') return viewOnboarding();
  (routes[hash] || viewHome)();
}
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('hashchange', router);
  window.addEventListener('DOMContentLoaded', router);
}

/* =====================================================================
 * 온보딩
 * ===================================================================*/
const ROLE_LABEL = { '학생': '수업', '직장인': '근무', '기타': '일정' };

function defaultProfile() {
  const schedule = {};
  for (const d of DAYS) schedule[d] = { has: false, start: '', end: '', place: '' };
  return { role: '', sameDaily: false, schedule, home: '', minBlock: 3, night: true, want15: false, goal: 0, age: null };
}

function viewOnboarding() {
  const existing = Store.get('profile');
  const ob = existing ? { ...defaultProfile(), ...JSON.parse(JSON.stringify(existing)) } : defaultProfile();
  let step = 1;

  const render = () => {
    const lbl = ROLE_LABEL[ob.role] || '일정';
    app().innerHTML = `
      <div class="page-head">
        <div class="eyebrow">Onboarding · ${step} / 4</div>
        <h1>${existing ? '내 정보 수정' : '처음 한 번만 알려주세요'}</h1>
        <p>잘 바뀌지 않는 정보입니다. 이후에는 홈에서 요약만 확인하면 됩니다.</p>
      </div>
      <div class="stepper">${[1,2,3,4].map(i => `<div class="st ${i <= step ? 'done' : ''}"></div>`).join('')}</div>
      <div class="card" style="max-width:760px;margin:0 auto;">
        ${step === 1 ? stepRole() : step === 2 ? stepSchedule(lbl) : step === 3 ? stepHome() : stepGoal()}
        <div class="actions">
          <button class="btn ghost" id="prev" ${step === 1 ? 'disabled' : ''}>← 이전</button>
          <button class="btn primary" id="next">${step === 4 ? '완료하고 알바 찾기 →' : '다음 →'}</button>
        </div>
      </div>`;
    bind();
  };

  const stepRole = () => `
    <div class="step-title">어떤 일상을 보내고 계신가요?</div>
    <div class="step-desc">선택에 따라 다음 단계의 표현이 바뀝니다.</div>
    <div class="role-grid">
      ${[['학생','🎓','수업 시간표 기준'],['직장인','💼','출퇴근 시간 기준'],['기타','🧭','직접 일정 입력']].map(([r, ic, ds]) => `
        <div class="role-card ${ob.role === r ? 'on' : ''}" data-role="${r}"><div class="ic">${ic}</div><div class="nm">${r}</div><div class="ds">${ds}</div></div>`).join('')}
    </div>`;

  const stepSchedule = (lbl) => `
    <div class="step-title">고정 ${lbl} 일정을 알려주세요</div>
    <div class="step-desc">${lbl}이 끝나는 장소를 기준으로 이동 가능한 알바를 찾습니다.</div>
    <div class="toggle ${ob.sameDaily ? 'on' : ''}" id="same">
      <div><div class="t-label">평일 매일 같음</div><div class="t-sub">월~금 ${lbl} 시간과 장소가 동일하면 켜 주세요. 한 줄만 입력하면 됩니다.</div></div>
      <div class="switch"></div>
    </div>
    <div style="height:14px"></div>
    <table class="sched-table">
      <thead><tr><th>요일</th><th>${lbl} 있음</th><th>시작</th><th>종료</th><th>종료 장소</th></tr></thead>
      <tbody>
        ${DAYS.map((d, i) => {
          const s = ob.schedule[d];
          const locked = ob.sameDaily && i > 0 && i < 5;
          return `<tr class="${s.has ? '' : 'off'}" data-day="${d}">
            <td>${DAY_KO[d]}</td>
            <td><span class="chk ${s.has ? 'on' : ''}" data-f="has" ${locked ? 'style="opacity:.4;pointer-events:none"' : ''}>✓</span></td>
            <td><input type="time" data-f="start" value="${s.start}" ${!s.has || locked ? 'disabled' : ''}></td>
            <td><input type="time" data-f="end" value="${s.end}" ${!s.has || locked ? 'disabled' : ''}></td>
            <td><select data-f="place" ${!s.has || locked ? 'disabled' : ''}><option value="" ${s.place ? '' : 'selected'} disabled>장소 선택</option>${REGIONS.map(r => `<option ${s.place === r ? 'selected' : ''}>${r}</option>`).join('')}</select></td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
    <div class="note" style="margin-top:12px">종료 장소는 이동시간 계산을 위해 15개 지역 중에서만 고를 수 있습니다.</div>`;

  const stepHome = () => `
    <div class="step-title">집 위치와 근무 제약</div>
    <div class="step-desc">일정이 없는 날은 집에서 출발하는 것으로 계산합니다.</div>
    <div class="grid-2">
      <div class="field"><label class="small">집 위치</label>
        <select id="home"><option value="" ${ob.home ? '' : 'selected'} disabled>집과 가장 가까운 지역 선택</option>${REGIONS.map(r => `<option ${ob.home === r ? 'selected' : ''}>${r}</option>`).join('')}</select></div>
      <div class="field"><label class="small">최소 연속 근무 가능 시간</label>
        <div class="seg" id="minblock">${[2,3,4].map(h => `<button class="${ob.minBlock === h ? 'on' : ''}" data-v="${h}">${h}시간</button>`).join('')}</div></div>
    </div>
    <div style="height:16px"></div>
    <div class="grid-2">
      <div class="field"><label class="small">나이 <span style="font-weight:400">(선택)</span></label>
        <input type="number" id="age" min="14" max="99" step="1" value="${ob.age || ''}" placeholder="예: 26">
        <div class="note" style="margin-top:6px">연령 제한이 있는 공고를 걸러냅니다. 비워 두면 연령 조건을 확인하지 못했다는 안내가 결과에 함께 표시됩니다.</div></div>
      <div></div>
    </div>
    <div style="height:16px"></div>
    <div class="toggle ${ob.night ? 'on' : ''}" id="night"><div><div class="t-label">야간(22시 이후) 근무 가능</div><div class="t-sub">끄면 22시 넘어 끝나는 공고를 제외합니다.</div></div><div class="switch"></div></div>
    <div class="toggle ${ob.want15 ? 'on' : ''}" id="want15"><div><div class="t-label">주 15시간 이상 근무 희망</div><div class="t-sub">주 15시간 이상 일하면 주휴수당이 발생해 실수령이 늘어납니다.</div></div><div class="switch"></div></div>`;

  const stepGoal = () => `
    <div class="step-title">이번 달 목표 금액</div>
    <div class="step-desc">목표를 기준으로 필요한 근무시간과 알바 개수를 역산합니다.</div>
    <div class="field"><label class="small">목표 금액 (원)</label>
      <input type="number" id="goal" step="10000" min="0" value="${ob.goal || ''}" placeholder="예: 800000" style="font-size:22px;font-weight:800;height:56px"></div>
    <div class="goal-calc" id="goalcalc"></div>
    <div class="note" style="margin-top:12px">2026년 최저시급 ${MIN_WAGE.toLocaleString()}원 기준 참고값입니다. 실제 추천 금액은 서버 계산 결과를 씁니다.</div>`;

  const updateGoalCalc = () => {
    const el = $('#goalcalc'); if (!el) return;
    const h = hoursNeededForGoal(ob.goal);
    el.innerHTML = ob.goal > 0
      ? `최저시급 기준 <strong>주 ${h.toFixed(1)}시간</strong> 필요합니다. ${h > 20 ? '알바 2~3개 조합을 권장합니다.' : h > 10 ? '알바 1~2개면 충분합니다.' : '알바 1개로 달성 가능합니다.'}`
      : '금액을 입력하면 필요한 주간 근무시간을 계산합니다.';
  };

  const bind = () => {
    $('#prev').onclick = () => { step--; render(); };
    $('#next').onclick = () => {
      if (step === 1 && !ob.role) { toast('신분을 선택해 주세요.'); return; }
      if (step === 2) {
        const anyHas = DAYS.some(d => ob.schedule[d].has);
        for (const d of DAYS) {
          const s = ob.schedule[d]; if (!s.has) continue;
          if (!s.start || !s.end) { toast(`${DAY_KO[d]}요일 시작·종료 시간을 입력해 주세요.`); return; }
          if (!s.place) { toast(`${DAY_KO[d]}요일 종료 장소를 선택해 주세요.`); return; }
          if (toMin(s.end) <= toMin(s.start)) { toast(`${DAY_KO[d]}요일 종료 시간이 시작보다 빠릅니다.`); return; }
        }
        if (!anyHas) { toast('고정 일정을 최소 하루 이상 체크해 주세요.'); return; }
      }
      if (step === 3) {
        if (!ob.home) { toast('집 위치를 선택해 주세요.'); return; }
        if (ob.age !== null && ob.age !== undefined && ob.age !== '' && !(ob.age >= 14 && ob.age <= 99)) { toast('나이는 14~99 사이로 입력하거나 비워 주세요.'); return; }
      }
      if (step === 4) {
        if (!(ob.goal > 0)) { toast('목표 금액을 입력해 주세요.'); return; }
        if (ob.goal > 10000000) { toast('목표 금액은 10,000,000원 이하로 입력해 주세요.'); return; }
        Store.set('profile', ob); toast('저장되었습니다.'); navigate('/'); return;
      }
      step++; render();
    };
    document.querySelectorAll('.role-card').forEach(el => el.onclick = () => { if (!existing) ob.sameDaily = el.dataset.role === '직장인'; ob.role = el.dataset.role; render(); });
    const same = $('#same');
    if (same) same.onclick = () => { ob.sameDaily = !ob.sameDaily; if (ob.sameDaily) syncWeekdays(); render(); };
    document.querySelectorAll('.sched-table tr[data-day]').forEach(tr => {
      const d = tr.dataset.day;
      tr.querySelector('[data-f=has]').onclick = () => { const sd = ob.schedule[d]; sd.has = !sd.has; if (sd.has) { sd.start = sd.start || '09:00'; sd.end = sd.end || '18:00'; } if (ob.sameDaily && d === 'MON') syncWeekdays(); render(); };
      tr.querySelectorAll('input,select').forEach(inp => inp.onchange = () => { ob.schedule[d][inp.dataset.f] = inp.value; if (ob.sameDaily && d === 'MON') { syncWeekdays(); render(); } });
    });
    const home = $('#home'); if (home) home.onchange = () => ob.home = home.value;
    const age = $('#age'); if (age) age.oninput = () => { ob.age = age.value === '' ? null : (+age.value || null); };
    document.querySelectorAll('#minblock button').forEach(b => b.onclick = () => { ob.minBlock = +b.dataset.v; render(); });
    const night = $('#night'); if (night) night.onclick = () => { ob.night = !ob.night; render(); };
    const w15 = $('#want15'); if (w15) w15.onclick = () => { ob.want15 = !ob.want15; render(); };
    const goal = $('#goal'); if (goal) { goal.oninput = () => { ob.goal = +goal.value || 0; updateGoalCalc(); }; updateGoalCalc(); }
  };
  const syncWeekdays = () => { for (const d of ['TUE','WED','THU','FRI']) ob.schedule[d] = { ...ob.schedule.MON }; };
  render();
}

/* =====================================================================
 * 홈 — 알바 찾기
 * ===================================================================*/
function viewHome() {
  const profile = Store.get('profile');
  const search = Store.get('lastSearch') || { count: 2, categories: [], priority: 'wage' };
  const slots = computeFreeSlots(profile);       // 요청 전 미리보기(추정). 확정 빈 시간은 서버가 내려준다.
  const lbl = ROLE_LABEL[profile.role];
  const needH = hoursNeededForGoal(profile.goal);
  const hist = usableHistory();

  app().innerHTML = `
    <div class="hero">
      <div class="eyebrow" style="color:#c7d2fe">Part-time Scheduler</div>
      <h1>남는 시간과 이동 동선까지 계산한<br>알바 조합을 추천합니다</h1>
      <p>고정 일정이 끝나는 위치에서 실제로 도착 가능한 공고만 골라, 겹치지 않는 조합을 주간 시간표로 만들어 드립니다.</p>
      <div class="pts"><span>이동 가능성 반영</span><span>조합 최적화</span><span>목표 금액 역산</span><span>실질 시급 계산</span></div>
    </div>
    ${sourceNotice(null)}
    <div style="height:16px"></div>
    <div class="grid-main">
      <div>
        <div class="card">
          <div class="card-title">탐색 조건 <span class="hint">이번 탐색에만 적용</span></div>
          <div class="section-label">알바 개수</div>
          <div class="seg" id="count">${[1,2,3].map(n => `<button class="${search.count === n ? 'on' : ''}" data-v="${n}">${n}개</button>`).join('')}</div>
          <div class="note info" style="margin-top:10px">목표 ${fmtWon(profile.goal)} 달성에 최저시급 기준 주 ${needH.toFixed(1)}시간이 필요합니다.</div>
          <div class="section-label">카테고리 <span style="font-weight:400">(비우면 전체)</span></div>
          <div class="chips" id="cats">${CATEGORIES.map(c => `<span class="chip ${search.categories.includes(c) ? 'on' : ''}" data-v="${c}"><i class="dot" style="background:${CATEGORY_COLORS[c]}"></i>${c}</span>`).join('')}</div>
          <div class="section-label">가장 중요한 것</div>
          <div class="seg" id="prio">${[['wage','시급'],['distance','거리'],['rating','평점'],['flex','시간 유연성']].map(([v, l]) => `<button class="${search.priority === v ? 'on' : ''}" data-v="${v}">${l}</button>`).join('')}</div>
          <div style="margin-top:24px"><button class="btn primary lg block" id="go">추천 조합 생성하기</button></div>
          <div class="note" style="margin-top:10px">버튼을 누르면 서버가 빈 시간 계산 → 후보 필터 → 조합 생성을 수행합니다. 최대 30초까지 걸릴 수 있습니다.</div>
          ${hist.length ? `<div style="margin-top:10px;text-align:center"><a class="btn ghost sm" href="#/result">🕘 이전 추천 결과 ${hist.length}건 보기</a></div>` : ''}
        </div>
      </div>
      <div>
        <div class="card summary-card">
          <div class="card-title">내 정보 <a class="btn sm" href="#/onboarding">수정</a></div>
          <div class="kv">
            <div><div class="k">신분</div><div class="v">${profile.role}</div></div>
            <div><div class="k">집 위치</div><div class="v">${profile.home}</div></div>
            <div><div class="k">최소 연속 근무</div><div class="v">${profile.minBlock}시간</div></div>
            <div><div class="k">야간 근무</div><div class="v">${profile.night ? '가능' : '불가'}</div></div>
            <div><div class="k">목표 금액</div><div class="v">${fmtWon(profile.goal)}</div></div>
            <div><div class="k">주 15h 이상</div><div class="v">${profile.want15 ? '희망' : '무관'}</div></div>
            <div><div class="k">나이</div><div class="v">${profile.age ? profile.age + '세' : '미입력'}</div></div>
          </div>
          <div class="sched-mini">${DAYS.map(d => { const s = profile.schedule[d]; return `<div class="d ${s.has ? 'on' : ''}">${DAY_KO[d]}<small>${s.has ? parseInt(s.start) + '~' + parseInt(s.end) + '시' : '없음'}</small></div>`; }).join('')}</div>
          <div style="font-size:12px;opacity:.7;margin-top:8px">${lbl} 종료 장소: ${[...new Set(DAYS.filter(d => profile.schedule[d].has).map(d => profile.schedule[d].place))].join(', ') || '—'}</div>
        </div>
      </div>
    </div>
    <div class="slot-bar"><div class="slot-bar-inner">
      <div class="ttl">빈 시간 미리보기<br><span style="font-weight:500">${slots.length}개 · 추정</span></div>
      <div class="slot-list">${slots.length ? slots.map(s => `<div class="slot-pill ${s.allDay ? 'allday' : ''}"><b>${DAY_KO[s.day]}</b>${s.allDay ? '종일' : toHM(s.start) + ' ~ ' + toHM(s.end)} <span>(${s.label})</span></div>`).join('') : '<div class="slot-pill">조건에 맞는 빈 시간이 없습니다. 최소 연속 근무 시간을 줄여 보세요.</div>'}</div>
    </div></div>`;

  document.querySelectorAll('#count button').forEach(b => b.onclick = () => { search.count = +b.dataset.v; viewHomeRefresh(); });
  document.querySelectorAll('#cats .chip').forEach(c => c.onclick = () => { const v = c.dataset.v; search.categories = search.categories.includes(v) ? search.categories.filter(x => x !== v) : [...search.categories, v]; viewHomeRefresh(); });
  document.querySelectorAll('#prio button').forEach(b => b.onclick = () => { search.priority = b.dataset.v; viewHomeRefresh(); });
  function viewHomeRefresh() { Store.set('lastSearch', search); viewHome(); }
  $('#go').onclick = () => {
    Store.set('lastSearch', search);
    // 후보 판정은 서버가 한다. 여기서 로컬 데이터로 미리 거르지 않는다.
    Session.set({ search, pinned: [], excluded: [], seenPlanIds: [], selected: 0, response: null, error: null });
    navigate('/result');
  };
}

/* =====================================================================
 * 결과 — 서버 응답(3안) → 시간표 → 상세
 * ===================================================================*/

/* /result 화면이 다시 열릴 때마다 오른다. 날아가 있는 요청의 주인이 누구인지
 * 가리는 데 쓴다 — 아래 owns() 주석 참고. */
let resultEpoch = 0;

/** 결과 상태 한 벌을 가리키는 이름표. 저장소가 갈아치워졌는지 보는 데 쓴다. */
function newSessionId() {
  return 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function viewResult() {
  const currentProfile = Store.get('profile');
  let st = Session.get();
  let busy = false;

  /* ---------- 응답의 소유권 ----------
   * 요청 하나는 30초까지 걸린다. 그 사이에 사용자는 홈으로 돌아가 조건을 바꾸고
   * 다시 추천을 누를 수도, 옆의 기록을 열 수도 있다. 그때 먼저 나간 요청이 늦게
   * 도착해 Session·기록·화면을 덮어쓰면, 화면은 지금 조건이 아닌 옛 조건의 결과를
   * "지금 결과"라고 말하게 된다. 그래서 요청마다 주인 자격을 확인한다.
   *   epoch  — 이 /result 화면 자체가 아직 그 자리인가 (홈 → 재진입 시 오른다)
   *   seq    — 이 화면 안에서 사용자가 다른 상태로 갈아타지 않았는가 (기록 열기 등)
   *   sid    — 저장소의 결과가 아직 이 요청이 쓰던 그것인가. 초기화(#/reset)나
   *            홈에서의 새 검색은 sessionStorage 를 통째로 갈아치우므로, 그 뒤에
   *            도착한 응답이 지운 결과를 되살리거나 새 검색을 덮지 못하게 막는다.
   * 주인이 아닌 응답은 성공이든 실패든 아무것도 쓰지 않고 조용히 버린다.
   * location.hash 비교만으로는 세 경우 모두 같은 '#/result' 라 구분되지 않는다. */
  const epoch = ++resultEpoch;
  let runSeq = 0;
  const sessionHolds = () => { const cur = Session.get(); return !!cur && !!st.sid && cur.sid === st.sid; };
  const owns = seq => epoch === resultEpoch && seq === runSeq && sessionHolds();
  /** 사용자가 다른 상태로 갈아탔다 — 날아가 있는 요청에서 주인 자격을 거둔다. */
  const takeOver = () => { runSeq++; busy = false; };

  /**
   * 지금 그리고 있는 결과가 받아졌을 때의 내 정보.
   * 당시 값이 남아 있지 않은 옛 기록은 지금의 고정 일정을 그 결과의 일부인 양
   * 겹쳐 그리지 않는다 — 모르는 것은 그리지 않고 아래 안내로 밝힌다.
   */
  const viewProfile = () => {
    if (st.profile) return safeProfile(st.profile);
    if (st.profileUnknown) return safeProfile({ ...safeProfile(currentProfile), schedule: {} });
    return safeProfile(currentProfile);
  };

  if (!st) {
    const usable = usableHistory();
    if (!usable.length) { navigate('/'); return; }
    st = fromHistory(usable[0]);
    Session.set(st);
  }
  st.pinned = st.pinned || [];
  st.excluded = st.excluded || [];
  st.seenPlanIds = st.seenPlanIds || [];
  if (!st.sid) st.sid = newSessionId();
  Session.set(st);

  function fromHistory(h) {
    // profile 이 없는 옛 기록은 null 로 둔다. 현재 내 정보로 그리되 그 사실을 밝힌다.
    return { sid: newSessionId(), profileUnknown: !h.profile, search: h.search || Store.get('lastSearch') || { count: 2, categories: [], priority: 'wage' }, pinned: h.pinned || [], excluded: h.excluded || [], seenPlanIds: [], selected: 0, response: h.response, profile: h.profile || null, historyId: h.id, error: null };
  }

  /* ---------- 서버 호출 ---------- */
  const run = async (opts = {}) => {
    if (busy) return;
    if (typeof WeeklyApi === 'undefined') {
      st.error = { code: 'NETWORK', message: 'API 클라이언트(js/api-client.js)를 불러오지 못했습니다. 새로고침해 주세요.', details: null, suggestions: [], retryable: true };
      Session.set(st); drawError(); return;
    }
    busy = true;
    const seq = ++runSeq;
    st.error = null;
    Session.set(st);
    drawLoading(opts);
    const startedAt = location.hash;
    try {
      const { request, response } = await WeeklyApi.requestRecommendations(
        currentProfile, st.search, regeneratePayload(st, opts));
      if (!owns(seq)) return;                     // 주인이 아니다 — 기록에도 남기지 않는다
      st.request = request;
      st.response = response;
      // 이 응답이 어떤 내 정보로 받아진 것인지 함께 붙든다. 나중에 내 정보를 바꾸면
      // 이 결과는 그 조건의 답이 아니므로, 지금의 고정 일정 위에 겹쳐 그리지 않는다.
      st.profile = JSON.parse(JSON.stringify(currentProfile));
      st.profileUnknown = false;
      st.seenPlanIds = mergeSeenPlanIds(st.seenPlanIds, WeeklyApi.planIds(response));
      st.selected = 0;
      st.historyId = pushHistory({ search: st.search, pinned: st.pinned, excluded: st.excluded, profile: st.profile, response });
      Session.set(st);
      // 응답을 기다리는 동안 다른 화면으로 갔다면 그 화면을 덮어쓰지 않는다.
      // 결과는 Session 에 남아 있으므로 /result 로 돌아오면 그대로 보인다.
      if (location.hash === startedAt) draw();
    } catch (e) {
      if (!owns(seq)) return;                     // 늦게 온 옛 요청의 실패로 지금 결과를 지우지 않는다
      st.error = { code: e.code || 'NETWORK', message: e.message, details: e.details || null, suggestions: e.suggestions || [], retryable: e.retryable !== false };
      Session.set(st);
      if (location.hash === startedAt) drawError();
    } finally {
      if (owns(seq)) busy = false;
    }
  };

  /* ---------- 기록 사이드바 ---------- */
  const sidebar = () => {
    const h = historyEntries();
    return `<aside class="hist">
      <div class="hist-head"><b>추천 결과 기록</b><span>${h.length}건</span>${h.length ? '<button class="btn ghost sm" id="hist-clear">전체 삭제</button>' : ''}</div>
      ${h.length ? h.map((e, i) => {
        const no = h.length - i;
        const d = new Date(e.createdAt);
        const time = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
        const usable = e.schemaVersion >= 2 && e.response && Array.isArray(e.response.plans) && e.response.plans.length;
        if (usable) {
          const best = e.response.plans[0];
          return `<div class="hist-item ${e.id === st.historyId ? 'on' : ''}" data-hid="${e.id}">
            <div class="hi-top"><span class="hi-no">#${no}</span><span class="hi-time">${time}</span><button class="hi-del" data-hdel="${e.id}" title="삭제">✕</button></div>
            <div class="hi-money">${fmtMoney(best.metrics.monthlyIncome)} <small>${fmtRate(best.metrics.targetAchievementRate)}</small></div>
            <div class="hi-jobs">${best.jobs.map(j => `<span style="border-left:3px solid ${CATEGORY_COLORS[j.category] || 'var(--border)'}">${esc(j.company || j.title || j.jobId)}</span>`).join('')}</div>
            <div class="hi-cond">${(e.search || {}).count || '?'}개 · ${PRIORITY_LABEL[(e.search || {}).priority] || ''} 우선 · ${((e.search || {}).categories || []).length ? e.search.categories.length + '개 카테고리' : '전체'}${(e.pinned || []).length ? ' · 📌' + e.pinned.length : ''}${(e.excluded || []).length ? ' · ✕' + e.excluded.length : ''}</div>
          </div>`;
        }
        return `<div class="hist-item legacy" data-hlegacy="${e.id}">
          <div class="hi-top"><span class="hi-no">#${no}</span><span class="hi-time">${time}</span><button class="hi-del" data-hdel="${e.id}" title="삭제">✕</button></div>
          <div class="hi-jobs"><span class="badge legacy">이전 형식</span></div>
          <div class="hi-cond">예전 버전에서 만든 기록이라 다시 열 수 없습니다. 같은 조건으로 새로 추천받아 주세요.</div>
        </div>`;
      }).join('') : '<div class="hist-empty">아직 기록이 없습니다.<br>추천을 생성하면 여기에 쌓입니다.</div>'}
    </aside>`;
  };

  const bindSidebar = () => {
    document.querySelectorAll('.hist-item[data-hid]').forEach(el => el.onclick = e => {
      if (e.target.closest('[data-hdel]')) return;
      const entry = historyEntries().find(x => x.id === el.dataset.hid); if (!entry) return;
      takeOver();                                  // 날아가 있던 요청이 이 기록을 밀어내지 못하게
      st = fromHistory(entry); Session.set(st); draw();
    });
    document.querySelectorAll('.hist-item[data-hlegacy]').forEach(el => el.onclick = e => {
      if (e.target.closest('[data-hdel]')) return;
      toast('예전 형식 기록입니다. 조건을 확인하고 새로 추천받아 주세요.');
    });
    document.querySelectorAll('[data-hdel]').forEach(b => b.onclick = e => {
      e.stopPropagation();
      const id = b.dataset.hdel;
      const rest = historyEntries().filter(x => x.id !== id);
      Store.set('history', rest);
      if (st.historyId === id) {
        const usable = rest.filter(isUsableHistory);
        if (usable.length) { takeOver(); st = fromHistory(usable[0]); Session.set(st); draw(); }
        else { takeOver(); Session.clear(); navigate('/'); }
      } else draw();
    });
    const clr = $('#hist-clear'); if (clr) clr.onclick = () => { Store.set('history', []); st.historyId = null; draw(); toast('기록을 모두 지웠습니다.'); };
  };

  /* ---------- 로딩 ---------- */
  const drawLoading = (opts = {}) => {
    app().innerHTML = `<div class="result-layout">${sidebar()}<div>
      <div class="page-head"><div class="eyebrow">Result</div><h1>${opts.regenerate ? '다른 조합을 다시 찾는 중입니다…' : '추천 조합을 계산하고 있습니다…'}</h1>
        <p>빈 시간 계산 → 이동 가능 후보 필터 → 조합 생성·검증. 최대 30초까지 걸릴 수 있습니다.</p></div>
      ${sourceNotice(null)}
      <div style="height:16px"></div>
      <div class="grid-3" aria-busy="true">${[0,1,2].map(() => `<div class="card"><div class="skeleton sk-line" style="width:40%"></div><div class="skeleton sk-line" style="width:70%;height:28px"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line" style="width:80%"></div></div>`).join('')}</div>
      <div class="note" style="margin-top:16px" role="status" aria-live="polite">입력한 조건은 그대로 유지됩니다. 실패하면 다시 시도할 수 있습니다.</div>
    </div></div>`;
    bindSidebar();
  };

  /* ---------- 에러 ---------- */
  const drawError = () => {
    const e = st.error;
    const d = e.details || {};
    const stageRows = [
      ['전체 공고', d.filteredFrom],
      ['시간 필터 통과', d.afterTimeFilter],
      ['카테고리 필터 통과', d.afterCategoryFilter],
      ['연령 필터 통과', d.afterAgeFilter],
    ].filter(([, v]) => typeof v === 'number');

    app().innerHTML = `<div class="result-layout">${sidebar()}<div>
      <div class="page-head"><div class="eyebrow">Result</div><h1>추천을 받지 못했습니다</h1></div>
      <div class="card api-error" role="alert">
        <div class="ae-code">${esc(e.code)}</div>
        <div class="ae-msg">${esc(e.message)}</div>
        ${(d.problems || []).length > 1 ? `<ul class="ae-problems">${d.problems.map(pr => `<li>${esc(pr.message)}</li>`).join('')}</ul>` : ''}
        ${stageRows.length ? `<div class="ae-stages">${stageRows.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v.toLocaleString('ko-KR')}건</div></div>`).join('')}</div>` : ''}
        ${(e.suggestions || []).length ? `<div class="section-label">이렇게 바꿔 볼 수 있습니다</div>
          <div class="chips">${e.suggestions.map((s, i) => `<button class="chip" data-sugg="${i}">${esc(s.label || s.type)}</button>`).join('')}</div>` : ''}
        ${(st.pinned.length || st.excluded.length) ? `<div class="note" style="margin-top:14px">현재 고정 ${st.pinned.length}건 · 제외 ${st.excluded.length}건이 적용돼 있습니다. 조건이 너무 좁으면 초기화해 보세요.</div>` : ''}
        <div class="toolbar" style="margin-top:18px;margin-bottom:0">
          ${e.retryable ? '<button class="btn primary" id="retry">다시 시도</button>' : '<button class="btn primary" id="retry">조건 그대로 다시 시도</button>'}
          ${(st.pinned.length || st.excluded.length) ? '<button class="btn" id="clearpins">고정·제외 초기화 후 재시도</button>' : ''}
          <a class="btn ghost" href="#/">조건 다시 설정</a>
          <a class="btn ghost" href="#/onboarding">내 정보 수정</a>
        </div>
      </div>
      ${e.code === 'NETWORK' || e.code === 'CLIENT_TIMEOUT' ? `<div class="note warn" style="margin-top:14px">서버(POST ${esc(WeeklyApi.ENDPOINT)})에 연결하지 못했습니다. 로컬 예시 데이터로 결과를 지어내지 않기 때문에 화면이 비어 있습니다.</div>` : ''}
    </div></div>`;

    bindSidebar();
    $('#retry').onclick = () => run();
    const cp = $('#clearpins'); if (cp) cp.onclick = () => { st.pinned = []; st.excluded = []; Session.set(st); run({ fresh: true }); };
    document.querySelectorAll('[data-sugg]').forEach(b => b.onclick = () => {
      const s = (st.error.suggestions || [])[+b.dataset.sugg]; if (!s) return;
      applySuggestion(s, currentProfile, st);
      Session.set(st);
      toast('조건을 바꿔 다시 요청합니다.');
      run({ fresh: true });
    });
  };

  /* ---------- 결과 ---------- */
  const draw = () => {
    if (st.error) return drawError();
    const resp = st.response;
    if (!resp || !resp.plans || !resp.plans.length) return run();

    st.selected = Math.min(st.selected || 0, resp.plans.length - 1);
    const sel = resp.plans[st.selected];
    const jobs = sel.jobs.map(toViewJob);
    const vp = viewProfile();
    // 이 결과를 받은 뒤 내 정보가 바뀌었는가. 당시 값이 아예 없는 옛 기록은
    // 지금 값으로 그릴 수밖에 없으므로, 단정하지 않고 그 사실을 밝힌다.
    const profileDrift = st.profile
      ? profileSignature(st.profile) !== profileSignature(currentProfile)
      : !!st.profileUnknown;
    const h = historyEntries();
    const hIdx = h.findIndex(x => x.id === st.historyId);
    const hNo = hIdx >= 0 ? h.length - hIdx : null;

    app().innerHTML = `<div class="result-layout">${sidebar()}<div>
      <div class="page-head">
        <div class="eyebrow">Result${hNo ? ' · 기록 #' + hNo : ''}${typeof resp.candidateCount === 'number' ? ' · 후보 ' + resp.candidateCount.toLocaleString('ko-KR') + '건' : ''}</div>
        <h1>추천 조합 ${resp.plans.length}안</h1>
        <p>카드를 눌러 비교하고, 마음에 드는 안을 시간표로 확인하세요.</p>
      </div>
      ${sourceNotice(resp.jobSource)}
      ${resp.source === 'fallback' ? '<div style="height:10px"></div><div class="note warn">LLM 대신 서버 계산 규칙으로 만든 조합입니다. 추천 사유 문구가 단순할 수 있습니다.</div>' : ''}
      ${resp.source === null ? '<div style="height:10px"></div><div class="note warn">서버가 생성 방식(source)을 밝히지 않았습니다.</div>' : ''}
      ${resp.droppedPlans ? `<div style="height:10px"></div><div class="note warn">응답 중 ${resp.droppedPlans}개 안은 형식이 맞지 않아 표시하지 않았습니다.</div>` : ''}
      ${profileDrift ? `<div style="height:10px"></div><div class="note warn profile-drift" role="note">${st.profile
        ? '<b>지금의 내 정보와 다른 조건으로 받은 결과입니다.</b> 아래 시간표의 고정 일정·목표 금액은 이 결과를 받을 당시 값입니다.'
        : '<b>이 기록에는 요청 당시 내 정보가 남아 있지 않습니다.</b> 당시 고정 일정을 알 수 없어 시간표에 그리지 않았습니다 — 지금의 고정 일정을 이 결과의 일부인 것처럼 보여 주지 않기 위해서입니다.'} 지금 조건으로 받으려면 <a href="#/">새로 추천받기</a>를 눌러 주세요.</div>` : ''}
      <div style="height:16px"></div>
      <div class="grid-3">
        ${resp.plans.map((p, i) => {
          const meta = PLAN_TYPE_META[p.type] || PLAN_TYPE_META.balanced;
          const pm = p.metrics;
          const rate = pm.targetAchievementRate;
          return `
          <div class="card plan-card ${meta.cls} ${i === st.selected ? 'on' : ''}" data-i="${i}" tabindex="0" role="button" aria-pressed="${i === st.selected}">
            <div class="check">✓</div>
            <span class="tag">${esc(p.label || meta.fallbackLabel)}</span>
            <div class="money">${fmtMoney(pm.monthlyIncome)}</div>
            <div class="rate">예상 월 수입 · 목표 대비 <b>${fmtRate(rate)}</b></div>
            <div class="progress"><div style="width:${rate === null ? 0 : Math.min(100, Math.max(0, rate * 100))}%"></div></div>
            <div class="jobs-line">${p.jobs.map(j => `<span>${esc(j.company || j.title || j.jobId)}</span>`).join('')}</div>
            <div class="mini-metrics">
              <div class="mm"><div class="k">주 근무</div><div class="v">${pm.weeklyWorkHours === null ? '—' : pm.weeklyWorkHours.toFixed(1) + 'h'}</div></div>
              <div class="mm"><div class="k">주 이동</div><div class="v">${pm.weeklyTravelMinutes === null ? '—' : fmtDur(Math.round(pm.weeklyTravelMinutes))}</div></div>
              <div class="mm"><div class="k">실질 시급</div><div class="v">${pm.effectiveHourlyWage === null ? '—' : Math.round(pm.effectiveHourlyWage).toLocaleString('ko-KR') + '원'}</div></div>
            </div>
            ${p.warnings.length ? `<div class="plan-warn">⚠ 확인할 사항 ${p.warnings.length}건</div>` : ''}
            <div class="reason">${esc(p.reason)}</div>
          </div>`; }).join('')}
      </div>

      <div style="height:24px"></div>
      <div class="toolbar">
        <h2 style="font-size:20px">${esc(sel.label || (PLAN_TYPE_META[sel.type] || PLAN_TYPE_META.balanced).fallbackLabel)} 주간 시간표</h2>
        <div class="sp"></div>
        ${st.pinned.length || st.excluded.length ? `<span class="badge pin">고정 ${st.pinned.length} · 제외 ${st.excluded.length}</span>` : ''}
        <button class="btn" id="regen">🔄 재생성${st.pinned.length ? ' (고정 유지)' : ''}</button>
        <button class="btn primary" id="save">💾 이 시간표 저장</button>
      </div>
      ${warningBlock(sel.warnings, vp)}
      ${metricsRowServer(sel.metrics, vp)}
      <div style="height:16px"></div>
      <div class="card">${renderTimetable(jobs, vp)}</div>
      ${slotsCard(resp.availableSlots)}
      <div style="height:16px"></div>
      <div class="card">
        <div class="card-title">알바 상세 <span class="hint">📌 고정: 이 알바는 유지하고 나머지만 재생성 · ✕ 제외: 빼고 다시</span></div>
        ${jobs.map(j => jobItem(j, vp, { pinned: st.pinned.includes(j.id) || j.pinned, controls: 'result' })).join('')}
      </div>
      <div class="note" style="margin-top:14px">${resp.requestId ? '요청 ID ' + esc(resp.requestId) + ' · ' : ''}${resp.generatedAt ? esc(resp.generatedAt) + ' 생성 · ' : ''}출처 ${SOURCE_LABEL[resp.source] || '미상'}${resp.contractVersion ? ' · 계약 ' + esc(resp.contractVersion) : ''}</div>
      ${disclosureBlock(resp.disclosures)}
    </div></div>`;

    bindSidebar();
    document.querySelectorAll('.plan-card').forEach(c => {
      c.onclick = () => { st.selected = +c.dataset.i; Session.set(st); draw(); };
      c.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); c.onclick(); } };
    });
    $('#regen').onclick = () => run({ regenerate: true });
    $('#save').onclick = () => saveDialog(sel);
    app().onclick = e => {
      const pin = e.target.closest('[data-pin]'); const ex = e.target.closest('[data-exclude]');
      if (pin) {
        const next = togglePin(st, pin.dataset.pin);
        if (!next.ok) { toast(`고정은 추천 개수(${next.limit}개)까지만 가능합니다.`); return; }
        st.pinned = next.pinned; st.excluded = next.excluded;
        Session.set(st); draw();
        toast(next.pinnedNow ? '고정했습니다. 재생성 시 유지됩니다.' : '고정을 해제했습니다.');
      }
      if (ex) {
        const next = excludeJob(st, ex.dataset.exclude);
        st.pinned = next.pinned; st.excluded = next.excluded;
        Session.set(st);
        toast('제외하고 다시 생성합니다.');
        run({ regenerate: true });
      }
    };
  };

  /* ---------- 저장 ---------- */
  const saveDialog = (plan) => {
    const meta = PLAN_TYPE_META[plan.type] || PLAN_TYPE_META.balanced;
    const def = `${new Date().getMonth() + 1}월 ${plan.label || meta.fallbackLabel}`;
    modal(`<h3>시간표 저장</h3><div class="field"><label class="small">제목</label><input type="text" id="mtitle" value="${esc(def)}"></div>
      <div class="note" style="margin-top:12px">서버가 계산한 지표와 배정 시간을 그대로 저장합니다. 로그인 없이 이 브라우저에만 보관됩니다.</div>
      <div class="actions"><button class="btn ghost" data-close>취소</button><button class="btn primary" id="mok">저장</button></div>`, () => {
      $('#mok').onclick = () => {
        const list = Store.get('schedules', []);
        const id = 's' + Date.now().toString(36);
        list.unshift({
          id,
          schemaVersion: SCHEMA_VERSION,
          title: $('#mtitle').value.trim() || def,
          createdAt: new Date().toISOString(),
          planId: plan.id,
          planType: plan.type,
          planLabel: plan.label,
          search: st.search,
          profile: JSON.parse(JSON.stringify(viewProfile())),
          source: st.response.source,
          jobSource: st.response.jobSource || null,
          requestId: st.response.requestId,
          generatedAt: st.response.generatedAt,
          metrics: plan.metrics,
          warnings: plan.warnings,
          jobs: JSON.parse(JSON.stringify(plan.jobs)),
        });
        if (!Store.set('schedules', list)) { toast('저장 공간이 부족합니다. 내 스케줄에서 오래된 항목을 지워 주세요.'); return; }
        closeModal(); toast('저장되었습니다.'); navigate('/schedule/' + id);
      };
    });
  };

  if (st.error) drawError();
  else if (st.response) draw();
  else run();
}

/* 서버가 제안한 완화 조건을 실제 입력에 적용한다. */
function applySuggestion(s, profile, st) {
  if (s.type === 'expandCategory') { st.search = { ...st.search, categories: [] }; Store.set('lastSearch', st.search); }
  else if (s.type === 'lowerMinBlock') { profile.minBlock = Number(s.value) || 2; Store.set('profile', profile); }
  else if (s.type === 'allowNight') { profile.night = true; Store.set('profile', profile); }
}

/* =====================================================================
 * 추천 기록 (localStorage 'history')
 * ===================================================================*/
function historyEntries() {
  return (Store.get('history', []) || []).map(e => ({ ...e, schemaVersion: e.schemaVersion || 1 }));
}
function isUsableHistory(e) {
  return e.schemaVersion >= 2 && e.response && Array.isArray(e.response.plans) && e.response.plans.length > 0;
}
function usableHistory() { return historyEntries().filter(isUsableHistory); }
function pushHistory(entry) {
  const id = 'h' + Date.now().toString(36);
  const record = { id, schemaVersion: SCHEMA_VERSION, createdAt: new Date().toISOString(), ...entry };
  const rest = historyEntries();
  for (const keep of [10, 5, 2, 1]) {
    if (Store.set('history', [record, ...rest].slice(0, keep))) return id;
  }
  toast('브라우저 저장 공간이 부족해 기록을 남기지 못했습니다.');
  return null;
}

/* =====================================================================
 * 저장된 스케줄 — 수정 · 검증 · 이미지 저장
 * ===================================================================*/
function viewSchedule(id) {
  const list = Store.get('schedules', []);
  const sc = list.find(s => s.id === id);
  if (!sc) { app().innerHTML = `<div class="card empty"><div class="ic">🔍</div>스케줄을 찾을 수 없습니다.<br><br><a class="btn" href="#/my">내 스케줄로</a></div>`; return; }

  const legacy = !(sc.schemaVersion >= 2);
  const profile = safeProfile(sc.profile);
  let editing = false, dirty = false;
  const work = (sc.jobs || []).map(j => (legacy ? toViewJobLegacy(j) : toViewJob(j)));

  const draw = () => {
    const issues = validateCombo(work, profile);       // 로컬 산술 검증 (겹침 · 이동 · 상한)
    const badDays = new Set(issues.filter(i => i.day).map(i => i.day));
    const local = localWeekly(work);
    /* 근무 시간을 손댄 시간표에는 서버 지표가 더 이상 맞지 않는다. 저장하면 dirty 는
     * 풀리지만 sc.edited 로 남으므로, 다시 열어도 같은 사실을 계속 밝힌다. */
    const editedMetrics = !legacy && (dirty || sc.edited === true);
    app().innerHTML = `
      <div class="page-head"><div class="eyebrow">Saved · ${new Date(sc.createdAt).toLocaleString('ko-KR')}</div><h1>${esc(sc.title)}</h1>
        <p>${esc(profile.role)} · 집 ${esc(profile.home)} · 목표 ${fmtWon(profile.goal)}${sc.planLabel ? ' · ' + esc(sc.planLabel) : ''}</p></div>
      ${sourceNotice(sc.jobSource || null)}
      ${legacy ? `<div style="height:12px"></div><div class="note warn" role="note"><b>이전 버전에서 저장된 시간표입니다.</b><br>
        당시 브라우저에서 계산한 값이라 지금의 서버 계산 결과와 다를 수 있습니다. 시간표와 연락처는 그대로 보존됩니다.
        최신 기준으로 다시 받으려면 <a href="#/">새로 추천받기</a>를 눌러 주세요.</div>` : ''}
      <div style="height:16px"></div>
      <div class="toolbar">
        <button class="btn ${editing ? 'primary' : ''}" id="edit">${editing ? '✓ 수정 완료' : '✏️ 수정'}</button>
        ${dirty ? `<button class="btn primary" id="savechg" ${issues.length ? 'disabled title="경고를 해결해야 저장할 수 있습니다"' : ''}>변경 저장</button>` : ''}
        <div class="sp"></div>
        <label style="font-size:13px;color:var(--muted);display:flex;gap:6px;align-items:center"><input type="checkbox" id="withcontact"> 이미지에 연락처 포함</label>
        <button class="btn" id="img">🖼️ 이미지로 저장</button>
        <button class="btn danger" id="del">삭제</button>
      </div>
      <div class="alerts">${issues.length ? issues.map(i => `<div class="alert">⚠ ${esc(i.msg)}</div>`).join('') : `<div class="alert ok">✓ 시간 겹침 · 이동 · 주간 상한 검증 통과 (브라우저 계산)</div>`}</div>
      ${!legacy && Array.isArray(sc.warnings) && sc.warnings.length ? warningBlock(sc.warnings, profile) : ''}
      ${legacy ? metricsRowLegacy(comboMetrics(work, profile), profile) : metricsRowServer(sc.metrics || {}, profile, { stale: editedMetrics })}
      ${editedMetrics ? `<div class="note warn edited-metrics" style="margin-top:10px">근무 시간을 수정한 시간표입니다${dirty ? '' : ' (수정 내용 저장됨)'}. 위 월 수입·실질 시급은 <b>추천 당시 서버 계산값</b>이며 자동으로 다시 계산하지 않습니다. 지금 시간 기준 주 근무는 ${local.hours.toFixed(1)}시간 · 주급 ${fmtWon(local.pay)}입니다.</div>` : ''}
      <div style="height:16px"></div>
      <div class="card">${renderTimetable(work, profile, { badDays })}</div>
      <div style="height:16px"></div>
      <div class="card">
        <div class="card-title">알바 상세 ${editing ? '<span class="hint">시간을 바꾸거나 삭제하면 즉시 재검증됩니다</span>' : ''}</div>
        ${work.length ? work.map(j => jobItem(j, profile, { controls: editing ? 'edit' : 'none' })).join('') : '<div class="empty">알바가 없습니다.</div>'}
      </div>
      <div class="capture-wrap" id="capwrap"></div>`;

    $('#edit').onclick = () => { editing = !editing; draw(); };
    const sv = $('#savechg');
    if (sv) sv.onclick = () => {
      sc.jobs = work.map(j => toStorageJob(j, legacy));
      sc.edited = true;
      if (!Store.set('schedules', list)) { toast('저장 공간이 부족합니다.'); return; }
      dirty = false; editing = false; toast('변경사항을 저장했습니다.'); draw();
    };
    $('#del').onclick = () => { if (confirm('이 스케줄을 삭제할까요?')) { Store.set('schedules', list.filter(s => s.id !== id)); toast('삭제했습니다.'); navigate('/my'); } };
    $('#img').onclick = () => exportImage(sc, work, profile, $('#withcontact').checked, legacy, editedMetrics);
    app().onclick = e => {
      const rm = e.target.closest('[data-remove]');
      if (rm) { const i = work.findIndex(j => j.id === rm.dataset.remove); if (i >= 0) { work.splice(i, 1); dirty = true; draw(); } }
    };
    app().querySelectorAll('input[data-shift]').forEach(inp => inp.onchange = () => {
      const [jid, idx, f] = inp.dataset.shift.split(':');
      const j = work.find(x => x.id === jid); if (!j) return;
      const s = j.shifts[+idx]; const v = inp.value;
      if (f === 'end' && v === '00:00') s.end = '24:00'; else s[f] = v;
      if (toMin(s.end) <= toMin(s.start)) { toast('종료 시간이 시작보다 빠릅니다.'); s[f] = f === 'start' ? toHM(toMin(s.end) - 60) : toHM(toMin(s.start) + 60); }
      // 시각을 바꾸면 서버가 계산한 이동 정보(출발 권장 시각)는 더 이상 유효하지 않다.
      s.travel = null; s.edited = true;
      dirty = true; draw();
    });
  };
  draw();
}

async function exportImage(sc, jobs, profile, withContact, legacy, editedMetrics = false) {
  const wrap = $('#capwrap');
  const metricCells = legacy
    ? (() => { const m = comboMetrics(jobs, profile); return [
        ['예상 월 수입', fmtWon(m.monthly)], ['목표 달성률', Math.round(m.rate * 100) + '%'],
        ['주 근무 / 이동', `${m.workHours.toFixed(1)}h / ${fmtDur(m.travelMin)}`],
        ['실질 시급', Math.round(m.effectiveWage).toLocaleString('ko-KR') + '원']]; })()
    : (() => { const m = sc.metrics || {}; const at = editedMetrics ? ' (수정 전)' : ''; return [
        ['예상 월 수입' + at, fmtMoney(m.monthlyIncome)], ['목표 달성률' + at, fmtRate(m.targetAchievementRate)],
        ['주 근무 / 이동' + at, `${m.weeklyWorkHours == null ? '—' : m.weeklyWorkHours.toFixed(1) + 'h'} / ${m.weeklyTravelMinutes == null ? '—' : fmtDur(Math.round(m.weeklyTravelMinutes))}`],
        ['실질 시급' + at, m.effectiveHourlyWage == null ? '—' : Math.round(m.effectiveHourlyWage).toLocaleString('ko-KR') + '원']]; })();

  wrap.innerHTML = `<div class="capture" id="capture">
    <h2>${esc(sc.title)}</h2>
    <div class="sub">${esc(profile.role)} · 집 ${esc(profile.home)} · ${new Date(sc.createdAt).toLocaleDateString('ko-KR')} 생성</div>
    ${renderTimetable(jobs, profile, { compact: true })}
    <div class="summary">${metricCells.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join('')}</div>
    <div class="c-jobs">${jobs.map(j => `<div><b style="color:${CATEGORY_COLORS[j.category] || '#94a3b8'}">●</b> ${esc(j.company)} · ${esc(j.title)} · ${esc(j.location)} · 시급 ${j.hourlyWage == null ? '—' : j.hourlyWage.toLocaleString('ko-KR') + '원'}${withContact && j.contact ? ` · ${esc(j.contact.manager || '')} ${esc(j.contact.phone || '')}` : ''}</div>`).join('')}</div>
    <div class="foot">알바 스케줄 추천 · 합성 데모 데이터 기반 · 실제 채용 공고가 아닙니다</div>
  </div>`;
  toast('이미지를 만드는 중…');
  try {
    if (typeof html2canvas !== 'function') throw new Error('html2canvas 미로드 (인터넷 연결 필요)');
    const canvas = await html2canvas($('#capture'), { scale: 2, backgroundColor: '#ffffff', useCORS: true });
    const a = document.createElement('a');
    a.download = `${sc.title.replace(/[\\/:*?"<>|]/g, '_')}.png`;
    a.href = canvas.toDataURL('image/png'); a.click();
    toast('PNG로 저장했습니다.');
  } catch (e) { toast('이미지 저장 실패: ' + e.message); }
}

/* =====================================================================
 * 내 스케줄
 * ===================================================================*/
function viewMy() {
  const list = Store.get('schedules', []);
  app().innerHTML = `
    <div class="page-head"><div class="eyebrow">My Schedules</div><h1>내 스케줄</h1><p>이 브라우저에 저장된 시간표입니다. 로그인 없이 바로 보관됩니다.</p></div>
    ${sourceNotice(null)}
    <div style="height:16px"></div>
    ${list.length ? list.map(s => {
      const legacy = !(s.schemaVersion >= 2);
      const sp = safeProfile(s.profile);
      const jobs = (s.jobs || []).map(j => (legacy ? toViewJobLegacy(j) : toViewJob(j)));
      const issues = validateCombo(jobs, sp);
      const money = legacy ? fmtWon(comboMetrics(jobs, sp).monthly) : fmtMoney((s.metrics || {}).monthlyIncome);
      const sub = legacy
        ? (() => { const m = comboMetrics(jobs, sp); return `목표 ${Math.round(m.rate * 100)}% · 주 ${m.workHours.toFixed(1)}h`; })()
        : (() => { const m = s.metrics || {}; return `목표 ${fmtRate(m.targetAchievementRate)} · 주 ${m.weeklyWorkHours == null ? '—' : m.weeklyWorkHours.toFixed(1) + 'h'}`; })();
      return `
      <a class="card saved-item" href="#/schedule/${s.id}">
        <div><div class="t">${esc(s.title)} ${legacy ? '<span class="badge legacy">이전 형식</span>' : ''} ${issues.length ? '<span class="badge danger">경고 ' + issues.length + '</span>' : ''}</div>
          <div class="m">${esc(s.planLabel || LEGACY_KIND_LABEL[s.kind] || '')} · ${jobs.map(j => esc(j.company)).join(' + ') || '알바 없음'} · ${new Date(s.createdAt).toLocaleDateString('ko-KR')}</div></div>
        <div style="text-align:right"><div style="font-weight:800;font-size:18px">${money}</div><div style="font-size:12px;color:var(--muted)">${sub}</div></div>
      </a>`; }).join('') : `<div class="card empty"><div class="ic">🗂️</div>저장된 스케줄이 없습니다.<br><br><a class="btn primary" href="#/">알바 찾으러 가기</a></div>`}`;
}

/* =====================================================================
 * 모델 변환 — 서버 PlanJob / 옛 저장 데이터 → 화면용 공통 형태
 *
 * 화면용 job:
 *   { id, title, company, category, location, address, hourlyWage, rating,
 *     reviewCount, timeNegotiable, minWeeks, benefits, contact, platform,
 *     descriptionSnippet, sourceUrl, weeklyHours, weeklyPay, pinned,
 *     shifts: [{ day, start, end, travel }], origin }
 * 서버 응답만으로 그려진다 — 로컬 공고 목록에서 jobId 를 찾지 않는다.
 * ===================================================================*/
function toViewJob(planJob) {
  return {
    id: planJob.jobId,
    legacy: false,
    raw: planJob,
    title: planJob.title,
    company: planJob.company,
    category: planJob.category,
    location: planJob.location,
    address: planJob.address,
    platform: planJob.platform,
    descriptionSnippet: planJob.descriptionSnippet,
    sourceUrl: planJob.sourceUrl,
    hourlyWage: planJob.hourlyWage,
    rating: planJob.rating,
    reviewCount: planJob.reviewCount,
    timeNegotiable: planJob.timeNegotiable,
    minWeeks: planJob.minWeeks,
    benefits: planJob.benefits || [],
    contact: planJob.contact,
    pinned: planJob.pinned === true,
    weeklyHours: planJob.weeklyHours,
    weeklyPay: planJob.weeklyPay,
    shifts: (planJob.assignedShifts || []).map(s => ({ day: s.day, start: s.start, end: s.end, travel: s.travel || null })),
  };
}

/* 옛 저장 데이터(js/data.js 기반)로 저장된 스케줄 — 있는 값만 옮긴다. */
function toViewJobLegacy(oldJob) {
  const shifts = (oldJob.shifts || []).map(s => ({ day: s.day, start: s.start, end: s.end, travel: null }));
  const hours = shifts.reduce((a, s) => a + (toMin(s.end) - toMin(s.start)), 0) / 60;
  return {
    id: oldJob.id,
    legacy: true,
    raw: oldJob,
    title: oldJob.title,
    company: oldJob.company,
    category: oldJob.category,
    location: oldJob.location,
    address: oldJob.address,
    platform: oldJob.platform || null,
    descriptionSnippet: oldJob.description || '',
    sourceUrl: oldJob.sourceUrl || null,
    hourlyWage: typeof oldJob.hourlyWage === 'number' ? oldJob.hourlyWage : null,
    rating: typeof oldJob.rating === 'number' ? oldJob.rating : null,
    reviewCount: typeof oldJob.reviewCount === 'number' ? oldJob.reviewCount : null,
    timeNegotiable: typeof oldJob.negotiable === 'boolean' ? oldJob.negotiable : null,
    minWeeks: typeof oldJob.minWeeks === 'number' ? oldJob.minWeeks : null,
    benefits: oldJob.benefits || [],
    contact: oldJob.contact || null,
    pinned: false,
    weeklyHours: hours,
    weeklyPay: typeof oldJob.hourlyWage === 'number' ? hours * oldJob.hourlyWage : null,
    shifts,
  };
}

function toStorageJob(viewJob, legacy) {
  const raw = viewJob.raw || {};
  if (legacy) return { ...raw, shifts: viewJob.shifts.map(s => ({ day: s.day, start: s.start, end: s.end })) };
  return { ...raw, assignedShifts: viewJob.shifts.map(s => ({ day: s.day, start: s.start, end: s.end, travel: s.travel || null })) };
}

/* 순수 산술 — 월 환산 계수를 쓰지 않는다 (월 지표는 서버 값만 쓴다). */
function localWeekly(jobs) {
  let minutes = 0, pay = 0;
  for (const j of jobs) for (const s of j.shifts) {
    const d = toMin(s.end) - toMin(s.start);
    minutes += d;
    if (typeof j.hourlyWage === 'number') pay += d / 60 * j.hourlyWage;
  }
  return { hours: minutes / 60, pay };
}

/* =====================================================================
 * 공용 컴포넌트
 * ===================================================================*/
const fmtMoney = v => (typeof v === 'number' && Number.isFinite(v) ? fmtWon(v) : '—');
const fmtRate = v => (typeof v === 'number' && Number.isFinite(v) ? Math.round(v * 100) + '%' : '—');

const SOURCE_LABEL = { fallback: '서버 규칙 계산', llm: 'LLM' };

/* 서버가 스스로 밝힌 한계(이동시간 추정 방식 · 주휴수당 미포함 등)를 그대로 보여준다. */
function disclosureBlock(list) {
  if (!Array.isArray(list) || !list.length) return '';
  return `<details class="disclosures"><summary>서버가 밝힌 계산 전제 ${list.length}건</summary>
    <ul>${list.map(d => `<li>${esc(d)}</li>`).join('')}</ul></details>`;
}

function syntheticNotice() {
  return `<div class="note warn synth-note" role="note">
    <b>합성 데모 데이터</b> · 실제 채용 공고가 아니며, 지원이나 예약이 확정되지 않습니다. 연락처·링크도 데모용입니다.
  </div>`;
}

/* 서버 응답이 밝힌 공고 출처를 그대로 보여준다.
 *
 * 규칙 세 가지만 지킨다.
 *   - 서버가 출처를 밝히기 전(첫 요청 전 · 출처 없는 옛 저장 데이터)에는 중립 문구.
 *     "합성 데이터"라고 단정하지도, "실제 공고"라고 말하지도 않는다.
 *   - 실제 공고를 합성 데모라고 부르지 않는다.
 *   - 수집(live)과 제공(authorized_import)을 섞지 않는다. 제공분은 실시간 크롤링이 아니다.
 * 데이터 출처는 resp.source(순위를 만든 방식)와 다른 축이므로 따로 표시한다. */
function sourceNotice(jobSource) {
  const js = jobSource && typeof jobSource === 'object' ? jobSource : null;
  const mode = js && js.mode;
  const dataMode = js && js.dataMode;
  if (mode === 'demo_json' && (dataMode === 'demo' || dataMode == null)) return syntheticNotice();
  if (mode === 'public_web' && (dataMode === 'live' || dataMode === 'authorized_import')) {
    const head = dataMode === 'live'
      ? '<b>실제 공개 공고</b> · 운영자가 검토해 수집한 실제 채용 공고로 만든 조합입니다.'
      : '<b>제공된 실제 공고</b> · 권한을 받은 경로로 제공된 실제 채용 공고입니다(실시간 크롤링 아님).';
    return `<div class="note warn source-note live" role="note">
      ${head} 합성 데모 데이터가 아닙니다. 지원·예약이 확정된 것은 아니며, 마감·근무 조건은 원문 공고에서 확인하세요.${sourceCountLine(js.counts)}
    </div>`;
  }
  return `<div class="note info source-note unknown" role="note">
    <b>공고 출처 미표시</b> · 서버가 이 화면의 공고 출처를 밝히지 않았습니다. 실제 공고인지 데모 데이터인지 여기서 단정하지 않습니다.
  </div>`;
}

function sourceCountLine(counts) {
  if (!counts || typeof counts !== 'object') return '';
  const parts = [];
  if (typeof counts.accepted === 'number') parts.push(`검증 통과 ${counts.accepted}건`);
  if (typeof counts.rejected === 'number' && counts.rejected > 0) parts.push(`제외 ${counts.rejected}건`);
  if (typeof counts.walkEstimated === 'number' && counts.walkEstimated > 0) parts.push(`도보 시간 데모 추정 ${counts.walkEstimated}건`);
  if (Array.isArray(counts.providers) && counts.providers.length) parts.push(`출처 ${counts.providers.map(esc).join(', ')}`);
  return parts.length ? `<br><span class="hint">${parts.join(' · ')}</span>` : '';
}

function warningBlock(warnings, profile) {
  if (!warnings || !warnings.length) return '';
  return `<div class="alerts warn-list" role="note">${warnings.map(w => {
    const head = WARNING_LABEL[w.code] || w.code;
    const action = w.code === 'AGE_UNVERIFIED' && !(profile && profile.age)
      ? ' <a href="#/onboarding">내 정보에서 나이 입력</a>' : '';
    return `<div class="alert warn">⚠ <b>${esc(head)}</b> — ${esc(w.message)}${action}</div>`;
  }).join('')}</div><div style="height:12px"></div>`;
}

function metricsRowServer(m, profile, opts = {}) {
  const hold = m.weeklyHolidayPayIncluded;
  return `<div class="metrics-row${opts.stale ? ' stale' : ''}">
    <div class="metric hl"><div class="k">예상 월 수입${opts.stale ? ' (수정 전)' : ''}</div><div class="v">${fmtMoney(m.monthlyIncome)}</div><div class="s">목표 ${fmtWon(profile.goal)}</div></div>
    <div class="metric"><div class="k">목표 달성률</div><div class="v">${fmtRate(m.targetAchievementRate)}</div><div class="s">서버 계산</div></div>
    <div class="metric"><div class="k">주 총 근무시간</div><div class="v">${m.weeklyWorkHours == null ? '—' : m.weeklyWorkHours.toFixed(1) + 'h'}</div><div class="s">${hold === true ? '주휴수당 포함' : hold === false ? '주휴수당 미포함' : '주휴수당 여부 미확인'}</div></div>
    <div class="metric"><div class="k">주 총 이동시간</div><div class="v">${m.weeklyTravelMinutes == null ? '—' : fmtDur(Math.round(m.weeklyTravelMinutes))}</div><div class="s">도보 포함 · 왕복</div></div>
    <div class="metric"><div class="k">실질 시급</div><div class="v">${m.effectiveHourlyWage == null ? '—' : Math.round(m.effectiveHourlyWage).toLocaleString('ko-KR') + '원'}</div><div class="s">급여 ÷ (근무+이동)</div></div>
  </div>`;
}

/* 옛 저장 데이터 전용 — 당시 브라우저 계산값을 그대로 보여준다. */
function metricsRowLegacy(m, profile) {
  return `<div class="metrics-row">
    <div class="metric hl"><div class="k">예상 월 수입</div><div class="v">${fmtWon(m.monthly)}</div><div class="s">목표 ${fmtWon(profile.goal)} · 저장 당시 계산</div></div>
    <div class="metric"><div class="k">목표 달성률</div><div class="v">${Math.round(m.rate * 100)}%</div><div class="s">주급 ${fmtWon(m.weeklyPay)}</div></div>
    <div class="metric"><div class="k">주 총 근무시간</div><div class="v">${m.workHours.toFixed(1)}h</div><div class="s">${m.workHours >= 15 ? '주휴수당 발생 구간' : '주 15h 미만'}</div></div>
    <div class="metric"><div class="k">주 총 이동시간</div><div class="v">${fmtDur(m.travelMin)}</div><div class="s">버퍼 ${TRAVEL_BUFFER}분 별도</div></div>
    <div class="metric"><div class="k">실질 시급</div><div class="v">${Math.round(m.effectiveWage).toLocaleString('ko-KR')}원</div><div class="s">급여 ÷ (근무+이동)</div></div>
  </div>`;
}

function slotsCard(slots) {
  if (!slots || !slots.length) return '';
  return `<div style="height:16px"></div><div class="card">
    <div class="card-title">서버가 계산한 빈 시간 <span class="hint">이 시간 안에서만 배정됩니다</span></div>
    <div class="slot-list">${slots.map(s => `<div class="slot-pill"><b>${DAY_KO[s.day] || s.day}</b>${esc(s.from)} ~ ${esc(s.to)}${s.fromLocation ? ` <span>(${esc(s.fromLocation)} 출발)</span>` : ''}</div>`).join('')}</div>
  </div>`;
}

/* 요일별 (고정 일정 + 배정 근무) 블록. 배정 근무는 assignedShifts 를 그대로 쓴다. */
function buildDayTimeline(jobs, profile, day) {
  const blocks = [];
  const fx = profile && profile.schedule ? profile.schedule[day] : null;
  if (fx && fx.has) blocks.push({ type: 'fixed', start: toMin(fx.start), end: toMin(fx.end), loc: fx.place, label: profile.role === '직장인' ? '근무' : profile.role === '학생' ? '수업' : '고정 일정' });
  for (const j of jobs) for (const s of j.shifts) if (s.day === day)
    blocks.push({ type: 'job', start: toMin(s.start), end: toMin(s.end), loc: j.location, job: j, shift: s });
  blocks.sort((a, b) => a.start - b.start);
  return blocks;
}

/* 편도 이동(분). 서버가 legMinutes(출발지 도보까지 합친 door-to-door)를 주면 그 값을
 * 쓰고, 없을 때만 대중교통+도보를 더한다. 어느 쪽도 없으면 null — 지어내지 않는다. */
function shiftTravelMinutes(shift) {
  const t = shift && shift.travel;
  if (!t) return null;
  if (typeof t.legMinutes === 'number') return t.legMinutes;
  const transit = typeof t.transitMinutes === 'number' ? t.transitMinutes : null;
  const walk = typeof t.walkMinutes === 'number' ? t.walkMinutes : null;
  if (transit === null && walk === null) return null;
  return (transit || 0) + (walk || 0);
}

function renderTimetable(jobs, profile, opts = {}) {
  const ROWH = opts.compact ? 16 : 14;
  const rows = (DAY_END - DAY_START) / 30;
  const y = min => (Math.max(min, DAY_START) - DAY_START) / 30 * ROWH;
  const badDays = opts.badDays || new Set();
  let usedEstimate = false;
  const cols = DAYS.map(day => {
    const tl = buildDayTimeline(jobs, profile, day);
    let prevLoc = profile && profile.home, prevEnd = null;
    const blocks = [];
    for (const b of tl) {
      if (b.type === 'job') {
        const serverMin = shiftTravelMinutes(b.shift);
        const t = serverMin !== null ? serverMin : travelMin(prevLoc, b.loc);
        if (serverMin === null && t > 0) usedEstimate = true;
        if (t > 0) {
          const depart = b.shift && b.shift.travel && b.shift.travel.departAt ? toMin(b.shift.travel.departAt) : null;
          const ts = depart !== null ? depart : Math.max(prevEnd ?? DAY_START, b.start - t);
          if (b.start - ts >= 10) blocks.push(`<div class="tt-block travel" title="${serverMin !== null ? '서버 계산 이동시간' : '브라우저 추정 이동시간'}" style="top:${y(ts)}px;height:${Math.max(4, y(b.start) - y(ts) - 1)}px">이동 ${t}분${serverMin === null ? '*' : ''}</div>`);
        }
        blocks.push(`<div class="tt-block ${badDays.has(day) ? 'warn-block' : ''}" style="top:${y(b.start)}px;height:${Math.max(6, y(b.end) - y(b.start) - 1)}px;background:${CATEGORY_COLORS[b.job.category] || '#64748b'}">
          <div class="b-title">${esc(b.job.company)}</div><div class="b-time">${toHM(b.start)}–${toHM(b.end)} · ${esc(b.loc)}</div></div>`);
      } else {
        blocks.push(`<div class="tt-block fixed" style="top:${y(b.start)}px;height:${Math.max(6, y(b.end) - y(b.start) - 1)}px"><div class="b-title">${b.label}</div><div class="b-time">${toHM(b.start)}–${toHM(b.end)} · ${esc(b.loc)}</div></div>`);
      }
      prevLoc = b.loc; prevEnd = b.end;
    }
    return `<div class="tt-col">${blocks.join('')}</div>`;
  });
  const axis = []; for (let h = 7; h <= 24; h += 1) axis.push(`<span style="top:${(h * 60 - DAY_START) / 30 * ROWH}px">${String(h).padStart(2,'0')}</span>`);
  const cats = [...new Set(jobs.map(j => j.category))].filter(Boolean);
  return `<div class="timetable" style="--rows:${rows};--rowh:${ROWH}px">
    <div class="tt-head"><div></div>${DAYS.map(d => `<div class="${['SAT','SUN'].includes(d) ? 'wknd' : ''}">${DAY_KO[d]}</div>`).join('')}</div>
    <div class="tt-body"><div class="tt-axis">${axis.join('')}</div>${cols.join('')}</div>
    <div class="legend"><span><i class="fixed"></i>고정 일정</span><span><i class="travel"></i>이동시간</span>${cats.map(c => `<span><i style="background:${CATEGORY_COLORS[c] || '#64748b'}"></i>${esc(c)}</span>`).join('')}${usedEstimate ? '<span>* 브라우저 추정 이동시간</span>' : ''}</div>
  </div>`;
}

function jobItem(j, profile, opts = {}) {
  const firstTravel = j.shifts.map(s => s.travel).find(Boolean) || null;
  const serverMin = j.shifts.map(shiftTravelMinutes).find(v => v !== null && v !== undefined);
  const travelText = serverMin != null
    ? `<b>${serverMin}분</b>${firstTravel && firstTravel.fromLocation ? ` (${esc(firstTravel.fromLocation)} 출발${firstTravel.departAt ? ' · ' + esc(firstTravel.departAt) + ' 출발 권장' : ''})` : ''}`
    : `<b>${travelMin(profile && profile.home, j.location)}분</b> (추정 · 집 ${esc(profile && profile.home || '')} 기준)`;
  // 주 근무시간은 배정 시간의 합이라 화면에서 더해도 같은 값이지만, 서버 값이 없을 때는
  // 그 사실을 * 로 밝힌다. 주급은 서버가 계산하는 지표이므로 대신 곱하지 않는다.
  const serverHours = typeof j.weeklyHours === 'number';
  const hours = serverHours ? j.weeklyHours : localWeekly([j]).hours;
  const payValue = typeof j.weeklyPay === 'number' ? j.weeklyPay : null;
  const c = j.contact;
  const editable = opts.controls === 'edit';
  return `<div class="job-item">
    <div class="bar" style="background:${CATEGORY_COLORS[j.category] || '#64748b'}"></div>
    <div>
      <div class="jt">${esc(j.title)} ${opts.pinned ? '<span class="badge pin">📌 고정됨</span>' : ''}</div>
      <div class="jc">${esc(j.company)}${j.platform ? ' · ' + esc(j.platform) : ''} · ${esc(j.category)} · ${esc(j.location)}${j.address ? ' · ' + esc(j.address) : ''}</div>
      ${j.descriptionSnippet ? `<div class="jc" style="margin-top:4px">${esc(j.descriptionSnippet)}</div>` : ''}
      <div class="js">
        <span>시급 <b>${j.hourlyWage == null ? '—' : j.hourlyWage.toLocaleString('ko-KR') + '원'}</b></span>
        <span>이동 ${travelText}</span>
        <span>주 <b>${hours.toFixed(1)}h${serverHours ? '' : '<span title="서버 값이 없어 배정 시간을 더한 값입니다">*</span>'}</b></span>
        <span>주급 <b>${fmtMoney(payValue)}</b></span>
        <span>최소 <b>${j.minWeeks == null ? '기간 미확인' : j.minWeeks + '주'}</b></span>
      </div>
      <div class="shifts">${j.shifts.length ? j.shifts.map((s, i) => editable
        ? `<span class="shift"><b>${DAY_KO[s.day] || s.day}</b><input type="time" data-shift="${esc(j.id)}:${i}:start" value="${s.start}"> ~ <input type="time" data-shift="${esc(j.id)}:${i}:end" value="${s.end === '24:00' ? '00:00' : s.end}"></span>`
        : `<span class="shift"><b>${DAY_KO[s.day] || s.day}</b> ${esc(s.start)} ~ ${esc(s.end)}${s.travel && s.travel.departAt ? ` <span style="color:var(--muted)">· ${esc(s.travel.departAt)} 출발</span>` : ''}</span>`).join('')
        : '<span class="shift">배정된 근무 시간이 없습니다</span>'}</div>
      ${j.benefits && j.benefits.length ? `<div class="js" style="margin-top:6px"><span style="color:var(--muted)">${j.benefits.map(esc).join(' · ')}</span></div>` : ''}
      ${c ? `<div class="contact">
        <span class="badge legacy">데모 연락처</span>
        ${c.manager ? `<span>👤 ${esc(c.manager)}</span>` : ''}
        ${c.phone ? `<span>📞 ${esc(c.phone)}</span>` : ''}
        ${c.preferred ? `<span style="color:var(--muted)">선호 연락 ${esc(c.preferred)}</span>` : ''}
        ${c.applyUrl ? `<span style="color:var(--muted)">지원 링크 ${esc(c.applyUrl)}</span>` : ''}
        <span style="color:var(--muted)">합성 데이터라 실제로 연결되지 않습니다.</span>
      </div>` : ''}
    </div>
    <div class="side">
      ${j.rating == null ? '' : `<span class="badge rating">★ ${j.rating.toFixed(1)}${j.reviewCount == null ? '' : ' (' + j.reviewCount + ')'}</span>`}
      ${j.timeNegotiable === true ? '<span class="badge neg">시간 협의 가능</span>' : ''}
      ${opts.controls === 'result' ? `<button class="btn sm ${opts.pinned ? 'primary' : ''}" data-pin="${esc(j.id)}">📌 ${opts.pinned ? '고정 해제' : '고정'}</button><button class="btn sm" data-exclude="${esc(j.id)}">✕ 제외</button>` : ''}
      ${editable ? `<button class="btn sm danger" data-remove="${esc(j.id)}">삭제</button>` : ''}
    </div>
  </div>`;
}

function modal(html, after) {
  const bg = document.createElement('div'); bg.className = 'modal-bg'; bg.id = 'modalbg';
  bg.innerHTML = `<div class="modal">${html}</div>`;
  document.body.appendChild(bg);
  bg.addEventListener('click', e => { if (e.target === bg || e.target.closest('[data-close]')) closeModal(); });
  const inp = bg.querySelector('input'); if (inp) { inp.focus(); inp.select(); }
  after && after();
}
function closeModal() { const m = $('#modalbg'); if (m) m.remove(); }

/* Node 테스트에서 렌더 함수만 떼어 쓰기 위한 내보내기 (브라우저에서는 무시) */
if (typeof module === 'object' && module.exports) {
  module.exports = {
    toViewJob, toViewJobLegacy, toStorageJob, localWeekly, renderTimetable, jobItem,
    warningBlock, metricsRowServer, slotsCard, buildDayTimeline, syntheticNotice,
    sourceNotice, sourceCountLine,
    disclosureBlock, shiftTravelMinutes, safeProfile,
    regeneratePayload, togglePin, excludeJob, mergeSeenPlanIds, MAX_SEEN_PLAN_IDS,
  };
}
