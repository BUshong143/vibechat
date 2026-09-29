import threading
import time

from django.core.cache import cache

TTL = 90
_lock = threading.Lock()


def _key(uid):
    return f"vibechat:presence:{uid}"


def _live(entries, now):
    return {channel: expires for channel, expires in (entries or {}).items() if expires > now}


def touch(uid, channel):
    now = time.time()
    with _lock:
        entries = _live(cache.get(_key(uid)), now)
        was_offline = not entries
        entries[channel] = now + TTL
        cache.set(_key(uid), entries, TTL * 2)
    return was_offline


def remove(uid, channel):
    now = time.time()
    with _lock:
        entries = _live(cache.get(_key(uid)), now)
        entries.pop(channel, None)
        if entries:
            cache.set(_key(uid), entries, TTL * 2)
        else:
            cache.delete(_key(uid))
    return not entries


def online_ids(uids):
    now = time.time()
    data = cache.get_many([_key(u) for u in uids])
    return {u for u in uids if _live(data.get(_key(u)), now)}
