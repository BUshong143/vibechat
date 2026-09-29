// Buttons on a profile page: friend actions, then reload to show the new state.
(function () {
  const username = document.querySelector('.profile').dataset.username;
  const err = document.getElementById('err');
  const csrf = () => (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1];
  const post = (url, body) => fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()}, body: JSON.stringify(body || {})
  });

  document.querySelectorAll('[data-act]').forEach(btn => btn.addEventListener('click', async () => {
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    document.querySelectorAll('[data-act]').forEach(b => b.disabled = true);
    err.hidden = true;
    try {
      let r;
      if (btn.dataset.act === 'message') {
        r = await post('/api/conversations/', {username});
        if (r.ok) { location.href = '/app/?c=' + (await r.json()).id; return; }
      } else {
        r = await post(`/api/friends/${encodeURIComponent(username)}/${btn.dataset.act}/`);
        if (r.ok || r.status === 409) { location.reload(); return; }  // 409: it changed under us, so show the fresh state
      }
      err.textContent = r.status === 403 ? 'You can only chat with friends.' : 'Something went wrong. Please try again.';
    } catch (e) {
      err.textContent = 'Connection problem. Please try again.';
    }
    err.hidden = false;
    document.querySelectorAll('[data-act]').forEach(b => b.disabled = false);
  }));
})();
