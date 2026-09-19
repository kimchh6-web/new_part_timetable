/* =====================================================================
 * api-client.js — POST /api/recommendations 클라이언트
 *
 * 역할 세 가지만 담당한다.
 *   1) 화면 입력(온보딩 프로필 + 탐색 조건) → 계약서 Request 로 변환
 *   2) 서버 응답 → 화면이 그대로 그릴 수 있는 형태로 정규화 (값은 서버 것만)
 *   3) 에러 응답·네트워크 실패 → 사용자에게 보여줄 수 있는 에러 객체로 변환
 *
 * 계약: polish-coordination/weekly-contract.md
 *       + PlanJob 추가 필드(timeNegotiable / minWeeks / benefits)
 *
 * 여기서는 지표를 계산하지 않는다. monthlyIncome·weeklyWorkHours 등은
 * 전부 서버 값이며, 없으면 null 로 두고 화면에서 "—" 로 표시한다.
 * ===================================================================*/
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.WeeklyApi = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  'use strict';

  const ENDPOINT = '/api/recommendations';
  const TIMEOUT_MS = 30000;              // 계약 7-3: 클라이언트 타임아웃 30초
  const MAX_TARGET_AMOUNT = 10000000;

  const DAY_ORDER = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
  const VALID_DAYS = new Set(DAY_ORDER);
  const VALID_MIN_BLOCK = [2, 3, 4];
  const VALID_PRIORITIES = new Set(['wage', 'distance', 'rating', 'flexibility']);
  const VALID_PLAN_TYPES = new Set(['maxIncome', 'minTravel', 'balanced']);

  /* 화면에서 쓰던 우선순위 키 → 계약 enum */
  const PRIORITY_MAP = { wage: 'wage', distance: 'distance', rating: 'rating', flex: 'flexibility', flexibility: 'flexibility' };

  /* ---------- 에러 ---------- */
  class ApiClientError extends Error {
    constructor(code, message, extra = {}) {
      super(message);
      this.name = 'ApiClientError';
      this.code = code;
      this.status = extra.status ?? null;
      this.details = extra.details ?? null;
      this.suggestions = extra.suggestions ?? [];
      this.retryable = extra.retryable ?? false;
    }
  }

  /* ---------- 작은 유틸 ---------- */
  const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  const str = (v, d = '') => (typeof v === 'string' ? v : d);
  const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const bool = v => (typeof v === 'boolean' ? v : null);
  const arr = v => (Array.isArray(v) ? v : []);

  const HHMM = /^([01]?\d|2[0-4]):([0-5]\d)$/;
  function normTime(v) {
    const m = HHMM.exec(str(v).trim());
    if (!m) return null;
    return String(m[1]).padStart(2, '0') + ':' + m[2];
  }
  function toMinutes(hhmm) {
    const t = normTime(hhmm);
    if (!t) return null;
    const [h, m] = t.split(':').map(Number);
    return h * 60 + m;
  }

  /* =====================================================================
   * 1. Request 어댑터 — 화면 모델 → 계약 Request
   *
   * 화면 프로필(js/app.js 온보딩 저장 형태)
   *   { role, home, minBlock, night, want15, goal, age?,
   *     schedule: { MON: { has, start, end, place }, ... } }
   * 탐색 조건
   *   { count, categories, priority }   // priority 는 'flex' 를 쓸 수 있음
   * ===================================================================*/
  function buildRequest(profile, search, regenerate) {
    const p = isObj(profile) ? profile : {};
    const s = isObj(search) ? search : {};
    const sched = isObj(p.schedule) ? p.schedule : {};

    const fixedSchedules = [];
    for (const day of DAY_ORDER) {
      const row = sched[day];
      if (!isObj(row) || !row.has) continue;
      const start = normTime(row.start);
      const end = normTime(row.end);
      if (!start || !end) continue;                 // 미입력 행은 보내지 않는다
      fixedSchedules.push({ day, start, end, endLocation: str(row.place) });
    }

    const minBlockHours = VALID_MIN_BLOCK.includes(Number(p.minBlock)) ? Number(p.minBlock) : 2;

    const constraints = {
      minBlockHours,
      allowNight: !!p.night,
      wantWeeklyHolidayPay: !!p.want15,
    };
    // 나이는 선택 입력. 값이 없으면 필드 자체를 빼고, 서버가 AGE_UNVERIFIED 로 알려준다.
    const age = Number(p.age);
    if (Number.isFinite(age) && age > 0) constraints.age = Math.round(age);

    const categories = [...new Set(arr(s.categories).filter(c => typeof c === 'string' && c))];
    const jobCountRaw = Number(s.count ?? s.jobCount);
    const jobCount = Number.isFinite(jobCountRaw) ? Math.min(3, Math.max(1, Math.round(jobCountRaw))) : 2;

    const req = {
      profile: {
        role: str(p.role),
        home: str(p.home),
        fixedSchedules,
        constraints,
        targetAmount: Math.round(Number(p.goal) || 0),
      },
      search: {
        jobCount,
        categories,
        priority: PRIORITY_MAP[str(s.priority)] || 'wage',
      },
    };

    const r = isObj(regenerate) ? regenerate : {};
    const pinnedJobIds = [...new Set(arr(r.pinned ?? r.pinnedJobIds).filter(x => typeof x === 'string' && x))];
    const excludedJobIds = [...new Set(arr(r.excluded ?? r.excludedJobIds).filter(x => typeof x === 'string' && x))];
    const previousPlanHashes = [...new Set(arr(r.previousPlanHashes).filter(x => typeof x === 'string' && x))];
    if (pinnedJobIds.length || excludedJobIds.length || previousPlanHashes.length) {
      req.regenerate = { pinnedJobIds, excludedJobIds, previousPlanHashes };
    }
    return req;
  }

  /* 네트워크에 나가기 전 로컬 검증. 서버 검증 규칙과 같은 항목만 본다. */
  function validateRequest(req) {
    const problems = [];
    const p = isObj(req) && isObj(req.profile) ? req.profile : {};
    const s = isObj(req) && isObj(req.search) ? req.search : {};

    if (!p.role) problems.push({ code: 'VALIDATION_ERROR', field: 'role', message: '신분을 선택해 주세요.' });
    if (!p.home) problems.push({ code: 'VALIDATION_ERROR', field: 'home', message: '집 위치를 선택해 주세요.' });

    const amount = Number(p.targetAmount);
    if (!Number.isFinite(amount) || amount < 1 || amount > MAX_TARGET_AMOUNT) {
      problems.push({ code: 'VALIDATION_ERROR', field: 'targetAmount', message: `목표 금액은 1원 이상 ${MAX_TARGET_AMOUNT.toLocaleString('ko-KR')}원 이하로 입력해 주세요.` });
    }

    const seen = new Set();
    for (const f of arr(p.fixedSchedules)) {
      if (!VALID_DAYS.has(f.day)) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', message: '고정 일정 요일이 올바르지 않습니다.' });
        continue;
      }
      if (seen.has(f.day)) problems.push({ code: 'SCHEDULE_CONFLICT', field: 'fixedSchedules', message: '같은 요일에 고정 일정이 두 번 있습니다.' });
      seen.add(f.day);
      const st = toMinutes(f.start), en = toMinutes(f.end);
      if (st === null || en === null || en <= st) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', message: '고정 일정의 종료 시간이 시작보다 빠릅니다.' });
      }
      if (!f.endLocation) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', message: '고정 일정이 끝나는 장소를 선택해 주세요.' });
      }
    }

    if (![1, 2, 3].includes(Number(s.jobCount))) {
      problems.push({ code: 'VALIDATION_ERROR', field: 'jobCount', message: '알바 개수는 1~3개 중에서 고를 수 있습니다.' });
    }
    if (!VALID_PRIORITIES.has(s.priority)) {
      problems.push({ code: 'VALIDATION_ERROR', field: 'priority', message: '우선순위 값이 올바르지 않습니다.' });
    }

    const pinned = isObj(req) && isObj(req.regenerate) ? arr(req.regenerate.pinnedJobIds) : [];
    if (pinned.length > Number(s.jobCount || 0)) {
      problems.push({ code: 'PINNED_EXCEEDS_COUNT', field: 'pinnedJobIds', message: `고정한 알바가 ${pinned.length}개인데 추천 개수는 ${s.jobCount}개입니다. 고정을 줄이거나 개수를 늘려 주세요.` });
    }
    return problems;
  }

  /* =====================================================================
   * 2. Response 어댑터 — 없는 값은 만들지 않는다 (null 로 둔다)
   * ===================================================================*/
  function adaptTravel(raw) {
    if (!isObj(raw)) return null;
    return {
      fromLocation: str(raw.fromLocation) || null,
      transitMinutes: num(raw.transitMinutes),
      walkMinutes: num(raw.walkMinutes),
      bufferMinutes: num(raw.bufferMinutes),
      departAt: normTime(raw.departAt),
    };
  }

  function adaptShift(raw) {
    if (!isObj(raw)) return null;
    const day = str(raw.day).toUpperCase();
    const start = normTime(raw.start);
    const end = normTime(raw.end);
    if (!VALID_DAYS.has(day) || !start || !end) return null;
    return { day, start, end, travel: adaptTravel(raw.travel) };
  }

  function adaptContact(raw) {
    if (!isObj(raw)) return null;
    return {
      manager: str(raw.manager) || null,
      phone: str(raw.phone) || null,
      kakao: str(raw.kakao) || null,
      applyUrl: str(raw.applyUrl) || null,
      preferred: str(raw.preferred) || null,
    };
  }

  function adaptJob(raw) {
    if (!isObj(raw)) return null;
    const jobId = str(raw.jobId);
    if (!jobId) return null;
    const assignedShifts = arr(raw.assignedShifts).map(adaptShift).filter(Boolean);
    return {
      jobId,
      pinned: raw.pinned === true,
      title: str(raw.title),
      company: str(raw.company),
      platform: str(raw.platform),
      category: str(raw.category),
      location: str(raw.location),
      address: str(raw.address),
      hourlyWage: num(raw.hourlyWage),
      rating: num(raw.rating),
      reviewCount: num(raw.reviewCount),
      thumbnail: str(raw.thumbnail) || null,
      descriptionSnippet: str(raw.descriptionSnippet),
      sourceUrl: str(raw.sourceUrl) || null,
      contact: adaptContact(raw.contact),
      timeNegotiable: bool(raw.timeNegotiable),   // 계약에 없으면 null → 배지를 걸지 않는다
      minWeeks: num(raw.minWeeks),
      benefits: arr(raw.benefits).filter(b => typeof b === 'string' && b),
      assignedShifts,
      weeklyHours: num(raw.weeklyHours),
      weeklyPay: num(raw.weeklyPay),
    };
  }

  function adaptMetrics(raw) {
    const m = isObj(raw) ? raw : {};
    return {
      monthlyIncome: num(m.monthlyIncome),
      targetAchievementRate: num(m.targetAchievementRate),
      weeklyWorkHours: num(m.weeklyWorkHours),
      weeklyTravelMinutes: num(m.weeklyTravelMinutes),
      effectiveHourlyWage: num(m.effectiveHourlyWage),
      weeklyHolidayPayIncluded: bool(m.weeklyHolidayPayIncluded),
    };
  }

  function adaptWarning(raw) {
    if (!isObj(raw)) return null;
    const message = str(raw.message);
    const code = str(raw.code);
    if (!message && !code) return null;
    return { code: code || 'UNKNOWN', message: message || code, jobId: str(raw.jobId) || null };
  }

  function adaptPlan(raw) {
    if (!isObj(raw)) return null;
    const id = str(raw.id);
    const jobs = arr(raw.jobs).map(adaptJob).filter(Boolean);
    if (!id || !jobs.length) return null;
    const type = str(raw.type);
    return {
      id,
      type: VALID_PLAN_TYPES.has(type) ? type : 'balanced',
      label: str(raw.label) || '추천안',
      reason: str(raw.reason),
      metrics: adaptMetrics(raw.metrics),
      jobs,
      warnings: arr(raw.warnings).map(adaptWarning).filter(Boolean),
    };
  }

  function adaptSlot(raw) {
    if (!isObj(raw)) return null;
    const day = str(raw.day).toUpperCase();
    const from = normTime(raw.from);
    const to = normTime(raw.to);
    if (!VALID_DAYS.has(day) || !from || !to) return null;
    return { day, from, to, fromLocation: str(raw.fromLocation) || null };
  }

  /**
   * 응답 본문 → 화면용 모델.
   * 구조가 깨졌으면(plans 배열이 없거나 쓸 수 있는 안이 하나도 없으면)
   * MALFORMED_RESPONSE 로 던진다. 조용히 다른 데이터로 대체하지 않는다.
   */
  function adaptResponse(raw) {
    if (!isObj(raw)) {
      throw new ApiClientError('MALFORMED_RESPONSE', '서버 응답을 읽을 수 없습니다.', { retryable: true });
    }
    if (isObj(raw.error)) throw adaptError({ body: raw });
    if (!Array.isArray(raw.plans)) {
      throw new ApiClientError('MALFORMED_RESPONSE', '서버 응답에 추천안(plans)이 없습니다.', { retryable: true });
    }
    const plans = raw.plans.map(adaptPlan).filter(Boolean);
    const droppedPlans = raw.plans.length - plans.length;
    if (!plans.length) {
      throw new ApiClientError('MALFORMED_RESPONSE', '서버가 보낸 추천안을 표시할 수 없습니다.', {
        retryable: true,
        details: { receivedPlans: raw.plans.length },
      });
    }
    const source = raw.source === 'fallback' ? 'fallback' : 'llm';
    return {
      requestId: str(raw.requestId) || null,
      generatedAt: str(raw.generatedAt) || null,
      source,
      availableSlots: arr(raw.availableSlots).map(adaptSlot).filter(Boolean),
      candidateCount: num(raw.candidateCount),
      plans,
      droppedPlans,
      meta: isObj(raw.meta) ? raw.meta : null,
    };
  }

  /* =====================================================================
   * 3. 에러 어댑터
   * ===================================================================*/
  const ERROR_TEXT = {
    VALIDATION_ERROR: { title: '입력값을 확인해 주세요.', retryable: false },
    SCHEDULE_CONFLICT: { title: '고정 일정이 서로 겹칩니다.', retryable: false },
    PINNED_EXCEEDS_COUNT: { title: '고정한 알바가 추천 개수보다 많습니다.', retryable: false },
    NO_CANDIDATES: { title: '조건에 맞는 공고를 찾지 못했습니다.', retryable: false },
    TIMEOUT: { title: '서버가 제때 답하지 못했습니다.', retryable: true },
    CLIENT_TIMEOUT: { title: '30초 안에 응답을 받지 못했습니다.', retryable: true },
    NETWORK: { title: '서버에 연결하지 못했습니다.', retryable: true },
    MALFORMED_RESPONSE: { title: '서버 응답 형식이 올바르지 않습니다.', retryable: true },
    SERVER_ERROR: { title: '서버에서 오류가 발생했습니다.', retryable: true },
  };

  /**
   * input: { status?, body?, cause? } 또는 이미 만들어진 ApiClientError
   */
  function adaptError(input) {
    if (input instanceof ApiClientError) return input;

    const status = input && typeof input.status === 'number' ? input.status : null;
    const body = input && isObj(input.body) ? input.body : null;
    const err = body && isObj(body.error) ? body.error : null;

    if (err) {
      const code = str(err.code) || 'SERVER_ERROR';
      const known = ERROR_TEXT[code] || ERROR_TEXT.SERVER_ERROR;
      const details = isObj(err.details) ? err.details : null;
      return new ApiClientError(code, str(err.message) || known.title, {
        status,
        details,
        suggestions: details ? arr(details.suggestions).filter(isObj) : [],
        retryable: known.retryable,
      });
    }

    const cause = input && input.cause;
    if (cause) {
      const name = str(cause.name);
      if (name === 'AbortError' || name === 'TimeoutError') {
        return new ApiClientError('CLIENT_TIMEOUT', ERROR_TEXT.CLIENT_TIMEOUT.title, { retryable: true });
      }
      return new ApiClientError('NETWORK', ERROR_TEXT.NETWORK.title, { retryable: true, details: { reason: cause.message || name } });
    }

    if (status && status >= 500) {
      return new ApiClientError('SERVER_ERROR', `${ERROR_TEXT.SERVER_ERROR.title} (HTTP ${status})`, { status, retryable: true });
    }
    if (status) {
      return new ApiClientError('SERVER_ERROR', `요청이 거부되었습니다. (HTTP ${status})`, { status, retryable: false });
    }
    return new ApiClientError('NETWORK', ERROR_TEXT.NETWORK.title, { retryable: true });
  }

  /* =====================================================================
   * 4. 호출
   * ===================================================================*/
  async function postRecommendations(request, opts = {}) {
    const fetchImpl = opts.fetch || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
    if (!fetchImpl) throw new ApiClientError('NETWORK', '이 브라우저에서 요청을 보낼 수 없습니다.', { retryable: false });

    const endpoint = opts.endpoint || ENDPOINT;
    const timeoutMs = opts.timeoutMs ?? TIMEOUT_MS;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;

    let res;
    try {
      res = await fetchImpl(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify(request),
        signal: controller ? controller.signal : undefined,
      });
    } catch (cause) {
      throw adaptError({ cause });
    } finally {
      if (timer) clearTimeout(timer);
    }

    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }

    if (!res.ok) throw adaptError({ status: res.status, body });
    return adaptResponse(body);
  }

  /** 프로필·조건 → 요청 → 정규화된 응답. 실패 시 ApiClientError 를 던진다. */
  async function requestRecommendations(profile, search, regenerate, opts = {}) {
    const request = buildRequest(profile, search, regenerate);
    const problems = validateRequest(request);
    if (problems.length) {
      throw new ApiClientError(problems[0].code, problems[0].message, {
        retryable: false,
        details: { problems },
      });
    }
    const response = await postRecommendations(request, opts);
    return { request, response };
  }

  /** 이미 본 조합 id — 재생성 시 previousPlanHashes 로 보낸다. */
  function planIds(response) {
    if (!isObj(response)) return [];
    return arr(response.plans).map(p => str(p && p.id)).filter(Boolean);
  }

  return {
    ENDPOINT,
    TIMEOUT_MS,
    PRIORITY_MAP,
    ApiClientError,
    buildRequest,
    validateRequest,
    adaptResponse,
    adaptError,
    postRecommendations,
    requestRecommendations,
    planIds,
    _internals: { normTime, adaptPlan, adaptJob, adaptShift, adaptSlot },
  };
});
