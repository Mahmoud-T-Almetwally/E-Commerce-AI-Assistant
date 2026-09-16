/*
 * Dashboard "AI Assistant" analytics — client-rendered from /admin/stats/*.
 * Pure-CSS charts in the dashboard's existing visual language: grouped
 * mini-bars (tokens / activity), single bars (checkouts), trend rows (tools).
 */
(() => {
  'use strict';

  const cfgEl = document.getElementById('dashboard-stats-config');
  let CFG = null;
  try { CFG = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { CFG = null; }
  if (!CFG || !CFG.urls || !window.AdminUI) {
    console.error('admin-stats.js: missing config or AdminUI.');
    return;
  }
  const U = CFG.urls;
  const { el, fetchJSON, fmtNum, fmtMoney, fmtDateTime, safeUrl } = window.AdminUI;
  const $ = (id) => document.getElementById(id);

  const SERIES_LIMITS = { day: 30, week: 12, month: 12 };
  let period = 'day';
  const ledger = { page: 1, total_pages: 1 };

  /* ---------- helpers ---------- */

  function bucketLabel(iso) {
    const d = new Date(iso + 'T00:00:00Z');
    if (Number.isNaN(d.getTime())) return iso;
    const opts = period === 'month'
      ? { month: 'short', year: '2-digit' }
      : { day: 'numeric', month: 'short' };
    try { return d.toLocaleDateString([], opts); } catch (err) { return iso; }
  }

  function chartError(container, err, retry) {
    container.replaceChildren();
    const box = el('div', 'chart-error', `Couldn't load this chart: ${err.message || err}`);
    const button = el('button', 'btn btn-sm', 'Retry');
    button.type = 'button';
    button.addEventListener('click', retry);
    box.appendChild(button);
    container.appendChild(box);
  }

  function kpi(label, value, sub) {
    const tile = el('div', 'kpi');
    tile.append(el('div', 'kpi-label', label),
                el('div', 'kpi-value', value),
                el('div', 'kpi-sub', sub));
    return tile;
  }

  /* Renders grouped mini-bars; returns summed series (or null when empty). */
  function renderGroupedChart(container, buckets, seriesDefs, tooltip) {
    container.replaceChildren();
    const values = buckets.flatMap((b) => seriesDefs.map((s) => Number(b[s.key]) || 0));
    if (!values.some((v) => v > 0)) {
      container.appendChild(el('div', 'empty-chart', 'No data for this period yet.'));
      return null;
    }
    const max = Math.max(...values, 1);
    const skip = Math.max(1, Math.ceil(buckets.length / 12));
    buckets.forEach((b, i) => {
      const col = el('div', 'bar-col');
      col.title = tooltip(b);
      const pair = el('div', 'bar-pair');
      seriesDefs.forEach((s) => {
        const bar = el('div', 'bar mini ' + s.cls);
        bar.style.setProperty('--h', ((Number(b[s.key]) || 0) / max * 100) + '%');
        pair.appendChild(bar);
      });
      const show = (i % skip === 0 || i === buckets.length - 1);
      col.append(pair, el('span', 'bar-label', show ? bucketLabel(b.bucket_start) : ''));
      container.appendChild(col);
    });
    return buckets.reduce((acc, b) => {
      seriesDefs.forEach((s) => { acc[s.key] = (acc[s.key] || 0) + (Number(b[s.key]) || 0); });
      return acc;
    }, {});
  }

  /* ---------- overview KPIs ---------- */

  async function loadOverview() {
    const grid = $('ai-kpis');
    try {
      const data = await fetchJSON(U.overview);
      const t = data.totals, d1 = data.last_24h, d7 = data.last_7d;
      grid.replaceChildren(
        kpi('Conversations', fmtNum(t.conversations),
            `${fmtNum(d7.conversations)} started in the last 7 days`),
        kpi('Chat turns', fmtNum(t.turns),
            `${fmtNum(d1.turns)} in the last 24h · ${fmtNum(t.errored_turns)} errored`),
        kpi('Tokens', fmtNum(t.tokens_in + t.tokens_out),
            `${fmtNum(t.tokens_in)} in · ${fmtNum(t.tokens_out)} out`),
        kpi('Tool calls', fmtNum(t.tool_calls),
            `${fmtNum(d7.tool_calls)} in the last 7 days`),
        kpi('Chat checkouts', fmtNum(t.chat_checkouts),
            `${fmtMoney(t.chat_revenue)} via the assistant`),
      );
    } catch (err) {
      grid.replaceChildren();
      const tile = el('div', 'kpi chart-error',
                      `Couldn't load statistics: ${err.message || err}`);
      const button = el('button', 'btn btn-sm', 'Retry');
      button.type = 'button';
      button.addEventListener('click', loadOverview);
      tile.appendChild(button);
      grid.appendChild(tile);
    }
  }

  /* ---------- series charts ---------- */

  async function loadSeries() {
    const qs = `?period=${period}&limit=${SERIES_LIMITS[period]}`;
    const [tokens, activity, checkouts] = await Promise.allSettled([
      fetchJSON(U.tokens + qs),
      fetchJSON(U.activity + qs),
      fetchJSON(U.checkouts + qs),
    ]);

    if (tokens.status === 'fulfilled') {
      const buckets = tokens.value.buckets || [];
      const sums = renderGroupedChart(
        $('ai-tokens-chart'), buckets,
        [{ key: 'tokens_in', cls: 'in' }, { key: 'tokens_out', cls: 'out' }],
        (b) => `${bucketLabel(b.bucket_start)} — ${fmtNum(b.tokens_in)} in, ` +
               `${fmtNum(b.tokens_out)} out · ${fmtNum(b.turns)} turn(s)`);
      $('ai-tokens-sub').textContent = sums
        ? `${fmtNum(sums.tokens_in)} in · ${fmtNum(sums.tokens_out)} out`
        : 'No usage yet';
    } else {
      chartError($('ai-tokens-chart'), tokens.reason, loadSeries);
      $('ai-tokens-sub').textContent = '';
    }

    if (activity.status === 'fulfilled') {
      const buckets = activity.value.buckets || [];
      const sums = renderGroupedChart(
        $('ai-activity-chart'), buckets,
        [{ key: 'turns', cls: 'turns thin' }, { key: 'tool_calls', cls: 'tools thin' },
         { key: 'chat_checkouts', cls: 'checkouts thin' }],
        (b) => `${bucketLabel(b.bucket_start)} — ${fmtNum(b.turns)} turns · ` +
               `${fmtNum(b.tool_calls)} tool calls · ${fmtNum(b.chat_checkouts)} checkout(s) · ` +
               `${fmtNum(b.guard_blocks)} blocked`);
      $('ai-activity-sub').textContent = sums
        ? `${fmtNum(sums.turns)} turns · ${fmtNum(sums.tool_calls)} tool calls`
        : 'No activity yet';
    } else {
      chartError($('ai-activity-chart'), activity.reason, loadSeries);
      $('ai-activity-sub').textContent = '';
    }

    if (checkouts.status === 'fulfilled') {
      const buckets = checkouts.value.buckets || [];
      const sums = renderGroupedChart(
        $('ai-checkouts-chart'), buckets,
        [{ key: 'chat_checkouts', cls: 'checkouts' }],
        (b) => `${bucketLabel(b.bucket_start)} — ${fmtNum(b.chat_checkouts)} checkout(s) · ` +
               `${fmtMoney(b.revenue)}`);
      const count = buckets.reduce((a, b) => a + (Number(b.chat_checkouts) || 0), 0);
      const revenue = buckets.reduce((a, b) => a + (Number(b.revenue) || 0), 0);
      $('ai-checkouts-sub').textContent = sums
        ? `${fmtNum(count)} order(s) · ${fmtMoney(revenue)} revenue`
        : 'No chat checkouts yet';
    } else {
      chartError($('ai-checkouts-chart'), checkouts.reason, loadSeries);
      $('ai-checkouts-sub').textContent = '';
    }
  }

  /* ---------- tool popularity ---------- */

  async function loadTools() {
    const body = $('ai-tools-body');
    try {
      const data = await fetchJSON(U.tools + '?limit=10');
      $('ai-tools-sub').textContent = data.total_calls
        ? `${fmtNum(data.total_calls)} calls` +
          (data.window_days ? ` · last ${data.window_days} days` : ' · all time')
        : '';
      body.replaceChildren();
      if (!data.tools.length) {
        body.appendChild(el('div', 'empty-chart', 'No tool usage recorded yet.'));
        return;
      }
      data.tools.forEach((t) => {
        const item = el('div', 'trend-item');
        const top = el('div', 'trend-top');
        top.append(el('span', null, t.tool_name),
                   el('strong', null, `${fmtNum(t.calls)} calls · ${t.share_pct}%`));
        const bar = el('div', 'trend-bar');
        const fill = el('span');
        fill.style.width = `${Math.min(t.share_pct || 0, 100)}%`;
        bar.appendChild(fill);
        const meta = el('div', 'tool-meta small muted',
          `✓ ${t.done} · errors ${t.errors} · declined ${t.declined} · blocked ${t.blocked}` +
          (t.avg_duration_ms != null ? ` · avg ${fmtNum(t.avg_duration_ms)} ms` : ''));
        item.append(top, bar, meta);
        body.appendChild(item);
      });
    } catch (err) {
      chartError(body, err, loadTools);
    }
  }

  /* ---------- checkout ledger ---------- */

  async function toggleLedger() {
    const wrap = $('ai-ledger');
    const opening = wrap.hidden;
    wrap.hidden = !opening;
    $('ai-ledger-toggle').textContent = opening ? 'Hide ledger' : 'View full ledger';
    if (opening) await loadLedger(ledger.page || 1);
  }

  async function loadLedger(page) {
    const rows = $('ai-ledger-rows');
    const loadingRow = el('tr');
    const loadingCell = el('td', 'muted small', 'Loading…');
    loadingCell.colSpan = 6;
    loadingRow.appendChild(loadingCell);
    rows.replaceChildren(loadingRow);
    try {
      const data = await fetchJSON(`${U.checkoutsList}?page=${page}&per_page=10`);
      ledger.page = data.page;
      ledger.total_pages = data.total_pages;
      const frag = document.createDocumentFragment();
      if (!data.checkouts.length) {
        const tr = el('tr');
        const td = el('td', 'muted small', 'No chat checkouts yet.');
        td.colSpan = 6;
        tr.appendChild(td);
        frag.appendChild(tr);
      }
      data.checkouts.forEach((c) => {
        const tr = el('tr');
        const badge = el('span', `badge badge-${c.order_status}`, c.order_status);
        const statusCell = el('td');
        statusCell.appendChild(badge);
        const convCell = el('td');
        const link = el('a', null, `#${c.conversation_id}`);
        const href = safeUrl(`${U.conversationsPage}?conversation=${c.conversation_id}`);
        if (href) link.href = href;
        convCell.appendChild(link);
        tr.append(
          el('td', 'nowrap small', fmtDateTime(c.created_at)),
          el('td', null, c.user ? c.user.name : '—'),
          el('td', null, `#${c.order_id}`),
          statusCell,
          el('td', 'num', fmtMoney(c.order_total)),
          convCell,
        );
        frag.appendChild(tr);
      });
      rows.replaceChildren(frag);
      $('ai-ledger-info').textContent =
        `${fmtNum(data.total)} checkout(s) · page ${data.page} of ${Math.max(data.total_pages, 1)}`;
      $('ai-ledger-prev').disabled = data.page <= 1;
      $('ai-ledger-next').disabled = data.page >= data.total_pages;
    } catch (err) {
      const tr = el('tr');
      const td = el('td', 'chart-error', `Couldn't load the ledger: ${err.message || err}`);
      td.colSpan = 6;
      tr.appendChild(td);
      rows.replaceChildren(tr);
    }
  }

  /* ---------- wiring ---------- */

  $('ai-period-seg').addEventListener('click', (event) => {
    const btn = event.target.closest('.seg-btn');
    if (!btn || btn.dataset.period === period) return;
    period = btn.dataset.period;
    document.querySelectorAll('#ai-period-seg .seg-btn')
      .forEach((b) => b.classList.toggle('active', b === btn));
    loadSeries();
  });
  $('ai-ledger-toggle').addEventListener('click', toggleLedger);
  $('ai-ledger-prev').addEventListener('click', () => { if (ledger.page > 1) loadLedger(ledger.page - 1); });
  $('ai-ledger-next').addEventListener('click', () => { if (ledger.page < ledger.total_pages) loadLedger(ledger.page + 1); });

  loadOverview();
  loadTools();
  loadSeries();
})();