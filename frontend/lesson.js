// Lesson player: start + calibration, segment reader, adaptive helper, aha toasts, and the debug pane.
const { $, el, icon, css, store, pct, kb } = NL;

const LABELS = { engaged: 'Engaged', confused: 'Possibly confused', aha: 'Aha!', bored: 'Drifting',
  artifact: 'Signal noisy', waiting: 'Waiting', complete: 'Lesson complete', calibrating: 'Calibrating', disconnected: 'Disconnected' };
const MODALITY = { rephrase: 'In simpler words', analogy: 'An analogy', example: 'A worked example',
  diagram: 'A picture of it', quiz: 'A quick check' };
const FEATS = [['theta_f', 'θ frontal'], ['alpha_p', 'α parietal'], ['eng', 'engage'], ['emg_fp', 'brow EMG'], ['gamma_t', 'γ temporal']];

let ws, lesson, allLessons = [], idx = 0, pauseTimer = null, ahaUntil = 0, lastState = 'waiting';
const spark = [];
let channels = [], eegRate = 50, camStream = null, finished = false;
const eeg = [];                    // per channel: last ~6 s of input samples
const visited = new Set(), confusedSegs = new Set();
const offers = {};                 // offer id -> { box, content, revealed }
const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));

// ---------------------------------------------------------------- boot
(async () => {
  allLessons = await NL.lessons();
  const lessonId = NL.params.get('lesson') || (allLessons[0] && allLessons[0].id);
  const pill = el('span', { class: 'pill waiting', id: 'pill' }, el('span', 'dot'), el('span', { id: 'pillText' }, 'Waiting'));
  const dbg = el('button', { class: 'icon-btn ghost', id: 'debugBtn', title: 'Debug panel (D)', 'aria-label': 'Toggle debug panel',
    onclick: () => setDebug(!document.body.classList.contains('debug-open')) }, icon('panel'));
  await NL.nav({ active: 'lesson', lessonId, right: [pill, dbg] });
  $('#paneClose').append(icon('close'));
  $('#paneClose').onclick = () => setDebug(false);
  setDebug(store.get('nl-debug', '0') === '1');
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'd' || e.key === 'D') && !e.target.matches('input, textarea')) setDebug(!document.body.classList.contains('debug-open'));
  });
  document.addEventListener('themechange', () => { drawSpark(); drawEeg(); });
  buildZRows();

  if (!lessonId) { $('#ovTitle').textContent = 'No lessons found'; return; }
  const res = await fetch(`/api/lessons/${encodeURIComponent(lessonId)}`);
  if (!res.ok) { $('#ovTitle').textContent = 'Lesson not found'; return; }
  lesson = await res.json();
  document.title = `NestLearn · ${lesson.title}`;
  $('#ovTitle').textContent = lesson.title;
  $('#ovDesc').textContent = `${lesson.meta.segments} parts · about ${lesson.meta.minutes} min. ${lesson.meta.description}`;
  $('#ovSubject').textContent = lesson.subject || 'Before we start';
  $('#subject').textContent = lesson.subject || 'Lesson';
  $('#lessonTitle').textContent = lesson.title;
  $('#user').value = store.get('nl-user', '');
  buildStepper();
  $('#startBtn').onclick = start;
  $('#user').addEventListener('keydown', (e) => e.key === 'Enter' && start());
})();

function setDebug(on) {
  document.body.classList.toggle('debug-open', on);
  $('#debugBtn') && $('#debugBtn').classList.toggle('on', on);
  store.set('nl-debug', on ? '1' : '0');
  send({ type: 'debug', on });      // the input signal is only streamed while the pane is open
  if (on) requestAnimationFrame(() => { drawSpark(); drawEeg(); });
}

function start() {
  if (!$('#consent').checked) {
    $('.consent').animate([{ transform: 'translateX(-4px)' }, { transform: 'translateX(4px)' }, { transform: 'none' }], 250);
    return;
  }
  const user = ($('#user').value || 'student').trim();
  store.set('nl-user', user);
  $('#startForm').hidden = true;
  $('#calib').hidden = false;
  setPill('calibrating');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?user=${encodeURIComponent(user)}&lesson=${encodeURIComponent(lesson.id)}`);
  ws.onopen = () => send({ type: 'debug', on: document.body.classList.contains('debug-open') });
  ws.onmessage = (e) => onMsg(JSON.parse(e.data));
  ws.onclose = () => { stopCamera(); if (!finished) setPill('disconnected'); };
  startCamera();
}

// Camera preview inside the calibration circle. Local only: the stream is attached to a <video> element
// on this page, never recorded or uploaded, and stopped as soon as calibration ends.
async function startCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: 480, height: 480 }, audio: false });
  } catch { return; }               // denied or no camera: keep the plain breathing circle
  if ($('#overlay').hidden) { stopCamera(); return; }   // calibration already finished
  const v = $('#cam');
  v.srcObject = camStream;
  v.hidden = false;
  $('.breath').classList.add('cam');
  $('#camNote').hidden = false;
  $('#calibHint').textContent = 'Relax, keep your face in the circle, and look at the screen.';
}

function stopCamera() {
  if (camStream) camStream.getTracks().forEach((t) => t.stop());
  camStream = null;
  const v = $('#cam');
  if (v) { v.srcObject = null; v.hidden = true; }
}

// ---------------------------------------------------------------- messages
function onMsg(m) {
  switch (m.type) {
    case 'hello':
      $('#modeBadge').textContent = m.mode === 'sim' ? 'simulated EEG' : m.mode;
      $('#simSec').hidden = m.mode !== 'sim';
      $('#llmBadge').textContent = m.llm.ready ? m.llm.model : 'LLM off · fallback';
      $('#llmBadge').className = 'badge ' + (m.llm.ready ? 'llm' : 'fallback');
      renderProtos(m.profile);
      channels = m.channels || [];
      $('#eegBadge').textContent = `${channels.length} ch`;
      break;
    case 'calibration':
      $('#ring').style.strokeDashoffset = 439.8 * (1 - m.progress);
      if (m.eeg) pushEeg(m.eeg);
      break;
    case 'calibrated':
      $('#ring').style.strokeDashoffset = 0;
      setTimeout(() => { $('#overlay').animate([{ opacity: 1 }, { opacity: 0 }], 250).onfinish = () => { $('#overlay').hidden = true; stopCamera(); }; }, 250);
      showSegment(0, false);
      break;
    case 'state': renderState(m); break;
    case 'intervention': showIntervention(m); break;
    case 'explanation': fillExplanation(m); break;
    case 'insight': toast(m.content, 'aha'); break;
    case 'notice': toast(m.text, 'info'); break;
    case 'memory': renderMemory(m); break;
    case 'trace': renderTrace(m.rec); break;
  }
}

function setPill(state) {
  const p = $('#pill');
  if (!p) return;
  p.className = 'pill ' + state;
  $('#pillText').textContent = LABELS[state] || state;
}

// ---------------------------------------------------------------- live state + debug pane
function renderState(m) {
  if (finished) return;
  if (m.eeg) pushEeg(m.eeg);
  if (m.state === 'aha') ahaUntil = Date.now() + 3000;
  const state = Date.now() < ahaUntil ? 'aha' : m.state;
  if (state !== lastState) { setPill(state); lastState = state; }
  const conf = m.confidence || 0;
  $('#confNum').textContent = conf.toFixed(2);
  const bar = $('#confBar');
  bar.style.width = Math.round(conf * 100) + '%';
  bar.style.background = conf >= 0.65 ? 'var(--confused)' : 'var(--ink-2)';
  $('#stateBadge').textContent = state;
  if (m.z) FEATS.forEach(([k]) => setZ(k, m.z[k]));
  spark.push({ v: conf, s: m.state });
  if (spark.length > 160) spark.shift();
  if (document.body.classList.contains('debug-open')) drawSpark();
  if (m.log) renderLogStats(m.log);
}

function buildZRows() {
  $('#zrows').append(...FEATS.map(([k, label]) => el('div', 'zrow',
    el('span', 'muted', label), el('div', 'zbar', el('i', { id: `z-${k}` })), el('span', { class: 'mono', id: `zv-${k}` }, '0.0'))));
}

function setZ(k, z) {
  const w = Math.min(Math.abs(z), 4) / 4 * 50;
  const i = $(`#z-${k}`);
  i.style.left = z >= 0 ? '50%' : `${50 - w}%`;
  i.style.width = `${w}%`;
  i.style.background = Math.abs(z) > 1.5 ? 'var(--accent)' : 'var(--ink-2)';
  $(`#zv-${k}`).textContent = (z >= 0 ? '+' : '') + z.toFixed(1);
}

function drawSpark() {
  const c = $('#spark'), g = c.getContext('2d');
  const W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  const y = (v) => H - 6 - v * (H - 12);
  g.strokeStyle = css('--line-2'); g.setLineDash([6, 6]); g.lineWidth = 2;
  g.beginPath(); g.moveTo(0, y(0.65)); g.lineTo(W, y(0.65)); g.stroke(); g.setLineDash([]);
  if (spark.length < 2) return;
  const x = (i) => (i / 159) * W;
  const grad = g.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, css('--confused') + '55'); grad.addColorStop(1, css('--confused') + '00');
  g.beginPath(); g.moveTo(x(0), H);
  spark.forEach((p, i) => g.lineTo(x(i), y(p.v)));
  g.lineTo(x(spark.length - 1), H); g.closePath(); g.fillStyle = grad; g.fill();
  g.beginPath(); spark.forEach((p, i) => (i ? g.lineTo(x(i), y(p.v)) : g.moveTo(x(i), y(p.v))));
  g.strokeStyle = css('--ink'); g.lineWidth = 2.5; g.lineJoin = 'round'; g.stroke();
  spark.forEach((p, i) => { if (p.s === 'aha') { g.fillStyle = css('--aha'); g.beginPath(); g.arc(x(i), y(p.v), 6, 0, 7); g.fill(); } });
}

function pushEeg(e) {
  eegRate = e.rate;
  const keep = eegRate * 6;
  e.ch.forEach((row, c) => {
    const buf = eeg[c] || (eeg[c] = []);
    buf.push(...row);
    if (buf.length > keep) buf.splice(0, buf.length - keep);
  });
  if (document.body.classList.contains('debug-open')) drawEeg();
}

function drawEeg() {
  const c = $('#eeg'), g = c.getContext('2d');
  const W = c.width, H = c.height, L = 62, n = eeg.length || channels.length || 8;
  g.clearRect(0, 0, W, H);
  g.font = '20px Inter, system-ui, sans-serif';
  const row = H / n, keep = eegRate * 6, scale = row / 90;      // ~90 uV of range per row
  for (let ch = 0; ch < n; ch++) {
    const mid = row * (ch + 0.5);
    g.fillStyle = css('--muted');
    g.fillText(channels[ch] || `ch${ch + 1}`, 8, mid + 7);
    g.strokeStyle = css('--line'); g.lineWidth = 1;
    g.beginPath(); g.moveTo(L, mid); g.lineTo(W, mid); g.stroke();
    const buf = eeg[ch];
    if (!buf || buf.length < 2) continue;
    const off = keep - buf.length;
    g.beginPath();
    for (let i = 0; i < buf.length; i++) {
      const x = L + ((i + off) / (keep - 1)) * (W - L);
      const y = mid - Math.max(-row * 0.48, Math.min(row * 0.48, buf[i] * scale));
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    }
    g.strokeStyle = ch % 2 ? css('--ink-2') : css('--accent'); g.lineWidth = 1.6; g.lineJoin = 'round'; g.stroke();
  }
  if (!eeg.length) { g.fillStyle = css('--muted'); g.fillText('Waiting for signal…', L + 20, H / 2); }
}

function renderLogStats(l) {
  $('#lgBytes').textContent = kb(l.bytes);
  $('#lgRatio').textContent = `${l.kf_ratio}×`;
  $('#lgEvents').textContent = `${l.events} · ${l.transitions}`;
  $('#lgTol').textContent = l.tol_mult === null ? 'kf off' : `x${l.tol_mult}`;
  $('#lgBudget').style.width = Math.min(100, (l.bytes / l.budget) * 100) + '%';
}

function renderTrace(r) {
  // mark a part on the progress bar only on a real (debounced) confusion onset in that part
  if (r.k === 'st' && r.to === 'confused' && r.seg) { confusedSegs.add(r.seg); if (!finished) paintStepper(); }
  const feed = $('#feed');
  let text;
  if (r.k === 'kf') text = `t=${r.ts}  conf=${r.v[0]}`;
  else if (r.k === 'st') text = `${r.from} → ${r.to}  (${r.dwell}s)`;
  else if (r.k === 'ev') text = `${r.kind}${r.modality ? ' ' + r.modality : ''}${r.source && r.source !== 'pending' ? ' [' + r.source + ']' : ''}`;
  else if (r.k === 'ep') text = `episode ${r.seg} ${r.dur}s → ${r.outcome}${r.helped_by ? ' via ' + r.helped_by : ''}`;
  else if (r.k === 'seg') text = `left ${r.seg} after ${r.dwell}s`;
  else text = r.k;
  feed.prepend(el('div', null, el('span', `k k-${r.k}`, r.k), el('span', null, text)));
  while (feed.children.length > 80) feed.lastChild.remove();
}

function renderMemory(m) {
  const u = m.update;
  const verb = { new: 'new prototype', delta: 'delta merged into', reinforce: 'reinforced' }[u.op] || u.op;
  $('#memUpdate').replaceChildren(
    el('span', 'strong', u.channel), ' · ', verb, ' ', el('span', 'mono', u.proto),
    u.delta && u.delta.length ? el('span', 'muted', ` (+${u.delta.join(', ')})`) : el('span', 'muted', ' (nothing new stored)'),
    u.compacted ? el('span', 'muted', ` · ${u.compacted}`) : null);
  renderProtos(m.snapshot, u.proto);
}

function renderProtos(snap, fresh) {
  if (!snap) return;
  const box = $('#memProtos');
  box.replaceChildren();
  [['confused', 'c'], ['aha', 'a']].forEach(([ch, cls]) => {
    (snap[ch] || []).slice(0, 4).forEach((p) => box.append(el('div', 'proto',
      el('span', 'w', p.w.toFixed(2)),
      el('div', 'chips', el('span', `chip ${cls}${p.id === fresh ? ' new' : ''}`, `${ch} ×${p.n}`),
        p.terms.slice(0, 4).map((t) => el('span', 'chip', t))))));
  });
  if (!box.children.length) box.append(el('p', 'small muted', 'Empty. This student has no history yet.'));
}

// ---------------------------------------------------------------- reader
function buildStepper() {
  $('#stepper').replaceChildren(...lesson.segments.map((s, i) =>
    el('button', { class: 'step', title: s.title, 'aria-label': `Part ${i + 1}: ${s.title}`,
      onclick: () => { if (visited.has(s.id)) { if (i < idx) send({ type: 'behavior', kind: 'scrollback' }); showSegment(i); } } })));
}

function paintStepper() {
  [...$('#stepper').children].forEach((b, i) => {
    const id = lesson.segments[i].id;
    b.className = 'step' + (i === idx ? ' current' : visited.has(id) ? ' done' : '') + (confusedSegs.has(id) ? ' c' : '');
  });
}

function showSegment(i, animate = true) {
  const root = $('#lesson');
  const old = root.firstElementChild;
  const render = () => {
    idx = i;
    const seg = lesson.segments[i];
    visited.add(seg.id);
    paintStepper();
    const last = i === lesson.segments.length - 1;
    const node = el('article', 'segment',
      el('div', 'part', `Part ${i + 1} of ${lesson.segments.length}`),
      el('h2', null, seg.title),
      el('div', 'prose', seg.body.split('\n\n').map((p) => el('p', null, p))),
      seg.quiz ? renderQuiz(seg.quiz, seg.id) : null,
      el('div', { id: 'helperSlot' }),
      el('div', 'lesson-nav',
        el('button', { disabled: i === 0, onclick: () => { send({ type: 'behavior', kind: 'scrollback' }); showSegment(i - 1); } }, 'Back'),
        el('button', { class: 'ghost', onclick: () => send({ type: 'request_alt' }) }, 'Explain it differently'),
        el('span', 'spacer'),
        el('button', { class: 'primary', onclick: () => (last ? finish() : showSegment(i + 1)) }, last ? 'Finish' : 'Next', icon('arrow'))));
    root.replaceChildren(node);
    window.scrollTo({ top: 0, behavior: animate ? 'smooth' : 'auto' });
    send({ type: 'segment', id: seg.id });
    clearTimeout(pauseTimer);
    pauseTimer = setTimeout(() => send({ type: 'behavior', kind: 'long_pause' }), 45000);
  };
  if (old && animate) { old.classList.add('out'); setTimeout(render, 200); } else render();
}

function renderQuiz(q, segId) {
  const t0 = Date.now();
  const fb = el('p', 'small muted', '');
  const box = el('div', 'quiz', el('div', 'q', q.question));
  const opts = el('div', 'opts', q.options.map((opt, k) => el('button', {
    onclick: (e) => {
      const correct = k === q.answer;
      send({ type: 'quiz', segment: segId, correct, rt: (Date.now() - t0) / 1000 });
      fb.textContent = correct ? 'Correct.' : 'Not quite. The explanation above has the key idea.';
      opts.querySelectorAll('button').forEach((x) => (x.disabled = true));
      e.currentTarget.classList.add(correct ? 'good' : 'bad');
      if (!correct) opts.children[q.answer].classList.add('good');
    } }, opt)));
  box.append(opts, fb);
  return box;
}

// ---------------------------------------------------------------- adaptive helper
function showIntervention(m) {
  const slot = $('#helperSlot');
  if (!slot || lesson.segments[idx].id !== m.segment) return;
  const body = el('div', 'body');
  const src = el('div', 'src');
  const inner = el('div', 'inner');
  const box = el('div', 'helper', el('div', 'bar-top'), inner);
  offers[m.id] = { body, src, content: m.content, source: m.source, revealed: false, quiz: m.modality === 'quiz' };

  const reveal = () => {
    offers[m.id].revealed = true;
    inner.replaceChildren(el('div', 'kicker', MODALITY[m.modality] || m.modality), body, src);
    paintOffer(m.id, m.segment);
    send({ type: 'intervention_response', id: m.id, accepted: true });
  };
  if (!m.auto) {
    slot.replaceChildren(box);
    reveal();
  } else {
    inner.append(el('div', 'kicker', 'Need another angle?'),
      el('p', { style: { margin: '0 0 12px' } }, 'This part looks tricky. Want it explained a different way?'),
      el('div', 'row',
        el('button', { class: 'primary', onclick: reveal }, 'Show me'),
        el('button', { class: 'ghost', onclick: () => { box.remove(); send({ type: 'intervention_response', id: m.id, accepted: false }); } }, 'Not now')));
    slot.replaceChildren(box);
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function paintOffer(id, segId) {
  const o = offers[id];
  if (!o || !o.revealed) return;
  if (o.quiz) { o.body.replaceChildren(renderQuiz(o.content, segId)); return; }
  if (!o.content) { o.body.replaceChildren(el('span', 'typing', el('i'), el('i'), el('i'))); return; }
  o.body.textContent = o.content;
  o.body.animate([{ opacity: 0 }, { opacity: 1 }], 350);
  const label = { llm: 'Generated for you', lesson: "From the lesson author", fallback: 'Offline fallback' }[o.source] || '';
  o.src.replaceChildren(el('span', `badge ${o.source}`, label));
}

function fillExplanation(m) {
  const o = offers[m.id];
  if (o) { o.content = m.content; o.source = m.source; paintOffer(m.id, m.segment); }
  $('#lastExpl').replaceChildren(el('span', `badge ${m.source}`, m.source), ` ${m.modality} · ${m.ms} ms`,
    m.error && m.source !== 'llm' ? el('span', 'muted', ` · ${m.error.slice(0, 60)}`) : null);
  $('#ctxText').textContent = m.context;
}

function toast(text, kind) {
  const t = el('div', 'toast', el('span', 'ic', kind === 'aha' ? '✓' : 'i'), el('div', null,
    el('div', 'strong small', kind === 'aha' ? 'That clicked' : 'Note'), el('div', 'small', text)));
  $('#toasts').append(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 7000);
}

// ---------------------------------------------------------------- finish
function finish() {
  clearTimeout(pauseTimer);
  send({ type: 'finish' });
  finished = true;
  idx = lesson.segments.length;
  paintStepper();
  setPill('complete');
  $('#stateBadge').textContent = 'finished';
  setTimeout(() => ws && ws.close(), 300);      // ends the session so the log gets its footer
  const i = allLessons.findIndex((l) => l.id === lesson.id);
  const next = allLessons[i + 1];
  $('#lesson').replaceChildren(el('div', 'segment done-card',
    el('div', 'big', 'Lesson complete'),
    el('p', 'muted', `You worked through ${lesson.segments.length} parts.`),
    el('div', { class: 'row', style: { justifyContent: 'center', marginTop: '18px' } }),
  ));
  $('.done-card .row').append(
    el('a', { class: 'btn', href: '/' }, 'Library'),
    next ? el('a', { class: 'btn primary', href: `/lesson.html?lesson=${encodeURIComponent(next.id)}` }, `Next: ${next.title}`) : null,
    el('a', { class: 'btn ghost', href: `/dashboard.html?lesson=${encodeURIComponent(lesson.id)}` }, 'Instructor view'));
}

document.querySelectorAll('[data-sim]').forEach((b) => { b.onclick = () => send({ type: 'sim', state: b.dataset.sim }); });
