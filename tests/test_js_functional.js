/**
 * test_js_functional.js
 * ----------------------
 * Test fonctionnel de app.js sans navigateur (Node.js + vm).
 *
 * Cas couverts :
 *   1. Chargement de la config (loadConfig) et rendu des declencheurs,
 *      options et tokens (renderTriggers, renderOptions, renderTokens).
 *   2. Bouton de telechargement de la page intercalaire :
 *      - declencheur normal  → fetch /api/separator/<idx>?type=qr
 *      - pattern glob        → aucun fetch, bouton desactive
 *   3. Exclusivite mutuelle des panneaux Dossiers / Email :
 *      - ouvrir l'un ferme l'autre, boutons .active coherents.
 *   4. Actions sur les declencheurs :
 *      - applyTriggerEdit avec doublon → message d'erreur traduit
 *      - removeTrigger → liste mise a jour
 *   5. Absence de TypeError "X is not a function" dans toutes ces
 *      operations (regression t() shadowing).
 *
 * Sortie : "OK" + recapitulatif ou message d'erreur + exit code 1.
 */

'use strict';
const fs  = require('fs');
const vm  = require('vm');
const path = require('path');

const ROOT   = path.resolve(__dirname, '..');
const APP_JS = path.join(ROOT, 'splitter', 'static', 'js', 'app.js');
const FR_JSON = path.join(ROOT, 'splitter', 'i18n', 'fr.json');

// ── DOM stub ────────────────────────────────────────────────────────────────
const elements = {};
function getEl(id) {
  if (!elements[id]) {
    const el = {
      id, innerHTML: '', textContent: '', value: '', checked: false,
      style: { display: 'none' }, dataset: {}, title: '', disabled: false,
      _attrs: {},
      setAttribute(name, value) { this._attrs[name] = String(value); },
      getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
      removeAttribute(name) { delete this._attrs[name]; },
      hasAttribute(name) { return name in this._attrs; },
      classList: {
        _s: new Set(),
        toggle(c, force) {
          force === undefined
            ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c))
            : (force ? this._s.add(c) : this._s.delete(c));
        },
        add(c)      { this._s.add(c);     },
        remove(c)   { this._s.delete(c);  },
        contains(c) { return this._s.has(c); },
      },
      addEventListener: () => {},
      querySelectorAll: () => [],
      querySelector:    () => null,
      click: () => {}, focus: () => {}, select: () => {},
      cloneNode() { return getEl(this.id); },
      parentNode: { replaceChild: () => {} },
    };
    elements[id] = el;
  }
  return elements[id];
}

// ── Config de test ──────────────────────────────────────────────────────────
const i18n = JSON.parse(fs.readFileSync(FR_JSON, 'utf8'));

const STATE = {
  app_config: {
    split_values: [
      { value: 'NEWDOC', page_handling: 'delete', case_sensitive: true },
      { value: 'FK3',    page_handling: 'start',  case_sensitive: true },
      { value: 'DOC*',   page_handling: 'start',  case_sensitive: true }, // glob
    ],
    subdirs_by_trigger: true,
    delete_source:      true,
    filename_tokens: [
      { type: 'trigger',   enabled: true },
      { type: 'timestamp', enabled: true, format: '%Y%m%d' },
      { type: 'counter',   enabled: true, digits: 3 },
    ],
    filename_separator: '_',
    log_verbose: false,
    language:    'fr',
    email_configs: [],
    dirs: { input:'input', output:'output', error:'output/error',
            processed:'output/processed', no_code:'output/no_code' },
    counter: 11,
  },
  events: [], queue: [],
  stats: { processed: 4, split_docs: 11, errors: 0, last_file: null, last_time: null },
  config: { version: 'ci', dirs_status: {}, email_configs_status: [], data_dir: '/data' },
};

// ── Sandbox ─────────────────────────────────────────────────────────────────
let lastFetchUrl = null;
const sandbox = {
  console,
  document: {
    getElementById:  getEl,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement:    () => getEl('__tmp__'),
  },
  window: { I18N: i18n, LANG: 'fr' },
  fetch: (url) => {
    lastFetchUrl = url;
    if (url === '/api/state')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(STATE) });
    if (url.startsWith('/api/separator'))
      return Promise.resolve({ ok: true, blob: () => Promise.resolve({}) });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
  },
  URL:       { createObjectURL: () => 'blob://x' },
  setInterval: () => {},
  setTimeout:  (fn) => { try { fn(); } catch(_) {} },
  alert:       () => {},
  confirm:     () => true,
  navigator:   { language: 'fr' },
  location:    { reload: () => {} },
};
sandbox.window.document = sandbox.document;
sandbox.global = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox);

// ── Helpers ──────────────────────────────────────────────────────────────────
let passed = 0; let failed = 0;
function assert(label, condition, extra = '') {
  if (condition) {
    console.log(`  ✓ ${label}`);
    passed++;
  } else {
    console.error(`  ✗ ${label}${extra ? ' — ' + extra : ''}`);
    failed++;
  }
}

// ── Tests ─────────────────────────────────────────────────────────────────────
(async () => {
  try {

    // ── 1. loadConfig + rendu ────────────────────────────────────────────────
    console.log('\n[1] loadConfig + rendu');
    await sandbox.loadConfig();

    const trigList = getEl('trigger-list');
    assert('trigger-list peuple (3 declencheurs)',
      trigList.innerHTML.includes('NEWDOC') &&
      trigList.innerHTML.includes('FK3')    &&
      trigList.innerHTML.includes('DOC*'));

    assert('icone ✂ presente pour NEWDOC (page_handling=delete)',
      trigList.innerHTML.includes('✂'));

    assert('icone ~ presente pour DOC* (glob)',
      trigList.innerHTML.includes('~'));

    // Regression v1.15.1 : renderOptions() ne tournait pas apres le crash
    // de renderTriggers() — les toggles affichaient OFF malgre config ON
    assert('toggle subdirs_by_trigger reflete la config (checked=true)',
      getEl('opt-subdirs').checked === true);
    assert('toggle delete_source reflete la config (checked=true)',
      getEl('opt-delete').checked === true);

    // Regression v1.15.1 : renderTokens() ne tournait pas non plus
    const tokenList = getEl('token-list');
    assert('token-list peuple (token-item presents dans le DOM)',
      tokenList.innerHTML.includes('token-item') &&
      tokenList.innerHTML.includes('token-type'));

    // ── 2. Telechargement de la page intercalaire ────────────────────────────
    console.log('\n[2] Page intercalaire');

    // 2a. Declencheur normal (NEWDOC, idx 0)
    sandbox.openTriggerPanel(0);
    getEl('tp-code-type').value = 'qr';
    lastFetchUrl = null;
    await sandbox.downloadSeparator();
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));
    assert('downloadSeparator(NEWDOC) appelle /api/separator/0?type=qr',
      lastFetchUrl === '/api/separator/0?type=qr',
      `fetch = ${lastFetchUrl}`);

    // 2b. Pattern glob (DOC*, idx 2)
    sandbox.openTriggerPanel(2);
    const sepBtn = getEl('tp-separator-btn');
    assert('bouton intercalaire desactive pour glob', sepBtn.disabled === true);
    lastFetchUrl = null;
    sandbox.downloadSeparator();
    assert('downloadSeparator(glob) ne fetche pas', lastFetchUrl === null);

    // ── 3. Exclusivite mutuelle des panneaux ─────────────────────────────────
    console.log('\n[3] Exclusivite mutuelle Dossiers / Email');
    const dirsPanel  = getEl('dirs-panel');
    const emailPanel = getEl('email-section');
    const dirsBtn    = getEl('dirs-panel-btn');
    const emailBtn   = getEl('email-panel-btn');
    // Partir d'un etat connu
    dirsPanel.style.display  = 'none';
    emailPanel.style.display = 'none';

    sandbox.toggleDirsPanel();
    assert('ouvrir Dossiers -> panel visible', dirsPanel.style.display === 'block');
    assert('ouvrir Dossiers -> bouton actif',  dirsBtn.classList.contains('active'));

    sandbox.toggleEmailSection();
    assert('ouvrir Email -> panel Email visible',   emailPanel.style.display === 'block');
    assert('ouvrir Email -> bouton Email actif',    emailBtn.classList.contains('active'));
    assert('ouvrir Email ferme Dossiers',           dirsPanel.style.display  === 'none');
    assert('ouvrir Email eteint bouton Dossiers',   !dirsBtn.classList.contains('active'));

    sandbox.toggleDirsPanel();
    assert('ouvrir Dossiers -> panel Dossiers visible', dirsPanel.style.display  === 'block');
    assert('ouvrir Dossiers ferme Email',               emailPanel.style.display === 'none');

    sandbox.toggleDirsPanel();
    assert('re-cliquer Dossiers ferme le panneau',  dirsPanel.style.display === 'none');
    assert('re-cliquer Dossiers eteint le bouton',  !dirsBtn.classList.contains('active'));

    // ── 4. Actions sur les declencheurs ──────────────────────────────────────
    console.log('\n[4] Actions declencheurs');

    // applyTriggerEdit : renommer NEWDOC (idx 0) vers 'FK3' (doublon)
    sandbox.openTriggerPanel(0);
    getEl('tp-edit-input').value = 'FK3';
    sandbox.applyTriggerEdit();
    const dupMsg = getEl('tp-separator-msg').textContent;
    assert('applyTriggerEdit doublon -> message traduit non vide', dupMsg.length > 0);

    // removeTrigger : supprimer FK3 (idx 1) — on verifie via le rendu DOM
    sandbox.openTriggerPanel(0); // reset panel
    sandbox.removeTrigger(1);
    assert('removeTrigger(FK3) retire le tag du DOM',
      !getEl('trigger-list').innerHTML.includes('FK3'));

    // ── Bilan ─────────────────────────────────────────────────────────────────
    console.log(`\n${'─'.repeat(50)}`);
    console.log(`Resultats : ${passed} OK, ${failed} ECHEC(s)`);
    if (failed > 0) process.exit(1);
    console.log('OK — tous les tests fonctionnels passent.');

  } catch (e) {
    console.error('\nErreur inattendue :', e.message);
    console.error(e.stack);
    process.exit(1);
  }
})();
