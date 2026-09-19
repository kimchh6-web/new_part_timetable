/* =====================================================================
 * tests/ui_flow/support/page.js
 *
 * index.html 이 하는 일(스크립트 5개를 한 전역에 로드)을 node:vm 컨텍스트
 * 안에서 그대로 재현한다. 프로덕션 JS 는 손대지 않고 원본 파일을 읽어 실행한다.
 *
 *   index.html: data.js → engine.js → api-client.js → app.js → live-demo.js
 *
 * 네트워크는 기본적으로 막혀 있다. 테스트가 명시적으로 응답을 큐에 넣기
 * 전에는 fetch 가 던지고, 상대경로(/api/...)가 아닌 주소는 무조건 거부한다.
 * 타이머는 가짜다 — toast(2200ms) 나 클라이언트 타임아웃(30s)이 실제
 * 프로세스를 붙잡지 않는다.
 * ===================================================================*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Doc, click, fire, change, typeInto } = require('./dom.js');

const REPO = path.resolve(__dirname, '..', '..', '..');
const SCRIPTS = ['js/data.js', 'js/engine.js', 'js/api-client.js', 'js/app.js', 'js/live-demo.js'];

/* index.html 의 topbar — router() 가 .nav a 를 만진다 */
const HEADER_HTML = '<div class="topbar-inner">'
  + '<a class="brand" href="#/"><span class="brand-mark">T</span>알바 스케줄 추천</a>'
  + '<nav class="nav">'
  + '<a href="#/live">라이브 데모</a>'
  + '<a href="#/">알바 찾기</a>'
  + '<a href="#/my">내 스케줄</a>'
  + '<a href="#/onboarding">내 정보</a>'
  + '<a href="#/reset" title="저장 정보 초기화">초기화</a>'
  + '</nav></div>';

function makeStorage() {
  const map = new Map();
  return {
    getItem: k => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: k => { map.delete(String(k)); },
    clear: () => { map.clear(); },
    key: i => ([...map.keys()][i] ?? null),
    get length() { return map.size; },
    _map: map,
  };
}

/** 테스트가 받아 볼 수 있는 fetch Response 대역. */
function jsonResponse(body, { status = 200, ok } = {}) {
  return {
    ok: ok === undefined ? status >= 200 && status < 300 : ok,
    status,
    headers: { get: () => 'application/json' },
    json: async () => {
      if (body === '__NOT_JSON__') throw new SyntaxError('Unexpected token < in JSON');
      return JSON.parse(JSON.stringify(body));
    },
    text: async () => JSON.stringify(body),
  };
}

function createPage() {
  const doc = new Doc();

  const header = doc.createElement('header');
  header.setAttribute('class', 'topbar');
  header.innerHTML = HEADER_HTML;
  const appEl = doc.createElement('main');
  appEl.setAttribute('class', 'container');
  appEl.setAttribute('id', 'app');
  const toastEl = doc.createElement('div');
  toastEl.setAttribute('class', 'toast');
  toastEl.setAttribute('id', 'toast');
  doc.body.appendChild(header);
  doc.body.appendChild(appEl);
  doc.body.appendChild(toastEl);

  const toasts = [];
  toastEl._onTextContent = msg => toasts.push(msg);

  /* ---------- 이벤트 (window) ---------- */
  const listeners = new Map();
  const addEventListener = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
  const dispatch = type => {
    for (const fn of (listeners.get(type) || []).slice()) fn({ type });
  };

  /* ---------- 라우팅 ---------- */
  const location = {
    _hash: '',
    href: 'http://localhost:5191/',
    origin: 'http://localhost:5191',
    pathname: '/',
    get hash() { return this._hash; },
    set hash(v) {
      const next = String(v);
      if (next === this._hash) return;
      this._hash = next;
      dispatch('hashchange');
    },
  };

  /* ---------- 네트워크 (기본 차단) ---------- */
  const calls = [];
  const queue = [];
  let responder = null;
  const fetchDouble = async (url, init = {}) => {
    if (typeof url !== 'string' || !url.startsWith('/')) {
      throw new Error('ui_flow: 원격 주소로 나가는 요청을 막았습니다 -> ' + url);
    }
    let body = null;
    if (init && typeof init.body === 'string') {
      try { body = JSON.parse(init.body); } catch { body = init.body; }
    }
    const call = { url, method: (init && init.method) || 'GET', headers: (init && init.headers) || {}, body, init };
    calls.push(call);
    const next = responder || queue.shift();
    if (!next) throw new Error('ui_flow: 준비되지 않은 네트워크 호출 -> ' + url);
    const out = typeof next === 'function' ? await next(call, calls.length) : next;
    if (out instanceof Error) throw out;
    return out;
  };

  /* ---------- 가짜 타이머 ---------- */
  const timers = new Map();
  let timerId = 1;
  const setTimeoutDouble = (fn, ms) => { const id = timerId++; timers.set(id, { fn, ms }); return id; };
  const clearTimeoutDouble = id => { timers.delete(id); };
  const runTimers = () => {
    const pending = [...timers.entries()];
    timers.clear();
    for (const [, t] of pending) t.fn();
  };

  let confirmResult = true;

  const sandbox = {
    document: doc,
    location,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    fetch: fetchDouble,
    setTimeout: setTimeoutDouble,
    clearTimeout: clearTimeoutDouble,
    setInterval: setTimeoutDouble,
    clearInterval: clearTimeoutDouble,
    queueMicrotask,
    AbortController,
    AbortSignal,
    performance: { now: () => 0 },
    console,
    navigator: { userAgent: 'ui_flow-test' },
    addEventListener,
    removeEventListener: (type, fn) => {
      const a = listeners.get(type);
      if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }
    },
    scrollTo: () => {},
    confirm: () => confirmResult,
    alert: () => {},
    URL,
    TextEncoder,
    TextDecoder,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);

  for (const rel of SCRIPTS) {
    const file = path.join(REPO, rel);
    vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, { filename: file });
  }

  const $ = sel => doc.querySelector(sel);
  const $$ = sel => doc.querySelectorAll(sel);
  const need = sel => {
    const el = $(sel);
    if (!el) throw new Error('선택자를 화면에서 찾지 못했습니다: ' + sel);
    return el;
  };

  return {
    ctx,
    doc,
    window: sandbox,
    location,
    localStorage: sandbox.localStorage,
    sessionStorage: sandbox.sessionStorage,

    /* 질의 */
    $, $$, need,
    html: () => appEl.innerHTML,
    text: () => appEl.textContent,
    bodyHtml: () => doc.body.innerHTML,
    toasts,

    /* 상호작용 */
    click: sel => click(typeof sel === 'string' ? need(sel) : sel),
    clickEl: el => click(el),
    change: (sel, v) => change(typeof sel === 'string' ? need(sel) : sel, v),
    type: (sel, v) => typeInto(typeof sel === 'string' ? need(sel) : sel, v),
    fire,
    runTimers,
    setConfirm: v => { confirmResult = v; },

    /* 라우팅 */
    boot: () => dispatch('DOMContentLoaded'),
    go: hash => {
      const next = hash.startsWith('#') ? hash : '#' + hash;
      if (location._hash === next) dispatch('hashchange');
      else location.hash = next;
    },
    hash: () => location._hash,

    /* 저장소 */
    seed: (key, value) => sandbox.localStorage.setItem(key, JSON.stringify(value)),
    stored: key => {
      const raw = sandbox.localStorage.getItem(key);
      return raw === null ? null : JSON.parse(raw);
    },
    rawStored: key => sandbox.localStorage.getItem(key),
    session: () => {
      const raw = sandbox.sessionStorage.getItem('result');
      return raw === null ? null : JSON.parse(raw);
    },

    /* 네트워크 */
    calls,
    queueJson: (body, opts) => { queue.push(jsonResponse(body, opts)); },
    queueRaw: value => { queue.push(value); },
    queueFailure: err => { queue.push(err); },
    onFetch: fn => { responder = fn; },

    /* vm 안의 어휘적 선언(const Store 등)을 읽기 위한 통로 */
    evalIn: expr => vm.runInContext(expr, ctx),
  };
}

module.exports = { createPage, jsonResponse, REPO };
