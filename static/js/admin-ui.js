/*
 * Shared helpers for admin pages (dashboard stats, conversations, playground).
 * Exposes window.AdminUI; loaded before the page-specific scripts (defer order).
 */
(() => {
  'use strict';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  async function fetchJSON(url, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (options.csrf) headers['X-CSRFToken'] = options.csrf;
    let body = options.body;
    if (body !== undefined && body !== null && typeof body !== 'string') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, {
        method: options.method || 'GET',
        headers,
        body: body === undefined ? undefined : body,
        signal: controller.signal,
        cache: 'no-store',
      });
    } catch (err) {
      throw new Error(err.name === 'AbortError' ? 'Request timed out.' : 'Network error.');
    } finally {
      clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      throw new Error((data && data.error) ? data.error : `Request failed (HTTP ${res.status}).`);
    }
    return data;
  }

  function toast(text, kind = 'info') {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    const node = el('div', `toast toast-${kind}`, text);
    stack.appendChild(node);
    setTimeout(() => node.remove(), 5000);
  }

  function fmtNum(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString() : '—';
  }

  function fmtMoney(value) {
    const n = Number(value);
    return Number.isFinite(n)
      ? '$' + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : '—';
  }

  function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    try {
      return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    } catch (err) {
      return d.toLocaleString();
    }
  }

  function safeUrl(url) {
    if (typeof url !== 'string') return null;
    const trimmed = url.trim();
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    if (trimmed === '/' || /^\/[^/]/.test(trimmed)) return trimmed;
    return null;
  }

  window.AdminUI = { el, fetchJSON, toast, fmtNum, fmtMoney, fmtDateTime, safeUrl };
})();