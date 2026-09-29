# VibeChat — Milestone 2 (accounts + messenger + friends, profiles, group chats, video calls & watch together)

**Deploying?** See [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md) — the project is ready for Railway (`railway.toml`, WhiteNoise, healthcheck, Volume-backed photos).
```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```
Open http://127.0.0.1:8000, register two accounts (second one in a private window), search for the other person, send a friend request, accept it from the other window, then chat. For a group you need three accounts, all friends of the one creating it.
Uses SQLite by default; set `DATABASE_URL` (see `.env.example`) to use PostgreSQL. **After pulling this milestone run `python manage.py migrate` against every database you use** (adds `accounts.0002_user_avatar` and the new `social` app). Group chats need no migration.

## Friends & profiles
- **Requests:** send, accept, decline (recipient), cancel (sender), unfriend. Declining/cancelling deletes the request, so the person can be asked again. If two people ask each other at the same time they simply become friends.
- **Chat is friends-only.** A one-to-one chat can only be started with an accepted friend, and only friends can send messages in it (HTTP and WebSocket both check). After an unfriend the history stays readable but the composer locks. Group chats are not affected.
- **Profiles:** `/u/<username>/` shows avatar, display name, handle and the right action for your relationship (Add friend / Cancel / Accept-Decline / Message-Unfriend). `/profile/edit/` changes your display name and photo. Photos are square-cropped to 256×256 and re-encoded as JPEG (this also strips EXIF/GPS data); max upload 5 MB.
- **Live:** friend requests/acceptances update open tabs over the existing WebSocket (`friends.changed`), with a toast and a badge on the Friends tab. Friends also see each other's online status before any chat exists.

| Endpoint | Purpose |
|---|---|
| `GET /api/friends/` | `{friends, incoming, outgoing}` |
| `POST /api/friends/<username>/{request,accept,decline,cancel,remove}/` | change a relationship, returns `{status}` |
| `GET /api/users/search/?q=` | matches username or display name, each with a `status` |

All friendship rules live in `apps/social/services.py`. `can_chat()` is the single gate for messaging, so blocking (next) only has to change that function and `relationship()`.

## Group chats
- **Create:** *+ New group* in the Chats tab. Pick at least 2 friends (up to 50 people in total, you included) and optionally name it. Without a name the group shows its members' names ("bob, carol +2").
- **Group info** (button in the chat header): rename, see members, add more of *your* friends, or leave. There is no owner, so any member can do all of these. Leaving is immediate; when the last person leaves, the group is deleted.
- **Only friends of the adder can be added**, so nobody is pulled into a group by a stranger. Members do not need to be friends with each other, and group messaging is never friends-gated (the friends-only rule applies to one-to-one chats).
- **History:** a person added later sees only messages sent after they joined (also in the chat-list preview). Old members keep everything. Removing this is one line: drop the `created_at__gte` filters in `apps/chat/views.py`.
- **Seen by:** each person's read position is shown under the latest of *your* messages they have seen: "Seen by bob, carol", or "Seen by everyone" when all other members have. One-to-one chats keep the plain "Seen". Because people added later never see older messages, an older message can stay at "Seen by alice, carol" while newer ones reach "everyone".
- **Live:** create, add, rename and leave reach open tabs over the WebSocket (`conversation.changed`), with a toast for the people just added. Bubbles show the sender's name in groups, and several people typing at once show as "bob and carol are typing".
- **Security detail:** each WebSocket connection caches a chat's member list. `conversation.changed` clears that cache on every connection of everyone affected, so someone who has just left cannot keep sending from an already-open tab, and someone just added is included straight away.

| Endpoint | Purpose |
|---|---|
| `POST /api/conversations/` with `{"type":"group","title":"…","usernames":[…]}` | create a group (title optional) |
| `POST /api/conversations/<id>/members/` `{"usernames":[…]}` | add friends; returns the group plus `added` |
| `POST /api/conversations/<id>/title/` `{"title":"…"}` | rename (empty resets to the members' names) |
| `POST /api/conversations/<id>/leave/` | leave |
| `GET /api/conversations/<id>/messages/` | now also returns `reads`: `{username: last message id seen}` |

All group rules live in `apps/chat/groups.py`.

## Video calls
- **Start:** open a one-to-one chat with a friend and press *Video call*. Your browser asks for camera and microphone first, and only then does the other person's screen ring. They can Accept or Decline; the caller can Cancel; unanswered calls stop after 45 seconds.
- **In a call:** mute, camera off, hang up. Closing the tab or losing the connection hangs up for both sides.
- **One call at a time:** someone already in a call (in any tab) is "busy" and the caller is told. If the callee has several tabs open they all ring, the first to answer wins and the others stop ringing.
- **Rules:** one-to-one and friends only (group calls are not supported yet), and the friend must be online. Set-up messages are checked on the server: only the two people in an answered call can signal, and only from the tab that carries the call.
- **How it works:** the browsers connect directly to each other (WebRTC); the server only relays the set-up messages over the existing WebSocket (`call.invite/accept/end/signal`, in `apps/chat/consumers.py`; call state and rules in `apps/chat/calls.py`). Audio and video never pass through Django. No migration is needed.
- **HTTPS:** browsers only allow the camera on `https://` pages or `localhost`, so a deployed site needs HTTPS (and `wss://`).
- **TURN server (needed in production; on Railway use a hosted one, Railway cannot run UDP):** STUN alone connects most home networks, but calls fail on some mobile networks and corporate firewalls. Run a TURN server such as coturn and set `CALL_TURN_URLS` and `CALL_TURN_SECRET` (coturn's `static-auth-secret`; credentials are then generated per call and expire after an hour), or `CALL_TURN_USERNAME`/`CALL_TURN_CREDENTIAL` for a fixed login. `CALL_STUN_URLS` overrides the default STUN server. See `.env.example`.
- **Multiple servers:** call state lives in Django's cache, so with more than one server process set `REDIS_URL` (as for presence).

| Endpoint | Purpose |
|---|---|
| `GET /api/calls/config/` | `{iceServers}` for the call (fetched per call) |

## Watch together
- **Start:** in a one-to-one chat with a friend press *Watch together* (or the same button on the call screen), paste a link and press *Watch*. The other person gets a toast to join, or the video simply opens if you are already on a call with them. A call is not required; the button turns into *Join video* while a video is active.
- **Links that work:** YouTube (watch, youtu.be, shorts, embed), Vimeo (including unlisted links) and direct video files ending in `.mp4 .webm .ogv .ogg .m4v .mov` over `https`. Some YouTube videos forbid embedding, and then the player says so.
- **Anything else** (Netflix, Twitch, a local file...): start a call and press *Share screen*. Pick the browser tab that plays the video and tick "Share tab audio"; its sound is mixed with your microphone so you can keep talking. Share the video's tab, not the VibeChat tab, or the other person hears themselves. Some services (Netflix, Disney+) black out screen capture in certain browsers.
- **Sync:** either person can play, pause or drag the bar and the other follows. Each browser polls its player twice a second; a change the person made is sent, a change we just applied is never echoed back. Someone who joins later starts at the right moment. Expect a fraction of a second of difference; there is no continuous drift correction, so if the two of you drift apart (for example after one side buffers), one seek brings you back together. If a browser refuses to autoplay, a *Tap to join playback* button appears.
- **While calling:** the call shrinks to a small tile in the corner of the video.
- **Ending:** *Leave* closes it for you only (you can rejoin); *Stop for both* ends it for both. A session is forgotten 6 hours after its last play, pause or seek.
- **Rules:** one-to-one and friends only, checked on every message; links are validated on the server (no other hosts, no `http` outside `DEBUG`, no credentials in the URL). Your browsers load the video from the link's site directly, so your friend's IP address is visible to that site, as with any link.
- **Under the hood:** `watch.start / watch.sync / watch.state / watch.stop` over the chat WebSocket (`apps/chat/consumers.py`); link parsing and session state in `apps/chat/watch.py`; players and sync engine in `static/js/watch.js`. No migration. The YouTube and Vimeo player scripts load from `youtube.com` and `player.vimeo.com`, so add those to a Content-Security-Policy if you use one. With several server processes set `REDIS_URL` (session state lives in the cache).

Uploaded photos go to `MEDIA_ROOT` (`media/` locally; `<volume>/media` on Railway when a Volume is attached) and are served by Django at `/media/`. To use object storage instead, switch the `default` storage in `STORAGES` to S3 or similar.

Tests: `python manage.py test` (set `DATABASE_URL=` empty first if your `.env` points at a remote database, so tests run on local SQLite).
Next: blocking (change `can_chat()` and `relationship()`; `can_chat()` already gates calls too), then notifications and call history (missed calls are only a toast for now). Stage 3 replaces the 3-second polling in `static/js/app.js` with Django Channels + Redis.
