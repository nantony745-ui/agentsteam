'use strict';

// 沿用工作台 Pointer Capture 的拖动与取消流程。
function createSplitLayout({ key: KEY, root, handleSelector, containerSelector, defaultWidth: DEFAULT_WIDTH, minWidth: MIN_WIDTH, maxWidth: MAX_WIDTH }) {
  const DETAIL_MIN_WIDTH = 320;
  let preferredWidth = DEFAULT_WIDTH;
  let width = DEFAULT_WIDTH;
  let maximum = MAX_WIDTH;
  let drag = null;
  let handles = [];
  let containers = [];

  function render() {
    const available = containers.find(node => node.getClientRects().length)?.clientWidth;
    maximum = available ? Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, available - DETAIL_MIN_WIDTH - 12)) : MAX_WIDTH;
    width = Math.max(MIN_WIDTH, Math.min(maximum, preferredWidth));
    document.querySelector(root).style.setProperty('--settings-list-width', `${width}px`);
    for (const handle of handles) {
      handle.hidden = window.innerWidth < 768;
      handle.setAttribute('aria-valuemin', MIN_WIDTH);
      handle.setAttribute('aria-valuemax', maximum);
      handle.setAttribute('aria-valuenow', Math.round(width));
    }
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(preferredWidth)); } catch { /* 存储不可用时继续调整布局。 */ }
  }

  function finish(cancelled) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancelled) preferredWidth = previous.preferredWidth;
    else save();
    if (previous.handle.hasPointerCapture(previous.pointerId)) previous.handle.releasePointerCapture(previous.pointerId);
    document.body.classList.remove('resizing');
    render();
  }

  function init() {
    handles = [...document.querySelectorAll(handleSelector)];
    containers = [...document.querySelectorAll(containerSelector)];
    try {
      const saved = JSON.parse(localStorage.getItem(KEY));
      if (typeof saved === 'number' && Number.isFinite(saved)) preferredWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, saved));
    } catch { /* 旧偏好损坏时使用默认宽度。 */ }
    for (const handle of handles) {
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0 || drag || window.innerWidth < 768) return;
        event.preventDefault();
        drag = { handle, pointerId: event.pointerId, x: event.clientX, width, preferredWidth };
        handle.setPointerCapture(event.pointerId);
        document.body.classList.add('resizing');
      });
      handle.addEventListener('pointermove', event => {
        if (drag?.handle !== handle || event.pointerId !== drag.pointerId) return;
        preferredWidth = Math.max(MIN_WIDTH, Math.min(maximum, drag.width + event.clientX - drag.x));
        render();
      });
      handle.addEventListener('pointerup', event => { if (event.pointerId === drag?.pointerId) finish(false); });
      handle.addEventListener('pointercancel', () => finish(true));
      handle.addEventListener('lostpointercapture', () => finish(true));
      handle.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || window.innerWidth < 768) return;
        event.preventDefault();
        preferredWidth = Math.max(MIN_WIDTH, Math.min(maximum, width + (event.key === 'ArrowRight' ? 16 : -16)));
        render();
        save();
      });
    }
    window.addEventListener('resize', () => { finish(true); render(); });
    window.addEventListener('blur', () => finish(true));
    window.addEventListener('hashchange', () => finish(true));
    const observer = new ResizeObserver(render);
    for (const container of containers) observer.observe(container);
    render();
  }

  return { init, render };
}

const SettingsLayout = createSplitLayout({
  key: 'agentsteam.settings.width.v1', root: '#settings-view',
  handleSelector: '.settings-resizer', containerSelector: '.settings-split',
  defaultWidth: 280, minWidth: 220, maxWidth: 520,
});
const EmployeeLayout = createSplitLayout({
  key: 'agentsteam.employees.width.v1', root: '#employees-view',
  handleSelector: '#employee-resizer', containerSelector: '#employees-view',
  defaultWidth: 240, minWidth: 180, maxWidth: 440,
});
