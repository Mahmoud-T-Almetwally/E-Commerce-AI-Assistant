/*
 * Admin playground config panel — the backend for chat/admin.html's right
 * column. Consumes /admin/llm-config: GET (load), PUT (save diff),
 * POST /test (dry-run, never saves), POST /reset (defaults).
 * The chat panel itself is handled by chat.js via the shared #chat-config.
 */
(() => {
  'use strict';

  const cfgEl = document.getElementById('playground-config');
  let CFG = null;
  try { CFG = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { CFG = null; }
  if (!CFG || !CFG.urls || !window.AdminUI) {
    console.error('admin-playground.js: missing config or AdminUI.');
    return;
  }
  const U = CFG.urls;
  const CSRF = CFG.csrfToken || '';
  const { el, fetchJSON, toast } = window.AdminUI;
  const $ = (id) => document.getElementById(id);

  let baseline = null;      // form values as last loaded/saved
  let providers = {};

  const LLM_FIELDS = [
    { key: 'provider', type: 'select', label: 'Provider' },
    { key: 'model_name', type: 'text', label: 'Model name',
      placeholder: 'e.g. llama-3.3-70b-versatile' },
    { key: 'temperature', type: 'number', label: 'Temperature', step: 0.1, min: 0, max: 2 },
    { key: 'max_tokens', type: 'number', label: 'Max tokens', min: 1 },
    { key: 'top_p', type: 'number', label: 'Top P (optional)', step: 0.01, min: 0, max: 1, optional: true },
    { key: 'seed', type: 'number', label: 'Seed (optional)', optional: true },
    { key: 'max_retries', type: 'number', label: 'Provider retries', min: 0, max: 10 },
    { key: 'timeout_seconds', type: 'number', label: 'Timeout (seconds)', min: 1 },
    { key: 'vision_capable', type: 'checkbox', label: 'Vision capable — enables chat image uploads' },
  ];
  const AGENT_FIELDS = [
    { key: 'guard_enabled', type: 'checkbox', label: 'Prompt-injection guard' },
    { key: 'guard_include_context', type: 'checkbox', label: 'Guard reviews conversation context' },
    { key: 'max_tool_retries', type: 'number', label: 'Default tool retries', min: 0, max: 5 },
    { key: 'tool_retry_backoff_seconds', type: 'number', label: 'Retry backoff (seconds)', step: 0.1, min: 0 },
    { key: 'status_events_enabled', type: 'checkbox', label: 'Status events in chat UI' },
    { key: 'sensitive_tool_names', type: 'list', label: 'Tools requiring confirmation',
      placeholder: 'add_to_cart, checkout' },
    { key: 'confirmation_timeout_seconds', type: 'number', label: 'Confirmation timeout (s)', min: 1 },
    { key: 'max_upload_mb', type: 'number', label: 'Max upload (MB)', min: 1 },
    { key: 'max_message_chars', type: 'number', label: 'Max message length', min: 1 },
    { key: 'allowed_upload_extensions', type: 'list', label: 'Allowed upload extensions',
      placeholder: '.png, .jpg, .jpeg' },
  ];

  /* ---------- form building ---------- */

  function fieldRow(group, f, value) {
    const id = `cfg-${group}-${f.key}`;
    const field = el('div', 'field');
    if (f.type === 'checkbox') {
      const check = el('div', 'config-check');
      const input = el('input');
      input.type = 'checkbox';
      input.id = id;
      input.checked = !!value;
      const label = el('label', null, f.label);
      label.htmlFor = id;
      check.append(input, label);
      field.appendChild(check);
      return field;
    }
    const label = el('label', null, f.label);
    label.htmlFor = id;
    field.appendChild(label);

    let input;
    if (f.type === 'select') {
      input = el('select');
      Object.keys(providers).forEach((name) => {
        const status = providers[name] || {};
        const option = el('option', null,
          name + (status.installed ? '' : ' — package missing'));
        option.value = name;
        if (value === name) option.selected = true;
        input.appendChild(option);
      });
    } else if (f.type === 'list') {
      input = el('input');
      input.type = 'text';
      input.placeholder = f.placeholder || '';
      input.value = Array.isArray(value) ? value.join(', ') : (value || '');
    } else {
      input = el('input');
      input.type = 'number';
      if (f.step != null) input.step = f.step;
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      input.placeholder = f.optional ? 'optional' : '';
      input.value = (value === null || value === undefined) ? '' : String(value);
    }
    input.id = id;
    field.appendChild(input);
    return field;
  }

  function buildForm(payload) {
    providers = payload.providers || {};
    const body = $('config-body');
    body.replaceChildren();

    const llmGroup = el('div');
    llmGroup.appendChild(el('h3', null, 'LLM'));
    LLM_FIELDS.forEach((f) => llmGroup.appendChild(fieldRow('llm', f, payload.llm_config[f.key])));
    const providerNote = el('div', 'provider-note');
    providerNote.textContent = Object.entries(providers)
      .map(([name, s]) => `${name}${s.installed ? ' ✓' : ' (missing)'}${s.key_present ? '' : ' · no key'}`)
      .join(' · ');
    llmGroup.appendChild(providerNote);
    body.appendChild(llmGroup);

    const agentGroup = el('div');
    agentGroup.appendChild(el('h3', null, 'Agent runtime'));
    AGENT_FIELDS.forEach((f) => agentGroup.appendChild(fieldRow('agent', f, payload.agent[f.key])));
    body.appendChild(agentGroup);

    const emb = (payload.read_only && payload.read_only.embedding_config) || {};
    const sc = (payload.read_only && payload.read_only.system_context) || {};
    const ro = el('div', 'readonly-note');
    ro.append(
      el('div', null, `Embeddings (read-only): ${emb.provider || '—'} · ${emb.model_name || '—'} · ` +
                      `key ${emb.api_key_set ? 'set ✓' : 'missing ✗'}`),
      el('div', null, `Persona (read-only): ${sc.company_name || '—'} — ${sc.tone || '—'}`),
    );
    body.appendChild(ro);

    baseline = collect();
    updateDirty();
  }

  /* ---------- value collection / diffing ---------- */

  function readField(group, f) {
    const input = document.getElementById(`cfg-${group}-${f.key}`);
    if (!input) return null;
    switch (f.type) {
      case 'checkbox': return input.checked;
      case 'list': return input.value.split(',').map((s) => s.trim()).filter(Boolean);
      case 'number': {
        const raw = input.value.trim();
        if (raw === '') return null;
        const n = Number(raw);
        return Number.isFinite(n) ? n : raw;   // invalid → string → server rejects clearly
      }
      default: return input.value;
    }
  }

  function collect() {
    const out = { llm_config: {}, agent: {} };
    LLM_FIELDS.forEach((f) => { out.llm_config[f.key] = readField('llm', f); });
    AGENT_FIELDS.forEach((f) => { out.agent[f.key] = readField('agent', f); });
    return out;
  }

  function diff() {
    const current = collect();
    const changes = {};
    for (const section of ['llm_config', 'agent']) {
      for (const [key, value] of Object.entries(current[section])) {
        if (JSON.stringify(value) !== JSON.stringify(baseline[section][key])) {
          (changes[section] = changes[section] || {})[key] = value;
        }
      }
    }
    return changes;
  }

  function updateDirty() {
    const dirty = Object.keys(diff()).length > 0;
    $('cfg-dirty').hidden = !dirty;
    $('cfg-save').disabled = !dirty;
  }

  function setStatus(text, kind) {
    const node = $('config-status');
    node.textContent = text || '';
    node.className = 'config-status' + (kind ? ' ' + kind : '');
  }

  /* ---------- actions ---------- */

  async function load() {
    try {
      buildForm(await fetchJSON(U.config));
      setStatus('');
    } catch (err) {
      $('config-body').replaceChildren(
        el('div', 'chart-error', `Couldn't load the configuration: ${err.message || err}`));
    }
  }

  async function testChanges() {
    setStatus('Testing…');
    try {
      const data = await fetchJSON(U.test, { method: 'POST', body: diff(), csrf: CSRF });
      if (data.ok) {
        setStatus(`✓ ${data.provider} · ${data.model_name} — ` +
                  (data.note || 'client constructed successfully.'), 'ok');
      } else {
        setStatus(`✕ ${data.error || 'Construction failed.'}`, 'err');
      }
    } catch (err) {
      setStatus(`✕ ${err.message || 'Test failed.'}`, 'err');
    }
  }

  async function saveChanges() {
    const changes = diff();
    if (!Object.keys(changes).length) { toast('No changes to save.', 'info'); return; }
    setStatus('Saving…');
    try {
      const payload = await fetchJSON(U.config, { method: 'PUT', body: changes, csrf: CSRF });
      buildForm(payload);   // re-render from server truth; resets the baseline
      setStatus('✓ Saved — new turns use the updated settings.', 'ok');
      toast('Configuration saved.', 'success');
    } catch (err) {
      setStatus(`✕ ${err.message || 'Save failed.'}`, 'err');
    }
  }

  async function resetDefaults() {
    if (!window.confirm('Reset the LLM and agent settings to their defaults?')) return;
    setStatus('Resetting…');
    try {
      const payload = await fetchJSON(U.reset, { method: 'POST', body: {}, csrf: CSRF });
      buildForm(payload);
      setStatus('✓ Restored defaults.', 'ok');
      toast('Configuration reset to defaults.', 'success');
    } catch (err) {
      setStatus(`✕ ${err.message || 'Reset failed.'}`, 'err');
    }
  }

  /* ---------- wiring ---------- */

  $('config-body').addEventListener('input', updateDirty);
  $('config-body').addEventListener('change', updateDirty);
  $('cfg-test').addEventListener('click', testChanges);
  $('cfg-save').addEventListener('click', saveChanges);
  $('cfg-reset').addEventListener('click', resetDefaults);

  load();
})();