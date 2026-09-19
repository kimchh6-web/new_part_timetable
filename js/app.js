/* =====================================================================
 * app.js — 해시 라우터 + 화면 (온보딩 / 홈 / 결과 / 스케줄 / 내 스케줄)
 * ===================================================================*/

/* ---------- 저장소 ---------- */
const Store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
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
  set(v) { sessionStorage.setItem('result', JSON.stringify(v)); },
};

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
window.addEventListener('hashchange', router);
window.addEventListener('DOMContentLoaded', router);

/* =====================================================================
 * 온보딩
 * ===================================================================*/
const ROLE_LABEL = { '학생': '수업', '직장인': '근무', '기타': '일정' };

function defaultProfile() {
  const schedule = {};
  for (const d of DAYS) schedule[d] = { has: false, start: '', end: '', place: '' };
  return { role: '', sameDaily: false, schedule, home: '', minBlock: 3, night: true, want15: false, goal: 0 };
}

function viewOnboarding() {
  const existing = Store.get('profile');
  const ob = existing ? JSON.parse(JSON.stringify(existing)) : defaultProfile();
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
    <div class="toggle ${ob.night ? 'on' : ''}" id="night"><div><div class="t-label">야간(22시 이후) 근무 가능</div><div class="t-sub">끄면 22시 넘어 끝나는 공고를 제외합니다.</div></div><div class="switch"></div></div>
    <div class="toggle ${ob.want15 ? 'on' : ''}" id="want15"><div><div class="t-label">주 15시간 이상 근무 희망</div><div class="t-sub">주 15시간 이상 일하면 주휴수당이 발생해 실수령이 늘어납니다.</div></div><div class="switch"></div></div>`;

  const stepGoal = () => `
    <div class="step-title">이번 달 목표 금액</div>
    <div class="step-desc">목표를 기준으로 필요한 근무시간과 알바 개수를 역산합니다.</div>
    <div class="field"><label class="small">목표 금액 (원)</label>
      <input type="number" id="goal" step="10000" min="0" value="${ob.goal || ''}" placeholder="예: 800000" style="font-size:22px;font-weight:800;height:56px"></div>
    <div class="goal-calc" id="goalcalc"></div>
    <div class="note" style="margin-top:12px">2026년 최저시급 ${MIN_WAGE.toLocaleString()}원 · 월 ${WEEKS_PER_MONTH}주 기준</div>`;

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
      if (step === 3 && !ob.home) { toast('집 위치를 선택해 주세요.'); return; }
      if (step === 4) {
        if (!(ob.goal > 0)) { toast('목표 금액을 입력해 주세요.'); return; }
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
  const slots = computeFreeSlots(profile);
  const lbl = ROLE_LABEL[profile.role];
  const needH = hoursNeededForGoal(profile.goal);

  app().innerHTML = `
    <div class="hero">
      <div class="eyebrow" style="color:#c7d2fe">Part-time Scheduler</div>
      <h1>남는 시간과 이동 동선까지 계산한<br>알바 조합을 추천합니다</h1>
      <p>고정 일정이 끝나는 위치에서 실제로 도착 가능한 공고만 골라, 겹치지 않는 조합을 주간 시간표로 만들어 드립니다.</p>
      <div class="pts"><span>이동 가능성 반영</span><span>조합 최적화</span><span>목표 금액 역산</span><span>실질 시급 계산</span></div>
    </div>
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
          ${(Store.get('history', []) || []).length ? `<div style="margin-top:10px;text-align:center"><a class="btn ghost sm" href="#/result">🕘 이전 추천 결과 ${Store.get('history', []).length}건 보기</a></div>` : ''}
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
          </div>
          <div class="sched-mini">${DAYS.map(d => { const s = profile.schedule[d]; return `<div class="d ${s.has ? 'on' : ''}">${DAY_KO[d]}<small>${s.has ? parseInt(s.start) + '~' + parseInt(s.end) + '시' : '없음'}</small></div>`; }).join('')}</div>
          <div style="font-size:12px;opacity:.7;margin-top:8px">${lbl} 종료 장소: ${[...new Set(DAYS.filter(d => profile.schedule[d].has).map(d => profile.schedule[d].place))].join(', ') || '—'}</div>
        </div>
      </div>
    </div>
    <div class="slot-bar"><div class="slot-bar-inner">
      <div class="ttl">빈 시간<br><span style="font-weight:500">${slots.length}개 슬롯</span></div>
      <div class="slot-list">${slots.length ? slots.map(s => `<div class="slot-pill ${s.allDay ? 'allday' : ''}"><b>${DAY_KO[s.day]}</b>${s.allDay ? '종일' : toHM(s.start) + ' ~ ' + toHM(s.end)} <span>(${s.label})</span></div>`).join('') : '<div class="slot-pill">조건에 맞는 빈 시간이 없습니다. 최소 연속 근무 시간을 줄여 보세요.</div>'}</div>
    </div></div>`;

  document.querySelectorAll('#count button').forEach(b => b.onclick = () => { search.count = +b.dataset.v; viewHomeRefresh(); });
  document.querySelectorAll('#cats .chip').forEach(c => c.onclick = () => { const v = c.dataset.v; search.categories = search.categories.includes(v) ? search.categories.filter(x => x !== v) : [...search.categories, v]; viewHomeRefresh(); });
  document.querySelectorAll('#prio button').forEach(b => b.onclick = () => { search.priority = b.dataset.v; viewHomeRefresh(); });
  function viewHomeRefresh() { Store.set('lastSearch', search); viewHome(); }
  $('#go').onclick = () => {
    Store.set('lastSearch', search);
    const candidates = filterCandidates(JOBS, profile, search);
    if (!candidates.length) { toast('조건에 맞는 공고가 없습니다. 카테고리를 넓히거나 제약을 완화해 보세요.'); return; }
    Session.set({ search, pinned: [], excluded: [], selected: 0, plans: null });
    navigate('/result');
  };
}

/* =====================================================================
 * 결과 — 3안 카드 → 시간표 → 상세
 * ===================================================================*/
function viewResult() {
  const profile = Store.get('profile');
  let st = Session.get();
  const history = () => Store.get('history', []);
  if (!st) {
    const h = history();
    if (!h.length) return navigate('/');
    st = fromHistory(h[0]); Session.set(st);
  }
  let candidates = filterCandidates(JOBS, profile, st.search);
  function fromHistory(h) { return { search: h.search, pinned: h.pinned || [], excluded: h.excluded || [], selected: 0, plans: h.plans, historyId: h.id }; }

  const run = () => {
    candidates = filterCandidates(JOBS, profile, st.search);
    // 스켈레톤 (LLM 응답 지연 대비 UI — 현재는 코드 엔진이 즉시 계산)
    app().innerHTML = `
      <div class="page-head"><div class="eyebrow">Result</div><h1>추천 조합을 계산하고 있습니다…</h1><p>빈 슬롯 → 이동 가능 후보 ${candidates.length}건 → 겹치지 않는 조합 검증</p></div>
      <div class="grid-3">${[0,1,2].map(() => `<div class="card"><div class="skeleton sk-line" style="width:40%"></div><div class="skeleton sk-line" style="width:70%;height:28px"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line" style="width:80%"></div></div>`).join('')}</div>`;
    setTimeout(() => {
      const plans = generatePlans(candidates, profile, st.search, { pinned: st.pinned, excluded: st.excluded });
      st.plans = plans.map(p => ({ ...p, jobIds: p.jobs.map(j => j.id), jobs: undefined }));
      st.selected = Math.min(st.selected || 0, Math.max(0, st.plans.length - 1));
      if (st.plans.length) {
        const entry = { id: 'h' + Date.now().toString(36), createdAt: new Date().toISOString(), search: st.search, pinned: st.pinned, excluded: st.excluded, plans: st.plans, candidateCount: candidates.length };
        Store.set('history', [entry, ...history()].slice(0, 30));
        st.historyId = entry.id;
      }
      Session.set(st);
      draw();
    }, 700);
  };

  const planJobs = p => p.jobIds.map(id => JOBS.find(j => j.id === id));
  const KIND = { income: '수입 최대안', ease: '여유 우선안', balance: '밸런스안' };
  const PRIO = { wage: '시급', distance: '거리', rating: '평점', flex: '유연성' };

  const sidebar = () => {
    const h = history();
    return `<aside class="hist">
      <div class="hist-head"><b>추천 결과 기록</b><span>${h.length}건</span>${h.length ? '<button class="btn ghost sm" id="hist-clear">전체 삭제</button>' : ''}</div>
      ${h.length ? h.map((e, i) => {
        const best = e.plans[0]; const bm = best.metrics;
        const d = new Date(e.createdAt);
        return `<div class="hist-item ${e.id === st.historyId ? 'on' : ''}" data-hid="${e.id}">
          <div class="hi-top"><span class="hi-no">#${h.length - i}</span><span class="hi-time">${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}</span><button class="hi-del" data-hdel="${e.id}" title="삭제">✕</button></div>
          <div class="hi-money">${fmtWon(bm.monthly)} <small>${Math.round(bm.rate * 100)}%</small></div>
          <div class="hi-jobs">${best.jobIds.map(id => { const j = JOBS.find(x => x.id === id); return j ? `<span style="border-left:3px solid ${CATEGORY_COLORS[j.category]}">${esc(j.company)}</span>` : ''; }).join('')}</div>
          <div class="hi-cond">${e.search.count}개 · ${PRIO[e.search.priority] || ''} 우선 · ${e.search.categories.length ? e.search.categories.length + '개 카테고리' : '전체'}${(e.pinned || []).length ? ' · 📌' + e.pinned.length : ''}${(e.excluded || []).length ? ' · ✕' + e.excluded.length : ''}</div>
        </div>`; }).join('') : '<div class="hist-empty">아직 기록이 없습니다.<br>추천을 생성하면 여기에 쌓입니다.</div>'}
    </aside>`;
  };
  const bindSidebar = () => {
    document.querySelectorAll('.hist-item').forEach(el => el.onclick = e => {
      if (e.target.closest('[data-hdel]')) return;
      const entry = history().find(x => x.id === el.dataset.hid); if (!entry) return;
      st = fromHistory(entry); Session.set(st); draw();
    });
    document.querySelectorAll('[data-hdel]').forEach(b => b.onclick = e => {
      e.stopPropagation();
      const id = b.dataset.hdel; const rest = history().filter(x => x.id !== id); Store.set('history', rest);
      if (st.historyId === id) { if (rest.length) { st = fromHistory(rest[0]); Session.set(st); draw(); } else { sessionStorage.removeItem('result'); navigate('/'); } }
      else draw();
    });
    const clr = $('#hist-clear'); if (clr) clr.onclick = () => { Store.set('history', []); st.historyId = null; draw(); toast('기록을 모두 지웠습니다.'); };
  };

  const draw = () => {
    if (!st.plans.length) {
      app().innerHTML = `<div class="result-layout">${sidebar()}<div><div class="page-head"><h1>조합을 만들 수 없습니다</h1></div>
        <div class="card empty"><div class="ic">🧩</div>고정 목록·제외 목록 조건에서 겹치지 않는 조합이 없습니다.<br><br>
        <button class="btn" id="reset">고정·제외 초기화</button> <a class="btn ghost" href="#/">조건 다시 설정</a></div></div></div>`;
      $('#reset').onclick = () => { st.pinned = []; st.excluded = []; Session.set(st); run(); };
      bindSidebar();
      return;
    }
    const sel = st.plans[st.selected];
    const jobs = planJobs(sel);
    const m = comboMetrics(jobs, profile);
    const hIdx = history().findIndex(x => x.id === st.historyId);
    const hNo = hIdx >= 0 ? history().length - hIdx : null;

    app().innerHTML = `<div class="result-layout">${sidebar()}<div>
      <div class="page-head"><div class="eyebrow">Result${hNo ? ' · 기록 #' + hNo : ''} · 후보 ${candidates.length}건 중 조합</div><h1>추천 조합 ${st.plans.length}안</h1><p>카드를 눌러 비교하고, 마음에 드는 안을 시간표로 확인하세요.</p></div>
      <div class="grid-3">
        ${st.plans.map((p, i) => { const pm = p.metrics; return `
          <div class="card plan-card ${p.kind} ${i === st.selected ? 'on' : ''}" data-i="${i}">
            <div class="check">✓</div>
            <span class="tag">${KIND[p.kind]}</span>
            <div class="money">${fmtWon(pm.monthly)}</div>
            <div class="rate">예상 월 수입 · 목표 대비 <b>${Math.round(pm.rate * 100)}%</b></div>
            <div class="progress"><div style="width:${Math.min(100, pm.rate * 100)}%"></div></div>
            <div class="jobs-line">${p.jobIds.map(id => `<span>${esc(JOBS.find(j => j.id === id).company)}</span>`).join('')}</div>
            <div class="mini-metrics">
              <div class="mm"><div class="k">주 근무</div><div class="v">${pm.workHours.toFixed(1)}h</div></div>
              <div class="mm"><div class="k">주 이동</div><div class="v">${fmtDur(pm.travelMin)}</div></div>
              <div class="mm"><div class="k">실질 시급</div><div class="v">${Math.round(pm.effectiveWage).toLocaleString()}원</div></div>
            </div>
            <div class="reason">${esc(p.reason)}</div>
          </div>`; }).join('')}
      </div>

      <div style="height:24px"></div>
      <div class="toolbar">
        <h2 style="font-size:20px">${KIND[sel.kind]} 주간 시간표</h2>
        <div class="sp"></div>
        ${st.pinned.length || st.excluded.length ? `<span class="badge pin">고정 ${st.pinned.length} · 제외 ${st.excluded.length}</span>` : ''}
        <button class="btn" id="regen">🔄 재생성${st.pinned.length ? ' (고정 유지)' : ''}</button>
        <button class="btn primary" id="save">💾 이 시간표 저장</button>
      </div>
      ${metricsRow(m, profile)}
      <div style="height:16px"></div>
      <div class="card">${renderTimetable(jobs, profile)}</div>
      <div style="height:16px"></div>
      <div class="card">
        <div class="card-title">알바 상세 <span class="hint">📌 고정: 이 알바는 유지하고 나머지만 재생성 · ✕ 제외: 빼고 다시</span></div>
        ${jobs.map(j => jobItem(j, profile, { pinned: st.pinned.includes(j.id), controls: 'result' })).join('')}
      </div>
    </div></div>`;

    bindSidebar();
    document.querySelectorAll('.plan-card').forEach(c => c.onclick = () => { st.selected = +c.dataset.i; Session.set(st); draw(); });
    $('#regen').onclick = () => run();
    $('#save').onclick = () => saveDialog(jobs, sel.kind);
    app().onclick = e => {
      const pin = e.target.closest('[data-pin]'); const ex = e.target.closest('[data-exclude]');
      if (pin) { const id = pin.dataset.pin; st.pinned = st.pinned.includes(id) ? st.pinned.filter(x => x !== id) : [...st.pinned, id]; st.excluded = st.excluded.filter(x => x !== id); Session.set(st); draw(); toast(st.pinned.includes(id) ? '고정했습니다. 재생성 시 유지됩니다.' : '고정을 해제했습니다.'); }
      if (ex) { const id = ex.dataset.exclude; st.excluded = [...new Set([...st.excluded, id])]; st.pinned = st.pinned.filter(x => x !== id); Session.set(st); run(); toast('제외하고 다시 생성합니다.'); }
    };
  };

  const saveDialog = (jobs, kind) => {
    const KIND = { income: '수입 최대안', ease: '여유 우선안', balance: '밸런스안' };
    const def = `${new Date().getMonth() + 1}월 ${KIND[kind]}`;
    modal(`<h3>시간표 저장</h3><div class="field"><label class="small">제목</label><input type="text" id="mtitle" value="${esc(def)}"></div>
      <div class="actions"><button class="btn ghost" data-close>취소</button><button class="btn primary" id="mok">저장</button></div>`, () => {
      $('#mok').onclick = () => {
        const list = Store.get('schedules', []);
        const id = 's' + Date.now().toString(36);
        list.unshift({ id, title: $('#mtitle').value.trim() || def, createdAt: new Date().toISOString(), kind, search: st.search,
          profile: JSON.parse(JSON.stringify(profile)), jobs: JSON.parse(JSON.stringify(jobs)) });
        Store.set('schedules', list); closeModal(); toast('저장되었습니다. 로그인 없이 이 브라우저에 보관됩니다.'); navigate('/schedule/' + id);
      };
    });
  };

  if (st.plans) draw(); else run();
}

/* =====================================================================
 * 저장된 스케줄 — 수정 · 검증 · 이미지 저장
 * ===================================================================*/
function viewSchedule(id) {
  const list = Store.get('schedules', []);
  const sc = list.find(s => s.id === id);
  if (!sc) { app().innerHTML = `<div class="card empty"><div class="ic">🔍</div>스케줄을 찾을 수 없습니다.<br><br><a class="btn" href="#/my">내 스케줄로</a></div>`; return; }
  const profile = sc.profile;
  let editing = false, dirty = false;
  const work = JSON.parse(JSON.stringify(sc.jobs));

  const draw = () => {
    const m = comboMetrics(work, profile);
    const issues = validateCombo(work, profile);
    const badDays = new Set(issues.filter(i => i.day).map(i => i.day));
    app().innerHTML = `
      <div class="page-head"><div class="eyebrow">Saved · ${new Date(sc.createdAt).toLocaleString('ko-KR')}</div><h1>${esc(sc.title)}</h1>
        <p>${profile.role} · 집 ${profile.home} · 목표 ${fmtWon(profile.goal)}</p></div>
      <div class="toolbar">
        <button class="btn ${editing ? 'primary' : ''}" id="edit">${editing ? '✓ 수정 완료' : '✏️ 수정'}</button>
        ${dirty ? `<button class="btn primary" id="savechg" ${issues.length ? 'disabled title="경고를 해결해야 저장할 수 있습니다"' : ''}>변경 저장</button>` : ''}
        <div class="sp"></div>
        <label style="font-size:13px;color:var(--muted);display:flex;gap:6px;align-items:center"><input type="checkbox" id="withcontact"> 이미지에 연락처 포함</label>
        <button class="btn" id="img">🖼️ 이미지로 저장</button>
        <button class="btn danger" id="del">삭제</button>
      </div>
      <div class="alerts">${issues.length ? issues.map(i => `<div class="alert">⚠ ${esc(i.msg)}</div>`).join('') : `<div class="alert ok">✓ 시간 겹침 · 이동 · 주간 상한 검증 통과</div>`}</div>
      ${metricsRow(m, profile)}
      <div style="height:16px"></div>
      <div class="card">${renderTimetable(work, profile, { badDays })}</div>
      <div style="height:16px"></div>
      <div class="card">
        <div class="card-title">알바 상세 ${editing ? '<span class="hint">시간을 바꾸거나 삭제하면 즉시 재검증됩니다</span>' : ''}</div>
        ${work.length ? work.map(j => jobItem(j, profile, { controls: editing ? 'edit' : 'none' })).join('') : '<div class="empty">알바가 없습니다.</div>'}
      </div>
      <div class="capture-wrap" id="capwrap"></div>`;

    $('#edit').onclick = () => { editing = !editing; draw(); };
    const sv = $('#savechg'); if (sv) sv.onclick = () => { sc.jobs = JSON.parse(JSON.stringify(work)); Store.set('schedules', list); dirty = false; editing = false; toast('변경사항을 저장했습니다.'); draw(); };
    $('#del').onclick = () => { if (confirm('이 스케줄을 삭제할까요?')) { Store.set('schedules', list.filter(s => s.id !== id)); toast('삭제했습니다.'); navigate('/my'); } };
    $('#img').onclick = () => exportImage(sc, work, profile, $('#withcontact').checked);
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
      dirty = true; draw();
    });
  };
  draw();
}

async function exportImage(sc, jobs, profile, withContact) {
  const m = comboMetrics(jobs, profile);
  const wrap = $('#capwrap');
  wrap.innerHTML = `<div class="capture" id="capture">
    <h2>${esc(sc.title)}</h2>
    <div class="sub">${profile.role} · 집 ${profile.home} · ${new Date(sc.createdAt).toLocaleDateString('ko-KR')} 생성</div>
    ${renderTimetable(jobs, profile, { compact: true })}
    <div class="summary">
      <div><div class="k">예상 월 수입</div><div class="v">${fmtWon(m.monthly)}</div></div>
      <div><div class="k">목표 달성률</div><div class="v">${Math.round(m.rate * 100)}%</div></div>
      <div><div class="k">주 근무 / 이동</div><div class="v">${m.workHours.toFixed(1)}h / ${fmtDur(m.travelMin)}</div></div>
      <div><div class="k">실질 시급</div><div class="v">${Math.round(m.effectiveWage).toLocaleString()}원</div></div>
    </div>
    <div class="c-jobs">${jobs.map(j => `<div><b style="color:${CATEGORY_COLORS[j.category]}">●</b> ${esc(j.company)} · ${esc(j.title)} · ${j.location} · 시급 ${j.hourlyWage.toLocaleString()}원${withContact ? ` · ${esc(j.contact.manager)} ${j.contact.phone}` : ''}</div>`).join('')}</div>
    <div class="foot">알바 스케줄 추천 · 목데이터 기반 데모</div>
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
  const KIND = { income: '수입 최대안', ease: '여유 우선안', balance: '밸런스안' };
  app().innerHTML = `
    <div class="page-head"><div class="eyebrow">My Schedules</div><h1>내 스케줄</h1><p>이 브라우저에 저장된 시간표입니다. 로그인 없이 바로 보관됩니다.</p></div>
    ${list.length ? list.map(s => { const m = comboMetrics(s.jobs, s.profile); const issues = validateCombo(s.jobs, s.profile); return `
      <a class="card saved-item" href="#/schedule/${s.id}">
        <div><div class="t">${esc(s.title)} ${issues.length ? '<span class="badge danger">경고 ' + issues.length + '</span>' : ''}</div>
          <div class="m">${KIND[s.kind] || ''} · ${s.jobs.map(j => esc(j.company)).join(' + ') || '알바 없음'} · ${new Date(s.createdAt).toLocaleDateString('ko-KR')}</div></div>
        <div style="text-align:right"><div style="font-weight:800;font-size:18px">${fmtWon(m.monthly)}</div><div style="font-size:12px;color:var(--muted)">목표 ${Math.round(m.rate * 100)}% · 주 ${m.workHours.toFixed(1)}h</div></div>
      </a>`; }).join('') : `<div class="card empty"><div class="ic">🗂️</div>저장된 스케줄이 없습니다.<br><br><a class="btn primary" href="#/">알바 찾으러 가기</a></div>`}`;
}

/* =====================================================================
 * 공용 컴포넌트
 * ===================================================================*/
function metricsRow(m, profile) {
  return `<div class="metrics-row">
    <div class="metric hl"><div class="k">예상 월 수입</div><div class="v">${fmtWon(m.monthly)}</div><div class="s">목표 ${fmtWon(profile.goal)}</div></div>
    <div class="metric"><div class="k">목표 달성률</div><div class="v">${Math.round(m.rate * 100)}%</div><div class="s">주급 ${fmtWon(m.weeklyPay)}</div></div>
    <div class="metric"><div class="k">주 총 근무시간</div><div class="v">${m.workHours.toFixed(1)}h</div><div class="s">${m.workHours >= 15 ? '주휴수당 발생 구간' : '주 15h 미만'}</div></div>
    <div class="metric"><div class="k">주 총 이동시간</div><div class="v">${fmtDur(m.travelMin)}</div><div class="s">버퍼 ${TRAVEL_BUFFER}분 별도</div></div>
    <div class="metric"><div class="k">실질 시급</div><div class="v">${Math.round(m.effectiveWage).toLocaleString()}원</div><div class="s">급여 ÷ (근무+이동)</div></div>
  </div>`;
}

function renderTimetable(jobs, profile, opts = {}) {
  const ROWH = opts.compact ? 16 : 14;
  const rows = (DAY_END - DAY_START) / 30;
  const y = min => (Math.max(min, DAY_START) - DAY_START) / 30 * ROWH;
  const badDays = opts.badDays || new Set();
  const cols = DAYS.map(day => {
    const tl = dayTimeline(jobs, profile, day);
    let prevLoc = profile.home, prevEnd = null;
    const blocks = [];
    for (const b of tl) {
      if (b.type === 'job') {
        const t = travelMin(prevLoc, b.loc);
        if (t > 0) {
          const ts = Math.max(prevEnd ?? DAY_START, b.start - t);
          if (b.start - ts >= 10) blocks.push(`<div class="tt-block travel" style="top:${y(ts)}px;height:${Math.max(4, y(b.start) - y(ts) - 1)}px">이동 ${t}분</div>`);
        }
        blocks.push(`<div class="tt-block ${badDays.has(day) ? 'warn-block' : ''}" style="top:${y(b.start)}px;height:${Math.max(6, y(b.end) - y(b.start) - 1)}px;background:${CATEGORY_COLORS[b.job.category]}">
          <div class="b-title">${esc(b.job.company)}</div><div class="b-time">${toHM(b.start)}–${toHM(b.end)} · ${b.loc}</div></div>`);
      } else {
        blocks.push(`<div class="tt-block fixed" style="top:${y(b.start)}px;height:${Math.max(6, y(b.end) - y(b.start) - 1)}px"><div class="b-title">${b.label}</div><div class="b-time">${toHM(b.start)}–${toHM(b.end)} · ${b.loc}</div></div>`);
      }
      prevLoc = b.loc; prevEnd = b.end;
    }
    return `<div class="tt-col">${blocks.join('')}</div>`;
  });
  const axis = []; for (let h = 7; h <= 24; h += 1) axis.push(`<span style="top:${(h * 60 - DAY_START) / 30 * ROWH}px">${String(h).padStart(2,'0')}</span>`);
  const cats = [...new Set(jobs.map(j => j.category))];
  return `<div class="timetable" style="--rows:${rows};--rowh:${ROWH}px">
    <div class="tt-head"><div></div>${DAYS.map(d => `<div class="${['SAT','SUN'].includes(d) ? 'wknd' : ''}">${DAY_KO[d]}</div>`).join('')}</div>
    <div class="tt-body"><div class="tt-axis">${axis.join('')}</div>${cols.join('')}</div>
    <div class="legend"><span><i class="fixed"></i>고정 일정</span><span><i class="travel"></i>이동시간</span>${cats.map(c => `<span><i style="background:${CATEGORY_COLORS[c]}"></i>${c}</span>`).join('')}</div>
  </div>`;
}

function jobItem(j, profile, opts = {}) {
  const wk = j.shifts.reduce((a, s) => a + (toMin(s.end) - toMin(s.start)), 0) / 60;
  const origin = (() => { const s = j.shifts[0]; const fx = profile.schedule[s.day]; return fx && fx.has && toMin(s.start) >= toMin(fx.end) ? fx.place : profile.home; })();
  const t = travelMin(origin, j.location);
  const c = j.contact;
  const editable = opts.controls === 'edit';
  return `<div class="job-item">
    <div class="bar" style="background:${CATEGORY_COLORS[j.category]}"></div>
    <div>
      <div class="jt">${esc(j.title)} ${opts.pinned ? '<span class="badge pin">📌 고정됨</span>' : ''}</div>
      <div class="jc">${esc(j.company)} · ${j.category} · ${j.location} · ${esc(j.address)}</div>
      <div class="js"><span>시급 <b>${j.hourlyWage.toLocaleString()}원</b></span><span>이동 <b>${t}분</b> (${origin} 출발)</span><span>주 <b>${wk.toFixed(1)}h</b></span><span>주급 <b>${fmtWon(wk * j.hourlyWage)}</b></span><span>최소 <b>${j.minWeeks}주</b></span></div>
      <div class="shifts">${j.shifts.map((s, i) => editable
        ? `<span class="shift"><b>${DAY_KO[s.day]}</b><input type="time" data-shift="${j.id}:${i}:start" value="${s.start}"> ~ <input type="time" data-shift="${j.id}:${i}:end" value="${s.end === '24:00' ? '00:00' : s.end}"></span>`
        : `<span class="shift"><b>${DAY_KO[s.day]}</b> ${s.start} ~ ${s.end}</span>`).join('')}</div>
      <div class="contact">
        <span>👤 ${esc(c.manager)}</span>
        <a class="tel" href="tel:${c.phone.replace(/-/g, '')}">📞 ${c.phone}</a>
        ${c.kakao ? `<a class="btn sm" href="${esc(c.kakao)}" target="_blank" rel="noopener">💬 카카오톡 채널</a>` : ''}
        ${c.applyUrl ? `<a class="btn sm" href="${esc(c.applyUrl)}" target="_blank" rel="noopener">🔗 지원 링크</a>` : ''}
        <span style="color:var(--muted)">${j.benefits.map(b => '· ' + esc(b)).join(' ')}</span>
      </div>
    </div>
    <div class="side">
      <span class="badge rating">★ ${j.rating.toFixed(1)} (${j.reviewCount})</span>
      ${j.negotiable ? '<span class="badge neg">시간 협의 가능</span>' : ''}
      ${opts.controls === 'result' ? `<button class="btn sm ${opts.pinned ? 'primary' : ''}" data-pin="${j.id}">📌 ${opts.pinned ? '고정 해제' : '고정'}</button><button class="btn sm" data-exclude="${j.id}">✕ 제외</button>` : ''}
      ${editable ? `<button class="btn sm danger" data-remove="${j.id}">삭제</button>` : ''}
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
