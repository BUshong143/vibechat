import asyncio
import json
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from apps.accounts.models import User
from apps.social.models import Friendship
from .consumers import ChatConsumer
from .models import Conversation, ConversationMember, Message


def befriend(a, b):
    return Friendship.objects.create(from_user=a, to_user=b, status=Friendship.ACCEPTED)


class ChatTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("alice", password="pw-12345!")
        self.b = User.objects.create_user("bob", password="pw-12345!")
        self.c = User.objects.create_user("carol", password="pw-12345!")
        befriend(self.a, self.b)

    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_send_and_read(self):
        self.client.login(username="alice", password="pw-12345!")
        cid = self.post("/api/conversations/", {"username": "bob"}).json()["id"]
        self.assertEqual(self.post("/api/conversations/", {"username": "bob"}).json()["id"], cid)
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "hi"}).status_code, 201)
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "  "}).status_code, 400)
        msgs = self.client.get(f"/api/conversations/{cid}/messages/").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["hi"])
        self.client.login(username="bob", password="pw-12345!")
        self.assertEqual(len(self.client.get("/api/conversations/").json()["conversations"]), 1)

    def test_outsider_blocked(self):
        self.client.login(username="alice", password="pw-12345!")
        cid = self.post("/api/conversations/", {"username": "bob"}).json()["id"]
        self.client.login(username="carol", password="pw-12345!")
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages/").status_code, 404)
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "x"}).status_code, 404)

    def test_login_required_and_self_chat(self):
        self.assertEqual(self.client.get("/api/conversations/").status_code, 302)
        self.client.login(username="alice", password="pw-12345!")
        self.assertEqual(self.post("/api/conversations/", {"username": "alice"}).status_code, 400)


class RealtimeTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user("alice", password="pw-12345!")
        self.b = User.objects.create_user("bob", password="pw-12345!")
        self.c = User.objects.create_user("carol", password="pw-12345!")
        self.conv = Conversation.objects.create()
        for u in (self.a, self.b):
            ConversationMember.objects.create(conversation=self.conv, user=u)
        befriend(self.a, self.b)

    async def connect(self, user):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        comm.scope["user"] = user
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        snapshot = await comm.receive_json_from()
        self.assertEqual(snapshot["type"], "presence.snapshot")
        comm.snapshot = snapshot["online"]
        return comm

    async def pair(self):
        a = await self.connect(self.a)
        b = await self.connect(self.b)
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["online"]), ("presence", "bob", True))
        return a, b

    async def send_message(self, comm, other, content="hi"):
        await comm.send_json_to({"type": "message", "conversation": self.conv.id, "content": content, "client_id": content})
        ev = await comm.receive_json_from()
        await other.receive_json_from()
        return ev["message"]["id"]

    async def test_message_and_typing(self):
        a, b = await self.pair()
        await a.send_json_to({"type": "typing", "conversation": self.conv.id})
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["user"]), ("typing", "alice"))
        self.assertTrue(await a.receive_nothing(0.2))  # you never see your own typing
        await a.send_json_to({"type": "message", "conversation": self.conv.id, "content": "yo", "client_id": "c1"})
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["message"]["content"], ev["client_id"]), ("message.new", "yo", "c1"))
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 1)
        await a.disconnect(); await b.disconnect()

    async def test_outsider_rejected(self):
        a, c = await self.connect(self.a), await self.connect(self.c)
        await c.send_json_to({"type": "message", "conversation": self.conv.id, "content": "hack", "client_id": "z"})
        ev = await c.receive_json_from()
        self.assertEqual((ev["type"], ev["client_id"]), ("error", "z"))
        self.assertTrue(await a.receive_nothing(0.2))
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 0)
        await a.disconnect(); await c.disconnect()

    async def test_anonymous_rejected(self):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        comm.scope["user"] = AnonymousUser()
        ok, _ = await comm.connect()
        self.assertFalse(ok)

    async def test_duplicate_client_id_ignored(self):
        a, b = await self.pair()
        frame = {"type": "message", "conversation": self.conv.id, "content": "once", "client_id": "same"}
        await a.send_json_to(frame); await a.send_json_to(frame)
        await b.receive_json_from()
        self.assertTrue(await b.receive_nothing(0.3))
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 1)
        await a.disconnect(); await b.disconnect()

    async def test_presence_online_offline_and_snapshot(self):
        a = await self.connect(self.a)
        self.assertEqual(a.snapshot, [])
        b = await self.connect(self.b)
        self.assertEqual(b.snapshot, ["alice"])
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["online"]), ("presence", "bob", True))
        await b.disconnect()
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["online"]), ("presence", "bob", False))
        await a.send_json_to({"type": "ping"})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["online"]), ("presence.snapshot", []))
        await a.disconnect()

    async def test_presence_two_tabs_and_strangers(self):
        a = await self.connect(self.a)
        b1 = await self.connect(self.b)
        await a.receive_json_from()
        b2 = await self.connect(self.b)
        self.assertTrue(await a.receive_nothing(0.2))
        await b1.disconnect()
        self.assertTrue(await a.receive_nothing(0.2))
        c = await self.connect(self.c)
        self.assertEqual(c.snapshot, [])
        self.assertTrue(await a.receive_nothing(0.2))
        await b2.disconnect()
        ev = await a.receive_json_from()
        self.assertEqual((ev["user"], ev["online"]), ("bob", False))
        await a.disconnect(); await c.disconnect()

    async def test_new_contact_refresh(self):
        a = await self.connect(self.a)
        c = await self.connect(self.c)
        self.assertTrue(await a.receive_nothing(0.2))
        conv2 = await database_sync_to_async(Conversation.objects.create)()
        for u in (self.a, self.c):
            await database_sync_to_async(ConversationMember.objects.create)(conversation=conv2, user=u)
        await a.send_json_to({"type": "contacts"})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["online"]), ("presence.snapshot", []))
        await asyncio.sleep(1.1)
        await a.send_json_to({"type": "ping"})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["online"]), ("presence.snapshot", ["carol"]))
        ev = await c.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["online"]), ("presence", "alice", True))
        await a.disconnect(); await c.disconnect()

    async def test_read_receipt(self):
        a, b = await self.pair()
        mid = await self.send_message(a, b, "one")
        await b.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": mid})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["message_id"]), ("read", "bob", mid))
        member = await database_sync_to_async(ConversationMember.objects.get)(conversation=self.conv, user=self.b)
        self.assertEqual(member.last_read_id, mid)
        await b.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": mid})
        self.assertTrue(await a.receive_nothing(0.2))
        mid2 = await self.send_message(a, b, "two")
        await b.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": mid2 + 1000})
        ev = await a.receive_json_from()
        self.assertEqual(ev["message_id"], mid2)
        await a.disconnect(); await b.disconnect()

    async def test_read_receipt_rejects_outsider_and_bad_ids(self):
        a, b = await self.pair()
        c = await self.connect(self.c)
        mid = await self.send_message(a, b, "secret")
        await c.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": mid})
        ev = await c.receive_json_from()
        self.assertEqual(ev["type"], "error")
        for bad in ("5", True, -1, 0, None):
            await b.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": bad})
        self.assertTrue(await a.receive_nothing(0.3))
        member = await database_sync_to_async(ConversationMember.objects.get)(conversation=self.conv, user=self.b)
        self.assertEqual(member.last_read_id, 0)
        await a.disconnect(); await b.disconnect(); await c.disconnect()


class ReadHttpTests(TestCase):
    def setUp(self):
        self.a = User.objects.create_user("alice", password="pw-12345!")
        self.b = User.objects.create_user("bob", password="pw-12345!")
        self.c = User.objects.create_user("carol", password="pw-12345!")
        self.conv = Conversation.objects.create()
        for u in (self.a, self.b):
            ConversationMember.objects.create(conversation=self.conv, user=u)
        self.m = Message.objects.create(conversation=self.conv, sender=self.a, content="hey")

    def post(self, data):
        return self.client.post(f"/api/conversations/{self.conv.id}/read/", json.dumps(data), content_type="application/json")

    def test_http_read_and_history_fields(self):
        self.client.login(username="bob", password="pw-12345!")
        self.assertEqual(self.post({"message_id": self.m.id}).json(), {"read": self.m.id})
        self.assertEqual(self.post({"message_id": self.m.id}).json(), {"read": 0})
        self.assertEqual(self.post({"message_id": "x"}).status_code, 400)
        data = self.client.get(f"/api/conversations/{self.conv.id}/messages/").json()
        self.assertEqual((data["my_read"], data["other_read"]), (self.m.id, 0))
        self.client.login(username="alice", password="pw-12345!")
        data = self.client.get(f"/api/conversations/{self.conv.id}/messages/").json()
        self.assertEqual((data["my_read"], data["other_read"]), (0, self.m.id))

    def test_http_read_outsider_and_anonymous(self):
        self.assertEqual(self.post({"message_id": self.m.id}).status_code, 302)
        self.client.login(username="carol", password="pw-12345!")
        self.assertEqual(self.post({"message_id": self.m.id}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/conversations/{self.conv.id}/read/").status_code, 405)

    def test_conversation_list_has_other_user(self):
        self.client.login(username="alice", password="pw-12345!")
        convs = self.client.get("/api/conversations/").json()["conversations"]
        self.assertEqual(convs[0]["other"], "bob")
