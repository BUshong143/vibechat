"""Watch together: one shared video per one-to-one chat, kept in sync between the two people.

The server never touches the video. It checks the link, remembers what is playing (so someone who
opens the chat later can join at the right moment) and relays play / pause / seek between the two people.
Supported links: YouTube, Vimeo and direct video files (.mp4, .webm ...). Anything else can be shown
by sharing a screen inside a call.
"""
import re
import time
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.cache import cache

TTL = 6 * 60 * 60          # a session is forgotten this long after its last play / pause / seek
MAX_URL = 500
MAX_POSITION = 24 * 60 * 60 * 10   # seconds; anything larger is nonsense
FILE_EXT = (".mp4", ".webm", ".ogv", ".ogg", ".m4v", ".mov")

YOUTUBE, VIMEO, FILE = "youtube", "vimeo", "file"
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_VIMEO_HOSTS = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIMEO_HASH = re.compile(r"^[0-9a-f]{8,16}$")


def _youtube(u):
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    vid = ""
    if host == "youtu.be":
        vid = parts[0] if parts else ""
    elif host in _YT_HOSTS:
        if u.path.rstrip("/") == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("embed", "shorts", "live", "v"):
            vid = parts[1]
    return vid if _YT_ID.match(vid) else None


def _vimeo(u):
    host = (u.hostname or "").lower()
    if host not in _VIMEO_HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if host == "player.vimeo.com":
        parts = parts[1:] if parts[:1] == ["video"] else []
    idx = next((i for i, p in enumerate(parts) if p.isdigit() and len(p) <= 12), None)
    if idx is None:
        return None
    vid = parts[idx]
    hashed = parts[idx + 1] if idx + 1 < len(parts) else (parse_qs(u.query).get("h") or [""])[0]
    return vid + ("/" + hashed if _VIMEO_HASH.match(hashed) else "")


def parse(url):
    """(provider, ref) for a supported link, else None. `ref` is a YouTube id, a Vimeo "id[/hash]" or a file URL."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > MAX_URL or re.search(r"\s", url):
        return None
    try:
        u = urlparse(url)
        u.port  # raises on a malformed port
    except ValueError:
        return None
    allowed = ("https", "http") if settings.DEBUG else ("https",)  # http media is blocked on https pages anyway
    if u.scheme not in allowed or not u.hostname or u.username or u.password:
        return None
    vid = _youtube(u)
    if vid:
        return YOUTUBE, vid
    ref = _vimeo(u)
    if ref:
        return VIMEO, ref
    if u.path.lower().endswith(FILE_EXT):
        return FILE, url
    return None


def _key(conversation):
    return f"vibechat:watch:{conversation}"


def start(conversation, user, provider, ref):
    rec = {"conversation": conversation, "provider": provider, "ref": ref, "by": user.username, "name": user.name,
           "playing": False, "position": 0.0, "at": time.time()}
    cache.set(_key(conversation), rec, TTL)
    return rec


def get(conversation):
    return cache.get(_key(conversation))


def update(conversation, playing, position):
    """Record a play / pause / seek. Returns the record, or None if nothing is being watched."""
    rec = get(conversation)
    if not rec:
        return None
    rec["playing"], rec["position"], rec["at"] = bool(playing), float(position), time.time()
    cache.set(_key(conversation), rec, TTL)
    return rec


def stop(conversation):
    rec = get(conversation)
    if rec:
        cache.delete(_key(conversation))
    return rec


def view(rec):
    """What clients receive. The position is moved on to now if the video is playing."""
    pos = rec["position"] + (time.time() - rec["at"] if rec["playing"] else 0)
    return {"provider": rec["provider"], "ref": rec["ref"], "by": rec["by"], "name": rec["name"],
            "playing": rec["playing"], "position": round(pos, 2)}


def valid_state(playing, position):
    return (isinstance(playing, bool) and isinstance(position, (int, float)) and not isinstance(position, bool)
            and 0 <= position <= MAX_POSITION)
