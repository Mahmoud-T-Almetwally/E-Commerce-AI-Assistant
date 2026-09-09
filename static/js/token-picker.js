/* ============================================================
   Token picker — searchable pill input for tags & categories.
   Self-initialises for every .token-field.

   Markup:
   <div class="token-field"
        data-name="tag"                 form field name (comma-joined value)
        data-mode="single|multiple"     default: multiple
        data-lookup="tags|categories"   enables server autocomplete
        data-allow-free="false"         disallow values not in suggestions
        data-input-id="some-id"         id for the inner input (label for=)
        data-placeholder="…"
        data-initial="A, B">            starting values
   ============================================================ */
(function () {
  'use strict';

  var DEBOUNCE_MS = 200;
  var MAX_OPTIONS = 10;

  document.querySelectorAll('.token-field').forEach(initField);

  function initField(root) {
    var mode = root.dataset.mode === 'single' ? 'single' : 'multiple';
    var allowFree = root.dataset.allowFree !== 'false';
    var lookup = root.dataset.lookup || '';
    var placeholder = root.dataset.placeholder || '';

    var values = split(root.dataset.initial);
    var options = [];   // [{value, count}]
    var active = -1;
    var timer = null;
    var seq = 0;

    var hidden = el('input', 'token-hidden');
    hidden.type = 'hidden';
    hidden.name = root.dataset.name;
    root.appendChild(hidden);

    var pills = el('span', 'token-pills');
    root.appendChild(pills);

    var input = el('input', 'token-input');
    input.type = 'text';
    input.autocomplete = 'off';
    if (root.dataset.inputId) input.id = root.dataset.inputId;
    root.appendChild(input);

    var menu = el('div', 'token-menu');
    menu.hidden = true;
    root.appendChild(menu);

    render();

    /* ---- events ---- */
    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(loadOptions, DEBOUNCE_MS);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (menu.hidden || !options.length) return;
        e.preventDefault();
        active = e.key === 'ArrowDown'
          ? Math.min(active + 1, options.length - 1)
          : Math.max(active - 1, 0);
        paintActive();
      } else if (e.key === 'Enter') {
        if (!menu.hidden && active >= 0) {
          e.preventDefault();
          commit(options[active].value);
        } else if (input.value.trim()) {
          e.preventDefault();
          commit(input.value.trim());
        } /* empty input → native form submit */
      } else if (e.key === 'Backspace' && !input.value && mode === 'multiple' && values.length) {
        e.preventDefault();
        values.pop();
        render();
      } else if (e.key === 'Escape') {
        hideMenu();
      }
    });

    menu.addEventListener('mousedown', function (e) {
      var opt = e.target.closest('.token-option');
      if (!opt) return;
      e.preventDefault();          // keep focus in the input
      commit(opt.dataset.value);
    });

    root.addEventListener('click', function (e) {
      var x = e.target.closest('.token-x');
      if (x) {
        remove(x.closest('.token-pill').dataset.value);
      } else if (e.target === root || e.target === pills) {
        input.focus();
      }
    });

    document.addEventListener('click', function (e) {
      if (!root.contains(e.target)) hideMenu();
    });

    /* ---- state ---- */
    function split(s) {
      return (s || '').split(',').map(function (v) { return v.trim(); }).filter(Boolean);
    }

    function render() {
      hidden.value = values.join(', ');
      pills.textContent = '';
      values.forEach(function (v) {
        var pill = el('span', 'token-pill');
        pill.dataset.value = v;
        var label = el('span');
        label.textContent = v;
        var x = el('button', 'token-x');
        x.type = 'button';
        x.setAttribute('aria-label', 'Remove ' + v);
        x.textContent = '×';
        pill.appendChild(label);
        pill.appendChild(x);
        pills.appendChild(pill);
      });
      input.placeholder = (placeholder && (mode === 'multiple' || !values.length)) ? placeholder : '';
    }

    function commit(v) {
      v = String(v).trim();
      if (!v) return;
      if (mode === 'single') {
        values = [v];
      } else if (!values.some(function (x) { return x.toLowerCase() === v.toLowerCase(); })) {
        values.push(v);
      }
      input.value = '';
      hideMenu();
      render();
      input.focus();
    }

    function remove(v) {
      values = values.filter(function (x) { return x !== v; });
      render();
      input.focus();
    }

    function loadOptions() {
      var q = input.value.trim();
      if (!lookup || !q) { hideMenu(); return; }
      var mySeq = ++seq;
      fetch('/lookup/' + lookup + '?q=' + encodeURIComponent(q), {
        headers: { 'Accept': 'application/json' }
      }).then(function (res) {
        if (!res.ok) throw new Error('lookup failed');
        return res.json();
      }).then(function (data) {
        if (mySeq !== seq) return;  // stale response — a newer keystroke won
        options = data.filter(function (d) {
          return !values.some(function (x) { return x.toLowerCase() === d.value.toLowerCase(); });
        }).slice(0, MAX_OPTIONS);
        showMenu();
      }).catch(function () {
        hideMenu(); // offline / rate-limited — free-text entry still works
      });
    }

    function showMenu() {
      menu.textContent = '';
      if (!options.length) {
        var empty = el('div', 'token-empty');
        empty.textContent = allowFree ? 'No matches — press Enter to use it anyway' : 'No matches';
        menu.appendChild(empty);
      } else {
        options.forEach(function (o) {
          var opt = el('div', 'token-option');
          opt.dataset.value = o.value;
          var label = el('span');
          label.textContent = o.value;
          opt.appendChild(label);
          if (o.count != null) {
            var count = el('span', 'token-count');
            count.textContent = o.count;
            opt.appendChild(count);
          }
          menu.appendChild(opt);
        });
      }
      active = -1;
      menu.hidden = false;
    }

    function paintActive() {
      var nodes = menu.querySelectorAll('.token-option');
      nodes.forEach(function (n, i) { n.classList.toggle('active', i === active); });
      if (nodes[active]) nodes[active].scrollIntoView({ block: 'nearest' });
    }

    function hideMenu() {
      menu.hidden = true;
      active = -1;
    }
  }

  function el(tag, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }
})();