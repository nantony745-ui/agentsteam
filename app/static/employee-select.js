'use strict';

const EmployeeSelect = (() => {
  let active = null;
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text) element.textContent = text;
    return element;
  };
  function icon(harness) {
    const image = node('img', 'employee-agent-icon');
    image.src = `/icons/agents/${harness}.svg`; image.alt = '';
    return image;
  }
  function mount(select, {label, searchable = false, icons = false}) {
    const wrapper = node('div', 'employee-select');
    const trigger = node('button', 'employee-select-trigger');
    const popup = node('div', 'employee-select-popup');
    const list = node('div', 'employee-select-options');
    const search = node('input', 'employee-select-search');
    const value = node('span', 'employee-select-value');
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    arrow.setAttribute('viewBox', '0 0 24 24'); arrow.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm8 10 4 4 4-4'); arrow.append(path);
    trigger.type = 'button'; trigger.id = select.id + '-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox'); trigger.setAttribute('aria-expanded', 'false');
    popup.id = select.id + '-popup'; popup.popover = 'auto';
    list.id = select.id + '-options'; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', label);
    trigger.setAttribute('aria-controls', list.id);
    trigger.append(value, arrow);
    if (searchable) {
      search.type = 'search'; search.placeholder = '搜索模型名称或 ID…';
      search.setAttribute('aria-label', '搜索' + label); search.autocomplete = 'off';
      search.setAttribute('role', 'combobox'); search.setAttribute('aria-controls', list.id);
      search.setAttribute('aria-expanded', 'false'); search.setAttribute('aria-autocomplete', 'list');
      popup.append(search);
    }
    popup.append(list); wrapper.append(trigger, popup);
    select.after(wrapper); select.hidden = true;
    let buttons = [], selectedIndex = -1;
    const opened = () => popup.matches(':popover-open');
    function sync() {
      const option = select.selectedOptions[0];
      value.textContent = option?.textContent || '请选择';
      trigger.querySelector('img')?.remove();
      if (icons && select.value) trigger.prepend(icon(select.value));
      trigger.disabled = select.disabled;
      trigger.setAttribute('aria-label', `${label}：${value.textContent}`);
    }
    function highlight(index) {
      selectedIndex = index;
      buttons.forEach((button, i) => button.dataset.active = String(i === index));
      for (const input of [trigger, search]) {
        if (buttons[index]) input.setAttribute('aria-activedescendant', buttons[index].id);
        else input.removeAttribute('aria-activedescendant');
      }
      if (buttons[index]) {
        const option = buttons[index].getBoundingClientRect(), viewport = list.getBoundingClientRect();
        if (option.top < viewport.top) list.scrollTop -= viewport.top - option.top;
        else if (option.bottom > viewport.bottom) list.scrollTop += option.bottom - viewport.bottom;
      }
    }
    function close() { if (opened()) popup.hidePopover(); }
    function render() {
      list.replaceChildren(); buttons = [];
      const query = search.value.trim().toLocaleLowerCase();
      for (const option of select.options) {
        if (option.disabled || (query && !`${option.textContent} ${option.value}`.toLocaleLowerCase().includes(query))) continue;
        const button = node('button', 'employee-select-option');
        button.type = 'button'; button.id = `${list.id}-${buttons.length}`;
        button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(option.selected));
        button.tabIndex = -1;
        if (icons && option.value) button.append(icon(option.value));
        const text = node('span', 'employee-option-text');
        text.append(node('span', 'employee-option-name', option.textContent));
        if (searchable && option.value && option.value !== option.textContent) text.append(node('span', 'employee-option-id', option.value));
        button.append(text, node('span', 'employee-option-check', option.selected ? '✓' : ''));
        button.addEventListener('click', () => {
          select.value = option.value; sync(); close(); trigger.focus({preventScroll:true});
          select.dispatchEvent(new Event('change', {bubbles:true}));
        });
        button.addEventListener('pointermove', () => {
          selectedIndex = buttons.indexOf(button);
          buttons.forEach(item => item.dataset.active = String(item === button));
        });
        buttons.push(button); list.append(button);
      }
      if (!buttons.length) list.append(node('p', 'employee-select-empty', '没有匹配的选项'));
      highlight(Math.max(0, buttons.findIndex(button => button.getAttribute('aria-selected') === 'true')));
    }
    function position() {
      const rect = trigger.getBoundingClientRect();
      if (!rect.width || rect.bottom < 0 || rect.top > innerHeight) { close(); return; }
      const below = innerHeight - rect.bottom - 12, above = rect.top - 12;
      const upward = below < 240 && above > below;
      popup.style.width = `${Math.min(Math.max(rect.width, searchable ? 300 : 0), innerWidth - 24)}px`;
      popup.style.maxHeight = `${Math.min(360, upward ? above : below)}px`;
      popup.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - popup.offsetWidth - 12))}px`;
      popup.style.top = `${upward ? rect.top - popup.offsetHeight - 6 : rect.bottom + 6}px`;
    }
    function open() {
      if (opened()) { close(); return; }
      active?.close(); search.value = '';
      popup.showPopover(); render(); position();
      active = {close, position};
      (searchable ? search : trigger).focus({preventScroll:true});
    }
    function keydown(event) {
      if (event.isComposing) return;
      if (!opened()) {
        if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); open(); }
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); close(); trigger.focus({preventScroll:true}); }
      else if (event.key === 'Tab') { close(); trigger.focus({preventScroll:true}); }
      else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : selectedIndex + (event.key === 'ArrowDown' ? 1 : -1);
        highlight(Math.max(0, Math.min(buttons.length - 1, index)));
      } else if (event.key === 'Enter' || (event.key === ' ' && event.target === trigger)) {
        event.preventDefault(); buttons[selectedIndex]?.click();
      }
    }
    popup.addEventListener('toggle', event => {
      const open = event.newState === 'open';
      trigger.setAttribute('aria-expanded', String(open)); search.setAttribute('aria-expanded', String(open));
      if (!open && active?.close === close) active = null;
    });
    trigger.addEventListener('click', open);
    trigger.addEventListener('keydown', keydown); popup.addEventListener('keydown', keydown);
    search.addEventListener('input', () => { render(); position(); });
    select.addEventListener('change', sync);
    sync();
    return {sync, close, destroy() {
      close(); if (active?.close === close) active = null;
      select.removeEventListener('change', sync); select.hidden = false; wrapper.remove();
    }};
  }
  window.addEventListener('resize', () => active?.position());
  document.addEventListener('scroll', () => active?.position(), true);
  window.addEventListener('hashchange', () => active?.close());
  return {mount, icon};
})();
