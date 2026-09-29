const $ = id => document.getElementById(id);
const ME = document.querySelector('.layout').dataset.me;
const csrf = () => (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1];
let current = null, lastId = 0, ready = false, inFlight = null, errShown = false;
let ws = null, retry = 0, lastTyping = 0;
let convList = [], buffer = [];
let online = new Set(), reads = {}, readSent = 0, incomingId = 0;  // reads: username -> last message id they have seen
const typers = new Map(); let lastSender = null, modalFor = null;
const seen = new Set(), pending = new Map();
let friendsState = {friends: [], incoming: [], outgoing: []}, results = [], toastTimer = null;

async function api(url, opts = {}) {
  const r = await fetch(url, {
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()}, ...opts
  });
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

function wsSend(payload) {
  if (ws && ws.readyState === 1) { ws.send(JSON.stringify(payload)); return true; }
  return false;
}

function avatarEl(f, size) {
  const s = document.createElement('span'); s.className = 'avatar ' + (size || 'md');
  s.style.background = f.color;
  if (f.avatar) { const i = document.createElement('img'); i.src = f.avatar; i.alt = ''; s.append(i); }
  else s.textContent = f.initial;
  return s;
}
function avatarWithDot(f, username) {
  const w = document.createElement('span'); w.className = 'av-wrap'; w.append(avatarEl(f, 'md'));
  if (username) { const d = document.createElement('span'); d.className = 'dot' + (online.has(username) ? ' on' : ''); w.append(d); }
  return w;
}
function profileUrl(username) { return '/u/' + encodeURIComponent(username) + '/'; }
const curConv = () => convList.find(x => x.id === current);
function nameOf(username) {  // display name of someone in the open chat
  const c = curConv();
  if (!c) return username;
  if (c.other === username) return c.title;
  const m = c.members && c.members.find(x => x.username === username);
  return m ? m.name : username;
}
function toast(text, onclick) {
  const t = $('toast'); t.textContent = text; t.hidden = false;
  t.onclick = () => { t.hidden = true; if (onclick) onclick(); };
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

function showStatus(text, dots) {
  const s = $('status'); s.replaceChildren();
  if (!text) return;
  s.append(text);
  if (dots) {
    const d = document.createElement('span'); d.className = 'dots';
    for (let i = 0; i < 3; i++) {
      const sp = document.createElement('span'); sp.textContent = '.'; sp.style.animationDelay = i * 0.2 + 's'; d.append(sp);
    }
    s.append(' ', d);
  }
}

function renderTyping() {
  const names = [...typers.keys()].map(nameOf);
  if (!names.length) { showStatus(''); return; }
  showStatus(names.length === 1 ? names[0] + ' is typing'
    : names.length === 2 ? names[0] + ' and ' + names[1] + ' are typing'
    : names[0] + ', ' + names[1] + ' and ' + (names.length - 2) + ' more are typing', true);
}
function setTyping(user, on) {
  if (typers.has(user)) clearTimeout(typers.get(user));
  if (on) typers.set(user, setTimeout(() => { typers.delete(user); renderTyping(); }, 3000));
  else typers.delete(user);
  renderTyping();
}
function clearTyping() { typers.forEach(clearTimeout); typers.clear(); }

function renderPresence() {
  const el = $('chat-presence');
  const c = convList.find(x => x.id === current);
  if (c && c.type === 'group') {
    const others = c.members.filter(m => m.username !== ME);
    const n = others.filter(m => online.has(m.username)).length;
    el.textContent = c.members.length + ' members' + (n ? ' · ' + n + ' online' : '');
    el.className = n ? 'on' : '';
    return;
  }
  if (!c || !c.other) { el.textContent = ''; el.className = ''; return; }
  const on = online.has(c.other);
  el.textContent = on ? 'Online' : 'Offline';
  el.className = on ? 'on' : '';
}

function renderConvs() {
  const box = $('convs'); box.replaceChildren();
  convList.forEach(c => {
    const li = document.createElement('li');
    li.className = 'row ' + (c.id === current ? 'active ' : '') + (c.unread ? 'unread' : '');
    li.append(avatarWithDot(c.face, c.other));
    const txt = document.createElement('div'); txt.className = 'txt';
    const t = document.createElement('span'); t.className = 'title'; t.textContent = c.title;
    const sm = document.createElement('small'); sm.textContent = c.last;
    txt.append(t, sm); li.append(txt);
    if (c.unread) { const b = document.createElement('span'); b.className = 'badge'; b.textContent = c.unread; li.append(b); }
    li.onclick = () => openConv(c.id, c.title);
    box.append(li);
  });
  renderPresence(); renderHead(); syncComposer();
}

function renderHead() {
  const c = convList.find(x => x.id === current);
  const av = $('chat-avatar'), t = $('chat-title');
  av.replaceChildren();
  $('chat-info').hidden = !c || c.type !== 'group';
  $('chat-call').hidden = !c || c.type === 'group' || !c.can_send;  // one-to-one, and only with friends
  if (typeof syncWatchButton === 'function') syncWatchButton();
  if (!c) { t.textContent = 'Select a conversation'; t.removeAttribute('href'); return; }
  av.append(avatarEl(c.face, 'sm'));
  t.textContent = c.title;
  if (c.other) t.href = profileUrl(c.other); else t.removeAttribute('href');
}

// One-to-one chats stay readable after an unfriend, but the composer locks.
function syncComposer() {
  const c = convList.find(x => x.id === current);
  const ok = !!c && c.can_send;
  $('text').disabled = $('send').disabled = !ok;
  const lock = $('locked'); lock.replaceChildren();
  lock.hidden = !c || ok;
  if (c && !ok) {
    lock.append('You can only message friends. ');
    if (c.other) { const a = document.createElement('a'); a.href = profileUrl(c.other); a.textContent = 'View ' + c.title + "'s profile"; lock.append(a); }
  }
}
async function loadConvs() {
  const {conversations} = await api('/api/conversations/');
  const unread = new Map(convList.map(c => [c.id, c.unread]));
  const before = new Set(convList.map(c => c.id));
  convList = conversations.map(c => ({...c, unread: unread.get(c.id) || 0}));
  if (current && !convList.some(c => c.id === current)) closeChat();
  renderConvs();
  if (convList.some(c => !before.has(c.id))) wsSend({type: 'contacts'});
}

function renderSeen() {
  const box = $('messages');
  box.querySelectorAll('.receipt').forEach(r => r.remove());
  const c = curConv();
  const mine = [...box.querySelectorAll('.msg.mine[data-id]')];
  if (!c || !mine.length) return;
  const ids = mine.map(e => Number(e.dataset.id));
  // Each person's receipt sits under the latest message of mine that they have seen.
  const byMsg = new Map();
  Object.entries(reads).forEach(([user, upto]) => {
    if (user === ME || (c.members && !c.members.some(m => m.username === user))) return;
    let i = ids.length - 1;
    while (i >= 0 && ids[i] > upto) i--;
    if (i >= 0) byMsg.set(i, [...(byMsg.get(i) || []), user]);
  });
  byMsg.forEach((users, i) => {
    const r = document.createElement('div'); r.className = 'receipt';
    if (c.type !== 'group') r.textContent = 'Seen';
    else if (users.length >= c.members.length - 1) r.textContent = 'Seen by everyone';
    else {
      const names = users.map(nameOf);
      r.textContent = 'Seen by ' + (names.length > 3 ? names.slice(0, 3).join(', ') + ' +' + (names.length - 3) : names.join(', '));
      r.title = names.join(', ');
    }
    mine[i].after(r);
  });
}

function syncRead() {
  if (!current || document.visibilityState !== 'visible' || incomingId <= readSent) return;
  const cid = current, upto = incomingId, prev = readSent;
  readSent = upto;
  if (wsSend({type: 'read', conversation: cid, message_id: upto})) return;
  api(`/api/conversations/${cid}/read/`, {method: 'POST', body: JSON.stringify({message_id: upto})})
    .catch(() => { if (cid === current && readSent === upto) readSent = prev; });
}

function drawBubble(m, isPending) {
  const c = curConv();
  if (c && c.type === 'group' && m.sender !== ME && m.sender !== lastSender) {  // name once per run of messages
    const l = document.createElement('div'); l.className = 'sender-label'; l.textContent = nameOf(m.sender);
    $('messages').append(l);
  }
  lastSender = m.sender;
  const d = document.createElement('div');
  d.className = 'msg ' + (m.sender === ME ? 'mine' : 'theirs') + (isPending ? ' pending' : '');
  d.textContent = m.content;
  d.title = m.sender + (m.created_at ? ' · ' + new Date(m.created_at).toLocaleString() : '');
  if (m.id) d.dataset.id = m.id;
  $('messages').append(d);
  return d;
}
function addMsg(m) {
  if (seen.has(m.id)) return;
  seen.add(m.id); lastId = Math.max(lastId, m.id);
  if (m.sender !== ME) incomingId = Math.max(incomingId, m.id);
  drawBubble(m, false);
}
function confirmMsg(el, m) {
  if (seen.has(m.id)) { el.remove(); return; }
  seen.add(m.id); lastId = Math.max(lastId, m.id);
  el.dataset.id = m.id;
  el.classList.remove('pending', 'failed');
  el.title = m.sender + ' · ' + new Date(m.created_at).toLocaleString();
}
function stick(fn, force) {
  const b = $('messages');
  const near = b.scrollHeight - b.scrollTop - b.clientHeight < 80;
  fn();
  if (force || near) b.scrollTop = b.scrollHeight;
}

async function fetchMessages() {
  if (!current || inFlight === current) return;
  const id = current; inFlight = id;
  try {
    const data = await api(`/api/conversations/${id}/messages/?after=${lastId}`);
    if (id !== current) return;
    readSent = Math.max(readSent, data.my_read || 0);
    Object.entries(data.reads || {}).forEach(([u, id]) => { reads[u] = Math.max(reads[u] || 0, id); });
    stick(() => { data.messages.forEach(addMsg); buffer.splice(0).forEach(addMsg); renderSeen(); }, lastId === 0);
    ready = true;
    syncRead();
    if (errShown) { showStatus(''); errShown = false; }
  } catch (e) {
    if (id === current) {
      showStatus('Connection problem, retrying…'); errShown = true;
      if (!ready) setTimeout(fetchMessages, 2000);
    }
  } finally {
    if (inFlight === id) inFlight = null;
  }
}

function openConv(id, title) {
  document.querySelector('.layout').classList.add('open');
  if (id === current) return;
  current = id; lastId = 0; ready = false; buffer = []; seen.clear(); pending.clear();
  reads = {}; readSent = 0; incomingId = 0; lastSender = null; clearTyping();
  const c = convList.find(x => x.id === id); if (c) c.unread = 0;
  $('messages').replaceChildren(); showStatus('');
  renderConvs();
  if (!$('text').disabled) $('text').focus();
  fetchMessages();
  if (typeof watchResync === 'function' && c && c.type !== 'group') watchResync(id);
}

function closeChat() {  // the open chat is gone (you left it, or were removed)
  current = null; lastId = 0; ready = false; buffer = []; seen.clear(); pending.clear();
  reads = {}; readSent = 0; incomingId = 0; lastSender = null; clearTyping();
  $('messages').replaceChildren(); showStatus('');
  document.querySelector('.layout').classList.remove('open');
}

async function httpSend(cid, content, el, clientId) {
  try {
    const m = await api(`/api/conversations/${cid}/messages/`, {method: 'POST', body: JSON.stringify({content})});
    pending.delete(clientId);
    if (cid === current) confirmMsg(el, m);
  } catch (e) { el.classList.add('failed'); el.title = 'Not sent'; }
}
let lastSent = {text: '', at: 0};
function send() {
  const t = $('text'), content = t.value.trim();
  const conv = convList.find(x => x.id === current);
  if (!content || !current || !conv || !conv.can_send) return;
  const now = Date.now();
  if (content === lastSent.text && now - lastSent.at < 500) return;
  lastSent = {text: content, at: now};
  t.value = ''; lastTyping = 0;
  const cid = current;
  const clientId = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : Date.now() + '-' + Math.random();
  let el;
  stick(() => { el = drawBubble({sender: ME, content}, true); }, true);
  pending.set(clientId, el);
  if (wsSend({type: 'message', conversation: cid, content, client_id: clientId})) {
    setTimeout(() => { if (pending.has(clientId)) { el.classList.add('failed'); el.title = 'Not sent'; } }, 10000);
  } else httpSend(cid, content, el, clientId);
}
$('send').onclick = send;
$('text').onkeydown = e => { if (e.key === 'Enter' && !e.repeat && !e.isComposing) send(); };
$('text').oninput = () => {
  const now = Date.now();
  if (current && $('text').value && now - lastTyping > 2000 && wsSend({type: 'typing', conversation: current})) lastTyping = now;
};

function handle(ev) {
  if (ev.type === 'message.new') {
    const m = ev.message, mine = m.sender === ME, cid = ev.conversation;
    const conv = convList.find(c => c.id === cid);
    if (!conv) loadConvs().then(() => {  // first message of a brand-new chat: it should still count as unread
      const c = convList.find(x => x.id === cid);
      if (c && !mine && cid !== current) { c.unread = (c.unread || 0) + 1; renderConvs(); }
    }).catch(() => {});
    else {
      conv.last = m.content.slice(0, 60);
      if (!mine && cid !== current) conv.unread = (conv.unread || 0) + 1;
      convList = [conv, ...convList.filter(c => c !== conv)];
      renderConvs();
    }
    if (cid !== current) return;
    const el = ev.client_id && pending.get(ev.client_id);
    if (el) { pending.delete(ev.client_id); confirmMsg(el, m); return; }
    if (!mine) setTyping(m.sender, false);
    if (!ready) buffer.push(m);
    else { stick(() => addMsg(m), mine); syncRead(); }
  } else if (ev.type === 'typing' && ev.conversation === current) {
    setTyping(ev.user, true);
  } else if (ev.type === 'read') {
    if (ev.conversation === current && ev.user !== ME) {
      reads[ev.user] = Math.max(reads[ev.user] || 0, ev.message_id);
      stick(renderSeen);
    }
  } else if (ev.type === 'conversation.changed') {
    loadConvs().then(() => {
      const c = convList.find(x => x.id === ev.conversation);
      if (c && current === c.id) { renderSeen(); if (modalFor === c.id) openGroupInfo(); }
      if (c && (ev.added || []).includes(ME)) {
        toast(ev.name + ' added you to ' + c.title, () => { showTab('chats'); openConv(c.id, c.title); });
      }
    }).catch(() => {});
    wsSend({type: 'contacts'});
  } else if (ev.type === 'presence') {
    if (ev.online) online.add(ev.user); else online.delete(ev.user);
    renderConvs(); renderFriends();
  } else if (ev.type === 'presence.snapshot') {
    online = new Set(ev.online);
    renderConvs(); renderFriends();
  } else if (ev.type === 'friends.changed') {
    loadFriends().catch(() => {}); loadConvs().catch(() => {}); wsSend({type: 'contacts'});
    if (ev.user !== ME && ev.event === 'request') toast(ev.name + ' sent you a friend request', () => showTab('friends'));
    else if (ev.user !== ME && ev.event === 'accepted') toast('You and ' + ev.name + ' are now friends', () => showTab('friends'));
  } else if (ev.type.startsWith('call.')) {
    callEvent(ev);
  } else if (ev.type.startsWith('watch.')) {
    watchEvent(ev);
  } else if (ev.type === 'error') {
    const el = ev.client_id && pending.get(ev.client_id);
    if (el) { el.classList.add('failed'); el.title = ev.detail; } else showStatus(ev.detail);
  }
}

function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/chat/`);
  ws.onopen = () => {
    retry = 0; $('conn').hidden = true; ready = false;
    loadConvs().catch(() => {}); loadFriends().catch(() => {}); fetchMessages();
    if (typeof watchResync === 'function') { watchResync(current); if (W && W.conv !== current) watchResync(W.conv); }
  };
  ws.onmessage = e => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = e => {
    $('conn').hidden = false;
    callSocketClosed();
    online = new Set(); renderConvs(); renderFriends();
    if (e.code === 4401) { location.reload(); return; }
    setTimeout(connect, Math.min(500 * 2 ** retry++, 8000));
  };
}

// ---- friends ---------------------------------------------------------------------
function btn(label, cls, fn) {
  const b = document.createElement('button'); b.textContent = label; if (cls) b.className = cls;
  b.onclick = async e => {
    e.preventDefault(); e.stopPropagation(); b.disabled = true;
    try { await fn(); b.disabled = false; }
    catch (err) { b.disabled = false; toast("That didn't work. Refreshing…"); loadFriends().catch(() => {}); }
  };
  return b;
}
async function friendAct(username, act) {
  const r = await api('/api/friends/' + encodeURIComponent(username) + '/' + act + '/', {method: 'POST', body: '{}'});
  await loadFriends();
  wsSend({type: 'contacts'});
  return r.status;
}
async function startChat(username) {
  const c = await api('/api/conversations/', {method: 'POST', body: JSON.stringify({username})});
  await loadConvs().catch(() => {});
  showTab('chats'); openConv(c.id, c.title);
}
function personRow(u, buttons) {
  const li = document.createElement('li'); li.className = 'row';
  const a = document.createElement('a'); a.className = 'who'; a.href = profileUrl(u.username);
  const txt = document.createElement('div'); txt.className = 'txt';
  const n = document.createElement('span'); n.className = 'title'; n.textContent = u.name;
  const h = document.createElement('small'); h.textContent = '@' + u.username;
  txt.append(n, h); a.append(avatarWithDot(u, u.username), txt);
  const acts = document.createElement('span'); acts.className = 'acts'; acts.append(...buttons);
  li.append(a, acts);
  return li;
}
function buttonsFor(u, status) {
  if (status === 'friends') return [btn('Message', '', () => startChat(u.username))];
  if (status === 'incoming') return [btn('Accept', '', () => friendAct(u.username, 'accept')),
                                     btn('Decline', 'secondary', () => friendAct(u.username, 'decline'))];
  if (status === 'outgoing') return [btn('Cancel', 'secondary', () => friendAct(u.username, 'cancel'))];
  return [btn('Add', '', () => friendAct(u.username, 'request'))];
}

function renderFriends() {
  const box = $('friends'); box.replaceChildren();
  const {friends, incoming, outgoing} = friendsState;
  const section = (title, list, status) => {
    if (!list.length) return;
    const h = document.createElement('h4'); h.textContent = title;
    const ul = document.createElement('ul');
    list.forEach(u => ul.append(personRow(u, buttonsFor(u, status))));
    box.append(h, ul);
  };
  section('Friend requests · ' + incoming.length, incoming, 'incoming');
  section('Sent requests', outgoing, 'outgoing');
  section('Friends · ' + friends.length, friends, 'friends');
  if (!friends.length && !incoming.length && !outgoing.length) {
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = 'No friends yet. Search for someone above and send a request.';
    box.append(p);
  }
  const b = $('req-badge'); b.textContent = incoming.length; b.hidden = !incoming.length;
  renderResults();  // search rows show the same relationship state
}
async function loadFriends() {
  friendsState = await api('/api/friends/');
  renderFriends();
}
function showTab(name) {
  $('convs').hidden = $('newgroup-row').hidden = name !== 'chats'; $('friends').hidden = name !== 'friends';
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
}
document.querySelectorAll('.tabs button').forEach(b => { b.onclick = () => showTab(b.dataset.tab); });

// ---- groups -----------------------------------------------------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function openModal(title, ...body) {
  closeModal();
  const sheet = $('modal').querySelector('.sheet');
  const x = el('button', 'secondary x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close'); x.onclick = closeModal;
  sheet.append(el('h3', '', title), x, ...body);
  $('modal').hidden = false;
  const first = sheet.querySelector('input'); if (first) first.focus();
}
function closeModal() {
  modalFor = null;
  $('modal').hidden = true; $('modal').querySelector('.sheet').replaceChildren();
}
$('modal').onclick = e => { if (e.target === $('modal')) closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('modal').hidden) closeModal(); });

// A searchable list of friends with checkboxes. `selected` is a Set of usernames that the caller reads.
function picker(people, selected, onChange) {
  const box = el('div', 'picker');
  const q = el('input', 'picker-search'); q.placeholder = 'Search friends…'; q.autocomplete = 'off';
  const ul = el('ul');
  const draw = () => {
    ul.replaceChildren();
    const term = q.value.trim().toLowerCase();
    const shown = people.filter(u => !term || u.name.toLowerCase().includes(term) || u.username.toLowerCase().includes(term));
    if (!shown.length) ul.append(el('p', 'empty', people.length ? 'No friends match.' : 'No friends to pick yet.'));
    shown.forEach(u => {
      const li = el('li', 'row pick'), lab = el('label', 'who');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = selected.has(u.username);
      cb.onchange = () => { if (cb.checked) selected.add(u.username); else selected.delete(u.username); onChange(); };
      const txt = el('div', 'txt'); txt.append(el('span', 'title', u.name), el('small', '', '@' + u.username));
      lab.append(cb, avatarEl(u, 'sm'), txt); li.append(lab); ul.append(li);
    });
  };
  q.oninput = draw; draw();
  box.append(q, ul);
  return box;
}

function openNewGroup() {
  const selected = new Set();
  const name = el('input', 'field'); name.placeholder = 'Group name (optional)'; name.maxLength = 100;
  const err = el('p', 'error'), count = el('small', 'count');
  const create = el('button', '', 'Create group'); create.type = 'button';
  const update = () => {
    count.textContent = selected.size + ' selected' + (selected.size < 2 ? ' · pick at least 2 friends' : '');
    create.disabled = selected.size < 2;
  };
  create.onclick = async () => {
    create.disabled = true; err.textContent = '';
    try {
      const c = await api('/api/conversations/', {method: 'POST',
        body: JSON.stringify({type: 'group', title: name.value, usernames: [...selected]})});
      closeModal(); showTab('chats');
      await loadConvs().catch(() => {});
      openConv(c.id, c.title);
    } catch (e) { err.textContent = e.message || "Couldn't create the group."; update(); }
  };
  const foot = el('div', 'foot'); foot.append(count, create);
  openModal('New group', name, picker(friendsState.friends, selected, update), err, foot);
  update();
}

function openGroupInfo() {
  const c = curConv();
  if (!c || c.type !== 'group') return;
  const cid = c.id, MAX = 50;
  const refresh = async () => { await loadConvs().catch(() => {}); if (current === cid) openGroupInfo(); };
  const err = el('p', 'error');
  const run = async (fn) => { err.textContent = ''; try { await fn(); } catch (e) { err.textContent = e.message || 'That did not work.'; } };

  const name = el('input', 'field'); name.value = c.custom_title || ''; name.placeholder = c.title; name.maxLength = 100;
  const save = el('button', '', 'Save'); save.type = 'button'; save.disabled = true;
  name.oninput = () => { save.disabled = name.value.trim() === (c.custom_title || ''); };
  save.onclick = () => run(async () => {
    await api(`/api/conversations/${cid}/title/`, {method: 'POST', body: JSON.stringify({title: name.value})});
    await refresh();
  });
  const nameRow = el('div', 'inline'); nameRow.append(name, save);

  const list = el('ul');
  c.members.forEach(u => {
    const li = el('li', 'row'), a = el('a', 'who'); a.href = profileUrl(u.username);
    const txt = el('div', 'txt'); txt.append(el('span', 'title', u.name + (u.username === ME ? ' (you)' : '')), el('small', '', '@' + u.username));
    a.append(avatarWithDot(u, u.username === ME ? null : u.username), txt); li.append(a); list.append(li);
  });

  const inGroup = new Set(c.members.map(m => m.username));
  const candidates = friendsState.friends.filter(f => !inGroup.has(f.username));
  const add = [];
  if (c.members.length >= MAX) add.push(el('p', 'empty', 'This group is full (' + MAX + ' people).'));
  else if (!candidates.length) add.push(el('p', 'empty', 'All your friends are already in this group.'));
  else {
    const selected = new Set();
    const btn = el('button', '', 'Add selected'); btn.type = 'button'; btn.disabled = true;
    add.push(picker(candidates, selected, () => { btn.disabled = !selected.size; }), btn);
    btn.onclick = () => run(async () => {
      btn.disabled = true;
      await api(`/api/conversations/${cid}/members/`, {method: 'POST', body: JSON.stringify({usernames: [...selected]})});
      await refresh();
    });
  }

  const leave = el('button', 'danger', 'Leave group'); leave.type = 'button';
  const leaveBox = el('div', 'leave'); leaveBox.append(leave);
  leave.onclick = () => {
    leaveBox.replaceChildren(el('span', '', 'Leave this group? You will stop receiving its messages.'));
    const yes = el('button', 'danger', 'Yes, leave'); yes.type = 'button';
    const no = el('button', 'secondary', 'Cancel'); no.type = 'button';
    no.onclick = openGroupInfo;
    yes.onclick = () => run(async () => {
      await api(`/api/conversations/${cid}/leave/`, {method: 'POST', body: '{}'});
      closeModal(); await loadConvs().catch(() => {});
    });
    leaveBox.append(yes, no);
  };

  openModal('Group info', el('h4', '', 'Name'), nameRow,
    el('h4', '', 'Members · ' + c.members.length), list,
    el('h4', '', 'Add people'), ...add, err, leaveBox);
  modalFor = cid;
}
$('new-group').onclick = openNewGroup;
$('chat-info').onclick = openGroupInfo;
$('chat-back').onclick = () => document.querySelector('.layout').classList.remove('open');  // phones: back to the chat list

// ---- search --------------------------------------------------------------------------
function statusOf(u) {  // trust live friend data over the moment the search ran
  const f = friendsState;
  if (f.friends.some(x => x.username === u.username)) return 'friends';
  if (f.incoming.some(x => x.username === u.username)) return 'incoming';
  if (f.outgoing.some(x => x.username === u.username)) return 'outgoing';
  return 'none';
}
function renderResults() {
  const box = $('results'); box.replaceChildren();
  results.forEach(u => box.append(personRow(u, buttonsFor(u, statusOf(u)))));
}
let st;
$('search').oninput = e => {
  clearTimeout(st);
  const q = e.target.value.trim();
  if (!q) { results = []; renderResults(); return; }
  st = setTimeout(async () => {
    try {
      const {users} = await api('/api/users/search/?q=' + encodeURIComponent(q));
      if (e.target.value.trim() !== q) return;  // a newer search is already on its way
      results = users; renderResults();
    } catch (err) {}
  }, 250);
};

function openFromUrl() {  // /app/?c=<id> comes from the "Message" button on a profile page
  const id = Number(new URLSearchParams(location.search).get('c'));
  if (!id) return;
  history.replaceState(null, '', location.pathname);
  const c = convList.find(x => x.id === id);
  if (c) openConv(c.id, c.title);
}

document.addEventListener('visibilitychange', syncRead);
window.addEventListener('focus', syncRead);
setInterval(() => wsSend({type: 'ping'}), 25000);

connect();
loadConvs().then(openFromUrl).catch(() => {});
