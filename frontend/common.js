// Shared UI: theme, navbar, small DOM helpers. Loaded by every page.
const NL = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    if (typeof attrs === 'string') e.className = attrs;
    else if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k === 'html') e.innerHTML = v;            // only ever used with our own static SVG strings
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (k === 'style') Object.assign(e.style, v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false)
      e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return e;
  }

  const ICON = {
    logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3.5 13.5c2 4.5 15 4.5 17 0"/><path d="M5.5 11.5c2.5 3 10.5 3 13 0"/><circle cx="9.5" cy="8" r="2.1" fill="currentColor" stroke="none"/><circle cx="14.5" cy="8" r="2.1" fill="var(--accent)" stroke="none"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>',
    panel: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M15 4v16"/><path d="M17.5 8.5h1M17.5 11.5h1"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    chev: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  };
  const icon = (name) => el('span', { html: ICON[name], style: { display: 'inline-flex' } });

  // ---- theme ----
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    store.set('nl-theme', t);
    const b = $('#themeBtn');
    if (b) { b.replaceChildren(icon(t === 'dark' ? 'sun' : 'moon')); b.title = t === 'dark' ? 'Light theme' : 'Dark theme'; }
    document.dispatchEvent(new CustomEvent('themechange'));
  }
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---- navbar ----
  let lessonsCache = null;
  async function lessons() {
    if (!lessonsCache) lessonsCache = fetch('/api/lessons').then((r) => r.json()).catch(() => []);
    return lessonsCache;
  }

  async function nav({ active, lessonId, right = [] } = {}) {
    const list = await lessons();
    const current = list.find((l) => l.id === lessonId);
    const menu = el('div', 'menu');
    const menuBtn = el('button', { class: 'ghost nav-link', 'aria-haspopup': 'true',
      onclick: (e) => { e.stopPropagation(); menu.classList.toggle('open'); } }, 'Lessons', icon('chev'));
    const items = el('div', 'menu-list', list.map((l) =>
      el('a', { href: `/lesson.html?lesson=${encodeURIComponent(l.id)}` }, l.title,
        el('span', 'tiny', `${l.segments} parts · ~${l.minutes} min`))));
    menu.append(menuBtn, items);
    document.addEventListener('click', () => menu.classList.remove('open'));

    const links = el('nav', 'nav-links',
      el('a', { class: 'nav-link' + (active === 'library' ? ' active' : ''), href: '/' }, 'Library'),
      menu,
      current && active === 'lesson' ? el('a', { class: 'nav-link active', href: location.href, title: current.title }, current.title) : null,
      el('a', { class: 'nav-link' + (active === 'dashboard' ? ' active' : ''),
        href: '/dashboard.html' + (lessonId ? `?lesson=${encodeURIComponent(lessonId)}` : '') }, 'Instructor'));

    const themeBtn = el('button', { id: 'themeBtn', class: 'icon-btn ghost', 'aria-label': 'Toggle theme',
      onclick: () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark') });
    const bar = el('header', 'nav',
      el('a', { class: 'brand', href: '/' }, icon('logo'), el('span', null, 'NestLearn')),
      links, el('div', 'nav-right', ...right, themeBtn));
    document.body.prepend(bar);
    setTheme(document.documentElement.dataset.theme || 'light');
    return { lessons: list, current };
  }

  const pct = (v) => (v === null || v === undefined ? '–' : Math.round(v * 100) + '%');
  const secs = (s) => (s === null || s === undefined ? '–' : s < 10 ? `${(Math.round(s * 10) / 10)}s` : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
  const kb = (b) => (b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(2)} MB`);
  const params = new URLSearchParams(location.search);

  return { $, el, icon, nav, setTheme, css, store, pct, secs, kb, params, lessons };
})();
