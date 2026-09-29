// static/js/watch.js
// Watch together: one shared video per one-to-one chat (YouTube, Vimeo or a direct video file).
// Play / pause / seek are relayed over the chat WebSocket (app.js); each browser plays the video itself.
// Loaded after app.js and call.js. Anything else (Netflix, Twitch...) is shown with "Share screen" in a call.
const SEEK_TOL_PLAYING = 2, SEEK_TOL_PAUSED = 1, HOLD_MS = 1500, POLL_MS = 500;
const watchSessions = new Map();   // conversation id -> latest session the server told us about
let W = null;                      // the open watch screen: {conv, key, adapter, engine, timer, loadId, host}
const scriptLoads = {};

// ---- sync engine ---------------------------------------------------------------------------
// Keeps a model of what the shared video should be doing. The player is polled; a difference from the
// model that the *person* caused (pressed play/pause, dragged the bar) is sent to the friend, while a
// command that *we* just applied is given time to take effect and is never echoed back.
class SyncEngine {
  constructor(adapter, emit, onBlocked, now) {
    this.a = adapter; this.emit = emit; this.onBlocked = onBlocked; this.now = now || (() => performance.now());
    this.model = {playing: false, time: 0, at: this.now()};
    this.hold = 0; this.remote = false; this.blocked = false; this.busy = false;
  }
  setBlocked(v) { if (v !== this.blocked) { this.blocked = v; this.onBlocked(v); } }

  async apply(s) {                       // the friend (or the server) says: be here, doing this
    this.busy = true;
    try {
      const t = this.now();
      this.model = {playing: !!s.playing, time: s.position, at: t};
      this.hold = t + HOLD_MS; this.remote = true; this.setBlocked(false);
      const cur = await this.a.time();
      if (Math.abs(cur - s.position) > 1) await this.a.seek(s.position);
      if (s.playing) { try { await this.a.play(); } catch (e) { /* autoplay refused; tick() notices */ } }
      else await this.a.pause();
    } finally { this.busy = false; }
  }

  async resume() {                       // the "tap to join playback" button
    const t = this.now(), m = this.model;
    await this.apply({playing: m.playing, position: m.playing ? m.time + (t - m.at) / 1000 : m.time});
  }

  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      const playing = await this.a.playing(), time = await this.a.time();
      const t = this.now(), m = this.model;
      if (playing === null) {            // buffering: not the person's doing, so just move the model along
        this.model = {playing: m.playing, time, at: t};
        return;
      }
      if (t < this.hold) return;         // a command we applied is still taking effect
      const expected = m.playing ? m.time + (t - m.at) / 1000 : m.time;
      const tol = m.playing ? SEEK_TOL_PLAYING : SEEK_TOL_PAUSED;
      if (playing === m.playing) {
        if (this.blocked) {              // they pressed play themselves: catch up to where the friend is now
          this.remote = false; this.setBlocked(false); this.hold = t + HOLD_MS;
          if (Math.abs(time - expected) > 1) await this.a.seek(expected);
          return;
        }
        this.remote = false;
        if (Math.abs(time - expected) > tol) this.send(playing, time, t);   // dragged the bar
        return;
      }
      if (this.remote && m.playing) { this.setBlocked(true); return; }     // told to play, browser refused
      this.send(playing, time, t);                                          // pressed play or pause
    } finally { this.busy = false; }
  }

  send(playing, time, t) {
    this.model = {playing, time, at: t}; this.remote = false;
    this.emit(playing, Math.round(time * 100) / 100);
  }
}

// ---- players ---------------------------------------------------------------------------------
// Every adapter offers: play(), pause(), seek(s), time(), playing() (null while buffering), destroy().
function loadScript(src) {
  return scriptLoads[src] || (scriptLoads[src] = new Promise((res, rej) => {
    const s = document.createElement('script'); s.src = src; s.onload = res;
    s.onerror = () => { delete scriptLoads[src]; rej(new Error('Could not load the video player. Check your connection or ad blocker.')); };
    document.head.append(s);
  }));
}
function loadYouTube() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  return new Promise((res, rej) => {
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (prev) prev(); res(); };
    loadScript('https://www.youtube.com/iframe_api').catch(rej);
  });
}

async function youtubeAdapter(host, id, onError) {
  await loadYouTube();
  return new Promise(resolve => {
    const p = new YT.Player(host, {
      width: '100%', height: '100%', videoId: id,
      playerVars: {playsinline: 1, rel: 0, origin: location.origin},
      events: {
        onReady: () => resolve({
          play: () => p.playVideo(), pause: () => p.pauseVideo(), seek: t => p.seekTo(t, true),
          time: () => p.getCurrentTime() || 0,
          playing: () => { const s = p.getPlayerState(); return s === 3 ? null : s === 1; },
          destroy: () => { try { p.destroy(); } catch (e) { /* already gone */ } },
        }),
        onError: e => onError([101, 150].includes(e.data) ? "The owner doesn't allow this video to be embedded. Try Share screen in a call instead."
          : [100].includes(e.data) ? 'This video is unavailable.' : 'The video could not be played.'),
      },
    });
  });
}

async function vimeoAdapter(host, ref, onError) {
  await loadScript('https://player.vimeo.com/api/player.js');
  const [id, hash] = ref.split('/');
  const f = document.createElement('iframe');
  f.src = 'https://player.vimeo.com/video/' + encodeURIComponent(id) + (hash ? '?h=' + encodeURIComponent(hash) : '');
  f.allow = 'autoplay; fullscreen; picture-in-picture'; f.allowFullscreen = true;
  host.replaceChildren(f);
  const p = new Vimeo.Player(f);
  p.on('error', () => onError('This Vimeo video could not be played.'));
  await p.ready();
  return {
    play: () => p.play(), pause: () => p.pause(), seek: t => p.setCurrentTime(t),
    time: () => p.getCurrentTime(), playing: async () => !(await p.getPaused()),
    destroy: () => { try { p.destroy(); } catch (e) { /* already gone */ } },
  };
}

async function fileAdapter(host, url, onError) {
  const v = document.createElement('video');
  v.controls = true; v.playsInline = true; v.preload = 'metadata'; v.src = url;
  v.onerror = () => onError("This video file can't be played (the link may be wrong, or the site blocks other pages from using it).");
  host.replaceChildren(v);
  return {
    play: () => v.play(), pause: () => v.pause(), seek: t => { v.currentTime = t; },
    time: () => v.currentTime || 0, playing: () => !v.paused && !v.ended,
    destroy: () => { v.pause(); v.removeAttribute('src'); v.load(); },
  };
}

function makeAdapter(session, host, onError) {
  if (session.provider === 'youtube') return youtubeAdapter(host, session.ref, onError);
  if (session.provider === 'vimeo') return vimeoAdapter(host, session.ref, onError);
  return fileAdapter(host, session.ref, onError);
}

// ---- the watch screen ------------------------------------------------------------------------
function setWatchMsg(text) { $('watch-msg').textContent = text || ''; }

function syncWatchButton() {
  const c = curConv();
  const b = $('chat-watch');
  b.hidden = !c || c.type === 'group' || !c.can_send;
  const live = !!(c && watchSessions.has(c.id)), label = live ? 'Join video' : 'Watch together';
  b.setAttribute('aria-label', label); b.title = label; b.classList.toggle('live', live);
}

// Shrinks the call to a corner tile while a video is on screen.
function syncCallMini() {
  const inCall = !!call && (call.state === 'active' || call.state === 'connecting');
  const mini = !!W && inCall;
  $('call').classList.toggle('mini', mini);
  if (!mini) { const st = $('call').style; st.left = st.top = st.right = ''; }   // forget where the tile was dragged
  $('watch-call').hidden = !W || !!call || !curConv();
}

function openWatch(cid) {
  const c = convList.find(x => x.id === cid);
  if (!c || c.type === 'group') return;
  if (current !== cid) openConv(c.id, c.title);
  if (!W || W.conv !== cid) { closeWatch(); W = {conv: cid, key: null, adapter: null, engine: null, timer: null, loadId: 0}; }
  $('watch').hidden = false; $('watch-title').textContent = 'Watching with ' + c.title;
  $('watch-stop').hidden = !watchSessions.has(cid);
  setWatchMsg(watchSessions.has(cid) ? 'Loading…' : 'Paste a link to start watching together.');
  const s = watchSessions.get(cid);
  if (s) watchLoad(s); else wsSend({type: 'watch.sync', conversation: cid});
  syncCallMini();
  if (!s) $('watch-url').focus();
}

function destroyPlayer() {
  if (!W) return;
  clearInterval(W.timer); W.timer = null; W.loadId++;
  if (W.adapter) W.adapter.destroy();
  W.adapter = W.engine = W.key = null;
  $('watch-stage').replaceChildren(); $('watch-resume').hidden = true;
}

function closeWatch() {
  if (W) destroyPlayer();
  W = null;
  $('watch').hidden = true; $('watch-url').value = '';
  syncCallMini();
}

async function watchLoad(session) {
  if (!W) return;
  const key = session.provider + ':' + session.ref;
  $('watch-stop').hidden = false;
  if (W.key === key && W.engine) { setWatchMsg(''); W.engine.apply(session); return; }   // same video: just catch up
  destroyPlayer();
  W.key = key;
  const id = ++W.loadId, mine = W;
  const host = document.createElement('div'); host.id = 'watch-player';
  $('watch-stage').replaceChildren(host);
  setWatchMsg('Loading…');
  try {
    const adapter = await makeAdapter(session, host, msg => { if (W === mine && W.loadId === id) setWatchMsg(msg); });
    if (W !== mine || W.loadId !== id) { adapter.destroy(); return; }   // closed or replaced while loading
    W.adapter = adapter;
    W.engine = new SyncEngine(adapter,
      (playing, position) => wsSend({type: 'watch.state', conversation: mine.conv, playing, position}),
      blocked => { $('watch-resume').hidden = !blocked; });
    setWatchMsg('');
    await W.engine.apply(session);
    W.timer = setInterval(() => { if (W && W.engine) W.engine.tick().catch(() => {}); }, POLL_MS);
  } catch (e) {
    if (W === mine && W.loadId === id) setWatchMsg(e.message || 'The video could not be loaded.');
  }
}

function startWatching() {
  const url = $('watch-url').value.trim();
  if (!W || !url) return;
  setWatchMsg('Checking the link…');
  if (!wsSend({type: 'watch.start', conversation: W.conv, url})) setWatchMsg('You are offline. Try again when reconnected.');
}

const WATCH_ERRORS = {
  bad_url: "That doesn't look like a YouTube, Vimeo or direct video link (.mp4, .webm…). For Netflix, Twitch and other sites, start a call and use Share screen.",
  not_friends: 'You can only watch with friends.',
  group: 'Watching together works in one-to-one chats for now.',
};

// Events from the server (dispatched by handle() in app.js).
function watchEvent(ev) {
  const cid = ev.conversation;
  if (ev.type === 'watch.started') {
    watchSessions.set(cid, ev); syncWatchButton();
    if (W && W.conv === cid) watchLoad(ev);
    else if (!ev.sync && ev.by !== ME) {
      if (call && call.peer === ev.by && call.state === 'active') openWatch(cid);   // already talking: just show it
      else toast(ev.name + ' started a video. Tap to join.', () => openWatch(cid));
    }
  } else if (ev.type === 'watch.state') {
    const s = watchSessions.get(cid);
    if (s) { s.playing = ev.playing; s.position = ev.position; }
    if (W && W.conv === cid && W.engine) W.engine.apply(ev);
  } else if (ev.type === 'watch.stopped' || ev.type === 'watch.none') {
    const hadSession = watchSessions.delete(cid); syncWatchButton();
    if (W && W.conv === cid) {
      // 'watch.none' is also the plain answer to "what is playing?" that openWatch() asks when the screen opens.
      // If nothing was playing and the screen is just waiting for a link, keep it open (closing it here made
      // the screen vanish right after pressing "Watch together"). Only close if a video really ended.
      if (ev.type === 'watch.none' && !hadSession && !W.key) return;
      closeWatch();
      if (ev.type === 'watch.stopped' && ev.by !== ME) toast(ev.name + ' stopped the video.');
    }
  } else if (ev.type === 'watch.failed') {
    if (W && W.conv === cid) setWatchMsg(WATCH_ERRORS[ev.reason] || "That didn't work.");
  }
}

// After a reconnect (or when a chat is opened) ask what is being watched, so nobody misses a start or a stop.
function watchResync(cid) { if (cid) wsSend({type: 'watch.sync', conversation: cid}); }

$('chat-watch').onclick = () => openWatch(current);
$('call-watch').onclick = () => { if (call) openWatch(call.conv); };
$('watch-call').onclick = () => startCall();
$('watch-go').onclick = startWatching;
$('watch-url').onkeydown = e => { if (e.key === 'Enter') startWatching(); };
$('watch-leave').onclick = closeWatch;
$('watch-stop').onclick = () => { if (W) wsSend({type: 'watch.stop', conversation: W.conv}); closeWatch(); };
$('watch-resume').onclick = () => { if (W && W.engine) W.engine.resume().catch(() => {}); };
