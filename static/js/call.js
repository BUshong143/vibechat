// One-to-one video calls. The set-up messages travel over the chat WebSocket (app.js);
// the audio and video go straight between the two browsers (WebRTC).
// Loaded after app.js, which provides $, api, wsSend, toast, convList and current.
const RING_MS = 45000, DISCONNECT_MS = 10000;
let call = null;   // {id, conv, peer, name, role: 'caller'|'callee', state, pc, stream, ...}
let ringtone = null;

const callId = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36).padEnd(8, '0'));

// ---- small helpers ---------------------------------------------------------------------
function callUi(state, text) {
  const on = !!call;
  $('call').hidden = !on;
  if (!on) return;
  call.state = state || call.state;
  $('call-name').textContent = call.name;
  $('call-state').textContent = text || '';
  const ringing = call.role === 'callee' && call.state === 'ringing';
  $('call-accept').hidden = !ringing;
  $('call-mic').hidden = $('call-cam').hidden = ringing || !call.stream;
  const live = call.state === 'active';
  $('call-share').hidden = !live || !(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  $('call-share').textContent = call.sharing ? 'Stop sharing' : 'Share screen';
  $('call-watch').hidden = !live;
  if (typeof syncCallMini === 'function') syncCallMini();
  $('call-end').textContent = ringing ? 'Decline' : (call.state === 'calling' ? 'Cancel' : 'Hang up');
  $('call').classList.toggle('live', call.state === 'active');
}

function ring(on) {  // a soft two-tone ring; browsers may refuse to play sound before the first click, which is fine
  try {
    if (!on) { if (ringtone) { clearInterval(ringtone.t); ringtone.ctx.close(); ringtone = null; } return; }
    if (ringtone) return;
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const beep = () => [0, 0.25].forEach((d, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = i ? 660 : 520; g.gain.value = 0.05;
      o.connect(g); g.connect(ctx.destination); o.start(ctx.currentTime + d); o.stop(ctx.currentTime + d + 0.2);
    });
    beep(); ringtone = {ctx, t: setInterval(beep, 2000)};
  } catch (e) { ringtone = null; }
}

async function iceConfig() {
  try { return (await api('/api/calls/config/')).iceServers; }
  catch (e) { return [{urls: ['stun:stun.l.google.com:19302']}]; }
}

async function getMedia() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error('Calls need a secure connection (https) or localhost.');
  }
  try {
    return await navigator.mediaDevices.getUserMedia({audio: true, video: {width: {ideal: 1280}, height: {ideal: 720}}});
  } catch (e) {
    const denied = e && (e.name === 'NotAllowedError' || e.name === 'SecurityError');
    const none = e && (e.name === 'NotFoundError' || e.name === 'OverconstrainedError');
    throw new Error(denied ? 'Allow camera and microphone access to make calls.'
      : none ? 'No camera or microphone found.' : 'Could not start your camera or microphone.');
  }
}

function sendSignal(data) {
  if (call) wsSend({type: 'call.signal', call_id: call.id, data});
}

// Stop everything and hide the call screen. `message` is shown as a toast when given.
function endCallLocally(message) {
  const c = call; call = null;
  ring(false);
  if (c) {
    stopShareLocal(c);
    clearTimeout(c.ringTimer); clearTimeout(c.dropTimer);
    if (c.pc) { c.pc.onicecandidate = c.pc.ontrack = c.pc.onconnectionstatechange = null; c.pc.close(); }
    if (c.stream) c.stream.getTracks().forEach(t => t.stop());
  }
  $('call-remote').srcObject = null; $('call-local').srcObject = null;
  $('call').hidden = true; $('call').classList.remove('live', 'mini', 'sharing', 'mysharing');
  if (typeof syncCallMini === 'function') syncCallMini();
  $('call-mic').textContent = 'Mute'; $('call-cam').textContent = 'Camera off';
  if (message) toast(message);
}

// ---- WebRTC ----------------------------------------------------------------------------
async function makePeer() {
  const pc = new RTCPeerConnection({iceServers: await iceConfig()});
  call.pc = pc; call.pendingIce = [];
  call.stream.getTracks().forEach(t => pc.addTrack(t, call.stream));
  pc.onicecandidate = e => { if (e.candidate) sendSignal({candidate: e.candidate.toJSON()}); };
  pc.ontrack = e => {
    const v = $('call-remote');
    if (v.srcObject !== e.streams[0]) v.srcObject = e.streams[0];
  };
  pc.onconnectionstatechange = () => {
    if (!call || call.pc !== pc) return;
    clearTimeout(call.dropTimer);
    if (pc.connectionState === 'connected') callUi('active', 'Connected');
    else if (pc.connectionState === 'disconnected') {
      callUi(call.state, 'Connection lost, trying to reconnect…');
      call.dropTimer = setTimeout(() => hangUp('Call ended: the connection was lost.'), DISCONNECT_MS);
    } else if (pc.connectionState === 'failed') {
      hangUp("Couldn't connect the call. A firewall may be blocking it (see README: TURN server).");
    }
  };
  return pc;
}

async function flushIce() {
  const q = call.pendingIce; call.pendingIce = [];
  for (const c of q) { try { await call.pc.addIceCandidate(c); } catch (e) { /* stale candidate */ } }
}

async function onSignal(ev) {
  if (!call || call.id !== ev.call_id) return;
  const d = ev.data || {};
  if (typeof d.share === 'boolean') { $('call').classList.toggle('sharing', d.share); return; }  // the other side shows a screen
  try {
    if (d.sdp && d.sdp.type === 'offer' && call.role === 'callee') {
      const pc = call.pc || await makePeer();
      await pc.setRemoteDescription(d.sdp);
      await flushIce();
      await pc.setLocalDescription(await pc.createAnswer());
      sendSignal({sdp: {type: pc.localDescription.type, sdp: pc.localDescription.sdp}});
    } else if (d.sdp && d.sdp.type === 'answer' && call.role === 'caller' && call.pc) {
      await call.pc.setRemoteDescription(d.sdp);
      await flushIce();
    } else if (d.candidate) {
      if (call.pc && call.pc.remoteDescription) { try { await call.pc.addIceCandidate(d.candidate); } catch (e) { /* stale */ } }
      else if (call.pendingIce) call.pendingIce.push(d.candidate);
      else call.pendingIce = [d.candidate];
    }
  } catch (e) {
    console.error(e);
    hangUp('The call could not be set up.');
  }
}

// ---- starting, answering, ending --------------------------------------------------------
async function startCall() {
  const c = curConv();
  if (call || !c || !c.other || !c.can_send) return;
  if (!ws || ws.readyState !== 1) { toast('You are offline. Try again when reconnected.'); return; }
  let stream;
  try { stream = await getMedia(); } catch (e) { toast(e.message); return; }  // ask before ringing anyone
  if (call) { stream.getTracks().forEach(t => t.stop()); return; }
  call = {id: callId(), conv: c.id, peer: c.other, name: c.title, role: 'caller', state: 'calling', stream};
  $('call-local').srcObject = stream;
  callUi('calling', 'Calling…');
  if (!wsSend({type: 'call.invite', conversation: c.id, call_id: call.id, video: true})) { endCallLocally('You are offline.'); return; }
  call.ringTimer = setTimeout(() => {  // nobody picked up
    if (!call || call.state !== 'calling' && call.state !== 'ringing') return;
    wsSend({type: 'call.end', call_id: call.id, reason: 'missed'});
  }, RING_MS);
}

async function answerCall() {
  if (!call || call.role !== 'callee' || call.state !== 'ringing') return;
  const mine = call;
  ring(false);
  let stream;
  try { stream = await getMedia(); }
  catch (e) { wsSend({type: 'call.end', call_id: mine.id}); if (call === mine) endCallLocally(e.message); return; }
  if (call !== mine) { stream.getTracks().forEach(t => t.stop()); return; }  // it ended while we asked for permission
  call.stream = stream;
  $('call-local').srcObject = stream;
  callUi('connecting', 'Connecting…');
  wsSend({type: 'call.accept', call_id: call.id});
}

function hangUp(message) {
  if (call) wsSend({type: 'call.end', call_id: call.id});
  endCallLocally(typeof message === 'string' ? message : '');
}

// Events from the server (dispatched by handle() in app.js).
async function callEvent(ev) {
  if (ev.type === 'call.incoming') {
    if (call) return;  // the server never rings someone who is busy; this is just a safety net
    call = {id: ev.call_id, conv: ev.conversation, peer: ev.user, name: ev.name, role: 'callee', state: 'ringing'};
    callUi('ringing', 'Incoming video call…');
    ring(true);
    call.ringTimer = setTimeout(() => { if (call && call.id === ev.call_id && call.state === 'ringing') endCallLocally(); }, (ev.ring || 45) * 1000 + 3000);
  } else if (ev.type === 'call.ringing') {
    if (call && call.id === ev.call_id) callUi('ringing', 'Ringing…');
  } else if (ev.type === 'call.accepted') {
    if (!call || call.id !== ev.call_id) return;
    if (call.role === 'callee' && call.state === 'ringing') { endCallLocally('Answered on another device.'); return; }
    if (call.role === 'caller') {
      clearTimeout(call.ringTimer);
      callUi('connecting', 'Connecting…');
      try {
        const pc = await makePeer();
        await pc.setLocalDescription(await pc.createOffer());
        sendSignal({sdp: {type: pc.localDescription.type, sdp: pc.localDescription.sdp}});
      } catch (e) { console.error(e); hangUp('The call could not be set up.'); }
    }
  } else if (ev.type === 'call.signal') {
    await onSignal(ev);
  } else if (ev.type === 'call.ended') {
    if (!call || call.id !== ev.call_id) return;
    const was = call, who = was.name;
    const msg = {declined: who + ' declined the call.', hangup: 'Call ended.', dropped: 'Call ended: ' + who + ' lost connection.',
      cancelled: was.role === 'callee' ? 'Missed call from ' + who + '.' : '',
      missed: was.role === 'callee' ? 'Missed call from ' + who + '.' : who + " didn't answer."}[ev.reason] || 'Call ended.';
    endCallLocally(msg);
  } else if (ev.type === 'call.failed') {
    if (!call || call.id !== ev.call_id) return;
    const who = call.name;
    const msg = {offline: who + ' is offline.', busy: who + ' is on another call.', in_call: 'You are already in a call (maybe in another tab).',
      not_friends: 'You can only call friends.', group: 'Calls are one-to-one for now.',
      gone: 'That call is no longer available.'}[ev.reason] || "The call couldn't be started.";
    endCallLocally(msg);
  }
}

// Server-side call state is tied to this connection, so if the WebSocket drops the call is over.
function callSocketClosed() { if (call) endCallLocally('Call ended: you lost your connection.'); }

$('chat-call').onclick = startCall;
$('call-accept').onclick = answerCall;
$('call-end').onclick = () => hangUp();
$('call-mic').onclick = () => {
  if (!call || !call.stream) return;
  const t = call.stream.getAudioTracks()[0]; if (!t) return;
  t.enabled = !t.enabled; $('call-mic').textContent = t.enabled ? 'Mute' : 'Unmute';
};
$('call-cam').onclick = () => {
  if (!call || !call.stream) return;
  const t = call.stream.getVideoTracks()[0]; if (!t) return;
  t.enabled = !t.enabled; $('call-cam').textContent = t.enabled ? 'Camera off' : 'Camera on';
};

// ---- screen sharing ---------------------------------------------------------------------------
// For anything that cannot be embedded (Netflix, Twitch, a local file...). The screen replaces the camera
// in the existing connection, and the shared tab's sound is mixed with your microphone, so no new
// negotiation is needed and you can keep talking. Share the tab that plays the video, not this one.
function stopShareLocal(c) {
  const sh = c && c.sharing; if (!sh) return;
  c.sharing = null;
  sh.disp.getTracks().forEach(t => { t.onended = null; t.stop(); });
  if (sh.ctx) sh.ctx.close().catch(() => {});
}

async function toggleShare() {
  if (!call || call.state !== 'active' || !call.pc) return;
  if (call.sharing) { await stopShare(true); return; }
  let disp;
  try { disp = await navigator.mediaDevices.getDisplayMedia({video: true, audio: true}); }
  catch (e) { return; }  // cancelled
  const mine = call;
  const vTrack = disp.getVideoTracks()[0];
  const senders = mine.pc.getSenders();
  const vs = senders.find(s => s.track && s.track.kind === 'video'), as = senders.find(s => s.track && s.track.kind === 'audio');
  if (!vTrack || !vs) { disp.getTracks().forEach(t => t.stop()); return; }
  let ctx = null;
  try {
    await vs.replaceTrack(vTrack);
    if (disp.getAudioTracks().length && as) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const dest = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(new MediaStream(mine.stream.getAudioTracks())).connect(dest);
      ctx.createMediaStreamSource(new MediaStream(disp.getAudioTracks())).connect(dest);
      await as.replaceTrack(dest.stream.getAudioTracks()[0]);
    }
  } catch (e) {
    console.error(e); disp.getTracks().forEach(t => t.stop()); if (ctx) ctx.close().catch(() => {});
    try { await vs.replaceTrack(mine.stream.getVideoTracks()[0]); if (as) await as.replaceTrack(mine.stream.getAudioTracks()[0]); } catch (e2) { /* call ending anyway */ }
    toast("Couldn't share your screen."); return;
  }
  if (call !== mine) { disp.getTracks().forEach(t => t.stop()); if (ctx) ctx.close().catch(() => {}); return; }  // call ended meanwhile
  mine.sharing = {disp, ctx, vs, as};
  vTrack.onended = () => stopShare(true);  // the browser's own "Stop sharing" bar
  $('call-local').srcObject = disp; $('call').classList.add('mysharing');
  sendSignal({share: true});
  callUi(mine.state, $('call-state').textContent);
  if (!disp.getAudioTracks().length) toast('Sharing without sound. To share sound, pick a browser tab and tick "Share tab audio".');
}

async function stopShare(notify) {
  const c = call; if (!c || !c.sharing) return;
  const {vs, as} = c.sharing;
  stopShareLocal(c);
  try {
    if (vs) await vs.replaceTrack(c.stream.getVideoTracks()[0]);
    if (as) await as.replaceTrack(c.stream.getAudioTracks()[0]);
  } catch (e) { /* the connection is going away */ }
  if (call !== c) return;
  $('call-local').srcObject = c.stream; $('call').classList.remove('mysharing');
  if (notify) sendSignal({share: false});
  callUi(c.state, $('call-state').textContent);
}

$('call-share').onclick = toggleShare;
