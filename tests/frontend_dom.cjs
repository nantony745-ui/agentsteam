// 仅验证 DOM 操作与事件逻辑；不提供浏览器布局、绘制或网络仿真。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function environment(width = 1440, saved = null) {
  let document;
  class Element {
    constructor(tag, attributes = {}) {
      this.tagName = tag;
      this.attributes = {};
      this.children = [];
      this.parentElement = null;
      this.listeners = {};
      this.dataset = {};
      this.style = { setProperty(key, value) { this[key] = value; } };
      this.hidden = false;
      this.disabled = false;
      this.open = false;
      this.value = '';
      this.textContent = '';
      this.innerHTML = '';
      this.scrollTop = 0;
      this.scrollHeight = 1200;
      this.clientHeight = 600;
      this.classList = {
        contains: name => this.className.split(' ').includes(name),
        add: name => { this.className = `${this.className} ${name}`.trim(); },
        remove: name => { this.className = this.className.split(' ').filter(item => item !== name).join(' '); },
      };
      for (const [key, value] of Object.entries(attributes)) this.setAttribute(key, value);
    }
    get className() { return this.attributes.class || ''; }
    set className(value) { this.attributes.class = value; }
    get id() { return this.attributes.id || ''; }
    set id(value) { this.setAttribute('id', value); }
    get lastElementChild() { return this.children.at(-1); }
    get isConnected() { return this === document?.documentElement || Boolean(this.parentElement?.isConnected); }
    setAttribute(key, value) {
      this.attributes[key] = String(value);
      if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = String(value);
    }
    getAttribute(key) { return this.attributes[key] ?? null; }
    removeAttribute(key) { delete this.attributes[key]; }
    toggleAttribute(key, force) {
      if (force) this.setAttribute(key, '');
      else this.removeAttribute(key);
      return force;
    }
    append(...elements) { for (const element of elements) { element.remove(); element.parentElement = this; this.children.push(element); } }
    insertBefore(element, reference) {
      if (element === reference) return element;
      element.remove();
      const index = reference === null ? this.children.length : this.children.indexOf(reference);
      if (index < 0) throw new Error('参考节点不属于当前父节点');
      element.parentElement = this;
      this.children.splice(index, 0, element);
      return element;
    }
    prepend(element) { element.remove(); element.parentElement = this; this.children.unshift(element); }
    replaceChildren(...elements) { for (const child of this.children) child.parentElement = null; this.children = []; this.append(...elements); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    matches(selector) {
      if (selector === '[hidden]') return this.hidden;
      if (selector.includes(':not(:disabled)')) {
        if (this.disabled) return false;
        selector = selector.replace(':not(:disabled)', '');
      }
      if (selector.startsWith('#')) return this.attributes.id === selector.slice(1);
      if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
      const attribute = selector.match(/^(\w+)?\[([^=\]]+)(?:="([^"]*)")?\]$/);
      if (attribute) {
        const [_, tag, name, value] = attribute;
        const actual = name.startsWith('data-') ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] : this.getAttribute(name);
        return (!tag || this.tagName === tag) && actual != null && (value === undefined || actual === value);
      }
      return this.tagName === selector;
    }
    querySelectorAll(selector) {
      if (selector.includes(',')) return [...new Set(selector.split(',').flatMap(part => this.querySelectorAll(part.trim())))];
      const parts = selector.split(/\s*(>)\s*|\s+/).filter(Boolean);
      const matches = element => {
        if (!element.matches(parts.at(-1))) return false;
        if (parts.length === 1) return true;
        if (parts[1] === '>') return element.parentElement?.matches(parts[0]);
        return Boolean(element.parentElement?.closest(parts[0]));
      };
      const result = [];
      const visit = parent => { for (const child of parent.children) { if (matches(child)) result.push(child); visit(child); } };
      visit(this);
      return result;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
    emit(name, options = {}) {
      const event = { type: name, target: this, button: 0, pointerId: 1, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...options };
      for (const handler of this.listeners[name] || []) handler(event);
      return event;
    }
    focus() { document.activeElement = this; }
    click() { this.emit('click'); }
    showModal() { this.open = true; }
    close() { if (this.open) { this.open = false; this.emit('close'); } }
    getClientRects() { return this.hidden || this.closest('[hidden]') ? [] : [{}]; }
    setPointerCapture(id) { this.pointerId = id; }
    hasPointerCapture(id) { return this.pointerId === id; }
    releasePointerCapture() { this.pointerId = null; this.emit('lostpointercapture'); }
  }
  const root = new Element('html');
  const body = new Element('body');
  document = {
    documentElement: root, body, activeElement: body, listeners: {},
    querySelector: selector => root.querySelector(selector),
    querySelectorAll: selector => root.querySelectorAll(selector),
    createElement: tag => new Element(tag),
    addEventListener: (name, handler) => { (document.listeners[name] ||= []).push(handler); },
    emit: (name, event) => { for (const handler of document.listeners[name] || []) handler(event); },
  };
  root.append(body);
  const add = (tag, id, parent = body, attributes = {}) => { const element = new Element(tag, { ...attributes, ...(id ? { id } : {}) }); parent.append(element); return element; };
  const header = add('header');
  for (const side of ['left', 'right']) add('button', `${side}-toggle`, header, { 'data-panel-toggle': side });
  add('button', 'execution-status', header, { 'data-panel-open': 'right' });
  add('button', 'stop-button', header);
  const left = add('aside', 'sidebar');
  const right = add('aside', 'inspector');
  for (const [panel, side] of [[left, 'left'], [right, 'right']]) {
    add('button', `${side}-close`, panel, { 'data-panel-toggle': side });
    add('div', `${side}-resizer`);
  }
  add('div', 'panel-backdrop');
  const main = add('main', null, body, { class: 'conversation' });
  for (const id of ['timeline', 'scroll-bottom', 'message-form', 'retry-button', 'discard-button', 'message-error', 'stream-status', 'stream-retry', 'session-title', 'session-cwd', 'composer-agent', 'composer-project', 'project-title', 'agent-choice']) add('div', id, main);
  add('textarea', 'message-input', main);
  const send = add('button', 'send-button', main);
  add('svg', 'send-icon', send);
  add('svg', 'pause-icon', send, { hidden: '' });
  for (const id of ['project-select', 'session-form', 'harness', 'create-session-button']) add('div', id, left);
  add('h2', 'projects-heading', left);
  const projectsContent = add('div', 'projects-content', left);
  add('nav', 'project-list', projectsContent);
  add('h2', 'recent-heading', left);
  add('nav', 'session-list', left);
  const projectDialog = add('dialog', 'project-dialog');
  const fileDialog = add('dialog', 'file-view-dialog');
  for (const id of ['file-view-title', 'file-view-message', 'file-view-content', 'file-view-close']) add(id === 'file-view-close' ? 'button' : 'div', id, fileDialog);
  add('button', 'project-dialog-close', projectDialog, { 'data-project-dialog-close': '' });
  const projectForm = add('form', 'project-form', projectDialog);
  add('input', 'project-path', projectForm);
  add('button', 'project-directory-open', projectForm);
  const projectBrowser = add('section', 'project-directory-browser', projectForm);
  projectBrowser.hidden = true;
  for (const id of ['project-directory-notice', 'project-directory-current', 'project-directory-roots', 'project-directory-entries']) add('div', id, projectBrowser);
  for (const id of ['project-directory-home', 'project-directory-parent', 'project-directory-use', 'project-directory-cancel']) add('button', id, projectBrowser);
  add('p', 'project-error', projectForm);
  add('button', 'project-dialog-cancel', projectForm, { 'data-project-dialog-close': '' });
  add('button', 'project-create-submit', projectForm);
  const sessionMenu = add('div', 'session-menu');
  sessionMenu.hidden = true;
  add('button', 'session-menu-delete', sessionMenu);
  const deleteDialog = add('dialog', 'session-delete-dialog');
  const deleteForm = add('form', 'session-delete-form', deleteDialog);
  for (const id of ['session-delete-description', 'session-delete-error', 'session-delete-confirm', 'session-delete-cancel']) add('button', id, deleteForm);
  document.querySelector('#harness').value = 'codex';
  for (const id of ['session-id', 'session-harness', 'native-id', 'run-list']) add('div', id, right);
  const changes = add('section', 'task-changes', right);
  add('div', 'task-diff-content', changes);
  for (const id of ['task-diff-refresh', 'task-diff-meta', 'task-diff-message', 'task-git-status', 'task-diff-patch',
    'task-untracked', 'task-preview-message', 'task-file-preview']) add('div', id, changes);
  const window = { innerWidth: width, innerHeight: 900, listeners: {}, addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); } };
  const storage = new Map(saved ? [['agentsteam.layout.v1', JSON.stringify(saved)]] : []);
  const frames = new Map();
  const timers = new Map();
  let timerId = 0;
  let frameId = 0;
  const sandbox = vm.createContext({
    console, URL, URLSearchParams, Intl, document, window, AbortController,
    setTimeout: handler => { timers.set(++timerId, handler); return timerId; },
    clearTimeout: id => timers.delete(id),
    requestAnimationFrame: handler => { frames.set(++frameId, handler); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    ResizeObserver: class { observe() {} },
  });
  for (const file of ['vendor/markdown-it.umd.min.js', 'layout.js', 'app.js']) {
    let source = fs.readFileSync(path.join(__dirname, '../app/static', file), 'utf8');
    if (file === 'app.js') source = source.replace(/\ninit\(\);\s*$/, '\n');
    vm.runInContext(source, sandbox);
  }
  return {
    document, window, frames, timers, storage,
    get: selector => document.querySelector(selector),
    run: code => vm.runInContext(code, sandbox),
    flushFrames() { const textTimer = vm.runInContext('state.textTimer', sandbox); if (timers.has(textTimer)) { const handler = timers.get(textTimer); timers.delete(textTimer); handler(); } while (frames.size) { const pending = [...frames]; frames.clear(); for (const [, handler] of pending) handler(); } },
    resize(nextWidth) { window.innerWidth = nextWidth; for (const handler of window.listeners.resize || []) handler(); },
  };
}

module.exports = { environment };
