// Flash messages: dismissible + auto-fade
document.querySelectorAll('.flash').forEach((el) => {
  const dismiss = () => el.remove();
  el.querySelector('.flash-close')?.addEventListener('click', dismiss);
  setTimeout(dismiss, 6000);
});

// Chat suggestion chips -> fill the composer
document.querySelectorAll('[data-suggest]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = document.getElementById('chat-input');
    if (input) { input.value = btn.dataset.suggest; input.focus(); }
  });
});

// Elements marked data-autosubmit submit their form on change (e.g. sort select)
document.querySelectorAll('form [data-autosubmit]').forEach((el) => {
  el.addEventListener('change', () => el.closest('form')?.submit());
});