"""One-to-one call state and the rules around it. Nothing here touches media: the browsers send
that to each other directly (WebRTC), and the server only relays the set-up messages.

A call is stored in the cache under its id, and each user has a pointer to the call they are in, so
a person can only be in one call at a time (across all their open tabs). Records expire on their own,
so a crashed browser or restarted server can never leave someone stuck as "busy".
"""
import base64
import hashlib
import hmac
import re
import threading
import time

from django.conf import settings
from django.core.cache import cache

RING_SECONDS = 45          # how long the callee's phone "rings"
RING_GRACE = 15            # slack so a late "missed" from the caller still finds the call
ACTIVE_TTL = 6 * 60 * 60   # longest call we keep track of
SIGNAL_LIMIT = 20000       # bytes of JSON per set-up message (a session description is ~5 KB)

RINGING, ACTIVE = "ringing", "active"
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_lock = threading.Lock()


def valid_call_id(value):
    return isinstance(value, str) and bool(_ID.match(value))


def _ckey(call_id):
    return f"vibechat:call:{call_id}"


def _ukey(uid):
    return f"vibechat:usercall:{uid}"


def _drop(rec):
    cache.delete(_ckey(rec["id"]))
    for uid in (rec["caller"], rec["callee"]):
        if cache.get(_ukey(uid)) == rec["id"]:
            cache.delete(_ukey(uid))


def get(call_id):
    """The live record for a call id, or None (also None once an unanswered call has timed out)."""
    rec = cache.get(_ckey(call_id))
    if rec and rec["state"] == RINGING and rec["ring_until"] < time.time():
        _drop(rec)
        return None
    return rec


def current(uid):
    """The call this user is in right now, or None."""
    call_id = cache.get(_ukey(uid))
    if not call_id:
        return None
    rec = get(call_id)
    if rec is None:
        cache.delete(_ukey(uid))
    return rec


def start(caller, callee, conversation, call_id, video, channel):
    """Register a new ringing call. Returns (record, None) or (None, reason).

    reason is "in_call" when the caller is already in a call (maybe in another tab),
    "busy" when the other person is, "exists" when the id is taken.
    """
    with _lock:
        if get(call_id):
            return None, "exists"
        if current(caller):
            return None, "in_call"
        if current(callee):
            return None, "busy"
        ttl = RING_SECONDS + RING_GRACE
        # add() is atomic in every cache backend, which also guards against two server processes racing
        if not cache.add(_ukey(caller), call_id, ttl):
            return None, "in_call"
        if not cache.add(_ukey(callee), call_id, ttl):
            cache.delete(_ukey(caller))
            return None, "busy"
        rec = {"id": call_id, "conversation": conversation, "caller": caller, "callee": callee,
               "video": bool(video), "state": RINGING, "ring_until": time.time() + RING_SECONDS,
               "chans": {str(caller): channel}, "started": time.time()}
        cache.set(_ckey(call_id), rec, ttl)
        return rec, None


def accept(call_id, uid, channel):
    """The callee picks up. Returns the record, or None if it is gone, not theirs, or already answered."""
    with _lock:
        rec = get(call_id)
        if not rec or rec["callee"] != uid or rec["state"] != RINGING:
            return None
        rec["state"] = ACTIVE
        rec["chans"][str(uid)] = channel
        cache.set(_ckey(call_id), rec, ACTIVE_TTL)
        for u in (rec["caller"], rec["callee"]):
            cache.set(_ukey(u), call_id, ACTIVE_TTL)
        return rec


def end(call_id, uid):
    """Either person ends the call. Returns (record, reason) or (None, None) if it is not theirs.

    reason: "cancelled" (caller hung up while ringing), "declined" (callee refused), "hangup".
    """
    with _lock:
        rec = get(call_id)
        if not rec or uid not in (rec["caller"], rec["callee"]):
            return None, None
        if rec["state"] == ACTIVE:
            reason = "hangup"
        else:
            reason = "cancelled" if uid == rec["caller"] else "declined"
        _drop(rec)
        return rec, reason


def drop_for_channel(uid, channel):
    """A connection closed: end the call it was carrying, if any. Returns the record or None."""
    with _lock:
        rec = current(uid)
        if not rec or rec["chans"].get(str(uid)) != channel:
            return None
        _drop(rec)
        return rec


def other_party(rec, uid):
    return rec["callee"] if uid == rec["caller"] else rec["caller"]


def signal_target(call_id, uid, channel):
    """Channel that should receive `uid`'s set-up message, or None if it must be refused.

    Only the two people in an answered call may signal, and only from the connection that is carrying
    the call, so a stray tab (or anyone else) cannot inject anything.
    """
    rec = get(call_id)
    if not rec or rec["state"] != ACTIVE or rec["chans"].get(str(uid)) != channel:
        return None
    return rec["chans"].get(str(other_party(rec, uid)))


def ice_servers(user):
    """STUN always; TURN when configured, with short-lived credentials if a shared secret is set."""
    servers = [{"urls": settings.CALL_STUN_URLS}]
    turn = settings.CALL_TURN_URLS
    if turn:
        if settings.CALL_TURN_SECRET:  # coturn "use-auth-secret": time-limited username, HMAC password
            username = f"{int(time.time()) + 3600}:{user.pk}"
            digest = hmac.new(settings.CALL_TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
            servers.append({"urls": turn, "username": username, "credential": base64.b64encode(digest).decode()})
        elif settings.CALL_TURN_USERNAME:
            servers.append({"urls": turn, "username": settings.CALL_TURN_USERNAME,
                            "credential": settings.CALL_TURN_CREDENTIAL})
    return servers
