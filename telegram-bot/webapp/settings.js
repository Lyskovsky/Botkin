const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

let settings = {};
const API = '/api/settings';
const SLOTS = {
  morning_before: '☀️ Утро (до еды)',
  morning_with:   '🌅 Утро (с завтраком)',
  evening:        '🌙 Вечер'
};

// ── Navigation ──────────────────────────────────────────────────────────────
function go(section) {
  document.querySelectorAll('#settings-section .section').forEach(s => s.classList.remove('active'));
  document.getElementById(section).classList.add('active');
}

function switchTab(tab) {
  flushDirtyInputs(); // save any typed-but-not-blurred inputs before leaving settings
  // Update tab bar active state
  document.querySelectorAll('.tab-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  // Show/hide sections
  const sectionMap = {
    'day': 'day-section',
    'supplements-tab': 'supplements-tab-section',
    'dashboard': 'dashboard-tab-section',
    'settings': 'settings-section',
  };
  document.querySelectorAll('.tab-panel').forEach(s => s.classList.remove('active'));
  const target = document.getElementById(sectionMap[tab]);
  if (target) target.classList.add('active');
  // Show global date picker only on date-scoped tabs: Diary and Supplements log.
  // Settings is a profile screen, not a daily log.
  const appHeader = document.querySelector('.app-header');
  if (appHeader) {
    appHeader.style.display = (tab === 'day' || tab === 'supplements-tab') ? '' : 'none';
  }
  // When switching to settings, reset to home sub-section
  if (tab === 'settings') go('home');
  // When switching to supplements log tab, load the data for current date
  if (tab === 'supplements-tab') loadSupplementsDay();
  // When switching to dashboard tab, lazy-load the iframe (cached after first open)
  if (tab === 'dashboard') loadDashboard();
}

// ── Load settings ───────────────────────────────────────────────────────────
async function load() {
  try {
    const res = await fetch(API, {
      headers: { Authorization: 'tma ' + tg.initData }
    });
    settings = await res.json();
    populate();
  } catch(e) {
    console.error('Failed to load settings', e);
  }
}

// ── Goal slider helpers ──────────────────────────────────────────────────────
function updateGoalSlider(pct) {
  const badge = document.getElementById('goal-pct-badge');
  const slider = document.getElementById('goal-pct');
  if (!badge || !slider) return;
  // Badge text + class
  if (pct < 0) {
    badge.textContent = `Дефицит ${Math.abs(pct)}%`;
    badge.className = 'goal-slider-badge';
  } else if (pct === 0) {
    badge.textContent = 'Поддержание';
    badge.className = 'goal-slider-badge maintain';
  } else {
    badge.textContent = `Профицит +${pct}%`;
    badge.className = 'goal-slider-badge surplus';
  }
  // Track fill: map [-30..+20] → [0..100]%
  const pos = ((pct - (-30)) / (20 - (-30))) * 100;
  const color = pct < 0 ? '#ff9500' : pct === 0 ? '#8e8e93' : '#34c759';
  const empty = window.matchMedia('(prefers-color-scheme: dark)').matches ? '#3a3a3c' : '#e5e5ea';
  slider.style.background =
    `linear-gradient(to right, ${color} 0%, ${color} ${pos}%, ${empty} ${pos}%, ${empty} 100%)`;
}

function populate() {
  // BMR is loaded separately via /api/profile/bmr (richer data than /api/settings)
  loadBmr();
  if (settings.target_weight_kg) document.getElementById('target-weight').value = settings.target_weight_kg;
  if (settings.target_weight_date) document.getElementById('target-date').value = settings.target_weight_date;
  document.getElementById('show-bar').checked = settings.show_calorie_budget_bar !== false;
  // Приватность: default TRUE (см. server_default колонки users.agent_review_consent)
  document.getElementById('review-consent-toggle').checked = settings.agent_review_consent !== false;
  // Goal slider
  const goalPct = settings.calorie_goal_pct ?? -15;
  const sliderEl = document.getElementById('goal-pct');
  if (sliderEl) { sliderEl.value = goalPct; updateGoalSlider(goalPct); }

  // Supplements
  renderSupplements();

  // Notifications
  document.getElementById('reminders-toggle').checked = !!settings.supplement_reminders_enabled;
  if (settings.supplement_reminder_time)
    document.getElementById('reminder-time').value = settings.supplement_reminder_time;
  toggleReminderTime();

  // Home summary
  const supps = (settings.supplements || []);
  document.getElementById('supp-count').textContent = supps.length + ' активных';

  // Username
  if (tg.initDataUnsafe && tg.initDataUnsafe.user)
    document.getElementById('user-name').textContent = tg.initDataUnsafe.user.first_name || 'Профиль';

  // Wire up autosave listeners (idempotent — marker attribute guards against re-binding)
  wireAutosave();
}

// ── Autosave infrastructure ─────────────────────────────────────────────────
let autosaveTimer = null;
let _retryTimer = null;       // scheduled retry after network error
const _dirtyInputs = new Set(); // text inputs modified but not yet blurred (tab-switch guard)

function showAutosavePill(ok, retrying = false) {
  const pill = document.getElementById('autosave-pill');
  pill.textContent = ok ? '✓ Сохранено' : (retrying ? '⚠ Ошибка — повтор...' : '⚠ Не сохранено');
  pill.classList.toggle('error', !ok);
  pill.classList.add('show');
  clearTimeout(autosaveTimer);
  // On success: hide after 1.5s. On persistent error: keep visible so user sees it.
  if (ok) autosaveTimer = setTimeout(() => pill.classList.remove('show'), 1500);
}

async function autosave(patch) {
  Object.assign(settings, patch);
  clearTimeout(_retryTimer);
  const send = () => fetch(API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'tma ' + tg.initData },
    body: JSON.stringify(settings),
  }).then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); });
  try {
    await send();
    showAutosavePill(true);
  } catch (e) {
    console.error('Autosave failed, retrying in 5s', e);
    showAutosavePill(false, true);
    _retryTimer = setTimeout(async () => {
      try { await send(); showAutosavePill(true); }
      catch (e2) { console.error('Autosave retry failed', e2); showAutosavePill(false, false); }
    }, 5000);
  }
}

// Flush any text inputs typed-in but not yet blurred (e.g. before tab switch on iOS Safari).
function flushDirtyInputs() {
  _dirtyInputs.forEach(el => el.blur());
  _dirtyInputs.clear();
}
// Also flush when Telegram WebApp goes to background (home button, notification, etc.)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flushDirtyInputs();
});

function wireAutosave() {
  // Prevent double-binding if populate() is called twice
  const root = document.getElementById('home');
  if (root.dataset.autosaveWired === '1') return;
  root.dataset.autosaveWired = '1';

  // BMR — handled by /api/profile/bmr (separate POST). Wire mode toggle + live preview.
  document.querySelectorAll('input[name="bmr-mode"]').forEach(el => {
    el.addEventListener('change', () => {
      toggleBmrModeUI();
      if (el.value === 'auto' && el.checked) saveBmrSourceAuto();
    });
  });
  ['bmr-age', 'bmr-height', 'bmr-weight', 'bmr-activity'].forEach(id => {
    const node = document.getElementById(id);
    if (node) node.addEventListener('input', recomputeBmrPreview);
  });
  document.querySelectorAll('input[name="bmr-sex"]').forEach(el => {
    el.addEventListener('change', recomputeBmrPreview);
  });

  // Target weight (same pattern)
  const tw = document.getElementById('target-weight');
  tw.addEventListener('input', () => _dirtyInputs.add(tw));
  tw.addEventListener('blur', () => {
    _dirtyInputs.delete(tw);
    const v = parseFloat(tw.value) || null;
    autosave({ target_weight_kg: v });
  });

  // Target date — validate before saving (bad date → POST 400 → silent error)
  const td = document.getElementById('target-date');
  td.addEventListener('change', () => {
    const val = td.value;
    if (val && isNaN(Date.parse(val))) return; // malformed date, ignore
    autosave({ target_weight_date: val || null });
  });

  // Show calorie bar
  document.getElementById('show-bar').addEventListener('change', e => {
    autosave({ show_calorie_budget_bar: e.target.checked });
  });

  // Приватность: согласие на доступ команды к переписке с BotkinClaw
  document.getElementById('review-consent-toggle').addEventListener('change', e => {
    autosave({ agent_review_consent: e.target.checked });
  });

  // Calorie goal slider — live badge update on drag, autosave on release
  const goalSlider = document.getElementById('goal-pct');
  if (goalSlider) {
    goalSlider.addEventListener('input', () => updateGoalSlider(parseInt(goalSlider.value)));
    goalSlider.addEventListener('change', () => autosave({ calorie_goal_pct: parseInt(goalSlider.value) }));
  }

  // Reminders toggle (also reveals time row)
  document.getElementById('reminders-toggle').addEventListener('change', e => {
    toggleReminderTime();
    autosave({ supplement_reminders_enabled: e.target.checked });
  });

  // Reminder time
  document.getElementById('reminder-time').addEventListener('change', e => {
    autosave({ supplement_reminder_time: e.target.value });
  });
}

// ── Supplement rendering ─────────────────────────────────────────────────────
function renderSupplements() {
  const supps = settings.supplements || [];
  const container = document.getElementById('supp-slots');
  container.innerHTML = '';

  for (const [slot, label] of Object.entries(SLOTS)) {
    const items = supps.filter(s => s.slot === slot);
    const div = document.createElement('div');
    div.innerHTML = `<div class="group-title">${escapeHtml(label)}</div>
      <div class="settings-group">
        ${items.length === 0
          ? '<div class="settings-row" style="color:#c7c7cc;font-style:italic;font-size:13px">— пусто —</div>'
          : items.map(s => {
              const safeName = escapeHtml(s.name);
              const attrName = safeName.replace(/"/g, '&quot;');
              const safeDose = s.dose ? escapeHtml(s.dose) : '';
              const doseSpan = safeDose ? ` <span class="supp-dose">(${safeDose})</span>` : '';
              return `
            <div class="settings-row">
              <div class="row-label">${safeName}${doseSpan}</div>
              <button class="del-btn-round" data-slot="${slot}" data-name="${attrName}" aria-label="Удалить">✕</button>
            </div>`;
            }).join('')}
      </div>`;
    container.appendChild(div);
  }
  // Wire delete buttons (event delegation would also work, but per-element is simpler here)
  container.querySelectorAll('.del-btn-round').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSupplement(btn.dataset.slot, btn.dataset.name);
    });
  });
}

function deleteSupplement(slot, name) {
  settings.supplements = (settings.supplements || []).filter(
    s => !(s.slot === slot && s.name === name)
  );
  renderSupplements();
  document.getElementById('supp-count').textContent = (settings.supplements || []).length + ' активных';
  autosave({ supplements: settings.supplements });
}

function toggleAddPanel() {
  const p = document.getElementById('add-panel');
  p.classList.toggle('open');
  if (p.classList.contains('open')) document.getElementById('new-supp-name').focus();
}

function addSupplement() {
  const name = document.getElementById('new-supp-name').value.trim();
  const dose = document.getElementById('new-supp-dose').value.trim();
  const slot = document.getElementById('new-supp-slot').value;
  if (!name) return;
  settings.supplements = settings.supplements || [];
  const entry = { name, slot };
  if (dose) entry.dose = dose;
  settings.supplements.push(entry);
  document.getElementById('new-supp-name').value = '';
  document.getElementById('new-supp-dose').value = '';
  document.getElementById('add-panel').classList.remove('open');
  renderSupplements();
  document.getElementById('supp-count').textContent = settings.supplements.length + ' активных';
  autosave({ supplements: settings.supplements });
}

// ── BMR section ─────────────────────────────────────────────────────────────
const PAL = { sedentary: 1.2, light: 1.375, moderate: 1.55, high: 1.725 };
const SOURCE_LABEL = {
  adaptive: 'цель по вашим данным (питание + вес) · БМР с часов',
  garmin: 'из Garmin · обновляется ежедневно по часам',
  apple_health: 'из Apple Health · обновляется ежедневно с часов / телефона',
  manual: 'формула Mifflin-St Jeor по твоим параметрам',
  default: 'оценка по умолчанию · введи параметры для точности',
};
let bmrState = null;  // last fetched /api/profile/bmr

async function loadBmr() {
  try {
    const r = await fetch('/api/profile/bmr', { headers: { Authorization: 'tma ' + tg.initData } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    bmrState = await r.json();
    renderBmrSummary();
    populateBmrForm();
  } catch (e) {
    console.error('loadBmr failed', e);
    document.getElementById('bmr-value').textContent = '—';
    document.getElementById('bmr-source-text').textContent = 'не удалось загрузить';
  }
}

function renderBmrSummary() {
  const r = bmrState?.resolved;
  const valEl = document.getElementById('bmr-value');
  const subEl = document.getElementById('bmr-source-text');
  if (!r || !r.bmr) {
    valEl.textContent = '—';
    subEl.textContent = SOURCE_LABEL.default;
    return;
  }
  valEl.textContent = `${r.bmr} ккал`;
  subEl.textContent = SOURCE_LABEL[r.source] || '';
}

function populateBmrForm() {
  if (!bmrState) return;
  const sel = bmrState.selected_source || 'auto';
  document.getElementById('bmr-mode-auto').checked = (sel === 'auto');
  document.getElementById('bmr-mode-manual').checked = (sel === 'manual');

  // Auto info block — show what's available
  const av = bmrState.available;
  const r = bmrState.resolved;
  const lines = [];
  if (r.source === 'adaptive') {
    const tdee = r.tdee ? ` · ${r.tdee} ккал` : '';
    lines.push(`<div><strong>Адаптивная оценка</strong> (питание + вес) — ✓ используется${tdee}</div>`);
  }
  if (av.garmin) {
    const tag = (r.source === 'garmin') ? '✓ используется' : 'доступно';
    lines.push(`<div><strong>Garmin</strong> — ${tag}${r.source === 'garmin' && r.bmr ? ` · ${r.bmr} ккал` : ''}</div>`);
  }
  if (av.apple_health) {
    const tag = (r.source === 'apple_health') ? '✓ используется' : (av.garmin ? 'резерв' : 'доступно');
    lines.push(`<div><strong>Apple Health</strong> — ${tag}${r.source === 'apple_health' && r.bmr ? ` · ${r.bmr} ккал` : ''}</div>`);
  }
  if (!av.garmin && !av.apple_health) {
    lines.push(`<div style="color:#8e8e93">Нет данных с часов. Используется среднее по умолчанию (~2150 ккал).</div>`);
    lines.push(`<div style="color:#8e8e93; margin-top:6px">Подключи Garmin или Apple Health, либо переключи на «Ручной» и введи параметры.</div>`);
  }
  document.getElementById('bmr-auto-info').innerHTML = lines.join('');

  // Manual form prefill
  const m = bmrState.manual || {};
  if (m.sex) {
    document.querySelectorAll('input[name="bmr-sex"]').forEach(el => el.checked = (el.value === m.sex));
  }
  if (m.age) document.getElementById('bmr-age').value = m.age;
  if (m.height_cm) document.getElementById('bmr-height').value = m.height_cm;
  if (m.weight_kg) document.getElementById('bmr-weight').value = m.weight_kg;
  if (m.activity_level) document.getElementById('bmr-activity').value = m.activity_level;

  toggleBmrModeUI();
  recomputeBmrPreview();
}

function toggleBmrPanel() {
  const panel = document.getElementById('bmr-panel');
  const chevron = document.getElementById('bmr-chevron');
  panel.classList.toggle('open');
  chevron.textContent = panel.classList.contains('open') ? '⌄' : '›';
}

function toggleBmrModeUI() {
  const mode = document.querySelector('input[name="bmr-mode"]:checked')?.value || 'auto';
  document.getElementById('bmr-auto-info').style.display = (mode === 'auto') ? 'block' : 'none';
  document.getElementById('bmr-manual-form').style.display = (mode === 'manual') ? 'block' : 'none';
}

function recomputeBmrPreview() {
  const sex = document.querySelector('input[name="bmr-sex"]:checked')?.value || 'male';
  const age = parseFloat(document.getElementById('bmr-age').value);
  const h = parseFloat(document.getElementById('bmr-height').value);
  const w = parseFloat(document.getElementById('bmr-weight').value);
  const act = document.getElementById('bmr-activity').value || 'light';
  const preview = document.getElementById('bmr-preview');
  if (!age || !h || !w) {
    preview.innerHTML = '<div style="color:#8e8e93">Заполни все поля для расчёта</div>';
    return;
  }
  const base = 10 * w + 6.25 * h - 5 * age;
  const bmr = Math.round(sex === 'male' ? base + 5 : base - 161);
  const tdee = Math.round(bmr * PAL[act]);
  const activity = tdee - bmr;
  preview.innerHTML = `
    <span class="bmr-big">${bmr} ккал</span>
    <span class="bmr-sub">базовый расход (BMR)</span>
    <div style="margin-top:8px; font-size:13px">
      🏃 +${activity} ккал — активность (×${PAL[act]})<br>
      🔥 <strong>${tdee} ккал/день</strong> — общий расход (TDEE)
    </div>
  `;
}

async function saveBmrManual() {
  const sex = document.querySelector('input[name="bmr-sex"]:checked')?.value || 'male';
  const age = parseInt(document.getElementById('bmr-age').value);
  const height_cm = parseInt(document.getElementById('bmr-height').value);
  const weight_kg = parseFloat(document.getElementById('bmr-weight').value);
  const activity_level = document.getElementById('bmr-activity').value;
  if (!age || !height_cm || !weight_kg) { alert('Заполни возраст, рост и вес'); return; }
  const btn = document.getElementById('bmr-save-btn');
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch('/api/profile/bmr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'tma ' + tg.initData },
      body: JSON.stringify({ source: 'manual', manual: { sex, age, height_cm, weight_kg, activity_level } }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    btn.textContent = '✓ Сохранено';
    setTimeout(() => { btn.textContent = '💾 Сохранить'; btn.disabled = false; }, 1200);
    await loadBmr();
  } catch (e) {
    console.error(e);
    btn.textContent = '⚠ Ошибка'; btn.disabled = false;
  }
}

async function saveBmrSourceAuto() {
  try {
    await fetch('/api/profile/bmr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'tma ' + tg.initData },
      body: JSON.stringify({ source: 'auto' }),
    });
    await loadBmr();
  } catch (e) { console.error(e); }
}

// ── Reminder time toggle ────────────────────────────────────────────────────
function toggleReminderTime() {
  const enabled = document.getElementById('reminders-toggle').checked;
  document.getElementById('reminder-time-row').style.display = enabled ? 'block' : 'none';
}

// ── Supplements daily log ───────────────────────────────────────────────────
// Tracks which supplements the user has taken on a given date, with times.
// Syncs the currently viewed date with window.__nutri.state.date (Diary tab).
function currentSuppDate() {
  // Reuse the date that Diary is viewing so calendar nav stays coherent.
  // day.js keeps state.date as a Date OBJECT — convert to YYYY-MM-DD (local, not UTC).
  const src = window.__nutri && window.__nutri.state && window.__nutri.state.date;
  const d = (src instanceof Date) ? src : new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

let _suppDayCache = { date: null, data: null };
// Generation counter: each call to loadSupplementsDay() increments this.
// A response that arrives after a newer request was fired is discarded —
// prevents stale innerHTML from overwriting a fresher render (toggle race).
let _suppLoadGen = 0;

async function loadSupplementsDay() {
  const date = currentSuppDate();
  const gen = ++_suppLoadGen;
  const container = document.getElementById('supp-day-container');
  try {
    const r = await fetch(`/api/supplements/day?date=${date}`, {
      headers: { Authorization: 'tma ' + tg.initData },
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const data = await r.json();
    if (gen !== _suppLoadGen) return; // stale response — a newer load is in flight
    _suppDayCache = { date, data };
    renderSupplementsDay(data);
  } catch (e) {
    if (gen !== _suppLoadGen) return; // stale error — ignore
    console.error('loadSupplementsDay failed', e);
    container.innerHTML = `<div class="supp-empty-state">
      <div class="icon">⚠️</div>
      <div class="title">Не удалось загрузить</div>
      <div class="hint">${String(e.message || e)}</div>
    </div>`;
  }
}

function renderSupplementsDay(data) {
  const container = document.getElementById('supp-day-container');
  const { progress, slots } = data;

  if (progress.total === 0) {
    container.innerHTML = `<div class="supp-empty-state">
      <div class="icon">💊</div>
      <div class="title">Список пуст</div>
      <div class="hint">Добавь свои добавки в <a href="#" onclick="switchTab('settings');return false;">⚙️ Настройки → Управление списком</a></div>
    </div>`;
    return;
  }

  const pct = progress.total > 0 ? Math.round(100 * progress.taken / progress.total) : 0;

  let html = `
    <div class="supp-progress-card">
      <div class="supp-progress-head">
        <div class="supp-progress-title">Принято сегодня</div>
        <div class="supp-progress-count">${progress.taken} из ${progress.total} · ${pct}%</div>
      </div>
      <div class="supp-progress-track"><div class="supp-progress-fill" style="width:${pct}%"></div></div>
    </div>`;

  for (const slot of slots) {
    if (!slot.items.length) continue;
    html += `<div class="supp-slot-title">${escapeHtml(slot.label)}</div>`;
    html += `<div class="supp-slot-group">`;
    for (const it of slot.items) {
      const taken = !!it.taken_at;
      const safeName = escapeHtml(it.name);
      const safeDose = it.dose ? escapeHtml(it.dose) : '';
      html += `
        <div class="supp-row${taken ? ' taken' : ''}" data-name="${safeName.replace(/"/g, '&quot;')}">
          <div class="supp-check"></div>
          <div class="supp-name">${safeName}${safeDose ? ` <span class="supp-dose">(${safeDose})</span>` : ''}</div>
        </div>`;
    }
    html += `</div>`;
  }

  container.innerHTML = html;

  // Wire up taps
  container.querySelectorAll('.supp-row').forEach(row => {
    row.addEventListener('click', () => toggleSupplement(row));
  });
}

async function toggleSupplement(row) {
  const name = row.dataset.name;
  const date = currentSuppDate();
  const isTaken = row.classList.contains('taken');
  const method = isTaken ? 'DELETE' : 'POST';

  // Optimistic UI update
  row.classList.toggle('taken');

  try {
    const r = await fetch('/api/supplements/take', {
      method,
      headers: { 'Content-Type': 'application/json', Authorization: 'tma ' + tg.initData },
      body: JSON.stringify({ date, name }),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    await r.json();
    // Refresh progress card by re-fetching full state (keeps counts accurate)
    loadSupplementsDay();
  } catch (e) {
    // Revert optimistic update on failure
    row.classList.toggle('taken');
    console.error('toggleSupplement failed', e);
    showAutosavePill(false);
  }
}

// Small helper — escape HTML to avoid XSS in names
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// When the Diary tab changes date, refresh the supplements log too (if its tab is active).
// day.js handles the raw nav (prev-day / next-day / date-picker); we piggyback with
// a small refresh-on-date-change hook.
function isSupplementsTabActive() {
  return document.querySelector('.tab-item[data-tab="supplements-tab"]')?.classList.contains('active');
}
['prev-day', 'next-day'].forEach(id => {
  const btn = document.getElementById(id);
  if (btn) btn.addEventListener('click', () => {
    // Run after day.js handler (microtask order): reload supp list if visible
    setTimeout(() => { if (isSupplementsTabActive()) loadSupplementsDay(); }, 50);
  });
});
const picker = document.getElementById('date-picker');
if (picker) picker.addEventListener('change', () => {
  setTimeout(() => { if (isSupplementsTabActive()) loadSupplementsDay(); }, 50);
});

// ── Profile links ────────────────────────────────────────────────────────────
let _dashboardUrl = null;
let _reportUrl = null;

async function loadLinks() {
  try {
    const data = await window.API.getDashboardUrl();
    _dashboardUrl = data.dashboard_url || null;
    _reportUrl = data.report_url || null;
  } catch(e) {
    console.error('Failed to load dashboard url', e);
  }
}

function openDashboard() {
  if (_dashboardUrl) tg.openLink(_dashboardUrl);
}

function openReport() {
  if (_reportUrl) tg.openLink(_reportUrl);
}

function copyReport() {
  if (!_reportUrl) return;
  navigator.clipboard.writeText(_reportUrl).then(() => {
    const toast = document.getElementById('links-toast');
    if (!toast) return;
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 2000);
  }).catch(() => {
    tg.openLink(_reportUrl);
  });
}

function copyDashboard() {
  if (!_dashboardUrl) return;
  navigator.clipboard.writeText(_dashboardUrl).then(() => {
    const toast = document.getElementById('links-toast');
    if (!toast) return;
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 2000);
  }).catch(() => {
    // fallback: открыть вместо копирования
    tg.openLink(_dashboardUrl);
  });
}

// ── Data Sources ─────────────────────────────────────────────────────────────
async function loadDataSources() {
  const group = document.getElementById('data-sources-group');
  if (!group) return;
  try {
    const res = await fetch('/api/profile/data_sources', {
      headers: { Authorization: 'tma ' + tg.initData }
    });
    if (!res.ok) throw new Error(res.status);
    const { sources } = await res.json();
    group.innerHTML = sources.map(s => _renderSourceRow(s)).join('');
    _bindDataSourceActions(group);
  } catch(e) {
    group.innerHTML = '<div class="settings-row disabled"><div class="row-label"><div class="row-sub">Не удалось загрузить</div></div></div>';
  }
}

// Делегированный обработчик кнопок в панелях источников (Apple Health / Health Connect).
// Кнопки несут data-* вместо inline-onclick с данными — значение токена не может
// «разорвать» кавычки атрибута (баг a94ab56: JSON.stringify в двойных кавычках
// onclick="..." закрывал атрибут → кнопки «Health Auto Export»/«iOS Shortcuts» и
// «Скопировать» падали с синтаксической ошибкой и не реагировали). Слушатель вешается
// один раз на контейнер и переживает пере-рендеры innerHTML.
function _bindDataSourceActions(group) {
  if (!group || group.dataset.actionsBound) return;
  group.dataset.actionsBound = '1';
  group.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn || !group.contains(btn)) return;
    if (btn.dataset.action === 'apple-method') {
      selectAppleMethod(btn.dataset.method, btn.dataset.token, btn.dataset.source);
    } else if (btn.dataset.action === 'copy') {
      copyToken(btn.dataset.copy, btn);
    }
  });
}

function _renderSourceRow(s) {
  const statusClass = s.connected ? 'source-status connected' : 'source-status';
  const statusText  = s.connected
    ? `подключён · ${_fmtDate(s.last_updated)}`
    : 'не подключён';
  const connectBtn = s.connected
    ? ''
    : `<button class="connect-btn" onclick="toggleConnect('${escapeHtml(s.id)}')">Подключить ›</button>`;
  const panel = s.connected
    ? ''
    : `<div class="connect-panel" id="panel-${escapeHtml(s.id)}">${_renderConnectPanel(s)}</div>`;
  return `<div class="source-row-wrap">
    <div class="settings-row">
      <div class="row-icon">${s.icon}</div>
      <div class="row-label">${escapeHtml(s.name)}<div class="row-sub ${statusClass}">${statusText}</div></div>
      ${connectBtn}
    </div>
    ${panel}
  </div>`;
}

function _renderConnectPanel(s) {
  const flow = s.connect_info && s.connect_info.flow;
  if (flow === 'coming_soon') {
    return `<div class="connect-content coming-soon">Поддержка подключения появится в следующем обновлении.</div>`;
  }
  if (flow === 'tg_deeplink') {
    const deeplink = escapeHtml(s.connect_info.deeplink || '');
    return `<div class="connect-content">
      <ol>
        <li>Открой приложение <strong>FreeStyle Libre 3</strong> → ☰ → <strong>Connected Apps</strong> → <strong>LibreLinkUp</strong></li>
        <li>Нажми <strong>Invite Follower</strong> и введи email: <code>dr@botkin.health</code></li>
      </ol>
      <p style="color:#8e8e93;font-size:12px">После отправки приглашения — запусти ожидание в боте. Бот сам поймает подключение (до 10 мин).</p>
      <a class="connect-tg-btn" href="${deeplink}">Запустить ожидание в боте →</a>
    </div>`;
  }
  if (flow === 'inline_token') {
    const token = s.connect_info.health_token || '';
    if (s.id === 'health_connect') {
      return _renderHealthConnectPanel(token);
    }
    return _renderAppleHealthPanel(token, s.id);
  }
  return `<div class="connect-content coming-soon">Подключение недоступно.</div>`;
}

function _renderAppleHealthPanel(token, sourceId) {
  const safeToken = escapeHtml(token);
  const safeSourceId = escapeHtml(sourceId);
  return `<div class="connect-content">
    <p>Выбери способ подключения:</p>
    <div class="connect-method-btns">
      <button class="method-btn" data-action="apple-method" data-method="hae" data-token="${safeToken}" data-source="${safeSourceId}">💰 Health Auto Export</button>
      <button class="method-btn" data-action="apple-method" data-method="shortcut" data-token="${safeToken}" data-source="${safeSourceId}">🆓 iOS Shortcuts</button>
    </div>
    <div id="apple-detail-${safeSourceId}"></div>
  </div>`;
}

function _renderHealthConnectPanel(token) {
  const safeToken = escapeHtml(token);
  return `<div class="connect-content">
    <p>Только для <strong>Android</strong>. Использует тот же ключ что и Apple Health.</p>
    <ol>
      <li>Установи APK: <a href="https://github.com/mcnaveen/health-connect-webhook/releases/latest" target="_blank">health-connect-webhook (GitHub Releases)</a></li>
      <li>В приложении укажи URL:<br><code>https://botkin.health/android_health_v1</code></li>
      <li>Твой ключ: <code>${safeToken}</code><button class="copy-btn" data-action="copy" data-copy="${safeToken}">Скопировать</button></li>
      <li>Выдай разрешения Health Connect и нажми Sync.</li>
    </ol>
  </div>`;
}

function selectAppleMethod(method, token, sourceId) {
  const detail = document.getElementById('apple-detail-' + sourceId);
  if (!detail) return;
  const safeToken = escapeHtml(token);
  if (method === 'hae') {
    detail.innerHTML = `<ol>
      <li>Установи <strong>Health Auto Export</strong> (App Store, $24.99 разово)</li>
      <li>Add Automation → REST API:<br>
        URL: <code>https://botkin.health/apple_health_v2</code><br>
        Header: <code>Authorization: Bearer ${safeToken}</code><button class="copy-btn" data-action="copy" data-copy="Bearer ${safeToken}">Скопировать</button></li>
      <li>Format: JSON · v2 · Aggregate ON · Group by Day · Range: Yesterday</li>
      <li>Выбери нужные метрики и сохрани.</li>
    </ol>
    <p style="color:#8e8e93;font-size:12px">Данные начнут поступать ночью (или через ручной экспорт в HAE).</p>`;
  } else {
    detail.innerHTML = `<ol>
      <li>Выбери версию команды:<br>
        📱 <a href="https://www.icloud.com/shortcuts/e3884d8261954664bde8bd78de0ccfdb" target="_blank">Только iPhone</a> — шаги, дистанция, калории, этажи<br>
        ⌚️ <a href="https://www.icloud.com/shortcuts/890c9df1ae614a4eaf5e8cd49416154a" target="_blank">iPhone + Apple Watch</a> — + пульс, HRV, SpO₂, дыхание, походка</li>
      <li>В поле «Текст» вставь ключ целиком:<br>
        <code>Bearer ${safeToken}</code><button class="copy-btn" data-action="copy" data-copy="Bearer ${safeToken}">Скопировать</button></li>
      <li>Установка, разрешения и автоматизация — в <a href="https://github.com/botkin-health/Botkin/blob/main/docs/user_guide/ru/apple-health.md#-%D0%B1%D0%B5%D1%81%D0%BF%D0%BB%D0%B0%D1%82%D0%BD%D1%8B%D0%B9-%D0%BF%D1%83%D1%82%D1%8C--ios-shortcuts-%D0%B1%D0%B5%D0%B7-hae" target="_blank">гайде</a></li>
    </ol>
    <p style="color:#8e8e93;font-size:12px">Вес, давление и сон — только через HAE.</p>`;
  }
}

function toggleConnect(id) {
  document.querySelectorAll('.connect-panel.open').forEach(p => {
    if (p.id !== `panel-${id}`) p.classList.remove('open');
  });
  const panel = document.getElementById(`panel-${id}`);
  if (panel) panel.classList.toggle('open');
}

function copyToken(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  }).catch(() => {
    // clipboard unavailable in some environments — silently ignore
  });
}

function _fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}

// ── Feedback (сообщить об ошибке / идею) — #271 ──────────────────────────────
async function sendFeedback() {
  const textEl = document.getElementById('fb-text');
  const statusEl = document.getElementById('fb-status');
  const btn = document.getElementById('fb-send-btn');
  const text = (textEl.value || '').trim();
  if (!text) {
    statusEl.textContent = 'Напишите сообщение';
    statusEl.className = 'fb-status error';
    return;
  }
  const kind = document.querySelector('input[name="fb-kind"]:checked')?.value || 'unspecified';
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Отправка…';
  statusEl.textContent = '';
  statusEl.className = 'fb-status';
  try {
    const res = await window.API.sendFeedback({ text, kind });
    if (res && res.status === 'opted_out') {
      statusEl.textContent = 'Вы отключили отправку обратной связи в настройках.';
      statusEl.className = 'fb-status error';
    } else {
      statusEl.textContent = '✓ Спасибо! Отправлено разработчикам.';
      statusEl.className = 'fb-status ok';
      textEl.value = '';
    }
  } catch (e) {
    console.error('sendFeedback failed', e);
    statusEl.textContent = '⚠ Не удалось отправить. Попробуйте позже.';
    statusEl.className = 'fb-status error';
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────
load();
loadLinks();
loadDataSources();
