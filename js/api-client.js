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

  const DAY_KO = { MON: '월', TUE: '화', WED: '수', THU: '목', FRI: '금', SAT: '토', SUN: '일' };

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
  /**
   * "9:00" → "09:00". 범위를 벗어난 값은 고쳐 주지 않고 null 을 돌려준다.
   * 24시는 하루의 끝(24:00)만 뜻이 있다. 24:01~24:59 는 시각이 아니므로 거부한다.
   */
  function normTime(v) {
    const m = HHMM.exec(str(v).trim());
    if (!m) return null;
    const hh = String(m[1]).padStart(2, '0');
    if (hh === '24' && m[2] !== '00') return null;
    return hh + ':' + m[2];
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

    // 켜져 있는 행은 값이 잘못돼 있어도 빼지 않는다. 조용히 빼면 제약이 사라진
    // 요청이 나가고, 사용자는 지키지 못할 시간표를 받는다. 시각을 읽을 수 없으면
    // null 로 남겨 validateRequest 가 요일까지 짚어 막게 한다.
    const fixedSchedules = [];
    for (const day of DAY_ORDER) {
      const row = sched[day];
      if (!isObj(row) || !row.has) continue;
      fixedSchedules.push({
        day,
        start: normTime(row.start),
        end: normTime(row.end),
        endLocation: str(row.place),
      });
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
      const ko = DAY_KO[f.day];
      if (seen.has(f.day)) problems.push({ code: 'SCHEDULE_CONFLICT', field: 'fixedSchedules', day: f.day, message: `${ko}요일에 고정 일정이 두 번 있습니다.` });
      seen.add(f.day);
      const st = toMinutes(f.start), en = toMinutes(f.end);
      if (st === null || en === null) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', day: f.day, message: `${ko}요일 고정 일정의 시작·종료 시간을 00:00~24:00 형식으로 입력해 주세요.` });
      } else if (en <= st) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', day: f.day, message: `${ko}요일 고정 일정의 종료 시간이 시작보다 빠릅니다.` });
      }
      if (!f.endLocation) {
        problems.push({ code: 'VALIDATION_ERROR', field: 'fixedSchedules', day: f.day, message: `${ko}요일 고정 일정이 끝나는 장소를 선택해 주세요.` });
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
      // 서버가 door-to-door 편도를 직접 계산해 주면(출발지 도보 포함) 그 값이 정답이다.
      originWalkMinutes: num(raw.originWalkMinutes),
      legMinutes: num(raw.legMinutes),
      slackMinutes: num(raw.slackMinutes),
      bufferMinutes: num(raw.bufferMinutes),
      departAt: normTime(raw.departAt),
    };
  }

  /** 근무 한 칸. 하나라도 읽을 수 없으면 null — 호출부가 그 안을 통째로 버린다. */
  function adaptShift(raw) {
    if (!isObj(raw)) return null;
    const day = str(raw.day).toUpperCase();
    const start = normTime(raw.start);
    const end = normTime(raw.end);
    if (!VALID_DAYS.has(day) || !start || !end) return null;
    if (toMinutes(end) <= toMinutes(start)) return null;
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

  /**
   * 공고 한 건. 배정 근무가 하나라도 깨져 있으면 null 을 돌려준다.
   * 깨진 칸만 빼고 그리면 화면의 시간표와 서버가 값을 매긴 시간표가 달라지는데,
   * 지표(monthlyIncome 등)는 서버 값 그대로 남으므로 "이 시간표로 이 돈"이라는
   * 거짓말이 된다. 그래서 부분 폐기 대신 안 전체를 버린다.
   */
  function adaptJob(raw) {
    if (!isObj(raw)) return null;
    const jobId = str(raw.jobId);
    if (!jobId) return null;
    const rawShifts = arr(raw.assignedShifts);
    const assignedShifts = rawShifts.map(adaptShift);
    if (!rawShifts.length || assignedShifts.some(s => s === null)) return null;
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
      holidayPay: isObj(raw.holidayPay) ? {
        includedInIncome: bool(raw.holidayPay.includedInIncome),
        employerHoursThresholdMet: bool(raw.holidayPay.employerHoursThresholdMet),
        postingClaimsWeeklyHolidayPay: bool(raw.holidayPay.postingClaimsWeeklyHolidayPay),
      } : null,
      unverifiedQualifications: arr(raw.unverifiedQualifications).filter(q => typeof q === 'string' && q),
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
    const rawJobs = arr(raw.jobs);
    const jobs = rawJobs.map(adaptJob);
    // 공고 한 건이라도 읽을 수 없으면 이 안은 쓰지 않는다. metrics 는 그 공고까지
    // 합쳐 계산된 값이라, 빠진 채로 그리면 지표와 시간표가 어긋난다.
    if (!id || !rawJobs.length || jobs.some(j => j === null)) return null;
    const type = str(raw.type);
    return {
      id,
      hash: str(raw.hash) || null,
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
  /**
   * 공고 데이터의 출처. 서버가 밝힌 것만 그대로 옮기고, 모르면 mode=null 로 둔다.
   * 여기서 기본값을 'demo_json' 으로 채우면 실제 공고를 데모라고 부르게 되므로
   * 절대 채우지 않는다.
   */
  const JOB_SOURCES = new Set(['demo_json', 'public_web']);
  const DATA_MODES = new Set(['demo', 'live', 'authorized_import']);

  function adaptJobSource(meta) {
    if (!isObj(meta)) return { mode: null, dataMode: null, counts: null, permission: null };
    const mode = JOB_SOURCES.has(meta.job_source) ? meta.job_source : null;
    const dataMode = DATA_MODES.has(meta.data_mode) ? meta.data_mode : null;
    const raw = isObj(meta.source_counts) ? meta.source_counts : null;
    return {
      mode,
      dataMode,
      counts: raw
        ? {
            attempted: num(raw.attempted),
            collected: num(raw.collected),
            accepted: num(raw.accepted),
            rejected: num(raw.rejected),
            walkEstimated: num(raw.walk_estimated),
            providers: arr(raw.providers).filter(v => typeof v === 'string' && v),
          }
        : null,
      permission: isObj(meta.source_permission) ? meta.source_permission : null,
    };
  }

  /* ---------------------------------------------------------------------
   * 추천 사유를 만든 방식 — LLM 인가, 서버 규칙인가.
   *
   * 서버가 추가로 내려보내는 값만 옮긴다(가산적 metadata). 모르는 값은 null 로
   * 두고, 화면이 "확인하지 못했다"고 말한다. 여기서 기본값을 채우면
   * 결정론적 계산 결과에 AI 가 평가했다는 이름표가 붙는다.
   *
   * 성공은 서버가 스스로 성공이라고 밝힌 한 가지 모양만 인정한다:
   *   source=llm + llmUsed=true + llmStatus=success (+ engine 이 deterministic 이 아님).
   * 하나라도 어긋나면 claimed(서버 주장)일 뿐 verified 가 아니다.
   * ------------------------------------------------------------------*/
  const LLM_ENGINES = new Set(['hybrid', 'deterministic']);
  const LLM_PROVIDERS = new Set(['nosana', 'openai']);
  /* fallback 사유. 서버가 준 enum 만 쓰고, 자유 문장은 쓰지 않는다(그대로 그리면
   * 제공자 오류 원문이 화면에 노출될 수 있다). */
  const LLM_FALLBACK_STATUSES = new Set(['not_configured', 'timeout', 'provider_error', 'invalid_response', 'budget_exhausted']);
  const MAX_MODEL_LEN = 60;

  function adaptLlm(meta) {
    const m = isObj(meta) ? meta : null;
    const latency = m ? num(m.llmLatencyMs) : null;
    const model = m ? str(m.llmModel).trim().slice(0, MAX_MODEL_LEN) : '';
    const status = m && (m.llmStatus === 'success' || LLM_FALLBACK_STATUSES.has(m.llmStatus)) ? m.llmStatus : null;
    return {
      engine: m && LLM_ENGINES.has(m.engine) ? m.engine : null,
      used: m ? bool(m.llmUsed) : null,
      provider: m && LLM_PROVIDERS.has(m.llmProvider) ? m.llmProvider : null,
      model: model || null,
      latencyMs: latency !== null && latency >= 0 ? latency : null,
      status,
    };
  }

  /** 서버가 밝힌 값들이 한 방향을 가리킬 때만 "AI 가 평가했다"고 인정한다. */
  function llmVerdict(source, llm) {
    const claimed = source === 'llm';
    const verified = claimed
      && llm.used === true
      && llm.status === 'success'
      && llm.engine !== 'deterministic';
    return { ...llm, claimed, verified };
  }

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
    // source 는 서버만 안다. 없으면 null 로 두고 화면이 "미상"으로 밝힌다.
    // source 는 *순위를 만든 방식*(fallback = 서버 규칙)이지 공고 데이터의 출처가
    // 아니다. 데이터 출처는 meta.job_source / meta.data_mode 로 따로 온다.
    const source = raw.source === 'fallback' || raw.source === 'llm' ? raw.source : null;
    const meta = isObj(raw.meta) ? raw.meta : null;
    return {
      requestId: str(raw.requestId) || null,
      generatedAt: str(raw.generatedAt) || null,
      source,
      jobSource: adaptJobSource(meta),
      llm: llmVerdict(source, adaptLlm(meta)),
      contractVersion: meta ? str(meta.contractVersion) || null : null,
      disclosures: meta ? arr(meta.disclosures).filter(d => typeof d === 'string' && d) : [],
      availableSlots: arr(raw.availableSlots).map(adaptSlot).filter(Boolean),
      candidateCount: num(raw.candidateCount),
      plans,
      droppedPlans,
      meta,
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
    // web_demo.py 가 실제로 내려보내는 503 두 가지. 둘 다 조건을 바꿀 필요는 없다.
    BUSY: { title: '앞선 추천을 아직 처리 중입니다.', retryable: true },
    DAYTONA_UNAVAILABLE: { title: '실행 환경(Daytona)에 연결하지 못했습니다.', retryable: true },
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
    // 30초는 "응답 헤더까지"가 아니라 "본문을 다 읽을 때까지"다. 헤더만 오고 본문이
    // 끊기는 경우가 실제로 있으므로 타이머는 res.json() 이 끝난 뒤에 해제한다.
    let timedOut = false;
    const timer = controller ? setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs) : null;

    const clientTimeout = () => new ApiClientError('CLIENT_TIMEOUT', ERROR_TEXT.CLIENT_TIMEOUT.title, { retryable: true });

    try {
      let res;
      try {
        res = await fetchImpl(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
          body: JSON.stringify(request),
          signal: controller ? controller.signal : undefined,
        });
      } catch (cause) {
        throw timedOut ? clientTimeout() : adaptError({ cause });
      }

      let body = null;
      try {
        body = await res.json();
      } catch (cause) {
        if (timedOut || isAbort(cause)) throw clientTimeout();
        body = null;                              // 본문이 JSON 이 아니면 상태 코드로만 판단한다
      }

      if (!res.ok) throw adaptError({ status: res.status, body });
      return adaptResponse(body);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function isAbort(e) {
    const name = e && typeof e.name === 'string' ? e.name : '';
    return name === 'AbortError' || name === 'TimeoutError';
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

  /**
   * 이미 본 조합 — 재생성 시 previousPlanHashes 로 보낸다.
   * 서버는 hash 와 plan id 를 둘 다 받지만, 같은 조합이 다른 이름표(수입 최대안 /
   * 밸런스안)로 다시 오는 것까지 막으려면 조합 자체를 가리키는 hash 가 맞다.
   */
  function planIds(response) {
    if (!isObj(response)) return [];
    return arr(response.plans).map(p => (isObj(p) ? str(p.hash) || str(p.id) : '')).filter(Boolean);
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
    _internals: { normTime, toMinutes, adaptPlan, adaptJob, adaptShift, adaptSlot, adaptTravel, adaptJobSource, adaptLlm, llmVerdict },
  };
});
