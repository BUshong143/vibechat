import json
import time

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import calls, presence, watch
from apps.social import services as social
from .models import Conversation, ConversationMember, Message
from .realtime import contacts_of, mark_read, message_dict, valid_id


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user, self.group, self.members, self.contacts = user, f"user_{user.id}", {}, {}
        self.peer = {}  # conversation id -> the other person, for one-to-one chats
        self.sent_ids = set()
        self.watch_at = {}  # conversation id -> when this connection last sent a play/pause/seek
        self.contacts_at, self.contacts_dirty = 0.0, False
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        self.contacts = await database_sync_to_async(contacts_of)(user)
        self.contacts_at = time.monotonic()
        if await sync_to_async(presence.touch)(user.id, self.channel_name):
            await self.announce(True)
        await self.send_snapshot()

    async def disconnect(self, code):
        if not hasattr(self, "group"):
            return
        await self.channel_layer.group_discard(self.group, self.channel_name)
        rec = await sync_to_async(calls.drop_for_channel)(self.user.id, self.channel_name)
        if rec:  # closing the tab (or losing the connection) hangs up
            await self.fan_out([rec["caller"], rec["callee"]],
                               {"type": "call.ended", "call_id": rec["id"], "reason": "dropped"})
        if await sync_to_async(presence.remove)(self.user.id, self.channel_name):
            await self.announce(False)

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or "")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "ping":
            if await sync_to_async(presence.touch)(self.user.id, self.channel_name):
                await self.announce(True)
            if self.contacts_dirty:
                await self.refresh_contacts()
            else:
                await self.send_snapshot()
            return
        if kind == "contacts":
            await self.refresh_contacts()
            return
        if isinstance(kind, str) and kind.startswith("call."):
            await self.handle_call(kind, data)
            return
        if isinstance(kind, str) and kind.startswith("watch."):
            await self.handle_watch(kind, data)
            return
        cid = data.get("conversation")
        if not valid_id(cid):
            return
        cid_client = str(data.get("client_id") or "")[:64] or None
        ids = await self.member_ids(cid)
        if self.user.id not in ids:
            await self.reply({"type": "error", "detail": "Not a member of this conversation", "client_id": cid_client})
            return
        if kind == "message":
            content = str(data.get("content", "")).strip()
            if not content or len(content) > 2000:
                await self.reply({"type": "error", "detail": "Message must be 1-2000 characters", "client_id": cid_client})
                return
            peer = self.peer.get(cid)
            if peer is not None and not await database_sync_to_async(social.can_chat)(self.user.id, peer):
                await self.reply({"type": "error", "detail": "You can only message friends", "client_id": cid_client})
                return
            if cid_client:
                if cid_client in self.sent_ids:
                    return
                if len(self.sent_ids) > 500:
                    self.sent_ids.clear()
                self.sent_ids.add(cid_client)
            msg = await self.save_message(cid, content)
            await self.fan_out(ids, {"type": "message.new", "conversation": cid, "client_id": cid_client, "message": msg})
        elif kind == "typing":
            await self.fan_out([i for i in ids if i != self.user.id],
                               {"type": "typing", "conversation": cid, "user": self.user.username})
        elif kind == "read":
            message_id = data.get("message_id")
            if not valid_id(message_id):
                return
            upto = await database_sync_to_async(mark_read)(self.user, cid, message_id)
            if upto:
                await self.fan_out([i for i in ids if i != self.user.id],
                                   {"type": "read", "conversation": cid, "user": self.user.username, "message_id": upto})

    async def handle_call(self, kind, data):
        """Set-up messages for one-to-one video calls. The media itself never touches the server."""
        me = self.user.id
        call_id = data.get("call_id")
        if not calls.valid_call_id(call_id):
            return
        if kind == "call.invite":
            cid = data.get("conversation")
            if not valid_id(cid):
                return
            ids = await self.member_ids(cid)
            peer = self.peer.get(cid)
            if me not in ids or not peer:
                await self.reply({"type": "call.failed", "call_id": call_id, "reason": "group"})
                return
            if not await database_sync_to_async(social.can_chat)(me, peer):
                await self.reply({"type": "call.failed", "call_id": call_id, "reason": "not_friends"})
                return
            if not await sync_to_async(presence.online_ids)([peer]):
                await self.reply({"type": "call.failed", "call_id": call_id, "reason": "offline"})
                return
            rec, why = await sync_to_async(calls.start)(me, peer, cid, call_id, data.get("video") is not False,
                                                       self.channel_name)
            if not rec:
                await self.reply({"type": "call.failed", "call_id": call_id, "reason": why})
                return
            await self.reply({"type": "call.ringing", "call_id": call_id})
            await self.fan_out([peer], {"type": "call.incoming", "call_id": call_id, "conversation": cid,
                                        "user": self.user.username, "name": self.user.name, "video": rec["video"],
                                        "ring": calls.RING_SECONDS})
        elif kind == "call.accept":
            rec = await sync_to_async(calls.accept)(call_id, me, self.channel_name)
            if not rec:
                await self.reply({"type": "call.failed", "call_id": call_id, "reason": "gone"})
                return
            # everyone involved hears it: the caller starts connecting, the callee's other tabs stop ringing
            await self.fan_out([rec["caller"], rec["callee"]], {"type": "call.accepted", "call_id": call_id})
        elif kind == "call.end":
            rec, reason = await sync_to_async(calls.end)(call_id, me)
            if rec:
                if reason == "cancelled" and data.get("reason") == "missed":
                    reason = "missed"
                await self.fan_out([rec["caller"], rec["callee"]],
                                   {"type": "call.ended", "call_id": call_id, "reason": reason})
        elif kind == "call.signal":
            body = data.get("data")
            if not isinstance(body, dict) or len(json.dumps(body)) > calls.SIGNAL_LIMIT:
                return
            target = await sync_to_async(calls.signal_target)(call_id, me, self.channel_name)
            if target:  # straight to the other person's connection that carries the call
                await self.channel_layer.send(target, {"type": "relay", "payload": {
                    "type": "call.signal", "call_id": call_id, "data": body}})

    async def handle_watch(self, kind, data):
        """Shared video in a one-to-one chat: start, sync (join late), play / pause / seek, stop."""
        me = self.user.id
        cid = data.get("conversation")
        if not valid_id(cid):
            return
        ids = await self.member_ids(cid)
        if me not in ids:
            return
        peer = self.peer.get(cid)

        async def fail(reason):
            await self.reply({"type": "watch.failed", "conversation": cid, "reason": reason})

        if not peer:
            await fail("group")
            return
        if not await database_sync_to_async(social.can_chat)(me, peer):
            await fail("not_friends")
            return
        if kind == "watch.start":
            parsed = watch.parse(data.get("url"))
            if not parsed:
                await fail("bad_url")
                return
            rec = await sync_to_async(watch.start)(cid, self.user, *parsed)
            await self.fan_out(ids, {"type": "watch.started", "conversation": cid, **watch.view(rec)})
        elif kind == "watch.sync":
            rec = await sync_to_async(watch.get)(cid)
            if rec:
                await self.reply({"type": "watch.started", "conversation": cid, "sync": True, **watch.view(rec)})
            else:
                await self.reply({"type": "watch.none", "conversation": cid})
        elif kind == "watch.state":
            playing, position = data.get("playing"), data.get("position")
            now = time.monotonic()
            if not watch.valid_state(playing, position) or now - self.watch_at.get(cid, 0) < 0.1:
                return
            self.watch_at[cid] = now
            if await sync_to_async(watch.update)(cid, playing, position):
                await self.fan_out([peer], {"type": "watch.state", "conversation": cid, "by": self.user.username,
                                            "playing": playing, "position": position})
        elif kind == "watch.stop":
            if await sync_to_async(watch.stop)(cid):
                await self.fan_out(ids, {"type": "watch.stopped", "conversation": cid, "by": self.user.username,
                                         "name": self.user.name})

    async def announce(self, is_online):
        await self.fan_out(list(self.contacts), {"type": "presence", "user": self.user.username, "online": is_online})

    async def send_snapshot(self):
        online = await sync_to_async(presence.online_ids)(list(self.contacts))
        await self.reply({"type": "presence.snapshot", "online": [self.contacts[i] for i in online]})

    async def refresh_contacts(self):
        if time.monotonic() - self.contacts_at < 1:
            self.contacts_dirty = True
        else:
            self.contacts_dirty = False
            old = set(self.contacts)
            self.contacts = await database_sync_to_async(contacts_of)(self.user)
            self.contacts_at = time.monotonic()
            added = [i for i in self.contacts if i not in old]
            if added:
                await self.fan_out(added, {"type": "presence", "user": self.user.username, "online": True})
        await self.send_snapshot()

    async def fan_out(self, user_ids, payload):
        for uid in user_ids:
            await self.channel_layer.group_send(f"user_{uid}", {"type": "relay", "payload": payload})

    async def relay(self, event):
        payload = event["payload"]
        if payload.get("type") == "conversation.changed":
            # Membership changed: forget the cached member list so nobody who left can keep sending,
            # and nobody who joined is left out of the fan-out.
            self.members.pop(payload.get("conversation"), None)
            self.peer.pop(payload.get("conversation"), None)
        await self.send(text_data=json.dumps(payload))

    async def reply(self, payload):
        await self.send(text_data=json.dumps(payload))

    async def member_ids(self, cid):
        if cid not in self.members:
            rows = await self._member_rows(cid)
            if not rows:
                return []
            ids = [uid for uid, _ in rows]
            self.members[cid] = ids
            direct = rows[0][1] == Conversation.DIRECT
            self.peer[cid] = next((i for i in ids if i != self.user.id), 0) if direct else None
        return self.members[cid]

    @database_sync_to_async
    def _member_rows(self, cid):
        return list(ConversationMember.objects.filter(conversation_id=cid).values_list("user_id", "conversation__type"))

    @database_sync_to_async
    def save_message(self, cid, content):
        return message_dict(Message.objects.create(conversation_id=cid, sender=self.user, content=content))
