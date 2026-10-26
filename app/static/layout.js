'use strict';

// 参照 Codeg SidebarProvider / AuxPanelProvider；仅管理浏览器布局偏好。
globalThis.WorkbenchLayout = (() => {
  const KEY = 'agentsteam.layout.v1';
  const LIMITS = { left: { min: 240, max: 440 }, right: { min: 280, max: 480 } };
  const preferences = { left: { isOpen: true, width: 300 }, right: { isOpen: false, width: 320 } };
  const drawers = { left: false, right: false };
  const panels = {};
  const handles = {};
  let mode;
  let activeDrawer = null;
  let returnFocus = null;
  let widths = { left: 0, right: 0 };
  let drag = null;
  let initialized = false;

  const clamp = (side, width) => Math.max(LIMITS[side].min, Math.min(LIMITS[side].max, width));
  const viewportMode = () => window.innerWidth < 768 ? 'mobile' : window.innerWidth < 1200 ? 'tablet' : 'desktop';
  const isDrawer = side => mode === 'mobile' || (side === 'right' && mode === 'tablet');
  const isOpen = side => isDrawer(side) ? drawers[side] : preferences[side].isOpen;

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY));
      for (const side of ['left', 'right']) {
        const value = saved?.[side];
        if (typeof value?.isOpen === 'boolean' && Number.isFinite(value.width)) {
          preferences[side] = { isOpen: value.isOpen, width: clamp(side, value.width) };
        }
      }
    } catch { /* 存储禁用或旧偏好损坏时使用默认布局。 */ }
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(preferences)); } catch { /* 不影响布局操作。 */ }
  }

  function fitWidths() {
    const result = {};
    for (const side of ['left', 'right']) result[side] = !isDrawer(side) && isOpen(side) ? preferences[side].width : 0;
    const gaps = (result.left ? 6 : 0) + (result.right ? 6 : 0);
    let excess = Math.max(0, result.left + result.right + gaps + 480 - window.innerWidth);
    for (const side of ['right', 'left']) {
      if (!result[side]) continue;
      const reduction = Math.min(excess, result[side] - LIMITS[side].min);
      result[side] -= reduction;
      excess -= reduction;
    }
    return result;
  }

  function maxWidth(side) {
    const other = side === 'left' ? 'right' : 'left';
    return Math.min(LIMITS[side].max, window.innerWidth - 480 - widths[other] - (widths[other] ? 6 : 0) - 6);
  }

  function focusable(panel) {
    return [...panel.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href], [tabindex="0"]')]
      .filter(element => element.getClientRects().length > 0);
  }

  function render() {
    widths = fitWidths();
    const root = document.documentElement;
    let nextDrawer = null;
    for (const side of ['left', 'right']) {
      const open = isOpen(side);
      const drawer = isDrawer(side);
      const panel = panels[side];
      panel.hidden = !open;
      panel.dataset.drawer = String(drawer);
      panel.setAttribute('role', drawer ? 'dialog' : 'complementary');
      if (drawer && open) {
        nextDrawer = side;
        panel.setAttribute('aria-modal', 'true');
      } else panel.removeAttribute('aria-modal');
      handles[side].hidden = !open || drawer;
      handles[side].setAttribute('aria-valuemin', LIMITS[side].min);
      handles[side].setAttribute('aria-valuemax', Math.max(LIMITS[side].min, maxWidth(side)));
      handles[side].setAttribute('aria-valuenow', Math.round(widths[side]));
      root.style.setProperty(`--${side}-width`, `${widths[side]}px`);
      root.style.setProperty(`--${side}-gap`, widths[side] ? '6px' : '0px');
      root.style.setProperty(`--${side}-panel-width`, `${preferences[side].width}px`);
      for (const button of document.querySelectorAll(`[data-panel-toggle="${side}"]`)) {
        const label = `${open ? '收起' : '展开'}${side === 'left' ? '左' : '右'}侧栏`;
        button.setAttribute('aria-expanded', String(open));
        button.setAttribute('aria-label', label);
        button.title = label;
      }
    }
    document.querySelector('#panel-backdrop').hidden = !nextDrawer;
    document.querySelector('.conversation').inert = Boolean(nextDrawer);
    const employees = document.querySelector('#employees-view');
    if (employees) employees.inert = Boolean(nextDrawer);
    for (const side of ['left', 'right']) panels[side].inert = Boolean(nextDrawer && side !== nextDrawer);
    if (nextDrawer !== activeDrawer) {
      const previous = activeDrawer;
      activeDrawer = nextDrawer;
      if (nextDrawer) focusable(panels[nextDrawer])[0]?.focus();
      else if (previous) {
        const fallback = document.querySelector(`[data-panel-toggle="${previous}"]`);
        (returnFocus?.isConnected && !returnFocus.closest('[hidden]') ? returnFocus : fallback).focus();
        returnFocus = null;
      }
    }
  }

  function setOpen(side, open, trigger) {
    if (isDrawer(side)) {
      if (open) {
        if (trigger || !activeDrawer) returnFocus = trigger || document.activeElement;
        if (mode === 'mobile') drawers[side === 'left' ? 'right' : 'left'] = false;
      }
      drawers[side] = open;
      if (mode === 'tablet') {
        preferences[side].isOpen = open;
        save();
      }
    } else {
      preferences[side].isOpen = open;
      save();
      if (!open && panels[side].contains(document.activeElement)) document.querySelector(`[data-panel-toggle="${side}"]`).focus();
    }
    render();
  }

  function resizeSide(side, requested) {
    preferences[side].width = Math.max(LIMITS[side].min, Math.min(maxWidth(side), requested));
    render();
  }

  function finishDrag(cancelled) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancelled) preferences[previous.side].width = previous.preferredWidth;
    else save();
    if (handles[previous.side].hasPointerCapture(previous.pointerId)) handles[previous.side].releasePointerCapture(previous.pointerId);
    document.body.classList.remove('resizing');
    render();
  }

  function bindHandle(side) {
    const handle = handles[side];
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0 || drag || isDrawer(side)) return;
      event.preventDefault();
      drag = { side, pointerId: event.pointerId, x: event.clientX, width: widths[side], preferredWidth: preferences[side].width };
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add('resizing');
    });
    handle.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      resizeSide(side, drag.width + (event.clientX - drag.x) * (side === 'left' ? 1 : -1));
    });
    handle.addEventListener('pointerup', event => { if (event.pointerId === drag?.pointerId) finishDrag(false); });
    handle.addEventListener('pointercancel', () => finishDrag(true));
    handle.addEventListener('lostpointercapture', () => finishDrag(true));
    handle.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const direction = (event.key === 'ArrowRight' ? 1 : -1) * (side === 'left' ? 1 : -1);
      resizeSide(side, widths[side] + direction * 16);
      save();
    });
  }

  function init() {
    if (initialized) return;
    initialized = true;
    mode = viewportMode();
    load();
    if (mode === 'tablet') drawers.right = preferences.right.isOpen;
    panels.left = document.querySelector('#sidebar');
    panels.right = document.querySelector('#inspector');
    for (const side of ['left', 'right']) {
      handles[side] = document.querySelector(`#${side}-resizer`);
      bindHandle(side);
    }
    for (const button of document.querySelectorAll('[data-panel-toggle], [data-panel-open]')) {
      button.addEventListener('click', () => {
        const side = button.dataset.panelToggle || button.dataset.panelOpen;
        setOpen(side, button.dataset.panelOpen ? true : !isOpen(side), button);
      });
    }
    document.querySelector('#panel-backdrop').addEventListener('click', () => setOpen(activeDrawer, false));
    document.addEventListener('keydown', event => {
      if (!activeDrawer) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        setOpen(activeDrawer, false);
      } else if (event.key === 'Tab') {
        const elements = focusable(panels[activeDrawer]);
        const first = elements[0];
        const last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    window.addEventListener('resize', () => {
      finishDrag(true);
      const next = viewportMode();
      if (next !== mode) {
        drawers.left = drawers.right = false;
        mode = next;
        if (mode === 'tablet') drawers.right = preferences.right.isOpen;
      }
      render();
    });
    window.addEventListener('blur', () => finishDrag(true));
    render();
  }

  return { init };
})();
