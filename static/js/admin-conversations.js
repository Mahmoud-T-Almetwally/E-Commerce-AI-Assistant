/*
 * Admin conversations management: filter/paginate the list, open the detail
 * drawer (transcript + execution stats), delete conversations.
 * All data flows through /admin/conversations* — the drawer transcript uses
 * the same replay format as the customer history endpoint.
 */
(() => {
  'use strict';

  const cfgEl = document.getElementById('conversations-config');
  let CFG = null;
  try { CFG = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { CFG = null; }
  if (!CFG || !CFG.urls || !window.AdminUI) {
    console.error('admin-conversations.js: missing config or AdminUI.');
    return;
  }
  const U = CFG.urls;
  const CSRF = CFG.csrfToken || '';
  const { el, fetchJSON, toast, fmtNum, fmtDateTime } = window.AdminUI;
  const $ = (id) => document.getElementById(id);

  const DETAIL_BASE = String(U.detailBase || '/admin/conversations/0').slice(0, -1);
  const PRODUCT_BASE = String(U.productBase || '/products/0').slice(0, -1);
  const detailUrl = (id) => DETAIL_BASE + id;

  let listState = { q: '', page: 1, total: 0, total_pages: 1 };
  let drawerId = null;

  /* ---------- list ---------- */

  function placeholderRow(text, retry) {
    const tr = el('tr');
    const td = el('td', 'muted small', text);
    td.colSpan = 9;
    if (retry) {
      td.replaceChildren(el('span', null, `${text} `));
      const button = el('button', 'btn btn-sm', 'Retry');
      button.type = 'button';
      button.addEventListener('click', retry);
      td.appendChild(button);
    }
    tr.appendChild(td);
    return tr;
  }

  async function loadList() {
    const rows = $('conv-rows');
    rows.replaceChildren(placeholderRow('Loading…'));
    try {
      const url = `${U.list}?page=${listState.page}&per_page=20&q=${encodeURIComponent(listState.q)}`;
      const data = await fetchJSON(url);
      listState = { q: listState.q, page: data.page, total: data.total, total_pages: data.total_pages };
      renderRows(data.conversations || []);
      $('conv-count').textContent = data.total ? `${fmtNum(data.total)} total` : '';
      $('conv-page-info').textContent = data.total
        ? `${fmtNum(data.total)} conversation(s) · page ${data.page} of ${Math.max(data.total_pages, 1)}` : '';
      $('conv-prev').disabled = data.page <= 1;
      $('conv-next').disabled = data.page >= data.total_pages;
      const params = new URLSearchParams();
      if (listState.q) params.set('q', listState.q);
      if (data.page > 1) params.set('page', String(data.page));
      history.replaceState(null, '', location.pathname + (params.toString() ? '?' + params : ''));
    } catch (err) {
      rows.replaceChildren(placeholderRow(`Couldn't load conversations: ${err.message || err}`, loadList));
    }
  }

  function renderRows(conversations) {
    const rows = $('conv-rows');
    const frag = document.createDocumentFragment();
    if (!conversations.length) {
      frag.appendChild(placeholderRow('No conversations found.'));
    }
    conversations.forEach((c) => {
      const tr = el('tr');
      const idCell = el('td', 'conv-idcell');
      idCell.append(el('strong', null, `#${c.id}`),
                    el('small', 'muted', c.created_at ? `created ${fmtDateTime(c.created_at)}` : ''));
      const userCell = el('td', 'conv-customer');
      if (c.user) {
        userCell.append(el('span', 'avatar', (c.user.name || '?').slice(0, 1).toUpperCase()),
                        el('span', null, c.user.name));
        if (c.user.email) userCell.title = c.user.email;
      } else {
        userCell.appendChild(el('span', 'muted', 'deleted user'));
      }
      const intentCell = el('td');
      if (c.last_intent) intentCell.appendChild(el('span', 'tag', c.last_intent));
      else intentCell.appendChild(el('span', 'muted small', '—'));
      const actions = el('td', 'conv-actions');
      const viewBtn = el('button', 'btn btn-sm', 'View');
      viewBtn.type = 'button';
      viewBtn.addEventListener('click', () => openDrawer(c.id));
      const delBtn = el('button', 'btn btn-sm btn-danger', 'Delete');
      delBtn.type = 'button';
      delBtn.addEventListener('click', () => deleteConversation(c.id));
      actions.append(viewBtn, delBtn);
      tr.append(
        idCell, userCell,
        el('td', 'num', fmtNum(c.turns)),
        el('td', 'num small', `${fmtNum(c.tokens_in)} / ${fmtNum(c.tokens_out)}`),
        el('td', 'num', fmtNum(c.tool_calls)),
        el('td', 'num', c.chat_checkouts ? fmtNum(c.chat_checkouts) : '—'),
        intentCell,
        el('td', 'nowrap small', fmtDateTime(c.updated_at)),
        actions,
      );
      frag.appendChild(tr);
    });
    rows.replaceChildren(frag);
  }

  /* ---------- drawer ---------- */

  function showTab(name) {
    document.querySelectorAll('.drawer-tab')
      .forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    $('drawer-transcript').hidden = name !== 'transcript';
    $('drawer-execution').hidden = name !== 'execution';
  }

  function closeDrawer() {
    drawerId = null;
    $('conv-drawer').hidden = true;
  }

  function errorPane(err, retry) {
    const box = el('div', 'chart-error', `Couldn't load: ${err.message || err}`);
    const button = el('button', 'btn btn-sm', 'Retry');
    button.type = 'button';
    button.addEventListener('click', retry);
    box.appendChild(button);
    return box;
  }

  async function openDrawer(id) {
    drawerId = id;
    $('conv-drawer').hidden = false;
    $('drawer-title').textContent = `Conversation #${id}`;
    $('drawer-sub').textContent = 'Loading…';
    $('drawer-transcript').replaceChildren(el('div', 'skeleton-row'));
    $('drawer-execution').replaceChildren(el('div', 'skeleton-row'));
    showTab('transcript');

    const [detail, transcript] = await Promise.allSettled([
      fetchJSON(detailUrl(id)),
      fetchJSON(detailUrl(id) + '/transcript'),
    ]);
    if (drawerId !== id) return;   // a different drawer was opened meanwhile

    if (transcript.status === 'fulfilled') {
      renderTranscript(transcript.value);
      const conv = transcript.value.conversation || {};
      const who = conv.user ? `${conv.user.name} (${conv.user.email})` : 'deleted user';
      $('drawer-sub').textContent = `${who} · updated ${fmtDateTime(conv.updated_at)}`;
    } else {
      $('drawer-transcript').replaceChildren(errorPane(transcript.reason, () => openDrawer(id)));
      $('drawer-sub').textContent = '';
    }
    if (detail.status === 'fulfilled') {
      renderExecution(detail.value);
    } else {
      $('drawer-execution').replaceChildren(errorPane(detail.reason, () => openDrawer(id)));
    }
  }

  function renderTranscript(data) {
    const pane = $('drawer-transcript');
    pane.replaceChildren();
    const messages = data.messages || [];
    if (!messages.length && !data.pending_confirmation) {
      pane.appendChild(el('div', 'empty-chart', 'No messages in this conversation.'));
      return;
    }
    const log = el('div', 'mini-log');
    messages.forEach((m) => {
      if (m.role === 'user') {
        log.appendChild(el('div', 'msg msg-user', m.content || ''));
      } else if (m.role === 'assistant') {
        log.appendChild(el('div', m.interim ? 'msg msg-note' : 'msg msg-bot', m.content || ''));
      } else if (m.role === 'carousel') {
        const line = el('div', 'msg msg-bot');
        line.appendChild(el('div', null, 'Products:'));
        const listEl = el('div');
        (m.product_ids || []).forEach((pid, i) => {
          if (i) listEl.appendChild(document.createTextNode(', '));
          const link = el('a', null, `#${pid}`);
          link.href = PRODUCT_BASE + pid;
          link.target = '_blank';
          link.rel = 'noopener';
          listEl.appendChild(link);
        });
        line.appendChild(listEl);
        log.appendChild(line);
      }
    });
    pane.appendChild(log);
    if (data.pending_confirmation) {
      const p = data.pending_confirmation;
      pane.appendChild(el('div', 'confirm-notice',
        `Awaiting user confirmation — ${p.message || p.tool || 'action'}`));
    }
  }

  function turnBadge(status) {
    return status === 'completed' ? 'badge-success'
      : status === 'error' ? 'badge-cancelled' : 'badge-neutral';
  }

  function stateGlyph(state) {
    return state === 'done' ? '✓' : state === 'error' ? '✕'
      : state === 'declined' ? '⊘' : '■';
  }

  function renderExecution(detail) {
    const pane = $('drawer-execution');
    pane.replaceChildren();
    const totals = detail.totals || {};
    const chips = el('div', 'exec-totals');
    [
      ['turns', totals.turns], ['tokens in', fmtNum(totals.tokens_in)],
      ['tokens out', fmtNum(totals.tokens_out)], ['tool calls', totals.tool_calls],
      ['chat orders', totals.chat_checkouts],
    ].forEach(([label, value]) => {
      chips.appendChild(el('span', 'tag', `${label}: ${value ?? 0}`));
    });
    pane.appendChild(chips);

    const turns = detail.turns || [];
    if (!turns.length) {
      pane.appendChild(el('div', 'empty-chart', 'No recorded turns.'));
      return;
    }
    turns.forEach((t) => {
      const card = el('div', 'exec-turn');
      const head = el('div', 'exec-turn-head');
      head.append(
        el('span', `badge ${turnBadge(t.status)}`, t.status),
        el('span', 'tag', t.intent || 'unknown intent'),
        el('span', 'muted small', fmtDateTime(t.started_at)),
        el('span', 'right muted small',
           `${fmtNum(t.tokens_in)}→${fmtNum(t.tokens_out)} tok · ${t.llm_calls} LLM call(s)`),
      );
      if (t.guard_blocked) head.appendChild(el('span', 'badge badge-cancelled', 'guard block'));
      card.appendChild(head);
      if (t.error) card.appendChild(el('div', 'small', `Error: ${t.error}`));
      if ((t.tool_calls || []).length) {
        const chipsRow = el('div', 'tool-chips');
        t.tool_calls.forEach((c) => {
          const chip = el('span', `tool-chip ${c.state}`);
          chip.append(el('span', 'tc-state', stateGlyph(c.state)),
                      el('span', null, c.tool_name));
          if (c.duration_ms != null) chip.appendChild(el('span', 'muted', `${c.duration_ms}ms`));
          if (c.order_id != null) chip.appendChild(el('span', 'muted', `→ #${c.order_id}`));
          chipsRow.appendChild(chip);
        });
        card.appendChild(chipsRow);
      }
      pane.appendChild(card);
    });
  }

  /* ---------- delete ---------- */

  async function deleteConversation(id) {
    if (!window.confirm(
        `Delete conversation #${id}? Its transcript and statistics will be removed. ` +
        'Store orders are NOT affected.')) return;
    try {
      const res = await fetchJSON(detailUrl(id), { method: 'DELETE', csrf: CSRF });
      toast(`Deleted conversation #${id} — ${res.turns_removed} turn(s), ` +
            `${res.tool_calls_removed} tool call(s) removed.`, 'success');
      if (drawerId === id) closeDrawer();
      loadList();
    } catch (err) {
      toast(err.message || 'Delete failed.', 'danger');
    }
  }

  /* ---------- wiring ---------- */

  $('conv-search-btn').addEventListener('click', () => {
    listState.q = $('conv-search').value.trim();
    listState.page = 1;
    loadList();
  });
  $('conv-search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      $('conv-search-btn').click();
    }
  });
  $('conv-prev').addEventListener('click', () => {
    if (listState.page > 1) { listState.page -= 1; loadList(); }
  });
  $('conv-next').addEventListener('click', () => {
    if (listState.page < listState.total_pages) { listState.page += 1; loadList(); }
  });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('conv-backdrop').addEventListener('click', closeDrawer);
  $('drawer-delete').addEventListener('click', () => {
    if (drawerId != null) deleteConversation(drawerId);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('conv-drawer').hidden) closeDrawer();
  });
  document.querySelector('.drawer-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.drawer-tab');
    if (tab) showTab(tab.dataset.tab);
  });

  /* initial state from URL (?q=, ?page=, ?conversation=) */
  const params = new URLSearchParams(location.search);
  listState.q = (params.get('q') || '').trim();
  if (listState.q) $('conv-search').value = listState.q;
  const pageParam = Number(params.get('page'));
  if (Number.isInteger(pageParam) && pageParam > 0) listState.page = pageParam;
  loadList();
  const initialId = Number(params.get('conversation'));
  if (Number.isInteger(initialId) && initialId > 0) openDrawer(initialId);
})();