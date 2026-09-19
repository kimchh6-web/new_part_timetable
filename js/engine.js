/* =====================================================================
 * engine.js — 계산은 코드가 담당 (빈 슬롯 · 후보 필터 · 조합 · 검증)
 * ===================================================================*/

const WEEKS_PER_MONTH = 4.345;
const DAY_START = 7 * 60;   // 시간표 표시 시작 07:00
const DAY_END = 24 * 60;    // 24:00
const NIGHT_CUTOFF = 22 * 60;
const WEEKLY_CAP_HOURS = 40; // 주간 근무시간 상한(고정 일정 제외)

const toMin = t => { const [h, m] = t.split(':').map(Number); return h * 60 + m; };
const toHM = m => `${String(Math.floor(m / 60)).padStart(2,'0')}:${String(m % 60).padStart(2,'0')}`;
const fmtWon = n => Math.round(n).toLocaleString('ko-KR') + '원';
const fmtDur = m => m >= 60 ? `${Math.floor(m/60)}시간${m%60 ? ' '+m%60+'분' : ''}` : `${m}분`;

/* ---------- 1. 빈 슬롯 계산 ---------- */
function computeFreeSlots(profile) {
  const slots = [];
  const minBlock = (profile.minBlock || 2) * 60;
  const dayEnd = profile.night ? DAY_END : NIGHT_CUTOFF;
  for (const day of DAYS) {
    const s = profile.schedule[day];
    if (s && s.has) {
      const st = toMin(s.start), en = toMin(s.end);
      // 일정 전 아침 슬롯 (집 출발)
      if (st - DAY_START >= minBlock) {
        slots.push({ day, start: DAY_START, end: st, origin: profile.home, allDay: false, label: '집 출발' });
      }
      // 일정 후 슬롯 (종료 장소 출발)
      if (dayEnd - en >= minBlock) {
        slots.push({ day, start: en, end: dayEnd, origin: s.place, allDay: false, label: `${s.place} 출발` });
      }
    } else {
      slots.push({ day, start: DAY_START, end: dayEnd, origin: profile.home, allDay: true, label: '집 출발' });
    }
  }
  return slots;
}

/* ---------- 2. 후보 필터 (시간 겹침 + 이동 가능) ---------- */
function shiftFitsSlot(shift, slot, jobLoc, profile) {
  if (shift.day !== slot.day) return false;
  const ss = toMin(shift.start), se = toMin(shift.end);
  if (!profile.night && se > NIGHT_CUTOFF) return false;
  const arrive = slot.start + (slot.allDay ? 0 : travelMin(slot.origin, jobLoc) + TRAVEL_BUFFER);
  return ss >= arrive && se <= slot.end;
}

function filterCandidates(jobs, profile, search) {
  const slots = computeFreeSlots(profile);
  const minBlock = (profile.minBlock || 2) * 60;
  return jobs.filter(job => {
    if (search.categories.length && !search.categories.includes(job.category)) return false;
    return job.shifts.every(sh => {
      if (toMin(sh.end) - toMin(sh.start) < minBlock) return false;
      return slots.some(sl => shiftFitsSlot(sh, sl, job.location, profile));
    });
  });
}

/* ---------- 3. 조합 검증 (겹침 · 이동 · 상한) ---------- */
function weeklyHours(jobs) {
  return jobs.reduce((a, j) => a + j.shifts.reduce((b, s) => b + (toMin(s.end) - toMin(s.start)), 0), 0) / 60;
}

function dayTimeline(jobs, profile, day) {
  // 해당 요일의 (고정일정 + 알바) 블록을 시간순 정렬
  const blocks = [];
  const fx = profile.schedule[day];
  if (fx && fx.has) blocks.push({ type:'fixed', start: toMin(fx.start), end: toMin(fx.end), loc: fx.place, label: profile.role === '직장인' ? '근무' : profile.role === '학생' ? '수업' : '고정 일정' });
  for (const j of jobs) for (const s of j.shifts) if (s.day === day)
    blocks.push({ type:'job', start: toMin(s.start), end: toMin(s.end), loc: j.location, job: j });
  blocks.sort((a, b) => a.start - b.start);
  return blocks;
}

function validateCombo(jobs, profile) {
  const issues = [];
  for (const day of DAYS) {
    const tl = dayTimeline(jobs, profile, day);
    let prevLoc = profile.home, prevEnd = null, prevLabel = '집';
    for (const b of tl) {
      if (prevEnd !== null && b.start < prevEnd) {
        issues.push({ type:'overlap', day, msg:`${DAY_KO[day]} ${toHM(b.start)} — 이전 일정(${prevLabel})과 시간이 겹침` });
      } else if (prevEnd !== null) {
        const need = travelMin(prevLoc, b.loc) + TRAVEL_BUFFER;
        if (b.start - prevEnd < need) {
          issues.push({ type:'travel', day, msg:`${DAY_KO[day]} ${prevLabel} 종료 후 ${b.loc} 도착 불가 (필요 ${need}분, 여유 ${b.start - prevEnd}분)` });
        }
      }
      prevLoc = b.loc; prevEnd = b.end; prevLabel = b.type === 'fixed' ? b.label : b.job.company;
    }
  }
  const wh = weeklyHours(jobs);
  if (wh > WEEKLY_CAP_HOURS) issues.push({ type:'cap', msg:`주간 근무시간 ${wh.toFixed(1)}h — 상한 ${WEEKLY_CAP_HOURS}h 초과` });
  if (!profile.night) {
    for (const j of jobs) for (const s of j.shifts) if (toMin(s.end) > NIGHT_CUTOFF)
      issues.push({ type:'night', msg:`${j.company} ${DAY_KO[s.day]} ${s.end} 종료 — 야간 근무 불가 설정` });
  }
  return issues;
}

/* ---------- 4. 지표 ---------- */
function comboMetrics(jobs, profile) {
  const workMin = jobs.reduce((a, j) => a + j.shifts.reduce((b, s) => b + (toMin(s.end) - toMin(s.start)), 0), 0);
  const weeklyPay = jobs.reduce((a, j) => a + j.shifts.reduce((b, s) => b + (toMin(s.end) - toMin(s.start)) / 60 * j.hourlyWage, 0), 0);
  // 이동시간: 각 요일 타임라인에서 이전 위치 → 알바 위치 편도 + 마지막 알바 → 집 귀가
  let travel = 0;
  for (const day of DAYS) {
    const tl = dayTimeline(jobs, profile, day);
    let prevLoc = profile.home;
    let hadJob = false;
    for (const b of tl) {
      if (b.type === 'job') { travel += travelMin(prevLoc, b.loc); hadJob = true; }
      prevLoc = b.loc;
    }
    if (hadJob) travel += travelMin(prevLoc, profile.home);
  }
  const monthly = weeklyPay * WEEKS_PER_MONTH;
  const rate = profile.goal ? monthly / profile.goal : 0;
  const effective = workMin + travel > 0 ? weeklyPay / ((workMin + travel) / 60) : 0;
  const avgRating = jobs.length ? jobs.reduce((a, j) => a + j.rating, 0) / jobs.length : 0;
  const avgWage = workMin ? weeklyPay / (workMin / 60) : 0;
  return {
    weeklyPay, monthly, rate,
    workHours: workMin / 60, travelMin: travel,
    effectiveWage: effective, avgRating, avgWage,
    blocks: jobs.reduce((a, j) => a + j.shifts.length, 0),
  };
}

/* ---------- 5. 조합 열거 & 3안 산출 ---------- */
function combinations(arr, k) {
  const out = [];
  const rec = (start, cur) => {
    if (cur.length === k) { out.push([...cur]); return; }
    for (let i = start; i < arr.length; i++) { cur.push(arr[i]); rec(i + 1, cur); cur.pop(); }
  };
  rec(0, []);
  return out;
}

function reasonFor(kind, m, jobs, profile, priority) {
  const pct = Math.round(m.rate * 100);
  const names = jobs.map(j => j.company).join(' + ');
  if (kind === 'income') return `${names} 조합으로 월 ${fmtWon(m.monthly)} 예상, 목표의 ${pct}%를 채웁니다. 주 ${m.workHours.toFixed(1)}시간 근무로 수입을 최대화한 안입니다.`;
  if (kind === 'ease') return `주 이동시간이 ${fmtDur(m.travelMin)}으로 가장 짧고 근무 블록이 ${m.blocks}개뿐이라 체력 부담이 적습니다. 실질 시급 ${fmtWon(m.effectiveWage)}.`;
  const pr = { wage:'시급', distance:'거리', rating:'평점', flex:'시간 유연성' }[priority] || '균형';
  const extra = priority === 'rating' ? `평균 평점 ${m.avgRating.toFixed(1)}점` : priority === 'wage' ? `평균 시급 ${fmtWon(m.avgWage)}` : priority === 'distance' ? `주 이동 ${fmtDur(m.travelMin)}` : `협의 가능 공고 위주`;
  return `"${pr}"을 우선해 고른 조합입니다. ${extra}, 목표 달성률 ${pct}%로 수입과 여유의 균형을 맞췄습니다.`;
}

function generatePlans(candidates, profile, search, opts = {}) {
  const pinned = (opts.pinned || []).map(id => candidates.find(j => j.id === id) || JOBS.find(j => j.id === id)).filter(Boolean);
  const excluded = new Set(opts.excluded || []);
  const pool = candidates.filter(j => !excluded.has(j.id) && !pinned.some(p => p.id === j.id));
  const k = Math.max(1, Math.min(3, search.count || 2));
  const need = Math.max(0, k - pinned.length);

  // 후보 상위 20건으로 축소 (기획서 9-4: 토큰 절감 원칙을 코드에도 적용)
  const scored = pool.map(j => ({ j, s: j.hourlyWage / 1000 + j.rating * 2 - travelMin(profile.home, j.location) / 20 }))
    .sort((a, b) => b.s - a.s).slice(0, 20).map(x => x.j);

  let combos = need === 0 ? [[...pinned]] : combinations(scored, need).map(c => [...pinned, ...c]);
  // 검증 통과 조합만
  const valid = combos.map(c => ({ jobs: c, m: comboMetrics(c, profile), issues: validateCombo(c, profile) }))
    .filter(x => x.issues.length === 0);
  if (!valid.length) return [];

  const usedKeys = new Set();
  const keyOf = c => c.jobs.map(j => j.id).sort().join('|');
  const pickBest = (scoreFn, kind) => {
    const sorted = [...valid].sort((a, b) => scoreFn(b) - scoreFn(a));
    let chosen = sorted.find(c => !usedKeys.has(keyOf(c))) || sorted[0];
    usedKeys.add(keyOf(chosen));
    return { kind, jobs: chosen.jobs, metrics: chosen.m, reason: reasonFor(kind, chosen.m, chosen.jobs, profile, search.priority) };
  };

  const goal = profile.goal || 0;
  const income = pickBest(c => c.m.monthly - (c.m.monthly > goal * 1.6 ? (c.m.monthly - goal * 1.6) * 0.5 : 0), 'income');
  const ease = pickBest(c => -c.m.travelMin * 3 - c.m.blocks * 20 + Math.min(c.m.rate, 1) * 200, 'ease');
  const balScore = c => {
    const base = Math.min(c.m.rate, 1.2) * 100 - c.m.travelMin / 5;
    switch (search.priority) {
      case 'wage': return base + c.m.avgWage / 100;
      case 'distance': return base - c.m.travelMin;
      case 'rating': return base + c.m.avgRating * 40;
      case 'flex': return base + c.jobs.filter(j => j.negotiable).length * 40 - c.jobs.reduce((a, j) => a + j.minWeeks, 0);
      default: return base;
    }
  };
  const balance = pickBest(balScore, 'balance');
  return [income, ease, balance];
}

/* ---------- 6. 최저시급 기준 필요 시간 ---------- */
function hoursNeededForGoal(goal) {
  if (!goal) return 0;
  return goal / MIN_WAGE / WEEKS_PER_MONTH;
}
