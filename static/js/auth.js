// Login / register pages: show a busy state on submit so a slow phone connection doesn't invite double taps,
// and scroll the first error into view.
(function () {
  const form = document.querySelector('.auth-card form');
  if (!form) return;
  const btn = form.querySelector('.submit');
  const idle = btn.innerHTML;
  form.addEventListener('submit', () => {
    if (btn.disabled) return;
    btn.disabled = true; btn.classList.add('busy');
    btn.innerHTML = '<span class="spin" aria-hidden="true"></span><span>Please wait…</span>';
  });
  // coming back with the browser's back button must not leave the button stuck
  window.addEventListener('pageshow', () => { btn.disabled = false; btn.classList.remove('busy'); btn.innerHTML = idle; });
  const bad = form.querySelector('.bad input, .alert');
  if (bad && bad.scrollIntoView) bad.scrollIntoView({block: 'center'});
})();
