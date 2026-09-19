/* =====================================================================
 * tests/ui_flow/support/dom.js
 *
 * js/app.js 를 브라우저 없이 돌리기 위한 최소 DOM 대역.
 * 프레임워크를 설치하지 않고 node:vm 안에서 실제 렌더 + 실제 핸들러를
 * 그대로 실행하는 것이 목적이므로, app.js 가 실제로 쓰는 것만 구현한다.
 *
 *   - innerHTML 파싱/직렬화 (따옴표 안의 '>' 를 존중하는 태그 스캐너)
 *   - querySelector(All) / closest : #id, .class, tag, [attr], [attr=v],
 *     자손 결합자, 쉼표 그룹
 *   - classList / dataset / value / checked / textContent
 *   - onclick·onchange·oninput·onkeydown 프로퍼티 + addEventListener
 *   - click() 은 실제처럼 조상까지 버블링한다 (app().onclick 위임 때문)
 * ===================================================================*/
'use strict';

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);

const camelToDash = s => s.replace(/[A-Z]/g, c => '-' + c.toLowerCase());

/* ---------- 선택자 ---------- */
function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [] };
  let i = 0;
  while (i < s.length) {
    const rest = s.slice(i);
    let m;
    if (s[i] === '#' && (m = /^#([\w-]+)/.exec(rest))) { c.id = m[1]; i += m[0].length; }
    else if (s[i] === '.' && (m = /^\.([\w-]+)/.exec(rest))) { c.classes.push(m[1]); i += m[0].length; }
    else if (s[i] === '[') {
      const end = s.indexOf(']', i);
      if (end < 0) break;
      const inner = s.slice(i + 1, end);
      const eq = inner.indexOf('=');
      if (eq < 0) c.attrs.push([inner.trim(), null]);
      else {
        let v = inner.slice(eq + 1).trim();
        if (v.length > 1 && (v[0] === '"' || v[0] === "'") && v[v.length - 1] === v[0]) v = v.slice(1, -1);
        c.attrs.push([inner.slice(0, eq).trim(), v]);
      }
      i = end + 1;
    }
    else if ((m = /^[\w-]+/.exec(rest))) { c.tag = m[0].toUpperCase(); i += m[0].length; }
    else i += 1;
  }
  return c;
}

function splitDescendants(part) {
  const out = [];
  let cur = '', depth = 0;
  for (const ch of part) {
    if (ch === '[') depth++;
    else if (ch === ']') depth--;
    if (depth === 0 && /\s/.test(ch)) { if (cur) { out.push(cur); cur = ''; } continue; }
    cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

const selectorCache = new Map();
function parseSelector(sel) {
  if (selectorCache.has(sel)) return selectorCache.get(sel);
  const groups = String(sel).split(',').map(p => p.trim()).filter(Boolean)
    .map(p => splitDescendants(p).map(parseCompound));
  selectorCache.set(sel, groups);
  return groups;
}

function matchesCompound(el, c) {
  if (!el || el.nodeType !== 1) return false;
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.getAttribute('id') !== c.id) return false;
  for (const cls of c.classes) if (!el.classList.contains(cls)) return false;
  for (const [name, value] of c.attrs) {
    if (!el.attributes.has(name)) return false;
    if (value !== null && el.attributes.get(name) !== value) return false;
  }
  return true;
}

function matchesChain(el, chain) {
  if (!matchesCompound(el, chain[chain.length - 1])) return false;
  const up = (node, i) => {
    if (i < 0) return true;
    let n = node;
    while (n && n.nodeType === 1) {
      if (matchesCompound(n, chain[i]) && up(n.parentNode, i - 1)) return true;
      n = n.parentNode;
    }
    return false;
  };
  return up(el.parentNode, chain.length - 2);
}

function matches(el, sel) {
  return parseSelector(sel).some(chain => matchesChain(el, chain));
}

/* ---------- 노드 ---------- */
class TextNode {
  constructor(text) { this.nodeType = 3; this.text = text; this.parentNode = null; }
}

class El {
  constructor(tag, doc) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = doc;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = new Map();
    this.onclick = null;
    this.onchange = null;
    this.oninput = null;
    this.onkeydown = null;
    this._value = undefined;
    this._checked = undefined;
    this._disabled = undefined;
    this._onTextContent = null;
  }

  /* 속성 */
  getAttribute(n) { return this.attributes.has(n) ? this.attributes.get(n) : null; }
  setAttribute(n, v) { this.attributes.set(n, String(v)); }
  removeAttribute(n) { this.attributes.delete(n); }
  hasAttribute(n) { return this.attributes.has(n); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }

  get classList() {
    const el = this;
    const list = () => (el.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    const write = a => el.setAttribute('class', a.join(' '));
    return {
      contains: c => list().includes(c),
      add: (...cs) => { const a = list(); for (const c of cs) if (!a.includes(c)) a.push(c); write(a); },
      remove: (...cs) => write(list().filter(c => !cs.includes(c))),
      toggle: (c, force) => {
        const a = list();
        const has = a.includes(c);
        const on = force === undefined ? !has : !!force;
        if (on && !has) a.push(c);
        if (!on && has) a.splice(a.indexOf(c), 1);
        write(a);
        return on;
      },
    };
  }

  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (_, p) => (typeof p === 'string' ? (el.getAttribute('data-' + camelToDash(p)) ?? undefined) : undefined),
      set: (_, p, v) => { el.setAttribute('data-' + camelToDash(String(p)), v); return true; },
      has: (_, p) => el.attributes.has('data-' + camelToDash(String(p))),
      ownKeys: () => [...el.attributes.keys()].filter(k => k.startsWith('data-')).map(k => k.slice(5)),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
  }

  /* 폼 */
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === 'SELECT') {
      const opts = this.querySelectorAll('option');
      const sel = opts.find(o => o.hasAttribute('selected')) || opts[0];
      if (!sel) return '';
      return sel.hasAttribute('value') ? sel.getAttribute('value') : sel.textContent;
    }
    return this.getAttribute('value') ?? '';
  }
  set value(v) { this._value = String(v); }
  get checked() { return this._checked !== undefined ? this._checked : this.hasAttribute('checked'); }
  set checked(v) { this._checked = !!v; }
  get disabled() { return this._disabled !== undefined ? this._disabled : this.hasAttribute('disabled'); }
  set disabled(v) { this._disabled = !!v; }
  focus() {}
  select() {}

  /* 트리 */
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; }
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }

  get innerHTML() { return this.childNodes.map(serialize).join(''); }
  set innerHTML(html) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    parseInto(String(html), this.ownerDocument, this);
  }
  get outerHTML() { return serialize(this); }

  get textContent() {
    let out = '';
    for (const c of this.childNodes) out += c.nodeType === 3 ? c.text : c.textContent;
    return out;
  }
  set textContent(v) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    const t = new TextNode(String(v));
    t.parentNode = this;
    this.childNodes.push(t);
    if (this._onTextContent) this._onTextContent(String(v));
  }

  /* 질의 */
  querySelector(sel) {
    let found = null;
    walk(this, el => { if (!found && matches(el, sel)) found = el; });
    return found;
  }
  querySelectorAll(sel) {
    const out = [];
    walk(this, el => { if (matches(el, sel)) out.push(el); });
    return out;
  }
  closest(sel) {
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, sel)) return n;
    return null;
  }
  matches(sel) { return matches(this, sel); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }

  /* 이벤트 */
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const a = this._listeners.get(type);
    if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }
  }
}

function walk(root, fn) {
  for (const c of root.childNodes) {
    if (c.nodeType !== 1) continue;
    fn(c);
    walk(c, fn);
  }
}

function serialize(n) {
  if (n.nodeType === 3) return n.text;
  const tag = n.tagName.toLowerCase();
  let attrs = '';
  for (const [k, v] of n.attributes) attrs += v === '' ? ' ' + k : ' ' + k + '="' + v + '"';
  if (VOID.has(tag)) return '<' + tag + attrs + '>';
  return '<' + tag + attrs + '>' + n.childNodes.map(serialize).join('') + '</' + tag + '>';
}

const ATTR_RE = /([a-zA-Z_:@][-a-zA-Z0-9_:.]*)\s*(?:=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g;
function parseAttrs(raw, el) {
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(raw))) {
    let v = m[2];
    if (v === undefined) v = '';
    else if ((v[0] === '"' || v[0] === "'") && v[v.length - 1] === v[0]) v = v.slice(1, -1);
    el.setAttribute(m[1], v);
  }
}

function parseInto(html, doc, parent) {
  const stack = [parent];
  const top = () => stack[stack.length - 1];
  const pushText = t => {
    if (!t) return;
    const n = new TextNode(t);
    n.parentNode = top();
    top().childNodes.push(n);
  };
  let i = 0;
  const len = html.length;
  while (i < len) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { pushText(html.slice(i)); break; }
    if (lt > i) pushText(html.slice(i, lt));
    if (html.startsWith('<!--', lt)) { const e = html.indexOf('-->', lt); i = e < 0 ? len : e + 3; continue; }
    if (html[lt + 1] === '/') {
      const gt = html.indexOf('>', lt);
      const name = html.slice(lt + 2, gt < 0 ? len : gt).trim().toLowerCase();
      for (let k = stack.length - 1; k > 0; k--) {
        if (stack[k].tagName.toLowerCase() === name) { stack.length = k; break; }
      }
      i = (gt < 0 ? len : gt) + 1;
      continue;
    }
    let j = lt + 1, quote = null;
    while (j < len) {
      const c = html[j];
      if (quote) { if (c === quote) quote = null; }
      else if (c === '"' || c === "'") quote = c;
      else if (c === '>') break;
      j++;
    }
    const raw = html.slice(lt + 1, j);
    i = j + 1;
    const selfClose = raw.endsWith('/');
    const body = selfClose ? raw.slice(0, -1) : raw;
    const nm = /^([a-zA-Z][a-zA-Z0-9:-]*)/.exec(body);
    if (!nm) { pushText(html.slice(lt, i)); continue; }
    const tag = nm[1];
    const el = doc.createElement(tag);
    parseAttrs(body.slice(tag.length), el);
    top().appendChild(el);
    if (!selfClose && !VOID.has(tag.toLowerCase())) stack.push(el);
  }
}

class Doc {
  constructor() {
    this.documentElement = new El('html', this);
    this.body = new El('body', this);
    this.documentElement.appendChild(this.body);
  }
  createElement(tag) { return new El(tag, this); }
  createTextNode(t) { return new TextNode(String(t)); }
  querySelector(sel) { return this.documentElement.querySelector(sel); }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  addEventListener() {}
  removeEventListener() {}
}

/* ---------- 이벤트 디스패치 ---------- */
function makeEvent(type, target, extra) {
  return {
    type,
    target,
    currentTarget: null,
    defaultPrevented: false,
    _stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
    ...extra,
  };
}

/* 클릭은 실제 DOM 처럼 target → 조상 순서로 전파한다.
 * 핸들러 안에서 다시 렌더되더라도 클릭 시점의 경로/핸들러로 전파한다. */
function click(el, extra = {}) {
  if (!el) throw new Error('click(): element not found');
  const path = [];
  for (let n = el; n && n.nodeType === 1; n = n.parentNode) path.push(n);
  const snapshot = path.map(n => ({ node: n, on: n.onclick, ls: (n._listeners.get('click') || []).slice() }));
  const ev = makeEvent('click', el, extra);
  for (const h of snapshot) {
    ev.currentTarget = h.node;
    if (typeof h.on === 'function') h.on.call(h.node, ev);
    for (const fn of h.ls) fn.call(h.node, ev);
    if (ev._stopped) break;
  }
  return ev;
}

function fire(el, type, extra = {}) {
  if (!el) throw new Error('fire(' + type + '): element not found');
  const prop = 'on' + type;
  const ev = makeEvent(type, el, extra);
  if (typeof el[prop] === 'function') el[prop].call(el, ev);
  for (const fn of (el._listeners.get(type) || []).slice()) fn.call(el, ev);
  return ev;
}

function change(el, value) {
  if (!el) throw new Error('change(): element not found');
  if (value !== undefined) el.value = value;
  return fire(el, 'change');
}

function typeInto(el, value) {
  if (!el) throw new Error('typeInto(): element not found');
  el.value = value;
  return fire(el, 'input');
}

module.exports = { Doc, El, TextNode, click, fire, change, typeInto, matches, serialize };
