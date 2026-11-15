// Instructor view. Everything here is read from the NestTrace log files via the API and refreshed
// whenever the logs change (cheap version check every 3 s).
const { $, el, css, pct, secs, kb } = NL;
const STATE_COLOR = { confused: '--confused', bored: '--bored', aha: '--aha' };

let lessonId = NL.params.get('lesson');
let version = null, selected = NL.params.get('session'), detail = null, lessonsList = [];
let firstPaint = true, detailKey = null;   // animate bars only on first paint, rebuild detail only when it changed

(async () => {
  lessonsList = await NL.lessons();
  if (!lessonId && lessonsList.length) lessonId = lessonsList[0].id;
  await NL.nav({ active: 'dashboard', lessonId });
  renderTabs();
  document.addEventListener('themechange', () => detail && drawTimeline());
  window.addEventListener('resize', () => detail && drawTimeline());
  await refresh(true);
  setInterval(() => refresh(false), 3000);
})();

function renderTabs() {
  $('#tabs').replaceChildren(...lessonsList.map((l) => el('button', {
    class: 'tab' + (l.id === lessonId ? ' active' : ''), title: l.title,
    onclick: () => { lessonId = l.id; selected = null; detail = null; version = null; firstPaint = true; detailKey = null; history.replaceState(null, '', `?lesson=${l.id}`);
      renderTabs(); $('#detail').replaceChildren(el('div', 'empty', 'Pick a session to see its timeline.')); refresh(true); } },
    l.title.length > 34 ? l.title.slice(0, 32) + '…' : l.title)));
}

async function refresh(force) {
  if (!lessonId) { $('#title').textContent = 'No lessons'; return; }
  const v = (await (await fetch(`/api/version/${lessonId}`)).json()).version;
  if (!force && v === version) return;
  version = v;
  const [a, sessions] = await Promise.all([
    fetch(`/api/analytics/${lessonId}`).then((r) => r.json()),
    fetch(`/api/sessions/${lessonId}`).then((r) => r.json())]);
  renderSummary(a, sessions);
  renderRows(a);
  renderTop(a);
  renderMods(a);
  renderSessions(sessions);
  firstPaint = false;
  if (selected) loadDetail(selected, false);
}

function renderSummary(a, sessions) {
  $('#title').textContent = a.title;
  const live = sessions.filter((s) => s.live).length;
  $('#meta').replaceChildren(
    live ? el('span', null, el('span', 'live-dot'), `${live} live now · `) : '',
    `${a.sessions.live} real + ${a.sessions.demo} simulated sessions · updated ${new Date().toLocaleTimeString()}`);
  const bytes = sessions.reduce((t, s) => t + s.bytes, 0);
  const ticks = sessions.reduce((t, s) => t + (s.ticks || 0), 0);
  const kfs = sessions.reduce((t, s) => t + (s.ticks ? s.keyframes : 0), 0);
  const med = a.segments.map((s) => s.median_episode_s).filter((x) => x !== null);
  const kpi = (v, l, sub) => el('div', 'card kpi' + (firstPaint ? ' reveal' : ''), el('div', 'v', v), el('div', 'l', l), sub ? el('div', 'tiny muted', sub) : null);
  $('#kpis').replaceChildren(
    kpi(String(sessions.length), 'sessions logged', live ? `${live} live` : null),
    kpi(String(a.totals.episodes), 'confusion episodes', 'relapses merged'),
    kpi(pct(a.totals.resolution_rate), 'ended in an aha', `${a.totals.resolved_by_aha} episodes`),
    kpi(med.length ? secs(med.reduce((x, y) => x + y) / med.length) : '–', 'typical episode length'),
    kpi(kb(bytes), 'total log size', ticks ? `${(ticks / Math.max(kfs, 1)).toFixed(1)} ticks per keyframe` : null));
}

function hbar(v, cls) {
  const fill = el('div', `fill ${cls}`);
  if (firstPaint) requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = pct(v || 0); }));
  else { fill.style.transition = 'none'; fill.style.width = pct(v || 0); }
  return el('div', 'hbar', el('div', 'track', fill), el('span', null, pct(v)));
}

function renderRows(a) {
  const rows = $('#rows');
  rows.replaceChildren();
  a.segments.forEach((s) => {
    const helped = Object.entries(s.helped_by || {}).sort((x, y) => y[1] - x[1]);
    rows.append(el('div', 'seg-row',
      el('div', null, el('div', 'strong', s.title),
        el('div', 'tiny muted', `${s.visits} visits` + (s.quiz_accuracy !== null ? ` · quiz ${pct(s.quiz_accuracy)}` : '') +
          (s.median_dwell_s !== null ? ` · ${secs(s.median_dwell_s)} typical` : '')),
        helped.length ? el('div', { class: 'chips', style: { marginTop: '5px' } }) : null),
      hbar(s.confusion_rate, 'c'), hbar(s.aha_rate, 'a'), hbar(s.time_confused, 'n')));
    if (helped.length) {
      rows.lastChild.querySelector('.chips').append(...helped.map(([m, n]) => el('span', 'chip a', `${m} helped ×${n}`)));
    }
  });
}

function renderTop(a) {
  const top = $('#top');
  top.replaceChildren();
  [...a.segments].sort((x, y) => y.confusion_rate - x.confusion_rate).filter((s) => s.confusion_rate > 0).slice(0, 3)
    .forEach((s) => {
      const out = el('pre', 'suggest');
      out.hidden = true;
      const btn = el('button', { class: 'ghost', onclick: async () => {
        btn.disabled = true; out.hidden = false; out.replaceChildren(el('span', 'typing', el('i'), el('i'), el('i')));
        const res = await fetch(`/api/rewrite/${lessonId}/${s.id}`, { method: 'POST' });
        const j = await res.json();
        out.textContent = j.suggestion || j.detail || 'No suggestion.';
        btn.disabled = false;
      } }, 'Suggest rewrite');
      const outcomes = s.outcomes || {};
      top.append(el('div', { style: { padding: '10px 0', borderBottom: '1px solid var(--line)' } },
        el('div', 'row', el('div', null, el('div', 'strong', s.title),
          el('div', 'tiny muted', `${pct(s.confusion_rate)} confused · ${s.episodes} episodes · ` +
            `${outcomes.aha || 0} resolved, ${(outcomes.faded || 0)} faded, ${(outcomes.moved_on || 0) + (outcomes.session_end || 0)} unresolved` +
            (s.relapses ? ` · ${s.relapses} relapses` : ''))),
          el('span', 'spacer'), btn), out));
    });
  if (!top.children.length) top.append(el('p', 'muted', 'No confusing parts yet.'));
}

function renderMods(a) {
  const box = $('#mods');
  const entries = Object.entries(a.modality || {}).sort((x, y) => (y[1].aha_rate || 0) - (x[1].aha_rate || 0));
  box.replaceChildren(...entries.map(([m, s]) => el('div', { style: { display: 'grid', gridTemplateColumns: '90px 1fr 92px', gap: '12px', alignItems: 'center', padding: '7px 0' } },
    el('span', 'strong small', m), hbar(s.aha_rate, 'a'),
    el('span', 'tiny muted', `${s.aha}/${s.offered} offers` + (s.llm ? ` · ${s.llm} LLM` : '')))));
  if (!entries.length) box.append(el('p', 'muted', 'No interventions yet.'));
}

function renderSessions(list) {
  $('#sessCount').textContent = `${list.length}`;
  const box = $('#sessions');
  const keepScroll = box.scrollTop;
  box.replaceChildren(...list.map((s) => el('div', { class: 'sess' + (s.session === selected ? ' sel' : ''),
    onclick: (e) => { selected = s.session; box.querySelectorAll('.sess').forEach((x) => x.classList.remove('sel'));
      e.currentTarget.classList.add('sel'); loadDetail(s.session, true); } },
    el('div', 'strong small', s.live ? el('span', 'live-dot') : null, s.user, s.demo ? el('span', 'tag-demo', 'sim') : null),
    el('div', 'tiny muted', new Date(s.start * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    el('div', 'tiny muted', `${secs(s.duration)} · ${s.onsets} confused · ${s.ahas} aha`),
    el('div', 'tiny muted', kb(s.bytes)))));
  box.scrollTop = keepScroll;
  if (!list.length) box.append(el('p', 'muted small', 'No sessions yet. Run scripts/seed_demo.py or start a lesson.'));
}

// ---------------------------------------------------------------- session detail
async function loadDetail(id, scroll) {
  const r = await fetch(`/api/sessions/${lessonId}/${encodeURIComponent(id)}`);
  if (!r.ok) return;
  const next = await r.json();
  const key = `${id}:${next.bytes}`;
  if (key === detailKey && !scroll) return;
  detailKey = key;
  detail = next;
  const d = detail, f = d.footer;
  const segTitle = Object.fromEntries((lessonsList.find((l) => l.id === lessonId)?.outline || []).map((s) => [s.id, s.title]));
  const canvas = el('canvas', { class: 'timeline', id: 'tl', width: 1400, height: 380 });
  const tip = el('div', { class: 'tiny muted', id: 'tip', style: { minHeight: '18px', marginTop: '6px' } }, 'Hover the timeline to inspect.');
  canvas.addEventListener('mousemove', (e) => hover(e, canvas, tip));
  const eps = d.episodes.map((e) => el('div', 'ep',
    el('span', 'mono', `${secs(e.start)}`),
    el('div', null, el('div', 'small strong', segTitle[e.seg] || e.seg),
      el('div', 'tiny muted', `${secs(e.dur)} · peak ${e.peak.toFixed(2)}` + (e.relapses ? ` · ${e.relapses} relapse` : '') +
        (e.offers.length ? ` · offered ${e.offers.map((o) => o.m + (o.acc === true ? ' ✓' : o.acc === false ? ' ✗' : '')).join(', ')}` : ''))),
    el('span', `small out-${e.outcome}`, e.outcome.replace('_', ' ') + (e.helped_by ? ` (${e.helped_by})` : ''))));
  const prof = d.profile || {};
  const protoChips = (ch, cls) => (prof[ch] || []).slice(0, 4).map((p) => el('div', 'proto',
    el('span', 'w', `×${p.n}`), el('div', 'chips', el('span', `chip ${cls}`, ch), p.terms.slice(0, 4).map((t) => el('span', 'chip', t)))));
  const lift = (prof.lift || []).map(([n, v]) => `${v > 0 ? 'more' : 'less'} confused on ${n} text (${v > 0 ? '+' : ''}${v})`).join('; ');

  $('#detail').replaceChildren(
    el('div', 'panel-head', el('h3', null, `Session · ${d.header.user}`), el('span', 'spacer'),
      el('span', 'tiny muted', f ? `ended (${f.reason})` : 'in progress')),
    canvas,
    el('div', 'legend', el('span', null, el('i', { style: { background: css('--ink') } }), 'confusion confidence (keyframes)'),
      el('span', null, el('i', { style: { background: css('--confused') + '33' } }), 'confused'),
      el('span', null, el('i', { style: { background: css('--bored') + '33' } }), 'drifting'),
      el('span', null, el('i', { style: { background: css('--aha') } }), 'aha'),
      el('span', null, el('i', { style: { background: css('--accent') } }), 'help offered')),
    tip,
    el('div', { class: 'cols', style: { marginTop: '14px' } }),
  );
  const cols = $('#detail .cols');
  cols.append(
    el('div', null, el('h3', null, `Confusion episodes (${d.episodes.length})`),
      eps.length ? el('div', null, eps) : el('p', 'muted small', 'None.')),
    el('div', null, el('h3', null, 'Learner profile (Concept Delta Memory)'),
      protoChips('confused', 'c'), protoChips('aha', 'a'),
      lift ? el('p', 'tiny muted', lift) : null,
      el('h3', { style: { marginTop: '16px' } }, 'Log'),
      el('div', 'stat-grid',
        el('div', 'stat', el('div', 'v', kb(d.bytes)), el('div', 'l', 'file size')),
        el('div', 'stat', el('div', 'v', f ? `${f.kf_ratio}×` : '…'), el('div', 'l', 'ticks per keyframe')),
        el('div', 'stat', el('div', 'v', String(d.transitions.length)), el('div', 'l', 'state transitions')),
        el('div', 'stat', el('div', 'v', String(d.events.length)), el('div', 'l', 'events')))));
  drawTimeline();
  if (scroll) $('#detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function geom(canvas) {
  const d = detail;
  const end = Math.max(1, ...d.curve.map((p) => p.ts), ...d.events.map((e) => e.ts));
  const start = (d.events.find((e) => e.kind === 'calibrated') || { ts: 0 }).ts;
  const W = canvas.width, H = canvas.height, L = 50, R = 16, T = 34, B = 30;
  return { start, end, W, H, L, R, T, B, x: (t) => L + ((t - start) / (end - start || 1)) * (W - L - R), y: (v) => T + (1 - v) * (H - T - B) };
}

function drawTimeline() {
  const c = $('#tl');
  if (!c || !detail) return;
  c.width = c.clientWidth * 2;
  const g = c.getContext('2d');
  const G = geom(c), d = detail;
  g.clearRect(0, 0, G.W, G.H);
  g.font = '22px Inter, system-ui, sans-serif';

  // state bands from debounced transitions
  const tr = d.transitions;
  tr.forEach((t, i) => {
    const col = STATE_COLOR[t.to];
    if (!col || t.to === 'aha') return;
    const nxt = tr.slice(i + 1).find((u) => u.to !== 'aha');
    const x0 = G.x(t.ts), x1 = G.x(nxt ? nxt.ts : G.end);
    g.fillStyle = css(col) + '26';
    g.fillRect(x0, G.T, x1 - x0, G.H - G.T - G.B);
  });
  // segment boundaries
  const outline = Object.fromEntries((lessonsList.find((l) => l.id === lessonId)?.outline || []).map((s, i) => [s.id, i + 1]));
  g.strokeStyle = css('--line-2'); g.fillStyle = css('--muted'); g.lineWidth = 2;
  d.events.filter((e) => e.kind === 'segment_enter').forEach((e) => {
    const x = G.x(e.ts);
    g.beginPath(); g.moveTo(x, G.T - 8); g.lineTo(x, G.H - G.B); g.stroke();
    g.fillText(`Part ${outline[e.seg] || e.seg}`, x + 6, G.T - 12);
  });
  // axes + threshold
  g.fillStyle = css('--muted');
  [0, 0.65, 1].forEach((v) => g.fillText(v.toFixed(2), 2, G.y(v) + 7));
  g.setLineDash([8, 8]); g.strokeStyle = css('--confused') + '99';
  g.beginPath(); g.moveTo(G.L, G.y(0.65)); g.lineTo(G.W - G.R, G.y(0.65)); g.stroke(); g.setLineDash([]);
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = G.start + ((G.end - G.start) * i) / ticks;
    g.fillText(secs(t - G.start), G.x(t) - (i === ticks ? 40 : 10), G.H - 6);
  }
  // reconstructed curve
  const pts = d.curve.filter((p) => p.ts >= G.start);
  g.beginPath();
  pts.forEach((p, i) => (i ? g.lineTo(G.x(p.ts), G.y(p.v[0])) : g.moveTo(G.x(p.ts), G.y(p.v[0]))));
  g.strokeStyle = css('--ink'); g.lineWidth = 3; g.lineJoin = 'round'; g.stroke();
  // markers
  d.events.forEach((e) => {
    const x = G.x(e.ts);
    if (e.kind === 'intervention') {
      g.fillStyle = css('--accent');
      g.beginPath(); g.moveTo(x, G.H - G.B - 2); g.lineTo(x - 8, G.H - G.B + 12); g.lineTo(x + 8, G.H - G.B + 12); g.fill();
    } else if (e.kind === 'aha') {
      g.fillStyle = css('--aha'); g.beginPath(); g.arc(x, G.y(0.95), 9, 0, 7); g.fill();
    } else if (e.kind === 'quiz') {
      g.fillStyle = e.correct ? css('--aha') : css('--confused'); g.fillRect(x - 6, G.y(0.05) - 6, 12, 12);
    }
  });
}

function hover(e, c, tip) {
  if (!detail) return;
  const G = geom(c);
  const rect = c.getBoundingClientRect();
  const px = ((e.clientX - rect.left) / rect.width) * G.W;
  const t = G.start + ((px - G.L) / (G.W - G.L - G.R)) * (G.end - G.start);
  const pts = detail.curve;
  let v = null;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i].ts >= t) { const a = pts[i - 1], b = pts[i], f = (t - a.ts) / (b.ts - a.ts || 1); v = a.v[0] + f * (b.v[0] - a.v[0]); break; }
  }
  const near = detail.events.filter((ev) => Math.abs(ev.ts - t) < (G.end - G.start) / 80 && ev.kind !== 'explanation')
    .map((ev) => ev.kind.replace('_', ' ') + (ev.modality ? ` (${ev.modality})` : '')).join(', ');
  tip.textContent = v === null ? '' : `t = ${secs(Math.max(0, t - G.start))} · confidence ≈ ${v.toFixed(2)}${near ? ' · ' + near : ''}`;
}
