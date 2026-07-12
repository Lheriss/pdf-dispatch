// =============================================================================
// pdf-dispatch — frontend
// =============================================================================
//
// Single script with no external dependency and no build step. Loaded by
// templates/index.html, which first injects window.I18N (the translation
// dictionary for the active language) and window.LANG (language code) — see
// the t() function below and the index() route in app.py.
//
// General lifecycle:
//   1. loadConfig() fetches /api/state (config + stats + log + queue) and
//      calls the render functions (renderTriggers, renderOptions,
//      renderTokens, ...) to populate the page.
//   2. refresh() runs every 3 seconds (setInterval, last line of the file)
//      to refresh stats/log/queue without reloading the whole config
//      (unless cfg has not been loaded yet).
//   3. Each user action (toggle, edit, add/remove) calls
//      saveSetting()/saveSettingWithLog(), which persists via POST /api/config
//      and, for the latter, appends an entry to the log via POST /api/log
//      (the message is translated client-side with t() before sending).
//
// Sections (identified by `// ── ... ──` comments):
//   - Local state            : global variables (cfg, panel state...)
//   - Polling                : loadConfig/refresh, setInterval loop
//   - Settings panel         : language, general options
//   - Triggers               : list of barcodes/QR codes -> split
//   - Options                : subfolders, archiving, verbose log
//   - Tokens (filename builder) : filename construction (drag & drop)
//   - Global separator       : generation/download of the separator PDF
//   - Timestamp presets      : date format shortcuts
//   - Additional string tokens : free text in the filename
//   - Drag & drop tokens     : token reordering
//   - Upload zone            : PDF file drop
//   - Folders                : display and editing of output paths
//   - Email panel            : IMAP configurations (CRUD, test, alerts)
//
// IMPORTANT (name collision): t('key', {...}) is the global translation
// function. Several render functions iterate over arrays of objects
// (triggers, tokens...) — NEVER name a map()/forEach() parameter `t` inside a
// function that also calls t('key'), otherwise the current object shadows the
// translation function (a silent TypeError that breaks rendering — see the
// v1.15.1 release history for a real case).
// =============================================================================

// ── Local state ───────────────────────────────────────────────────────────
let cfg = {};
let dragSrc = null;
let savedTokensHash = null;  // hash of the last saved token config

function _tokensHash(tokens, sep) {
  return JSON.stringify({tokens, sep});
}

function updateUnsavedWarning() {
  const current    = _tokensHash(cfg.filename_tokens || [], cfg.filename_separator ?? '_');
  const el         = document.getElementById('unsaved-warning');
  const saveBtn    = document.getElementById('save-format-btn');
  const hasChanges = !!(savedTokensHash && current !== savedTokensHash);
  if (el)      el.style.display  = hasChanges ? 'inline' : 'none';
  if (saveBtn) saveBtn.disabled  = !hasChanges;
}

// ── Polling ───────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const d = await (await fetch('/api/state')).json();

    document.getElementById('st-proc').textContent = d.stats.processed;
    document.getElementById('st-docs').textContent = d.stats.split_docs;
    document.getElementById('st-err').textContent  = d.stats.errors;
    document.getElementById('st-last').textContent = d.stats.last_file || '–';
    const verEl = document.getElementById('app-version');
    if (verEl) verEl.textContent = d.config.version || '';
    // Update the folder table if the panel is open
    if (d.config.dirs_status) {
      if (!cfg.app_config) cfg.app_config = {};
      cfg.app_config.dirs_status = d.config.dirs_status;
      cfg.app_config.data_dir    = d.config.data_dir;
      const panel = document.getElementById('dirs-panel');
      if (panel && panel.style.display === 'block' && !activeDirKey) {
        renderDirs(d.config.dirs_status, d.config.data_dir);
      }
    }
    // Alerte email + configs
    if (d.config.email_configs_status) {
      updateEmailAlert(d.config.email_configs_status);
    }
    if (d.app_config && d.app_config.email_configs) {
      // Do not overwrite cfg.email_configs while the panel is open (protects fields being edited)
      const epanel = document.getElementById('email-config-panel');
      const emailOpen = epanel && epanel.classList.contains('open');
      if (!emailOpen) {
        cfg.email_configs = d.app_config.email_configs;
        const esection = document.getElementById('email-section');
        if (esection && esection.style.display === 'block') {
          renderEmailConfigs();
        }
      }
    }
    const ccEl = document.getElementById('counter-current');
    if (ccEl) ccEl.textContent = d.app_config ? d.app_config.counter : '–';

    const qe = document.getElementById('queue-content');
    qe.innerHTML = d.queue.length
      ? d.queue.map(f=>`<span class="qi">${escapeHtml(f)}</span>`).join(' ')
      : `<span class="qe">${t('queue.empty')}</span>`;
    document.getElementById('sdot').className = 'dot' + (d.queue.length ? ' busy' : '');
    document.getElementById('stext').textContent = d.queue.length ? t('header.status_processing') : t('header.status_idle');

    const logEl = document.getElementById('log-wrap');
    const isFirstRender = !cfg.loaded;
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.innerHTML = [...d.events].reverse().map(e =>
      `<div class="le ${escapeHtml(e.level)}">
        <span class="le-ts">${escapeHtml(e.ts)}</span>
        <span class="le-lv ${escapeHtml(e.level)}">${escapeHtml(String(e.level).toUpperCase())}</span>
        <span class="le-msg">${escapeHtml(e.message)}</span>
      </div>`
    ).join('');
    if (isFirstRender || atBottom) logEl.scrollTop = logEl.scrollHeight;

    // Sync config from server only when the panel is closed
    // (avoids overwriting the user's in-progress changes)
    const panelOpen = document.getElementById('sbody').classList.contains('open');
    if (!cfg.loaded) {
      cfg = d.app_config;
      (cfg.filename_tokens || []).forEach(t => { if (t.type === 'string') t.enabled = true; });
      cfg.loaded = true;
      savedTokensHash = _tokensHash(cfg.filename_tokens || [], cfg.filename_separator ?? '_');
      renderTriggers();
      renderOptions();
      renderWebhook();
      renderApiKey();
      renderTokens();
    } else if (!panelOpen) {
      // Panel closed AND no pending changes: re-sync
      const currentHash = _tokensHash(cfg.filename_tokens || [], cfg.filename_separator ?? '_');
      const hasUnsaved  = savedTokensHash && currentHash !== savedTokensHash;
      if (!hasUnsaved) {
        cfg = d.app_config;
        (cfg.filename_tokens || []).forEach(t => { if (t.type === 'string') t.enabled = true; });
        cfg.loaded = true;
      }
    }
    // Re-sync email config panel after any cfg update.
    // Always refresh the trigger dropdown (fixes empty dropdown when split_values
    // wasn't yet populated at panel-open time).
    // When no unsaved edits are pending, also re-sync the action radio buttons
    // from the current cfg state (fixes stale radio after save+reload).
    if (activeEmailConfigId) {
      const _epSync = document.getElementById('email-config-panel');
      if (_epSync && _epSync.classList.contains('open')) {
        const _ec = (cfg.email_configs || []).find(c => c.id === activeEmailConfigId);
        if (_ec) {
          renderEmailTriggerSelect(_ec.default_trigger);
          if (!emailUnsaved) {
            const _act = _ec.action || 'read';
            document.querySelectorAll('input[name="em-action"]')
              .forEach(r => r.checked = r.value === _act);
          }
        }
      }
    }
    // Panel open: never overwrite cfg (protects in-progress edits)
  } catch(e) {
    document.getElementById('stext').textContent = t('header.status_offline');
  }
}

// ── Settings panel ────────────────────────────────────────────────────────
// Translation (i18n): window.I18N is injected by the server for the active language
function t(key, params) {
  let s = (window.I18N && window.I18N[key]) || key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.split('{' + k + '}').join(v);
    }
  }
  return s;
}
// Apply translations to every element carrying data-i18n
function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.dataset.i18nTitle);
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    el.setAttribute('aria-label', t(el.dataset.i18nAriaLabel));
  });
  const fr = document.getElementById('lang-btn-fr');
  const en = document.getElementById('lang-btn-en');
  if (fr && en) {
    fr.classList.toggle('active', window.LANG === 'fr');
    en.classList.toggle('active', window.LANG === 'en');
  }
}

// HTML escaping to prevent injection via user-supplied values
// (noms de fichiers, messages du journal, noms de configurations, etc.)
async function setLanguage(lang) {
  if (lang === window.LANG) return;
  await saveSettingWithLog('language', lang, 'Langue de l\'interface : ' + lang.toUpperCase());
  location.reload();
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
// Escape a value for insertion inside a JS string literal that is itself
// embedded in an inline HTML handler, e.g. onclick="f('${escapeJsStr(v)}')".
// Because that markup is injected via innerHTML, the HTML parser decodes
// entities BEFORE the JS parser runs, so escaping only \ and ' is not enough:
// a quote or angle bracket could break out of the attribute. We escape the
// JS-significant characters (backslash, quote) AND the HTML-significant ones
// (& < > " ') so the value is safe in both contexts simultaneously.
function escapeJsStr(s) {
  return String(s ?? '')
    .replace(/\\/g, '\\\\')   // backslash first
    .replace(/'/g,  "\\'")    // JS string delimiter
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;');
}

function toggleSettings() {
  const b = document.getElementById('sbody');
  const a = document.getElementById('sarrow');
  const open = b.classList.toggle('open');
  a.textContent = open ? '▲' : '▼';
  const hdr = document.querySelector('[aria-controls="sbody"]');
  if (hdr) hdr.setAttribute('aria-expanded', String(open));
  if (open && !cfg.loaded) loadConfig();
}

async function loadConfig() {
  const d = await (await fetch('/api/state')).json();
  cfg = d.app_config; cfg.loaded = true;
  renderTriggers(); renderOptions(); renderWebhook(); renderApiKey(); renderTokens();
}

// ── Triggers ───────────────────────────────────────────────────────────────
let activeTriggerIdx = null;

function renderTriggers() {
  const el   = document.getElementById('trigger-list');
  const vals = cfg.split_values || [];
  el.innerHTML = vals.map((tv, i) => {
    const v        = typeof tv === 'object' ? tv.value : tv;
    const ph       = typeof tv === 'object' ? (tv.page_handling || 'keep') : 'keep';
    const active   = activeTriggerIdx === i ? ' active' : '';
    const isGlob   = /[*?[\]]/.test(v);
    const caseSens = typeof tv === 'object' ? tv.case_sensitive !== false : true;
    const phIcon   = ph === 'delete' ? `<span class="del-icon" title="${t('triggers.delete_page_active_title')}">✂</span>` : '';
    return `<div class="trigger-tag${active}" onclick="openTriggerPanel(${i})"
      title="${t('common.click_to_configure')}">
      <span>${escapeHtml(v)}</span>
      ${isGlob   ? `<span class="glob-icon" title="${t('triggers.glob_active_title')}">~</span>` : ''}
      ${!caseSens ? `<span class="case-icon" title="${t('triggers.case_insensitive_title')}">Aa</span>` : ''}
      ${phIcon}
      <span class="rm" onclick="event.stopPropagation();removeTrigger(${i})">✕</span>
    </div>`;
  }).join('');
}

function openTriggerPanel(i) {
  const vals = cfg.split_values || [];
  const trig = vals[i];
  if (!trig) return;
  const v        = typeof trig === 'object' ? trig.value : trig;
  const isDelete = typeof trig === 'object' ? (trig.page_handling === 'delete') : false;
  const caseSens = typeof trig === 'object' ? trig.case_sensitive !== false : true;

  // Toggle panel if already open on this trigger
  if (activeTriggerIdx === i) {
    activeTriggerIdx = null;
    document.getElementById('trigger-panel').classList.remove('open');
    renderTriggers();
    return;
  }

  activeTriggerIdx = i;
  document.getElementById('tp-title').textContent = v;
  document.getElementById('tp-delete-page').checked   = isDelete;
  document.getElementById('tp-case-sensitive').checked = caseSens;
  document.getElementById('trigger-panel').classList.add('open');

  // Helper: save the current trigger state
  function saveTrigger() {
    cfg.split_values[i] = {
      value:         v,
      page_handling: document.getElementById('tp-delete-page').checked ? 'delete' : 'keep',
      case_sensitive: document.getElementById('tp-case-sensitive').checked,
    };
    saveSettingWithLog('split_values', cfg.split_values,
      t('triggers.log_updated', {value: v}));
    renderTriggers();
  }

  document.getElementById('tp-delete-page').onchange   = saveTrigger;
  document.getElementById('tp-case-sensitive').onchange = saveTrigger;
  renderTriggers();
  updateSeparatorBtn();
}

// Reserved routing labels — must mirror config.RESERVED_TRIGGER_VALUES on
// the server, which also rejects these (defence in depth). A trigger equal to
// one of these (case-insensitive) would collide with the internal no_code /
// blank output routing.
const RESERVED_TRIGGER_VALUES = ['no_code', 'blank'];

function addTrigger() {
  const inp = document.getElementById('new-trigger');
  const val = inp.value.trim();
  if (!val) return;
  if (RESERVED_TRIGGER_VALUES.includes(val.toLowerCase())) {
    alert(t('triggers.reserved_value', {value: val}));
    return;
  }
  const existing = (cfg.split_values || []).map(tok => typeof tok==='object'?tok.value:tok);
  if (!existing.includes(val)) {
    const newTrigger = {value: val, page_handling: 'keep', case_sensitive: true};
    cfg.split_values = [...(cfg.split_values || []), newTrigger];
    const labels = cfg.split_values.map(tok=>typeof tok==='object'?tok.value:tok);
    saveSettingWithLog('split_values', cfg.split_values,
      t('triggers.log_added', {value: val, list: labels.join(', ')}));
    renderTriggers();
  }
  inp.value = '';
}

function removeTrigger(i) {
  const trig    = cfg.split_values[i];
  const removed = typeof trig === 'object' ? trig.value : trig;
  cfg.split_values.splice(i, 1);
  if (activeTriggerIdx === i) {
    activeTriggerIdx = null;
    document.getElementById('trigger-panel').classList.remove('open');
  } else if (activeTriggerIdx > i) {
    activeTriggerIdx--;
  }
  const labels = cfg.split_values.map(t=>typeof t==='object'?t.value:t);
  saveSettingWithLog('split_values', cfg.split_values,
    t('triggers.log_removed', {value: removed, list: labels.join(', ')}));
  renderTriggers();
}

// ── Options ───────────────────────────────────────────────────────────────
function renderOptions() {
  // Detach existing listeners by replacing elements
  ['opt-subdirs', 'opt-delete'].forEach(id => {
    const el = document.getElementById(id);
    const clone = el.cloneNode(true);
    el.parentNode.replaceChild(clone, el);
  });
  document.getElementById('opt-subdirs').checked = !!cfg.subdirs_by_trigger;
  document.getElementById('opt-delete').checked  = !!cfg.delete_source;
  document.getElementById('opt-log-verbose').checked = !!cfg.log_verbose;

  const placement = cfg.separator_placement || 'before';
  document.getElementById('opt-sep-before').checked = (placement === 'before');
  document.getElementById('opt-sep-after').checked  = (placement === 'after');

  document.getElementById('opt-subdirs').addEventListener('change', function() {
    saveSettingWithLog('subdirs_by_trigger', this.checked,
      t('options.log_subdirs', {state: this.checked ? t('common.enabled') : t('common.disabled')}));
  });
  document.getElementById('opt-delete').addEventListener('change', function() {
    saveSettingWithLog('delete_source', this.checked,
      t('options.log_archive', {state: this.checked ? t('options.archive_enabled_state') : t('options.archive_disabled_state')}));
  });
  document.getElementById('opt-log-verbose').addEventListener('change', function() {
    saveSettingWithLog('log_verbose', this.checked,
      t('options.log_verbose_log', {state: this.checked ? t('common.enabled') : t('common.disabled')}));
  });

  ['opt-sep-before', 'opt-sep-after'].forEach(id => {
    document.getElementById(id).addEventListener('change', function() {
      if (!this.checked) return;
      const val = this.value;
      cfg.separator_placement = val;
      saveSettingWithLog('separator_placement', val,
        t('options.log_sep_placement', {value: val === 'before' ? t('options.separator_before') : t('options.separator_after')}));
      renderTriggers();  // icons may change
    });
  });

  renderSeparatorButtons();
}

// ── Webhook ───────────────────────────────────────────────────────────────────

function renderWebhook() {
  const enabled = !!cfg.webhook_enabled;
  const el = document.getElementById('opt-webhook-enabled');
  if (el) el.checked = enabled;
  const panel = document.getElementById('webhook-config');
  if (panel) panel.style.display = enabled ? 'block' : 'none';

  const urlEl    = document.getElementById('opt-webhook-url');
  const eventsEl = document.getElementById('opt-webhook-events');
  const secretEl = document.getElementById('opt-webhook-secret');
  if (urlEl)    urlEl.value    = cfg.webhook_url    || '';
  if (eventsEl) eventsEl.value = cfg.webhook_events || 'all';
  if (secretEl && !secretEl._dirty) secretEl.value = '';  // never echo secret back

  const enableEl = document.getElementById('opt-webhook-enabled');
  if (enableEl) {
    enableEl.onchange = function() {
      cfg.webhook_enabled = this.checked;
      document.getElementById('webhook-config').style.display = this.checked ? 'block' : 'none';
      saveSetting('webhook_enabled', this.checked);
    };
  }
}

function saveWebhookField() {
  const url    = (document.getElementById('opt-webhook-url')?.value    || '').trim();
  const events = document.getElementById('opt-webhook-events')?.value  || 'all';
  const secret = (document.getElementById('opt-webhook-secret')?.value || '').trim();

  const payload = {webhook_url: url, webhook_events: events};
  if (secret) payload.webhook_secret = secret;  // only send if non-empty

  cfg.webhook_url    = url;
  cfg.webhook_events = events;
  if (secret) cfg.webhook_secret = secret;

  // Mark secret field as dirty to avoid render() clearing it
  const secretEl = document.getElementById('opt-webhook-secret');
  if (secretEl) secretEl._dirty = true;

  fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).catch(e => console.error('saveWebhookField:', e));
}

async function testWebhook() {
  const result = document.getElementById('webhook-test-result');
  if (result) result.textContent = '…';
  try {
    const r = await fetch('/api/webhook/test', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      result.textContent = t('webhook.test_ok', {code: d.code});
      result.style.color = 'var(--accent)';
    } else {
      result.textContent = t('webhook.test_error', {msg: d.error || '?'});
      result.style.color = '#f5a623';
    }
  } catch(e) {
    if (result) { result.textContent = t('webhook.test_error', {msg: e.message}); result.style.color = '#f5a623'; }
  }
  setTimeout(() => { if (result) { result.textContent = ''; result.style.color = 'var(--muted)'; } }, 5000);
}

// ── API key ───────────────────────────────────────────────────────────────────

let _apiKeyEnvSet = false;

function renderApiKey() {
  const key = cfg.api_key || '';
  const envSet = !!cfg._api_key_env_set;
  _apiKeyEnvSet = envSet;

  const field = document.getElementById('api-key-field');
  if (field) {
    field.value = key;
    field.type  = 'password';
  }

  // Show button: keep its 👁 emoji — do not overwrite with 'apikey.copy'
  const showBtn = document.getElementById('api-key-show-btn');
  if (showBtn) showBtn.textContent = '👁';

  const envNote = document.getElementById('api-key-env-note');
  if (envNote) envNote.style.display = envSet ? 'block' : 'none';

  const regenBtn = document.getElementById('api-key-regen-btn');
  if (regenBtn) regenBtn.disabled = envSet;
}

function toggleApiKeyVisibility() {
  const field = document.getElementById('api-key-field');
  if (!field) return;
  const isHidden = field.type === 'password';
  field.type = isHidden ? 'text' : 'password';
  const btn = document.getElementById('api-key-show-btn');
  if (btn) btn.textContent = isHidden ? '🙈' : '👁';
}

async function copyApiKey() {
  const field = document.getElementById('api-key-field');
  if (!field?.value) return;
  try {
    await navigator.clipboard.writeText(field.value);
    const status = document.getElementById('api-key-status');
    if (status) { status.textContent = t('apikey.copied'); status.style.color = 'var(--accent)'; }
    setTimeout(() => { if (status) { status.textContent = ''; } }, 2000);
  } catch(e) { console.error('Copy failed:', e); }
}

async function regenerateApiKey() {
  if (_apiKeyEnvSet) return;
  if (!confirm(t('apikey.regenerate_confirm'))) return;
  try {
    const r = await fetch('/api/settings/regenerate-api-key', {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      cfg.api_key = d.key;
      const field = document.getElementById('api-key-field');
      if (field) { field.value = d.key; field.type = 'text'; }
      const status = document.getElementById('api-key-status');
      if (status) { status.textContent = '✓'; status.style.color = 'var(--accent)'; }
      setTimeout(() => { if (status) status.textContent = ''; }, 2000);
    }
  } catch(e) { console.error('Regenerate failed:', e); }
}

async function resetCounter() {
  if (!confirm(t('filename.confirm_reset_counter'))) return;
  await saveSettingWithLog('counter', 0, t('filename.log_counter_reset'));
}

async function saveSetting(key, value) {
  cfg[key] = value;
  const body = {}; body[key] = value;
  try {
    const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if (!d.ok) console.error('saveSetting failed:', d);
  } catch(e) {
    console.error('saveSetting error:', e);
  }
}

async function saveSettingWithLog(key, value, logMsg) {
  cfg[key] = value;
  const body = {}; body[key] = value;
  try {
    const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if (!d.ok) console.error('saveSettingWithLog failed:', d);
  } catch(e) {
    console.error('saveSettingWithLog error:', e);
  }
  // Write to the activity log
  try {
    await fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({level:'info', message:'⚙ Config : ' + logMsg})});
  } catch(e) {}
}

// ── Tokens (filename builder) ─────────────────────────────────────────────
const TOKEN_LABELS = () => ({trigger: t('tokens.trigger'), string: t('tokens.string'), timestamp: t('tokens.timestamp'), counter: t('tokens.counter')});

function renderTokens() {
  const list = document.getElementById('token-list');
  const tokens = cfg.filename_tokens || [];
  list.innerHTML = tokens.map((tok, i) => {
    const mandatory = tok.type === 'timestamp' || tok.type === 'counter';
    let fields = '';
    if (tok.type === 'string') {
      fields = `<span class="tl">${t('tokens.value_label')}</span>
        <input type="text" value="${escapeHtml(tok.value||'')}" oninput="updateToken(${i},'value',this.value)" placeholder="${t('tokens.value_placeholder')}">`;
      // No toggle for string tokens: all removable via ✕
    } else if (tok.type === 'timestamp') {
      fields = `<span class="tl">${t('tokens.format_label')}</span>
        <input type="text" id="ts-fmt-${i}" value="${escapeHtml(tok.format||'%Y%m%d-%H%M%S')}" oninput="updateToken(${i},'format',this.value)" style="width:150px">
        <span style="display:flex;gap:4px">
          <button class="btn secondary" style="padding:2px 7px;font-size:10px" onclick="setTsPreset(${i},'%Y%m%d')">${t('tokens.preset_date')}</button>
          <button class="btn secondary" style="padding:2px 7px;font-size:10px" onclick="setTsPreset(${i},'%Y%m%d-%H%M%S')">${t('tokens.preset_datetime')}</button>
        </span>`;
    } else if (tok.type === 'counter') {
      fields = `<span class="tl">${t('tokens.digits_label')}</span>
        <input type="number" min="3" max="8" value="${tok.digits||6}"
          oninput="updateToken(${i},'digits',this.value)"
          onblur="if(parseInt(this.value)<3||parseInt(this.value)>8){this.value=Math.min(8,Math.max(3,parseInt(this.value)||6));updateToken(${i},'digits',parseInt(this.value));this.style.borderColor='';}">`;
    }
    // Counter: show only the range, not "required"
    const mandatoryLabel = tok.type === 'counter'
      ? `<span class="tl" style="color:var(--muted)">${t('tokens.digits_range')}</span>`
      : ``;
    // All STRING tokens are removable via ✕ (no toggle)
    const isString = tok.type === 'string';
    const removeBtn = isString
      ? `<span style="cursor:pointer;color:var(--error);font-size:16px;margin-left:4px" onclick="removeStringToken(${i})" title="${t('tokens.remove_free_text_title')}">✕</span>`
      : '';
    return `<div class="token-item" draggable="true"
        ondragstart="dragStart(event,${i})" ondragover="dragOver(event,${i})"
        ondragleave="dragLeave(event)" ondrop="dragDrop(event,${i})" id="tok-${i}">
      <span class="token-drag">⠿</span>
      <span class="token-type">${TOKEN_LABELS()[tok.type]||tok.type}</span>
      <div class="token-fields">
        ${fields}
        ${(!mandatory && !isString) ? `<label class="toggle" title="${t('tokens.enable_disable_title')}">
          <input type="checkbox" ${tok.enabled!==false?'checked':''} onchange="updateToken(${i},'enabled',this.checked)">
          <span class="toggle-slider"></span></label>` : (mandatory ? mandatoryLabel : '')}
        ${removeBtn}
      </div>
    </div>`;
  }).join('');
  updatePreview();
  renderSeparatorButtons();
  // Update the trigger select in the email panel if it is open
  const epanel = document.getElementById('email-config-panel');
  if (epanel && epanel.classList.contains('open') && activeEmailConfigId) {
    const ec = (cfg.email_configs || []).find(c => c.id === activeEmailConfigId);
    if (ec) renderEmailTriggerSelect(ec.default_trigger);
  }
}

function updateToken(i, key, value) {
  // Validation du compteur
  if (key === 'digits') {
    const d = parseInt(value);
    if (isNaN(d) || d < 3 || d > 8) {
      const inp = document.getElementById(`tok-${i}`)?.querySelector('input[type=number]');
      if (inp) inp.style.borderColor = 'var(--error)';
      return;
    } else {
      const inp = document.getElementById(`tok-${i}`)?.querySelector('input[type=number]');
      if (inp) inp.style.borderColor = '';
      value = d;
    }
  }
  cfg.filename_tokens[i] = Object.assign({}, cfg.filename_tokens[i], {[key]: value});
  updatePreview();
}

function updatePreview() {
  const tokens = cfg.filename_tokens || [];
  // Client-side validation (simplified)
  const hasCnt = tokens.some(t => t.type==='counter' && t.enabled!==false);
  const errEl  = document.getElementById('fn-error');
  if (!hasCnt) {
    errEl.textContent = t('filename.counter_required');
    errEl.style.display = 'block';
    document.getElementById('fn-preview').textContent = '–';
    return;
  }
  errEl.style.display = 'none';
  // Build the filename preview
  const now  = new Date();
  const pad  = (n,l=2) => String(n).padStart(l,'0');
  // Simplified format for preview (Python strftime directives %Y etc.)
  const fmtTs = (fmt) => fmt
    .replace('%Y', now.getFullYear()).replace('%m', pad(now.getMonth()+1))
    .replace('%d', pad(now.getDate())).replace('%H', pad(now.getHours()))
    .replace('%M', pad(now.getMinutes())).replace('%S', pad(now.getSeconds()));
  const parts = [];
  tokens.forEach(t => {
    // String tokens have no toggle: always included when non-empty
    if (t.type !== 'string' && t.enabled === false) return;
    if (t.type==='trigger')   parts.push('NEWDOC');
    if (t.type==='string' && t.value) parts.push(t.value);
    if (t.type==='timestamp') parts.push(fmtTs(t.format||'%Y%m%d-%H%M%S'));
    if (t.type==='counter')   parts.push('1'.padStart(t.digits||6,'0'));
  });
  const sep = cfg.filename_separator !== undefined ? cfg.filename_separator : '_';
  document.getElementById('fn-preview').textContent = parts.join(sep) + '.pdf';
  updateUnsavedWarning();
}

function toggleTriggerEdit() {
  const row = document.getElementById('tp-edit-row');
  const inp = document.getElementById('tp-edit-input');
  const isOpen = row.classList.toggle('open');
  if (isOpen) {
    const trig = cfg.split_values[activeTriggerIdx];
    inp.value = typeof trig === 'object' ? trig.value : trig;
    inp.focus();
    inp.select();
    // Validate on Enter key
    inp.onkeydown = function(e) {
      if (e.key === 'Enter')  applyTriggerEdit();
      if (e.key === 'Escape') toggleTriggerEdit();
    };
  }
}

function applyTriggerEdit() {
  const inp     = document.getElementById('tp-edit-input');
  const newVal  = inp.value.trim();
  if (!newVal) return;

  const trig    = cfg.split_values[activeTriggerIdx];
  const oldVal  = typeof trig === 'object' ? trig.value : trig;
  if (newVal === oldVal) { toggleTriggerEdit(); return; }

  // Check for duplicates
  const others = cfg.split_values
    .filter((_, i) => i !== activeTriggerIdx)
    .map(x => typeof x === 'object' ? x.value : x);
  if (others.includes(newVal)) {
    document.getElementById('tp-edit-input').style.borderColor = 'var(--error)';
    document.getElementById('tp-separator-msg').textContent = t('triggers.duplicate_value', {value: newVal});
    document.getElementById('tp-separator-msg').style.color = 'var(--error)';
    return;
  }

  // Appliquer
  if (typeof cfg.split_values[activeTriggerIdx] === 'object') {
    cfg.split_values[activeTriggerIdx].value = newVal;
  } else {
    cfg.split_values[activeTriggerIdx] = newVal;
  }

  // Update the panel title
  document.getElementById('tp-title').textContent = newVal;

  saveSettingWithLog('split_values', cfg.split_values,
    t('triggers.log_renamed', {old: oldVal, new: newVal}));
  renderTriggers();
  toggleTriggerEdit();
  updateSeparatorBtn();
}

function downloadSeparator() {
  if (activeTriggerIdx === null) return;
  const trig  = cfg.split_values[activeTriggerIdx];
  const v     = typeof trig === 'object' ? trig.value : trig;
  const isGlob = /[*?[\]]/.test(v);
  const msg   = document.getElementById('tp-separator-msg');
  if (isGlob) {
    msg.textContent = t('triggers.glob_undefined_msg');
    msg.style.color = 'var(--warn)';
    return;
  }
  const codeType = document.getElementById('tp-code-type').value;
  msg.textContent = t('triggers.generating');
  msg.style.color = 'var(--muted)';
  const url = `/api/separator/${activeTriggerIdx}?type=${codeType}`;
  fetch(url)
    .then(r => {
      if (!r.ok) throw new Error(r.statusText);
      return r.blob();
    })
    .then(blob => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `separator_${v}.pdf`;
      a.click();
      msg.textContent = t('triggers.downloaded');
      msg.style.color = 'var(--accent)';
      setTimeout(() => msg.textContent = '', 3000);
    })
    .catch(e => {
      msg.textContent = t('common.error_short', {message: e.message});
      msg.style.color = 'var(--error)';
    });
}

function updateSeparatorBtn() {
  if (activeTriggerIdx === null) return;
  const trig   = cfg.split_values[activeTriggerIdx];
  const v      = typeof trig === 'object' ? trig.value : trig;
  const isGlob = /[*?[\]]/.test(v);
  const btn    = document.getElementById('tp-separator-btn');
  const msg    = document.getElementById('tp-separator-msg');
  if (!btn) return;
  if (isGlob) {
    btn.disabled = true;
    btn.style.opacity = '0.4';
    btn.title = t('triggers.glob_undefined_title');
    msg.textContent = t('triggers.glob_undefined_short');
    msg.style.color = 'var(--warn)';
  } else {
    btn.disabled = false;
    btn.style.opacity = '1';
    btn.title = '';
    msg.textContent = '';
  }
}

async function saveTokens() {
  const tokens = cfg.filename_tokens;
  const res    = await fetch('/api/config/validate_tokens', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({tokens})
  });
  const data = await res.json();
  const errEl = document.getElementById('fn-error');
  if (!data.ok) {
    errEl.textContent = data.error; errEl.style.display='block'; return;
  }
  errEl.style.display = 'none';
  const sep = cfg.filename_separator ?? '_';
  // Save tokens AND separator in a single API call for consistency
  cfg.filename_tokens   = tokens;
  cfg.filename_separator = sep;
  const body = {filename_tokens: tokens, filename_separator: sep};
  try {
    await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    await fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({level:'info', message: t('filename.log_format_updated')})});
  } catch(e) { console.error('saveTokens error:', e); }
  savedTokensHash = _tokensHash(tokens, sep);
  updateUnsavedWarning();
  const ok = document.getElementById('save-ok');
  ok.style.display='inline'; setTimeout(()=>ok.style.display='none', 2500);
}

// ── Global separator ──────────────────────────────────────────────────────
function setSeparator(sep) {
  cfg.filename_separator = sep;
  ['_', '-', '.', ''].forEach(s => {
    const id = s === '' ? 'sep-none' : `sep-${s}`;
    const btn = document.getElementById(id);
    if (btn) btn.className = s === sep ? 'btn' : 'btn secondary';
  });
  updatePreview();
  updateUnsavedWarning();
}

function renderSeparatorButtons() {
  const sep = cfg.filename_separator ?? '_';
  ['_', '-', '.', ''].forEach(s => {
    const id = s === '' ? 'sep-none' : `sep-${s}`;
    const btn = document.getElementById(id);
    if (btn) btn.className = s === sep ? 'btn' : 'btn secondary';
  });
}

// ── Presets timestamp ──────────────────────────────────────────────────────
function setTsPreset(i, fmt) {
  cfg.filename_tokens[i].format = fmt;
  const inp = document.getElementById(`ts-fmt-${i}`);
  if (inp) inp.value = fmt;
  updatePreview();
}

// ── Tokens string additionnels ─────────────────────────────────────────────
function addStringToken() {
  const newToken = {
    type: 'string',
    enabled: true,
    value: '',
    id: 'string-' + Date.now()
  };
  cfg.filename_tokens.push(newToken);
  renderTokens();
}

function removeStringToken(i) {
  cfg.filename_tokens.splice(i, 1);
  renderTokens();
}

// ── Drag & drop tokens ────────────────────────────────────────────────────
function dragStart(e, i) { dragSrc = i; e.currentTarget.classList.add('dragging'); }
function dragOver(e, i)  { e.preventDefault(); if(i!==dragSrc) e.currentTarget.classList.add('dragover'); }
function dragLeave(e)    { e.currentTarget.classList.remove('dragover'); }
function dragDrop(e, i)  {
  e.preventDefault();
  e.currentTarget.classList.remove('dragover');
  if (dragSrc === null || dragSrc === i) return;
  const tokens = cfg.filename_tokens;
  const moved = tokens.splice(dragSrc, 1)[0];
  tokens.splice(i, 0, moved);
  renderTokens();
  dragSrc = null;
}

// ── Init ──────────────────────────────────────────────────────────────────
// ── Upload zone ──────────────────────────────────────────────────────────
function uzDragOver(e) {
  e.preventDefault();
  // Reject directories
  if (e.dataTransfer.items) {
    for (const item of e.dataTransfer.items) {
      if (item.webkitGetAsEntry && item.webkitGetAsEntry()?.isDirectory) return;
    }
  }
  document.getElementById('upload-zone').classList.add('dragover');
}
function uzDragLeave(e) { document.getElementById('upload-zone').classList.remove('dragover'); }
function uzDrop(e) {
  e.preventDefault();
  document.getElementById('upload-zone').classList.remove('dragover');
  const items = e.dataTransfer.items;
  const files = [];
  if (items) {
    for (const item of items) {
      if (item.webkitGetAsEntry && item.webkitGetAsEntry()?.isDirectory) continue;
      const f = item.getAsFile();
      if (f && f.name.toLowerCase().endsWith('.pdf')) files.push(f);
    }
  } else {
    for (const f of e.dataTransfer.files) {
      if (f.name.toLowerCase().endsWith('.pdf')) files.push(f);
    }
  }
  if (files.length) uzSend(files);
}
function uzFiles(fileList) {
  const files = Array.from(fileList).filter(f => f.name.toLowerCase().endsWith('.pdf'));
  if (files.length) uzSend(files);
  document.getElementById('upload-input').value = '';
}
async function uzSend(files) {
  const flash = document.getElementById('upload-flash');
  flash.className = 'upload-flash';
  flash.textContent = t('upload.sending', {count: files.length});
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  try {
    const r = await fetch('/api/upload', {method:'POST', body: fd});
    const d = await r.json();
    if (d.saved && d.saved.length) {
      flash.textContent = t('upload.sent', {count: d.saved.length});
      flash.className = 'upload-flash';
    }
    if (d.errors && d.errors.length) {
      flash.textContent += ' · ' + t('upload.errors', {errors: d.errors.join(', ')});
      flash.className = 'upload-flash err';
    }
    setTimeout(() => flash.textContent = '', 4000);
  } catch(e) {
    flash.textContent = t('common.error_short', {message: e.message});
    flash.className = 'upload-flash err';
  }
}

// ── Detection test panel ──────────────────────────────────────────────────
function toggleDetectPanel() {
  const b = document.getElementById('detect-body');
  const a = document.getElementById('detect-arrow');
  const open = b.classList.toggle('open');
  a.textContent = open ? '▲' : '▼';
  const hdr = document.querySelector('[aria-controls="detect-body"]');
  if (hdr) hdr.setAttribute('aria-expanded', String(open));
}
function dtDragOver(e) {
  e.preventDefault();
  document.getElementById('detect-zone').classList.add('dragover');
}
function dtDragLeave(e) { document.getElementById('detect-zone').classList.remove('dragover'); }
function dtDrop(e) {
  e.preventDefault();
  document.getElementById('detect-zone').classList.remove('dragover');
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f && f.name.toLowerCase().endsWith('.pdf')) dtSend(f);
}
function dtFiles(fileList) {
  const f = fileList && fileList[0];
  if (f && f.name.toLowerCase().endsWith('.pdf')) dtSend(f);
  document.getElementById('detect-input').value = '';
}
async function dtSend(file) {
  const box = document.getElementById('detect-results');
  box.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--muted)">${t('detect.analyzing', {filename: escapeHtml(file.name)})}</div>`;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/detect', {method: 'POST', body: fd});
    const d = await r.json();
    if (!d.ok) {
      box.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--error)">${escapeHtml(d.error || 'error')}</div>`;
      return;
    }
    dtRender(d, box);
  } catch (e) {
    box.innerHTML = `<div style="font-family:var(--mono);font-size:11px;color:var(--error)">${escapeHtml(e.message)}</div>`;
  }
}
function dtRender(d, box) {
  const mono = 'font-family:var(--mono);font-size:11px;';
  let html = `<div style="${mono}color:var(--muted);margin-bottom:8px">` +
    escapeHtml(t('detect.env', {scanner: d.scanner, dpi_scan: d.dpi_scan,
                                dpi: d.dpi, upscale: d.upscale})) + '</div>';
  if (d.permissive) {
    html += `<div style="${mono}color:var(--warn);margin-bottom:8px">${t('detect.permissive_note')}</div>`;
  }
  if (d.truncated) {
    html += `<div style="${mono}color:var(--warn);margin-bottom:8px">` +
      escapeHtml(t('detect.truncated', {analyzed: d.pages_analyzed, total: d.pages_total})) + '</div>';
  }
  for (const p of d.pages) {
    html += `<div style="${mono}color:var(--text);margin:10px 0 4px;letter-spacing:.05em">` +
      escapeHtml(t('detect.page', {page: p.page})) + '</div>';
    if (!p.codes.length) {
      html += `<div style="${mono}color:var(--muted);padding-left:12px">— ${t('detect.no_codes')}</div>`;
      continue;
    }
    for (const c of p.codes) {
      const pos = c.bbox
        ? `x${c.bbox.x} y${c.bbox.y} · ${c.bbox.w}×${c.bbox.h}px`
        : '—';
      const prodColor = c.production_detected ? 'var(--accent)' : 'var(--error)';
      const prodLabel = c.production_detected ? t('detect.prod_ok') : t('detect.prod_ko');
      let matchHtml;
      if (c.matches.length) {
        matchHtml = c.matches.map(m =>
          `<span style="color:var(--accent)">«${escapeHtml(m.pattern)}»</span>` +
          `<span style="color:var(--muted)"> ${m.is_glob ? 'glob · ' : ''}${escapeHtml(m.page_handling)} → ${escapeHtml(m.effective)}</span>`
        ).join('<br>');
      } else {
        matchHtml = `<span style="color:var(--warn)">${t('detect.match_none')}</span>`;
      }
      const splitBadge = c.would_split
        ? `<span style="color:var(--accent)">✓ ${t('detect.would_split')}</span>`
        : `<span style="color:var(--muted)">✗ ${t('detect.no_split')}</span>`;
      html += `<div style="${mono}padding:6px 12px;margin:4px 0;background:var(--surface2);border-left:2px solid ${prodColor};border-radius:2px">
        <div><span style="color:var(--text)">${escapeHtml(c.value)}</span>
             <span style="color:var(--muted)"> · ${escapeHtml(c.type)} · ${escapeHtml(pos)}</span></div>
        <div style="margin-top:2px;color:${prodColor}">${prodLabel}</div>
        ${!c.at_scan_dpi && c.at_full_dpi
          ? `<div style="margin-top:2px;color:var(--warn)">${escapeHtml(t('detect.scan_gate_warning', {dpi: d.dpi, dpi_scan: d.dpi_scan}))}</div>` : ''}
        <div style="margin-top:2px">${matchHtml}</div>
        <div style="margin-top:2px">${splitBadge}</div>
      </div>`;
    }
  }
  box.innerHTML = html;
}

// ── Dossiers ──────────────────────────────────────────────────────────────
const DIR_LABELS = () => ({input: '📂 ' + t('dirs.label_input'), output: '📁 ' + t('dirs.label_output'), no_code: '🔍 ' + t('dirs.label_no_code'), error: '⚠️ ' + t('dirs.label_error'), processed: '✅ ' + t('dirs.label_processed')});
const DIR_ORDER  = ['input', 'output', 'no_code', 'error', 'processed'];
let activeDirKey = null;

function renderDirs(dirsStatus, dataDir) {
  document.getElementById('dir-data-root').textContent = dataDir || '/data';
  const tbody = document.getElementById('dirs-tbody');
  if (!tbody || !dirsStatus) return;
  const ordered = DIR_ORDER
    .filter(k => dirsStatus[k])
    .map(k => [k, dirsStatus[k]])
    .concat(Object.entries(dirsStatus).filter(([k]) => !DIR_ORDER.includes(k)));
  tbody.innerHTML = ordered.map(([k, v]) => {
    const rel  = v.path ? v.path.replace((dataDir || '/data') + '/', '') : v.path;
    const warn = !v.exists ? `<span class="dw">${t('dirs.missing')}</span>` : '';
    const recreate = !v.exists
      ? `<button class="btn secondary" style="font-size:10px;padding:2px 8px" onclick="recreateDir('${escapeJsStr(k)}')">${t('dirs.recreate')}</button>` : '';
    return `<tr id="dir-row-${escapeHtml(k)}">
      <td class="dk">${escapeHtml(DIR_LABELS()[k]||k)}</td>
      <td class="dv">${escapeHtml(rel)} ${warn}</td>
      <td style="white-space:nowrap;display:flex;align-items:center;gap:6px">
        ${recreate}
        <button class="dir-edit-btn" onclick="openDirEdit('${escapeJsStr(k)}','${escapeJsStr(rel)}')" title="${t('dirs.edit_path_title')}">
          <svg width="20" height="20" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="#f5a623" d="M5 3c-1.11 0-2 .89-2 2v14c0 1.11.89 2 2 2h14c1.11 0 2-.89 2-2v-7h-2v7H5V5h7V3H5m12.78 1a.69.69 0 0 0-.48.2l-1.22 1.21 2.5 2.5L19.8 6.7c.26-.26.26-.7 0-.96l-1.54-1.54a.68.68 0 0 0-.48-.2m-2.41 2.12L9 12.5V15h2.5l6.37-6.38-2.5-2.5Z"/></svg>
        </button>
      </td>
    </tr>
    <tr id="dir-edit-${k}" style="display:none">
      <td colspan="3" style="padding:0">
        <div class="dir-edit-row open" style="padding:8px 10px">
          <input type="text" id="dir-inp-${k}" placeholder="${rel}" value="${rel}"
            onkeydown="if(event.key==='Enter')applyDirEdit('${k}');if(event.key==='Escape')closeDirEdit('${k}')">
          <button class="btn" onclick="applyDirEdit('${k}')">${t('common.apply')}</button>
          <button class="btn secondary" onclick="closeDirEdit('${k}')">${t('common.cancel')}</button>
          <span id="dir-err-${k}" style="font-family:var(--mono);font-size:11px;color:var(--error)"></span>
        </div>
      </td>
    </tr>`;
  }).join('');
}

async function confirmResetStats() {
  if (!confirm(t('options.confirm_reset_stats'))) return;
  try {
    await fetch('/api/stats/reset', {method: 'POST'});
    // Update the display immediately
    document.getElementById('st-proc').textContent = '0';
    document.getElementById('st-docs').textContent = '0';
    document.getElementById('st-err').textContent  = '0';
    document.getElementById('st-last').textContent = '–';
  } catch(e) {
    alert(t('options.reset_stats_error', {message: e.message}));
  }
}

// ── Email panel ───────────────────────────────────────────────────────────
let activeEmailConfigId = null;

/** Close all inline panels except the given one (null = close everything). */
function toggleOptionsSection() {
  const body  = document.getElementById('options-body');
  const arrow = document.getElementById('options-arrow');
  if (!body || !arrow) return;
  const open = body.style.display === 'none' || !body.style.display;
  if (!open) _closeAllPanels(null);   // close inline panels when collapsing
  body.style.display = open ? 'block' : 'none';
  arrow.textContent  = open ? '▼' : '▶';
  const hdr = document.querySelector('[aria-controls="options-body"]');
  if (hdr) hdr.setAttribute('aria-expanded', String(open));
}

function _closeAllPanels(except) {
  const panels = [
    { panel: 'dirs-panel',    arrow: 'dirs-panel-arrow',    btn: 'dirs-panel-btn'    },
    { panel: 'email-section', arrow: 'email-panel-arrow',   btn: 'email-panel-btn'   },
    { panel: 'webhook-panel', arrow: 'webhook-panel-arrow', btn: 'webhook-panel-btn' },
    { panel: 'apikey-panel',  arrow: 'apikey-panel-arrow',  btn: 'apikey-panel-btn'  },
  ];
  for (const p of panels) {
    if (p.panel === except) continue;
    const el = document.getElementById(p.panel);
    if (el && el.style.display === 'block') {
      el.style.display = 'none';
      const a = document.getElementById(p.arrow); if (a) a.textContent = '▶';
      const b = document.getElementById(p.btn);   if (b) b.classList.remove('active');
    }
  }
}

function toggleWebhookPanel() {
  const panel = document.getElementById('webhook-panel');
  const arrow = document.getElementById('webhook-panel-arrow');
  const btn   = document.getElementById('webhook-panel-btn');
  const open  = panel.style.display !== 'block';
  if (open) _closeAllPanels('webhook-panel');
  panel.style.display = open ? 'block' : 'none';
  arrow.textContent   = open ? '▼' : '▶';
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', String(open));
  if (open) renderWebhook();
}

function toggleApiKeyPanel() {
  const panel = document.getElementById('apikey-panel');
  const arrow = document.getElementById('apikey-panel-arrow');
  const btn   = document.getElementById('apikey-panel-btn');
  const open  = panel.style.display !== 'block';
  if (open) _closeAllPanels('apikey-panel');
  panel.style.display = open ? 'block' : 'none';
  arrow.textContent   = open ? '▼' : '▶';
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', String(open));
  if (open) renderApiKey();
}

function toggleEmailSection() {
  const panel  = document.getElementById('email-section');
  const arrow  = document.getElementById('email-panel-arrow');
  const btn    = document.getElementById('email-panel-btn');
  const open   = panel.style.display === 'none' || !panel.style.display;
  if (open) _closeAllPanels('email-section');
  panel.style.display = open ? 'block' : 'none';
  arrow.textContent   = open ? '▼' : '▶';
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', String(open));
  if (open) renderEmailConfigs();
}

function renderEmailConfigs() {
  const el      = document.getElementById('email-config-list');
  const configs = cfg.email_configs || [];
  el.innerHTML = configs.map(ec => {
    const active   = activeEmailConfigId === ec.id ? ' active' : '';
    const blocked  = ec.polling_blocked ? `<span class="del-icon" title="${t('email.polling_blocked_title')}">⛔</span>` : '';
    const disabled = !ec.enabled ? `<span class="case-icon" title="${t('email.disabled_title')}">⏸</span>` : '';
    return `<div class="trigger-tag${active}" onclick="openEmailConfigPanel('${escapeJsStr(ec.id)}')"
      title="${t('common.click_to_configure')}">
      <span>${escapeHtml(ec.name || t('email.default_config_name'))}</span>
      ${disabled}${blocked}
      <span class="rm" onclick="event.stopPropagation();removeEmailConfig('${escapeJsStr(ec.id)}')">✕</span>
    </div>`;
  }).join('');
}

function openEmailConfigPanel(id) {
  const configs = cfg.email_configs || [];
  const ec = configs.find(c => c.id === id);
  if (!ec) return;

  if (activeEmailConfigId === id) {
    activeEmailConfigId = null;
    document.getElementById('email-config-panel').classList.remove('open');
    renderEmailConfigs();
    return;
  }

  activeEmailConfigId = id;
  document.getElementById('email-config-panel').classList.add('open');
  fillEmailConfigForm(ec);
  renderEmailConfigs();
}

function fillEmailConfigForm(ec) {
  document.getElementById('em-name').value     = ec.name || '';
  document.getElementById('em-enabled').checked = !!ec.enabled;
  document.getElementById('em-host').value      = ec.host || '';
  document.getElementById('em-port').value      = ec.port || 993;
  document.getElementById('em-username').value  = ec.username || '';
  document.getElementById('em-password').value  = '';
  document.getElementById('em-folder').value    = ec.folder || 'INBOX';
  document.getElementById('em-interval').value  = ec.poll_interval || 5;
  document.getElementById('em-filter-from').value    = ec.filter_from || '';
  document.getElementById('em-filter-subject').value = ec.filter_subject || '';
  document.getElementById('em-use-ssl').checked = ec.use_ssl !== false;
  document.getElementById('em-ssl').checked     = ec.verify_ssl !== false;
  _emailUpdateSslRow();

  const action = ec.action || 'read';
  document.querySelectorAll('input[name="em-action"]').forEach(r => r.checked = r.value === action);

  renderEmailTriggerSelect(ec.default_trigger);

  const blockedSec = document.getElementById('em-blocked-section');
  if (blockedSec) blockedSec.style.display = ec.polling_blocked ? 'block' : 'none';

  document.getElementById('em-test-result').textContent = '';
  emailUnsaved = false;
  updateEmailStatusMessage();
}

function renderEmailTriggerSelect(current) {
  const sel = document.getElementById('em-default-trigger');
  if (!sel) return;
  const triggers = (cfg.split_values || []).map(sv => typeof sv === 'object' ? sv.value : sv);
  sel.innerHTML = `<option value="">${t('common.none_no_code')}</option>` +
    triggers.map(v => `<option value="${escapeHtml(v)}"${v===current?' selected':''}>${escapeHtml(v)}</option>`).join('');
  if (current && !triggers.includes(current)) {
    sel.value = '';
  }
}

let emailUnsaved = false;

function collectEmailFormData() {
  return {
    name:            document.getElementById('em-name').value.trim(),
    enabled:         document.getElementById('em-enabled').checked,
    host:            document.getElementById('em-host').value.trim(),
    port:            parseInt(document.getElementById('em-port').value) || 993,
    username:        document.getElementById('em-username').value.trim(),
    folder:          document.getElementById('em-folder').value.trim() || 'INBOX',
    poll_interval:   parseInt(document.getElementById('em-interval').value) || 5,
    filter_from:     document.getElementById('em-filter-from').value.trim(),
    filter_subject:  document.getElementById('em-filter-subject').value.trim(),
    action:          document.querySelector('input[name="em-action"]:checked')?.value || 'read',
    use_ssl:         document.getElementById('em-use-ssl').checked,
    verify_ssl:      document.getElementById('em-ssl').checked,
    default_trigger: document.getElementById('em-default-trigger').value || null,
  };
}

function _emailUsernameKey(username) {
  let u = (username || '').trim().toLowerCase();
  const at = u.indexOf('@');
  if (at !== -1) u = u.slice(0, at);
  return u;
}

function _emailSignature(c) {
  // Intentionally excludes: action, default trigger, interval, enabled/disabled
  return [
    (c.host || '').trim().toLowerCase(),
    _emailUsernameKey(c.username),
    (c.folder || 'INBOX').trim().toLowerCase(),
    (c.filter_from || '').trim().toLowerCase(),
    (c.filter_subject || '').trim().toLowerCase(),
  ].join('|');
}

function updateEmailStatusMessage() {
  // Un seul emplacement de message : priorite aux erreurs de doublon,
  // otherwise show an "unsaved changes" warning.
  const w       = document.getElementById('email-unsaved-warning');
  const saveBtn = document.getElementById('em-save-btn');
  if (!activeEmailConfigId) return false;

  const current = collectEmailFormData();

  // 1. Name already used by another configuration
  const nameNorm = (current.name || '').trim().toLowerCase();
  const nameDup = (cfg.email_configs || []).find(c =>
    c.id !== activeEmailConfigId && (c.name || '').trim().toLowerCase() === nameNorm);
  if (nameDup) {
    w.textContent = t('email.duplicate_name', {name: current.name || '?'});
    w.style.display = 'inline';
    saveBtn.disabled = true;
    return true;
  }

  // 2. Identical configuration (server, user, folder, filters)
  const sig = _emailSignature(current);
  const dup = (cfg.email_configs || []).find(c => c.id !== activeEmailConfigId && _emailSignature(c) === sig);
  if (dup) {
    w.textContent = t('email.duplicate_config', {name: dup.name || '?'});
    w.style.display = 'inline';
    saveBtn.disabled = true;
    return true;
  }

  // 3. No error: unsaved-change warning, or nothing
  saveBtn.disabled = false;
  if (emailUnsaved) {
    w.textContent = t('common.unsaved_warning');
    w.style.display = 'inline';
  } else {
    w.style.display = 'none';
  }
  return false;
}

function emailAutoSsl() {
  /* Auto-toggle use_ssl when the port changes to a well-known value. */
  const port = parseInt(document.getElementById('em-port').value) || 0;
  if (port === 993 || port === 3993) {
    document.getElementById('em-use-ssl').checked = true;
  } else if (port === 143 || port === 3143) {
    document.getElementById('em-use-ssl').checked = false;
  }
  _emailUpdateSslRow();
}

function emailUseSslChanged() {
  _emailUpdateSslRow();
  emailFieldChanged();
}

function _emailUpdateSslRow() {
  /* Grey out and disable verify_ssl when use_ssl is off (plain IMAP).
     Uses an explicit CSS class (.toggle-off) in addition to setting .checked,
     because Safari does not always repaint :checked on programmatic changes. */
  const useSsl   = document.getElementById('em-use-ssl').checked;
  const row      = document.getElementById('em-ssl-row');
  const sslInput = document.getElementById('em-ssl');
  if (!row || !sslInput) return;
  row.style.opacity       = useSsl ? '1' : '0.4';
  row.style.pointerEvents = useSsl ? '' : 'none';
  const toggleLabel = row.querySelector('.toggle');
  if (!useSsl) {
    sslInput.checked = false;
    if (toggleLabel) toggleLabel.classList.add('toggle-off');
  } else {
    sslInput.checked = true;
    if (toggleLabel) toggleLabel.classList.remove('toggle-off');
  }
}

function emailFieldChanged() {
  emailUnsaved = true;
  updateEmailStatusMessage();
}

function addEmailConfig() {
  // Create a local draft (not persisted): it will only be saved to the
  // to the server only when "Save" is clicked. If the user does nothing,
  // server when the user clicks Save. The draft disappears on next refresh.
  const inp  = document.getElementById('new-email-config-name');
  const name = inp.value.trim();
  const draft = {
    id: 'draft_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    _isDraft: true,
    name: name || ('Config ' + ((cfg.email_configs || []).length + 1)),
    enabled: false, host: '', port: 993, username: '', folder: 'INBOX',
    poll_interval: 5, filter_from: '', filter_subject: '', action: 'read',
    verify_ssl: true, default_trigger: null,
    processed_ids: [], processed_ids_oldest: null, polling_blocked: false,
  };
  cfg.email_configs = [...(cfg.email_configs || []), draft];
  inp.value = '';
  renderEmailConfigs();
  openEmailConfigPanel(draft.id);
  emailUnsaved = false;  // an empty draft is not considered "unsaved" by default
  updateEmailStatusMessage();
}

async function removeEmailConfig(id) {
  const ec = (cfg.email_configs || []).find(c => c.id === id);
  if (!ec) return;

  // Draft never saved: immediate local removal, no confirmation or API call
  if (ec._isDraft) {
    cfg.email_configs = (cfg.email_configs || []).filter(c => c.id !== id);
    if (activeEmailConfigId === id) {
      activeEmailConfigId = null;
      document.getElementById('email-config-panel').classList.remove('open');
    }
    renderEmailConfigs();
    return;
  }

  if (!confirm(t('email.confirm_delete', {name: ec.name}))) return;
  try {
    const r = await fetch('/api/email/configs/' + id, {method: 'DELETE'});
    const d = await r.json();
    if (d.ok) {
      cfg.email_configs = (cfg.email_configs || []).filter(c => c.id !== id);
      if (activeEmailConfigId === id) {
        activeEmailConfigId = null;
        document.getElementById('email-config-panel').classList.remove('open');
      }
      renderEmailConfigs();
    } else {
      alert(d.error || t('email.delete_error'));
    }
  } catch(e) { alert(t('common.error_label', {message: e.message})); }
}

async function saveEmailConfig() {
  if (!activeEmailConfigId) return;
  if (updateEmailStatusMessage()) return;
  const payload = collectEmailFormData();
  const pw = document.getElementById('em-password').value;
  if (pw) payload.password = pw;

  const current  = (cfg.email_configs || []).find(c => c.id === activeEmailConfigId);
  const isDraft  = current && current._isDraft;
  const url      = isDraft ? '/api/email/configs' : ('/api/email/configs/' + activeEmailConfigId);

  try {
    const r = await fetch(url, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if (d.ok) {
      emailUnsaved = false;
      if (isDraft) {
        // The draft becomes a real configuration with the server-assigned id
        cfg.email_configs = (cfg.email_configs || []).map(c =>
          c.id === activeEmailConfigId ? d.config : c);
        activeEmailConfigId = d.config.id;
      } else {
        cfg.email_configs = (cfg.email_configs || []).map(c =>
          c.id === activeEmailConfigId ? {...c, ...d.config} : c);
      }
      renderEmailConfigs();
      updateEmailStatusMessage();
      log_event_local('info', t('email.log_saved', {name: payload.name}));
    } else {
      const w = document.getElementById('email-unsaved-warning');
      w.textContent = '⚠ ' + (d.error || t('email.save_error_default'));
      w.style.display = 'inline';
      document.getElementById('em-save-btn').disabled = true;
    }
  } catch(e) { alert(t('common.error_label', {message: e.message})); }
}

async function testEmailConnection() {
  const btn = document.getElementById('em-test-btn');
  const res = document.getElementById('em-test-result');
  btn.disabled = true;
  res.textContent = t('email.testing');
  const payload = {
    name:       document.getElementById('em-name').value.trim() || '?',
    host:       document.getElementById('em-host').value.trim(),
    port:       parseInt(document.getElementById('em-port').value) || 993,
    username:   document.getElementById('em-username').value.trim(),
    folder:     document.getElementById('em-folder').value.trim() || 'INBOX',
    verify_ssl: document.getElementById('em-ssl').checked,
    use_ssl:    document.getElementById('em-use-ssl').checked,
  };
  const pw = document.getElementById('em-password').value;
  if (pw) {
    payload.password = pw;  // mot de passe saisi en clair
  } else if (!String(activeEmailConfigId).startsWith('draft_')) {
    // Empty field: the server retrieves the already-encrypted password via the id
    // (the encrypted password is never transmitted to the browser)
    payload.id = activeEmailConfigId;
  }
  try {
    const r = await fetch('/api/email/test', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    res.textContent = d.message;
    res.style.color = d.ok ? 'var(--accent)' : 'var(--error)';
  } catch(e) {
    res.textContent = t('common.error_label', {message: e.message});
    res.style.color = 'var(--error)';
  }
  btn.disabled = false;
}

async function resetEmailIds() {
  if (!activeEmailConfigId) return;
  if (!confirm(t('email.confirm_reset_ids'))) return;
  try {
    const r = await fetch('/api/email/reset_ids/' + activeEmailConfigId, {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      const ec = (cfg.email_configs || []).find(c => c.id === activeEmailConfigId);
      if (ec) {
        ec.processed_ids = [];
        ec.processed_ids_oldest = null;
        ec.polling_blocked = false;
      }
      const blockedSec = document.getElementById('em-blocked-section');
      if (blockedSec) blockedSec.style.display = 'none';
      renderEmailConfigs();
    }
  } catch(e) { alert(t('common.error_label', {message: e.message})); }
}

function updateEmailAlert(configsStatus) {
  const el = document.getElementById('email-alert');
  if (!el) return;
  const blocked = (configsStatus || []).filter(c => c.polling_blocked);
  if (blocked.length === 0) {
    el.className = 'email-alert';
    return;
  }
  el.className = 'email-alert visible';
  const reasons = blocked.map(c => {
    const r = c.processed_ids_count >= 1000
      ? t('email.alert_reason_ids_limit', {count: c.processed_ids_count})
      : t('email.alert_reason_old_messages');
    return t('email.alert_reason_format', {name: c.name, reason: r, since: c.processed_ids_oldest || '–'});
  });
  document.getElementById('email-alert-reason').textContent = reasons.join(' · ');
}

function log_event_local(level, msg) {
  fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({level, message: msg})});
}


function toggleDirsPanel() {
  const panel = document.getElementById('dirs-panel');
  const arrow = document.getElementById('dirs-panel-arrow');
  const btn   = document.getElementById('dirs-panel-btn');
  const open  = panel.style.display === 'none' || panel.style.display === '';
  if (open) _closeAllPanels('dirs-panel');
  panel.style.display = open ? 'block' : 'none';
  arrow.textContent   = open ? '▼' : '▶';
  btn.classList.toggle('active', open);
  btn.setAttribute('aria-expanded', String(open));
  if (open && cfg.app_config) {
    renderDirs(cfg.app_config.dirs_status || {}, cfg.app_config.data_dir || '/data');
  }
}

function openDirEdit(k, current) {
  closeDirEdit(activeDirKey);
  activeDirKey = k;
  const row = document.getElementById(`dir-edit-${k}`);
  if (row) row.style.display = 'table-row';
  const inp = document.getElementById(`dir-inp-${k}`);
  if (inp) { inp.value = current; inp.focus(); inp.select(); }
}

function closeDirEdit(k) {
  if (!k) return;
  const row = document.getElementById(`dir-edit-${k}`);
  if (row) row.style.display = 'none';
  if (activeDirKey === k) activeDirKey = null;
}

async function applyDirEdit(k) {
  const inp  = document.getElementById(`dir-inp-${k}`);
  const errEl = document.getElementById(`dir-err-${k}`);
  if (!inp) return;
  const val = inp.value.trim();
  errEl.textContent = '';
  try {
    const r = await fetch('/api/dirs/rename', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({key: k, path: val})
    });
    const d = await r.json();
    if (!d.ok) { errEl.textContent = d.error; return; }
    saveSettingWithLog('dirs', d.dirs, t('dirs.log_renamed', {key: k, path: val}));
    closeDirEdit(k);
    cfg.loaded = false; // forcer rechargement
  } catch(e) { errEl.textContent = t('common.error_label', {message: e.message}); }
}

async function recreateDir(k) {
  const r = await fetch('/api/dirs/recreate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({key: k})
  });
  const d = await r.json();
  if (d.ok) cfg.loaded = false;
}

applyI18n();
refresh();
setInterval(refresh, 3000);
