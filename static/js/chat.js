/* Minimal chat client for the Socket.IO + SSE agent API.
 * Assumes the page is served by the Flask app (shares the session cookie). */
(function () {
  const socket = io("/chat");
  let threadId = localStorage.getItem("chat_thread_id") || null;
  let eventSource = null;

  const statusEl = document.getElementById("chat-status");
  const logEl = document.getElementById("chat-log");
  const confirmEl = document.getElementById("chat-confirmation");

  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  function appendBubble(role, text) {
    if (!logEl) return;
    const div = document.createElement("div");
    div.className = `bubble ${role}`;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function connectEvents() {
    if (!threadId) return;
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/chat/conversations/${threadId}/events`);
    eventSource.addEventListener("status", (e) => {
      const d = JSON.parse(e.data);
      setStatus(d.stage === "tool_executing" ? `executing '${d.tool}'... (attempt ${d.attempt}/${d.max_attempts})`
        : d.stage === "tool_retrying" ? `retrying '${d.tool}'...`
        : d.stage === "awaiting_confirmation" ? "awaiting your confirmation..."
        : d.stage === "intent_classified" ? `intent: ${d.intent}`
        : d.stage);
    });
    eventSource.addEventListener("confirmation_required", (e) => {
      const d = JSON.parse(e.data);
      renderConfirmation(d.tool_calls);
    });
  }

  function renderConfirmation(toolCalls) {
    if (!confirmEl) return;
    confirmEl.innerHTML = "";
    const summary = document.createElement("p");
    summary.textContent = "Confirm action(s): " +
      toolCalls.map(tc => `${tc.name}(${JSON.stringify(tc.args)})`).join(", ");
    confirmEl.appendChild(summary);

    const yes = document.createElement("button");
    yes.textContent = "Confirm";
    const no = document.createElement("button");
    no.textContent = "Decline";

    const decide = (approved) => () => {
      confirmEl.innerHTML = "";
      fetch(`/chat/conversations/${threadId}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
    };
    yes.onclick = decide(true);
    no.onclick = decide(false);
    confirmEl.append(yes, no);
  }

  socket.on("connect", () => {
    setStatus("connected");
    if (threadId) { socket.emit("join", { thread_id: threadId }); connectEvents(); }
  });

  socket.on("assistant_message", (m) => {
    appendBubble("assistant", m.content);
    setStatus("done");
  });

  socket.on("error", (e) => {
    appendBubble("assistant", `⚠ ${e.error || "error"}`);
    setStatus("error");
  });

  window.sendChatMessage = function (text, attachments) {
    if (!threadId) {
      threadId = crypto.randomUUID();
      localStorage.setItem("chat_thread_id", threadId);
      socket.emit("join", { thread_id: threadId });
      connectEvents();
    }
    appendBubble("user", text || "[attachment]");
    socket.emit("send_message", { thread_id: threadId, content: text, attachments: attachments || [] });
    setStatus("thinking...");
  };
})();
/*
 * OmniCart customer chat client — one Socket.IO connection for the assistant.
 *
 * Event contract (see routes/chat.py):
 *   client → server   user_message {conversation_id?, content, attachment_id?}
 *                     confirmation_response {request_id, accepted}
 *   server → client   connected, conversation_started, agent_status, agent_note,
 *                     display_product_carousel, get_user_confirmation,
 *                     confirmation_resolved, chat_message, chat_error
 *
 * Safety: every dynamic string is inserted via textContent and every URL is
 * validated (safeUrl) before use — no innerHTML with untrusted content.
 *
 * Locking rules (server enforces one in-flight turn per user):
 *   - the composer is locked from emit until chat_message / a terminal
 *     chat_error; it stays locked across conversation switches;
 *   - chat_error codes are classified: TURN_FAILURE → inline error + unlock,
 *     PRE_TURN (validation failures emitted before any turn started) →
 *     toast + unlock, busy → toast + re-lock (a turn IS running elsewhere).
 */
(() => {
  'use strict';

  /* ---------- bootstrap: config + socket.io client must exist ---------- */
  const $ = (sel, root = document) => root.querySelector(sel);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  const cfgEl = document.getElementById('chat-config');
  let CFG = null;
  try { CFG = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { CFG = null; }

  if (!CFG || typeof io === 'undefined') {
    console.error('chat.js: missing #chat-config or the vendored socket.io client.');
    const log = $('#chat-log');
    if (log) {
      log.appendChild(el('div', 'msg msg-error',
        'The chat client failed to load. Please refresh the page.'));
    }
    return;
  }

  /* ---------- constants ---------- */
  const TOOL_LABELS = {
    search_products: 'product search',
    get_product_details: 'product lookup',
    search_knowledge_base: 'knowledge base search',
    view_cart: 'cart review',
    add_to_cart: 'add to cart',
    update_cart_quantity: 'cart update',
    remove_from_cart: 'cart removal',
    checkout: 'checkout',
    list_recent_orders: 'order lookup',
    get_order_status: 'order status check',
  };
  // Turn started and failed mid-flight → inline error bubble + unlock.
  const TURN_FAILURE = new Set(['internal_error', 'loop_limit', 'empty_response']);
  // Emitted by the server BEFORE any turn starts → toast + unlock (never wedges the composer).
  const PRE_TURN = new Set(['invalid_payload', 'empty_message', 'message_too_long',
    'uploads_disabled', 'attachment_not_found', 'conversation_not_found']);

  const HISTORY_BASE = String((CFG.urls && CFG.urls.historyBase) || '/chat/history/0').slice(0, -1);
  const PING_URL = (CFG.urls && CFG.urls.ping) || '/chat/ping';
  const UPLOAD_URL = (CFG.urls && CFG.urls.upload) || '/chat/upload';
  const LOOKUP_URL = (CFG.urls && CFG.urls.productLookup) || '/lookup/products';
  const LOGIN_URL = (CFG.urls && CFG.urls.login) || '/login';
  const IMG_FALLBACK = (CFG.urls && CFG.urls.productImageFallback)
    || '/static/images/default-product.png';

  /* ---------- state ---------- */
  const state = {
    socket: null,
    conn: 'connecting',        // connecting | online | offline
    mode: 'idle',              // idle | agent_turn
    flags: (CFG.flags && typeof CFG.flags === 'object') ? CFG.flags : {},
    activeConversationId: (typeof CFG.initialConversationId === 'number')
      ? CFG.initialConversationId : null,
    autoScroll: true,
    attachment: null,          // {id, filename} once the upload succeeded
    attachmentChip: null,      // {chip, objectUrl}
    pendingConfirmation: null, // live confirmation-card record
    bufferedSend: null,        // message awaiting socket reconnection
    needsResync: false,
    everConnected: false,
    redirecting: false,
    pingFailures: 0,
    lastAuthCheck: 0,
  };

  const els = {
    log: $('#chat-log'),
    statusLine: $('#status-line'),
    statusText: $('#status-text'),
    form: $('#chat-form'),
    input: $('#chat-input'),
    send: $('#send-btn'),
    attachBtn: $('#attach-btn'),
    fileInput: $('#file-input'),
    tray: $('#attachment-tray'),
    convList: $('#conv-list'),
    newChatBtn: $('#new-chat-btn'),
    dot: $('#conn-dot'),
    connLabel: $('#conn-label'),
    toasts: $('#toast-stack'),
  };
  const REQUIRED_ELEMENTS = ['log', 'statusLine', 'statusText', 'form', 'input',
    'send', 'attachBtn', 'fileInput', 'tray', 'dot', 'connLabel', 'toasts'];
  for (const name of REQUIRED_ELEMENTS) {
    if (!els[name]) { console.error(`chat.js: missing element for "${name}".`); return; }
  }

  /* ---------- small utilities ---------- */
  function safeUrl(url) {
    if (typeof url !== 'string') return null;
    const trimmed = url.trim();
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    if (trimmed === '/' || /^\/[^/]/.test(trimmed)) return trimmed; // site-relative only
    return null;
  }

  function fmtMoney(value) {
    const n = Number(value);
    return Number.isFinite(n) ? '$' + n.toFixed(2) : '';
  }

  function fmtTime(date = new Date()) {
    try { return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
    catch (err) { return ''; }
  }

  async function fetchTimeout(url, ms, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try { return await fetch(url, { ...options, signal: controller.signal }); }
    finally { clearTimeout(timer); }
  }

  /* ---------- connection indicator & auth ---------- */
  function setConn(next) {
    state.conn = next;
    els.dot.classList.toggle('connecting', next === 'connecting');
    els.dot.classList.toggle('offline', next === 'offline');
    els.connLabel.textContent =
      next === 'online' ? 'Connected' :
      next === 'connecting' ? 'Connecting…' : 'Offline';
  }

  function redirectToLogin() {
    if (state.redirecting) return;
    state.redirecting = true;
    const sep = LOGIN_URL.includes('?') ? '&' : '?';
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `${LOGIN_URL}${sep}next=${next}`;
  }

  /* ---------- flags (upload gating, char limits) ---------- */
  function applyFlags(flags) {
    if (!flags || typeof flags !== 'object') return;
    state.flags = flags;
    const uploadsOn = !!flags.uploads_enabled;
    els.attachBtn.hidden = !uploadsOn;
    els.fileInput.disabled = !uploadsOn;
    if (Number(flags.max_message_chars) > 0) els.input.maxLength = flags.max_message_chars;
    if (!uploadsOn && state.attachment) {
      clearAttachment('File uploads have been disabled — attachment removed.');
    }
  }

  /* ---------- status line ---------- */
  function showStatus(text) {
    els.statusLine.classList.add('on');
    els.statusText.textContent = text;
  }
  function clearStatus() {
    els.statusLine.classList.remove('on');
    els.statusText.textContent = '';
  }

  function describeStatus(data) {
    const tool = data.tool ? (TOOL_LABELS[data.tool] || data.tool) : null;
    const attempt = Number(data.attempt) || 1;
    const retries = Number(data.retries);
    const total = Number.isFinite(retries) ? retries + 1 : null;
    switch (data.phase) {
      case 'safety_check': return 'Checking your message…';
      case 'classifying': return 'Understanding your request…';
      case 'retrieving': return 'Searching our knowledge base…';
      case 'thinking': return 'Thinking…';
      case 'resuming': return 'Continuing…';
      case 'awaiting_confirmation': return 'Waiting for your approval…';
      case 'tool_running':
        if (attempt > 1) {
          return total ? `Retrying ${tool} (attempt ${attempt} of ${total})…`
                       : `Retrying ${tool} (attempt ${attempt})…`;
        }
        return tool ? `Running ${tool}…` : 'Working…';
      case 'tool_error': return tool ? `Had trouble with ${tool}` : null;
      default: return null;
    }
  }

  /* ---------- composer lock (agent's turn) ---------- */
  function setMode(mode) {
    state.mode = mode;
    const locked = mode === 'agent_turn';
    els.form.classList.toggle('locked', locked);
    els.input.readOnly = locked;
    els.send.disabled = locked;
    els.attachBtn.disabled = locked;
    if (!locked) els.input.focus({ preventScroll: true });
  }

  /* ---------- log & bubbles ---------- */
  function nearBottom() {
    return els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 90;
  }
  function scrollBottom(force) {
    if (force || state.autoScroll) els.log.scrollTop = els.log.scrollHeight;
  }
  els.log.addEventListener('scroll', () => { state.autoScroll = nearBottom(); });

  function clearLog() { els.log.replaceChildren(); }

  function appendToLog(node) {
    els.log.appendChild(node);
    scrollBottom(false);
  }

  function showEmptyState() {
    clearLog();
    const wrap = el('div', 'chat-empty');
    const first = String(CFG.userName || '').split(' ')[0];
    wrap.appendChild(el('h3', null, `Hi ${first || 'there'} 👋`));
    wrap.appendChild(el('p', null,
      'Ask me about products, your cart, orders or store policies — or tap a suggestion below.'));
    els.log.appendChild(wrap);
  }

  function addUserBubble(text, opts = {}) {
    const bubble = el('div', 'msg msg-user', text);
    if (!opts.history) bubble.appendChild(el('div', 'msg-meta', fmtTime()));
    appendToLog(bubble);
  }

  function addBotBubble(text, usage) {
    const bubble = el('div', 'msg msg-bot', text);
    const meta = el('div', 'msg-meta', fmtTime());
    const tin = usage ? Number(usage.tokens_in) : NaN;
    const tout = usage ? Number(usage.tokens_out) : NaN;
    if (Number.isFinite(tin) || Number.isFinite(tout)) {
      meta.textContent += ` · ${Number.isFinite(tin) ? tin : 0} in / ${Number.isFinite(tout) ? tout : 0} out tokens`;
    }
    bubble.appendChild(meta);
    appendToLog(bubble);
  }

  function addNoteBubble(text) { appendToLog(el('div', 'msg msg-note', text)); }
  function addErrorBubble(text) { appendToLog(el('div', 'msg msg-error', text)); }

  function toast(text, kind = 'info') {
    const node = el('div', `toast toast-${kind}`, text);
    els.toasts.appendChild(node);
    setTimeout(() => node.remove(), 5000);
  }

  /* ---------- conversation sidebar ---------- */
  function setSidebarActive(conversationId) {
    if (!els.convList) return;
    els.convList.querySelectorAll('.conv-item').forEach((item) => {
      item.classList.toggle('active', Number(item.dataset.conversationId) === conversationId);
    });
  }

  function markUnread(conversationId) {
    if (!els.convList) return;
    const id = String(Number(conversationId));
    const item = els.convList.querySelector(`.conv-item[data-conversation-id="${id}"]`);
    if (item) item.classList.add('unread');
  }

  function addSidebarItem(conversationId) {
    if (!els.convList) return null;
    const anchor = el('a', 'conv-item');
    anchor.href = HISTORY_BASE + String(conversationId);
    anchor.dataset.conversationId = String(conversationId);
    anchor.appendChild(el('span', 'conv-label', 'Today ' + fmtTime()));
    anchor.appendChild(el('span', 'conv-dot'));
    const empty = els.convList.querySelector('.conv-empty');
    if (empty) empty.remove();
    els.convList.prepend(anchor);
    return anchor;
  }

  /*
   * True if an event's conversation_id targets the conversation being viewed.
   * If the user's just-created conversation id is still unknown (its
   * conversation_started event raced a disconnect), adopt it — the server
   * only ever runs one turn per user, so it can only be ours.
   */
  function eventIsForActive(conversationId) {
    if (conversationId == null) return true;
    if (conversationId === state.activeConversationId) return true;
    if (state.activeConversationId == null && state.mode === 'agent_turn') {
      state.activeConversationId = conversationId;
      addSidebarItem(conversationId);
      updateUrl(conversationId);
      setSidebarActive(conversationId);
      return true;
    }
    return false;
  }

  /* ---------- conversation switching & history ---------- */
  function updateUrl(conversationId) {
    const url = conversationId != null
      ? `${location.pathname}?conversation_id=${conversationId}`
      : location.pathname;
    history.replaceState(null, '', url);
  }

  async function openConversation(conversationId) {
    state.activeConversationId = conversationId;
    clearPendingConfirmation();
    setSidebarActive(conversationId);
    updateUrl(conversationId);
    clearLog();
    appendToLog(el('div', 'chat-empty', 'Loading conversation…'));
    try {
      const res = await fetchTimeout(HISTORY_BASE + conversationId, 10000,
        { headers: { Accept: 'application/json' } });
      if (res.status === 401) { redirectToLogin(); return; }
      if (res.status === 404) {
        clearLog();
        appendToLog(el('div', 'msg msg-error', 'Conversation not found.'));
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      renderHistory(await res.json());
    } catch (err) {
      clearLog();
      appendToLog(el('div', 'msg msg-error',
        "Couldn't load this conversation. Check your connection and try again."));
    }
  }

  function renderHistory(data) {
    clearLog();
    const messages = Array.isArray(data.messages) ? data.messages : [];
    if (!messages.length && !data.pending_confirmation) { showEmptyState(); return; }

    let endsWithFinal = false; // heuristic: turn already finished while we were away
    for (const m of messages) {
      if (m.role === 'user') {
        addUserBubble(m.content || '(image attached)', { history: true });
        endsWithFinal = false;
      } else if (m.role === 'assistant') {
        if (m.interim) { addNoteBubble(m.content || ''); endsWithFinal = false; }
        else { addBotBubble(m.content || ''); endsWithFinal = true; }
      } else if (m.role === 'carousel') {
        addCarousel(m.product_ids || []);
        endsWithFinal = false;
      }
    }

    if (data.pending_confirmation) {
      const p = data.pending_confirmation;
      let expiresAt = new Date(p.expires_at);
      if (Number.isNaN(expiresAt.getTime())) expiresAt = new Date(Date.now() + 60000);
      addConfirmationCard({
        requestId: p.request_id, tool: p.tool,
        message: p.message, args: p.args, expiresAt,
      });
      endsWithFinal = false;
    }

    // Missed the final chat_message during a disconnect → unlock now.
    if (state.mode === 'agent_turn' && endsWithFinal) setMode('idle');
    scrollBottom(true);
  }

  /* ---------- product carousel ---------- */
  function addCarousel(productIds) {
    const ids = (Array.isArray(productIds) ? productIds : [])
      .map((id) => Number(id))
      .filter((id) => Number.isInteger(id) && id > 0)
      .slice(0, 12);

    const wrap = el('div', 'carousel');
    const track = el('div', 'carousel-track');
    wrap.appendChild(track);
    appendToLog(wrap);

    if (!ids.length) {
      track.appendChild(el('div', 'carousel-empty', 'No products to show.'));
      return;
    }

    const skeletons = ids.map(() => {
      const skeleton = el('div', 'carousel-skeleton');
      track.appendChild(skeleton);
      return skeleton;
    });

    fetchTimeout(`${LOOKUP_URL}?ids=${encodeURIComponent(ids.join(','))}`, 10000,
      { headers: { Accept: 'application/json' } })
      .then((res) => { if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.json(); })
      .then((cards) => {
        skeletons.forEach((s) => s.remove());
        if (!Array.isArray(cards) || !cards.length) {
          track.appendChild(el('div', 'carousel-empty', 'No products found.'));
          return;
        }
        cards.forEach((p) => track.appendChild(buildCarouselCard(p)));
        scrollBottom(false);
      })
      .catch(() => {
        skeletons.forEach((s) => s.remove());
        track.appendChild(el('div', 'carousel-empty', "Couldn't load product details."));
      });
  }

  function buildCarouselCard(p) {
    const href = safeUrl(p.url);
    const card = el('a', 'carousel-card');
    if (href) {
      card.href = href;
      card.title = p.name || 'View product';
    } else {
      card.classList.add('carousel-dead');
    }
    const img = el('img', 'carousel-thumb');
    img.src = safeUrl(p.image_url) || IMG_FALLBACK;
    img.alt = p.name || 'Product image';
    img.loading = 'lazy';
    card.appendChild(img);

    const info = el('div', 'carousel-info');
    info.appendChild(el('span', 'carousel-name', p.name || `Product #${p.id}`));
    info.appendChild(el('span', 'carousel-price', fmtMoney(p.price)));
    const stock = Number(p.stock_quantity);
    let badge;
    if (p.in_stock === false) badge = el('span', 'badge badge-cancelled', 'Out of stock');
    else if (Number.isFinite(stock) && stock <= 5) badge = el('span', 'badge badge-low-stock', `Only ${stock} left`);
    else badge = el('span', 'badge badge-success', 'In stock');
    info.appendChild(badge);
    card.appendChild(info);
    return card;
  }

  /* ---------- confirmation card ---------- */
  function addConfirmationCard({ requestId, tool, message, args, expiresAt }) {
    // Any live card still open is superseded by this one (server keeps one per thread).
    if (state.pendingConfirmation && !state.pendingConfirmation.done) {
      settleCard(state.pendingConfirmation, 'superseded');
    }
    state.pendingConfirmation = null;

    const card = el('div', 'confirm-card');
    card.appendChild(el('div', 'confirm-tool',
      'Action requested — ' + (TOOL_LABELS[tool] || tool || 'tool')));
    card.appendChild(el('div', 'confirm-message', message || 'Allow this action?'));

    const argEntries = Object.entries(args || {}).filter(([key]) => key !== 'retries');
    if (argEntries.length) {
      const argsEl = el('div', 'confirm-args');
      for (const [key, value] of argEntries) {
        const row = el('div', 'confirm-arg');
        row.appendChild(el('span', 'confirm-arg-key', key));
        const val = (value !== null && typeof value === 'object')
          ? JSON.stringify(value) : String(value);
        row.appendChild(el('span', 'confirm-arg-val', val));
        argsEl.appendChild(row);
      }
      card.appendChild(argsEl);
    }

    const countdown = el('div', 'confirm-countdown', '');
    const actions = el('div', 'confirm-actions');
    const accept = el('button', 'btn btn-primary btn-sm', 'Accept');
    accept.type = 'button';
    const decline = el('button', 'btn btn-sm', 'Decline');
    decline.type = 'button';
    actions.append(accept, decline);
    card.append(countdown, actions);
    appendToLog(card);
    scrollBottom(true);

    const record = {
      requestId: String(requestId),
      card, accept, decline, countdown,
      expiresAt: expiresAt instanceof Date ? expiresAt : new Date(expiresAt),
      timer: null,
      done: false,
    };

    const tick = () => {
      const remaining = record.expiresAt.getTime() - Date.now();
      if (remaining <= 0) { 
        settleCard(record, 'timeout');
        if (state.socket && state.socket.connected) {
          state.socket.emit('confirmation_response', {
            request_id: record.requestId,
            accepted: false
          });
        }
        return;
      }
      const seconds = Math.ceil(remaining / 1000);
      countdown.textContent =
        `Expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    };
    record.timer = setInterval(tick, 1000);
    tick();

    accept.addEventListener('click', () => respondConfirmation(record, true));
    decline.addEventListener('click', () => respondConfirmation(record, false));
    state.pendingConfirmation = record;
  }

  function settleCard(record, reason) {
    if (!record || record.done) return;
    record.done = true;
    if (record.timer) clearInterval(record.timer);
    record.accept.disabled = true;
    record.decline.disabled = true;
    record.card.classList.add(
      reason === 'accepted' ? 'accepted' :
      reason === 'declined' ? 'declined' :
      reason === 'timeout' ? 'expired' : 'superseded');
    const labels = {
      accepted: '✓ Approved',
      declined: '✕ Declined',
      timeout: '⏱ Expired',
      superseded: 'Cancelled — you sent a new message',
    };
    record.countdown.textContent = labels[reason] || 'Closed';
    if (state.pendingConfirmation === record) state.pendingConfirmation = null;
  }

  function clearPendingConfirmation() {
    const record = state.pendingConfirmation;
    if (!record) return;
    if (record.timer) clearInterval(record.timer);
    state.pendingConfirmation = null; // the card's DOM dies with the log
  }

  function respondConfirmation(record, accepted) {
    if (!state.socket || !state.socket.connected) {
      toast('Reconnecting — please try again in a moment.', 'warning');
      return;
    }
    if (record.done) return;
    record.accept.disabled = true;
    record.decline.disabled = true;
    state.socket.emit('confirmation_response', {
      request_id: record.requestId,
      accepted: !!accepted,
    });
    // Safety valve: if no confirmation_resolved arrives (dropped socket,
    // double-answered in another tab), re-enable the buttons shortly.
    setTimeout(() => {
      if (!record.done) { record.accept.disabled = false; record.decline.disabled = false; }
    }, 15000);
  }

  /* ---------- attachments (gated on vision_capable) ---------- */
  function renderAttachmentChip({ filename, objectUrl }) {
    clearAttachment();
    const chip = el('div', 'attachment-chip pending');
    const thumb = el('img', 'attachment-thumb');
    thumb.src = objectUrl;
    thumb.alt = '';
    const name = el('span', 'attachment-name', filename);
    const remove = el('button', 'attachment-remove', '✕');
    remove.type = 'button';
    remove.title = 'Remove attachment';
    remove.setAttribute('aria-label', 'Remove attachment');
    remove.addEventListener('click', () => clearAttachment());
    chip.append(thumb, name, remove);
    els.tray.appendChild(chip);
    state.attachmentChip = { chip, objectUrl };
  }

  function clearAttachment(notice) {
    if (state.attachmentChip) {
      if (state.attachmentChip.objectUrl) URL.revokeObjectURL(state.attachmentChip.objectUrl);
      state.attachmentChip.chip.remove();
      state.attachmentChip = null;
    }
    state.attachment = null;
    if (notice) toast(notice, 'info');
  }

  els.attachBtn.addEventListener('click', () => {
    if (!els.fileInput.disabled) els.fileInput.click();
  });

  els.fileInput.addEventListener('change', async () => {
    const file = els.fileInput.files && els.fileInput.files[0];
    els.fileInput.value = ''; // allow re-selecting the same file
    if (!file) return;
    if (!state.flags.uploads_enabled) { toast('File uploads are disabled.', 'warning'); return; }

    const ext = '.' + ((file.name.split('.').pop() || '').toLowerCase());
    const allowed = (state.flags.allowed_upload_extensions || [])
      .map((e) => String(e).toLowerCase());
    if (allowed.length && !allowed.includes(ext)) {
      toast(`File type "${ext}" is not allowed.`, 'warning');
      return;
    }
    const maxBytes = (Number(state.flags.max_upload_mb) || 0) * 1024 * 1024;
    if (maxBytes && file.size > maxBytes) {
      toast(`Files are limited to ${state.flags.max_upload_mb} MB.`, 'warning');
      return;
    }

    renderAttachmentChip({ filename: file.name, objectUrl: URL.createObjectURL(file) });
    try {
      const body = new FormData();
      body.append('file', file);
      const res = await fetchTimeout(UPLOAD_URL, 30000, {
        method: 'POST',
        headers: { 'X-CSRFToken': CFG.csrfToken || '' },
        body,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Upload failed (HTTP ${res.status}).`);
      state.attachment = { id: data.attachment_id, filename: data.filename || file.name };
      if (state.attachmentChip) state.attachmentChip.chip.classList.remove('pending');
    } catch (err) {
      clearAttachment();
      toast(err.message || 'Upload failed.', 'danger');
    }
  });

  /* ---------- socket ---------- */
  state.socket = io();

  state.socket.on('connect', () => {
    const first = !state.everConnected;
    state.everConnected = true;
    setConn('online');

    if (state.bufferedSend) {
      const payload = state.bufferedSend;
      state.bufferedSend = null;
      state.socket.emit('user_message', payload);
      setMode('agent_turn');
      showStatus('Connecting…'); // hold the required label until the server answers
      return;
    }
    // Reconnect resync: rooms don't buffer events, so refresh the viewed
    // conversation from the checkpointer (also heals a live confirmation card).
    if (!first && state.activeConversationId != null &&
        (state.needsResync || state.mode === 'agent_turn')) {
      openConversation(state.activeConversationId);
    }
    state.needsResync = false;
  });

  state.socket.on('disconnect', () => {
    setConn('connecting');
    state.needsResync = true;
  });

  state.socket.on('connect_error', () => {
    setConn('connecting');
    state.needsResync = true;
    const now = Date.now();
    if (now - state.lastAuthCheck < 5000) return; // throttle auth probes
    state.lastAuthCheck = now;
    (async () => {
      try {
        const res = await fetchTimeout(PING_URL, 5000);
        const data = await res.json().catch(() => null);
        if (data && data.authenticated === false) redirectToLogin();
      } catch (err) { /* network unreachable — the socket keeps retrying */ }
    })();
  });

  state.socket.on('connected', (data) => applyFlags(data));

  state.socket.on('conversation_started', (data) => {
    const id = data && Number(data.conversation_id);
    if (!Number.isInteger(id)) return;
      if (!els.convList || !els.convList.querySelector(`.conv-item[data-conversation-id="${id}"]`)) {
      addSidebarItem(id);
    }
    if (state.activeConversationId == null) {
      state.activeConversationId = id;
      updateUrl(id);
    }
    setSidebarActive(state.activeConversationId);
  });

  state.socket.on('agent_status', (data) => {
    const phase = data && data.phase;
    if (phase === 'done' || phase === 'blocked' || phase === 'tool_done' ||
        phase === 'tool_declined' || phase === 'tool_blocked') {
      clearStatus();
      return;
    }
    const label = describeStatus(data || {});
    if (label) showStatus(label);
  });

  state.socket.on('agent_note', (data) => {
    if (!data || !data.content) return;
    if (!eventIsForActive(data.conversation_id)) { markUnread(data.conversation_id); return; }
    addNoteBubble(data.content);
  });

  state.socket.on('display_product_carousel', (data) => {
    if (!data) return;
    if (!eventIsForActive(data.conversation_id)) { markUnread(data.conversation_id); return; }
    addCarousel(data.product_ids || []);
  });

  state.socket.on('get_user_confirmation', (data) => {
    if (!data || !data.request_id) return;
    if (!eventIsForActive(data.conversation_id)) {
      markUnread(data.conversation_id);
      toast('The assistant needs your approval in another conversation.', 'warning');
      return;
    }
    addConfirmationCard({
      requestId: data.request_id,
      tool: data.tool,
      message: data.message,
      args: data.args,
      expiresAt: new Date(data.expires_at),
    });
  });

  state.socket.on('confirmation_resolved', (data) => {
    if (!data || !data.request_id) return;
    const record = state.pendingConfirmation;
    if (record && record.requestId === String(data.request_id)) {
      settleCard(record, data.reason || (data.accepted ? 'accepted' : 'declined'));
    }
  });

  state.socket.on('chat_message', (data) => {
    if (!data) return;
    if (!eventIsForActive(data.conversation_id)) {
      markUnread(data.conversation_id);
      setMode('idle');
      clearStatus();
      return;
    }
    addBotBubble(data.content || '', data.usage);
    setMode('idle');
    clearStatus();
  });

  state.socket.on('chat_error', (data) => {
    const code = (data && data.code) || 'internal_error';
    const message = (data && data.message) || 'Something went wrong.';

    if (code === 'unauthorized') {
      toast(message, 'danger');
      redirectToLogin();
      return;
    }
    if (TURN_FAILURE.has(code)) {
      if (!data.conversation_id || data.conversation_id === state.activeConversationId) {
        addErrorBubble(message);
      }
      setMode('idle');
      clearStatus();
      return;
    }
    if (PRE_TURN.has(code)) {
      // The turn never started — unlocking is mandatory or the composer wedges.
      toast(message, 'warning');
      setMode('idle');
      clearStatus();
      return;
    }
    if (code === 'busy') {
      toast(message, 'warning');
      setMode('agent_turn'); // a turn IS running (this tab or another) — reflect it
      return;
    }
    toast(message, 'info'); // unknown_confirmation and future codes
  });

  /* ---------- heartbeat (connection indicator + flag sync + auth probe) ---------- */
  async function heartbeat() {
    if (document.hidden) return;
    try {
      const res = await fetchTimeout(PING_URL, 5000);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.pingFailures = 0;
      if (data && data.authenticated === false) { redirectToLogin(); return; }
      applyFlags(data);
      if (state.conn === 'offline') setConn('connecting'); // server is back; wait for socket
    } catch (err) {
      state.pingFailures += 1;
      if (state.pingFailures >= 2 && state.conn !== 'online') setConn('offline');
    }
  }
  setInterval(heartbeat, 15000);

  /* ---------- composer & sidebar wiring ---------- */
  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.mode === 'agent_turn') return;

    const content = els.input.value.trim();
    const attachment = state.attachment;
    if (!content && !attachment) return;

    const payload = { content };
    if (state.activeConversationId != null) payload.conversation_id = state.activeConversationId;
    if (attachment) payload.attachment_id = attachment.id;

    addUserBubble(content ? (attachment ? `${content} 📎` : content) : '(image attached)');
    els.input.value = '';
    clearAttachment();

    if (!state.socket || !state.socket.connected) {
      // Offline: show the required hardcoded label, buffer, flush on reconnect.
      state.bufferedSend = payload;
      setMode('agent_turn');
      setConn('connecting');
      showStatus('Connecting…');
      return;
    }
    state.socket.emit('user_message', payload);
    setMode('agent_turn');
    showStatus('Sending…');
  });

  if (els.convList) {
    els.newChatBtn.addEventListener('click', () => {
      clearPendingConfirmation();
      state.activeConversationId = null;
      setSidebarActive(null);
      updateUrl(null);
      showEmptyState();
      els.input.focus({ preventScroll: true });
    });
  }

  if (els.newChatBtn) {
    els.convList.addEventListener('click', (event) => {
      const item = event.target.closest('a.conv-item');
      if (!item) return;
      event.preventDefault();
      const id = Number(item.dataset.conversationId);
      if (!Number.isInteger(id)) return;
      item.classList.remove('unread');
      if (id === state.activeConversationId) return; // already current — no refetch flicker
      openConversation(id);
    });
  }
  /* ---------- init ---------- */
  applyFlags(state.flags);
  setMode('idle');
  setConn('connecting');
  if (state.activeConversationId != null) openConversation(state.activeConversationId);
  else showEmptyState();
  heartbeat();
})();