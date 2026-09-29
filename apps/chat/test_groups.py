import json
from datetime import timedelta

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.social.models import Friendship
from . import groups
from .consumers import ChatConsumer
from .models import Conversation, ConversationMember, Message

PW = "pw-12345!"


def befriend(a, b):
    return Friendship.objects.create(from_user=a, to_user=b, status=Friendship.ACCEPTED)


def make_users(*names):
    return [User.objects.create_user(n, password=PW) for n in names]


class GroupHttpTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a, cls.b, cls.c, cls.d, cls.e = make_users("alice", "bob", "carol", "dave", "erin")
        for other in (cls.b, cls.c, cls.d):
            befriend(cls.a, other)  # alice is friends with bob, carol, dave; erin is a stranger to all

    def login(self, name):
        self.client.login(username=name, password=PW)

    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def create(self, title="Squad", names=("bob", "carol")):
        self.login("alice")
        return self.post("/api/conversations/", {"type": "group", "title": title, "usernames": list(names)})

    def test_create_group(self):
        r = self.create()
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual((data["type"], data["title"], data["can_send"]), ("group", "Squad", True))
        self.assertEqual(sorted(m["username"] for m in data["members"]), ["alice", "bob", "carol"])
        conv = Conversation.objects.get(pk=data["id"])
        self.assertEqual(conv.type, Conversation.GROUP)
        self.assertEqual(conv.members.count(), 3)
        self.login("bob")  # and it shows up for the people added
        listed = self.client.get("/api/conversations/").json()["conversations"]
        self.assertEqual([c["id"] for c in listed], [conv.id])

    def test_create_needs_friends_and_enough_people(self):
        self.login("alice")
        for names in (["bob"], [], ["bob", "bob"], ["bob", "alice"]):  # duplicates and yourself don't count
            self.assertEqual(self.create(names=names).status_code, 400, names)
        self.assertEqual(self.create(names=["bob", "erin"]).status_code, 403)  # erin is not alice's friend
        self.assertEqual(self.create(names=["bob", "nobody"]).status_code, 400)
        self.assertEqual(self.post("/api/conversations/", {"type": "group", "usernames": "bob,carol"}).status_code, 400)
        self.assertEqual(self.post("/api/conversations/", {"type": "group", "usernames": ["bob", 5]}).status_code, 400)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_create_title_rules(self):
        self.assertEqual(self.create(title="x" * 101).status_code, 400)
        r = self.create(title="   ")  # blank is fine: the name falls back to the members
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["title"], "bob, carol")
        self.assertEqual(r.json()["custom_title"], "")

    def test_create_member_limit(self):
        self.login("alice")
        extra = [User.objects.create_user(f"u{i}", password=PW) for i in range(groups.MAX_MEMBERS)]
        for u in extra:
            befriend(self.a, u)
        too_many = self.post("/api/conversations/", {"type": "group", "usernames": [u.username for u in extra]})
        self.assertEqual(too_many.status_code, 400)  # 50 others + you = 51
        ok = self.post("/api/conversations/", {"type": "group", "usernames": [u.username for u in extra[:-1]]})
        self.assertEqual(ok.status_code, 201)  # exactly 50 people

    def test_group_members_can_message_without_being_friends(self):
        cid = self.create().json()["id"]
        self.login("bob")  # bob and carol are not friends with each other
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "hi all"}).status_code, 201)
        self.login("carol")
        msgs = self.client.get(f"/api/conversations/{cid}/messages/").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["hi all"])

    def test_outsider_cannot_touch_group(self):
        cid = self.create().json()["id"]
        self.login("erin")
        for suffix in ("messages/", "read/", "members/", "title/", "leave/"):
            url = f"/api/conversations/{cid}/{suffix}"
            r = self.post(url, {"content": "x", "message_id": 1, "usernames": ["erin"], "title": "hi"})
            self.assertEqual(r.status_code, 404, suffix)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages/").status_code, 404)

    def test_add_members(self):
        cid = self.create().json()["id"]
        self.login("alice")
        r = self.post(f"/api/conversations/{cid}/members/", {"usernames": ["dave", "bob"]})  # bob is already in
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["added"], ["dave"])
        self.assertEqual(len(r.json()["members"]), 4)
        again = self.post(f"/api/conversations/{cid}/members/", {"usernames": ["dave"]})
        self.assertEqual(again.json()["added"], [])  # idempotent, no duplicate row
        self.assertEqual(ConversationMember.objects.filter(conversation_id=cid).count(), 4)

    def test_add_members_only_from_your_friends(self):
        cid = self.create().json()["id"]
        self.login("bob")  # bob is not friends with dave
        self.assertEqual(self.post(f"/api/conversations/{cid}/members/", {"usernames": ["dave"]}).status_code, 403)
        self.assertEqual(self.post(f"/api/conversations/{cid}/members/", {"usernames": []}).status_code, 400)
        self.assertEqual(ConversationMember.objects.filter(conversation_id=cid).count(), 3)

    def test_add_members_respects_limit(self):
        cid = self.create().json()["id"]
        extra = [User.objects.create_user(f"u{i}", password=PW) for i in range(groups.MAX_MEMBERS)]
        for u in extra:
            befriend(self.a, u)
        self.login("alice")
        r = self.post(f"/api/conversations/{cid}/members/", {"usernames": [u.username for u in extra]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ConversationMember.objects.filter(conversation_id=cid).count(), 3)

    def test_group_endpoints_reject_direct_chats(self):
        befriend(self.b, self.d)
        self.login("bob")
        cid = self.post("/api/conversations/", {"username": "dave"}).json()["id"]
        for suffix, body in (("members/", {"usernames": ["alice"]}), ("title/", {"title": "x"}), ("leave/", {})):
            self.assertEqual(self.post(f"/api/conversations/{cid}/{suffix}", body).status_code, 404, suffix)
        self.assertEqual(ConversationMember.objects.filter(conversation_id=cid).count(), 2)

    def test_rename(self):
        cid = self.create().json()["id"]
        self.login("bob")  # any member may rename
        r = self.post(f"/api/conversations/{cid}/title/", {"title": "  Weekend plans "})
        self.assertEqual((r.status_code, r.json()["title"]), (200, "Weekend plans"))
        self.assertEqual(self.post(f"/api/conversations/{cid}/title/", {"title": "x" * 101}).status_code, 400)
        self.assertEqual(Conversation.objects.get(pk=cid).title, "Weekend plans")
        self.assertEqual(self.post(f"/api/conversations/{cid}/title/", {"title": ""}).json()["title"], "alice, carol")

    def test_leave_and_last_one_out(self):
        cid = self.create().json()["id"]
        self.login("carol")
        self.assertEqual(self.post(f"/api/conversations/{cid}/leave/", {}).json(), {"left": True})
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages/").status_code, 404)
        self.assertEqual(self.client.get("/api/conversations/").json()["conversations"], [])
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "x"}).status_code, 404)
        self.login("bob")
        self.assertEqual(self.post(f"/api/conversations/{cid}/leave/", {}).status_code, 200)
        self.assertTrue(Conversation.objects.filter(pk=cid).exists())  # alice is still there
        self.login("alice")
        self.assertEqual(self.post(f"/api/conversations/{cid}/leave/", {}).status_code, 200)
        self.assertFalse(Conversation.objects.filter(pk=cid).exists())  # last one out closes it

    def test_a_removed_member_can_be_added_back(self):
        cid = self.create().json()["id"]
        self.login("bob")
        self.post(f"/api/conversations/{cid}/leave/", {})
        self.login("alice")
        self.assertEqual(self.post(f"/api/conversations/{cid}/members/", {"usernames": ["bob"]}).json()["added"], ["bob"])

    def test_new_member_sees_nothing_from_before_they_joined(self):
        cid = self.create().json()["id"]
        conv = Conversation.objects.get(pk=cid)
        ConversationMember.objects.filter(conversation=conv).update(joined_at=timezone.now() - timedelta(hours=2))
        old = Message.objects.create(conversation=conv, sender=self.a, content="before dave")
        Message.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.login("alice")
        self.post(f"/api/conversations/{cid}/members/", {"usernames": ["dave"]})
        Message.objects.create(conversation=conv, sender=self.a, content="after dave")
        self.login("dave")
        msgs = self.client.get(f"/api/conversations/{cid}/messages/").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["after dave"])
        self.assertEqual(self.client.get("/api/conversations/").json()["conversations"][0]["last"], "after dave")
        self.login("bob")  # people who were already in still see everything
        msgs = self.client.get(f"/api/conversations/{cid}/messages/").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["before dave", "after dave"])

    def test_preview_is_empty_for_a_new_member_until_someone_talks(self):
        cid = self.create().json()["id"]
        conv = Conversation.objects.get(pk=cid)
        old = Message.objects.create(conversation=conv, sender=self.a, content="secret plan")
        Message.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.login("alice")
        self.post(f"/api/conversations/{cid}/members/", {"usernames": ["dave"]})
        self.login("dave")
        self.assertEqual(self.client.get("/api/conversations/").json()["conversations"][0]["last"], "")

    def test_reads_map_for_seen_by(self):
        cid = self.create().json()["id"]
        conv = Conversation.objects.get(pk=cid)
        m1 = Message.objects.create(conversation=conv, sender=self.a, content="one")
        m2 = Message.objects.create(conversation=conv, sender=self.a, content="two")
        self.login("bob")
        self.post(f"/api/conversations/{cid}/read/", {"message_id": m2.id})
        self.login("carol")
        self.post(f"/api/conversations/{cid}/read/", {"message_id": m1.id})
        self.login("alice")
        data = self.client.get(f"/api/conversations/{cid}/messages/").json()
        self.assertEqual(data["reads"], {"bob": m2.id, "carol": m1.id})  # you are not in your own map
        self.assertEqual((data["other_read"], data["my_read"]), (m2.id, 0))

    def test_direct_chats_unchanged(self):
        self.login("alice")
        r = self.post("/api/conversations/", {"username": "bob"})
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual((data["type"], data["other"], "members" in data), ("direct", "bob", False))
        self.assertEqual(self.post("/api/conversations/", {"username": "erin"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/conversations/{data['id']}/messages/").json()["reads"], {"bob": 0})

    def test_removed_group_shows_no_more_for_old_history(self):
        # a group chat with a member who was unfriended is still fully usable by everyone
        cid = self.create().json()["id"]
        Friendship.objects.filter(from_user=self.a, to_user=self.b).delete()
        self.login("bob")
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "still here"}).status_code, 201)
        self.assertTrue(self.client.get("/api/conversations/").json()["conversations"][0]["can_send"])


class GroupRealtimeTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.a, self.b, self.c, self.d = make_users("alice", "bob", "carol", "dave")
        for other in (self.b, self.c, self.d):
            befriend(self.a, other)
        self.conv = Conversation.objects.create(type=Conversation.GROUP, title="Squad")
        for u in (self.a, self.b, self.c):
            ConversationMember.objects.create(conversation=self.conv, user=u)

    async def connect(self, user):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        comm.scope["user"] = user
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        snap = await comm.receive_json_from()
        self.assertEqual(snap["type"], "presence.snapshot")
        return comm

    async def drain(self, *comms):
        for comm in comms:
            while not await comm.receive_nothing(0.15):
                await comm.receive_output()

    async def test_message_reaches_every_member_and_read_receipts_fan_out(self):
        a, b, c = [await self.connect(u) for u in (self.a, self.b, self.c)]
        await self.drain(a, b, c)
        await a.send_json_to({"type": "message", "conversation": self.conv.id, "content": "hey all", "client_id": "k1"})
        ids = set()
        for comm in (a, b, c):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["message"]["content"]), ("message.new", "hey all"))
            ids.add(ev["message"]["id"])
        self.assertEqual(len(ids), 1)
        mid = ids.pop()
        await b.send_json_to({"type": "read", "conversation": self.conv.id, "message_id": mid})
        for comm in (a, c):  # everyone except the reader hears that bob has seen it
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["user"], ev["message_id"]), ("read", "bob", mid))
        self.assertTrue(await b.receive_nothing(0.2))
        await c.send_json_to({"type": "typing", "conversation": self.conv.id})
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["user"]), ("typing", "carol"))
        for comm in (a, b, c):
            await comm.disconnect()

    async def test_someone_who_left_can_no_longer_send_and_someone_added_hears_everything(self):
        a, b, d = await self.connect(self.a), await self.connect(self.b), await self.connect(self.d)
        await self.drain(a, b, d)
        # bob's connection has already cached the member list from this first message
        await b.send_json_to({"type": "message", "conversation": self.conv.id, "content": "one", "client_id": "m1"})
        await self.drain(a, b)

        def change():
            self.client.login(username="alice", password=PW)
            self.client.post(f"/api/conversations/{self.conv.id}/members/", json.dumps({"usernames": ["dave"]}),
                             content_type="application/json")
            self.client.login(username="bob", password=PW)
            self.client.post(f"/api/conversations/{self.conv.id}/leave/", "{}", content_type="application/json")
        await database_sync_to_async(change)()
        ev = await d.receive_json_from()  # dave is told he was added
        self.assertEqual((ev["type"], ev["event"], ev["user"]), ("conversation.changed", "added", "alice"))
        await self.drain(a, b, d)

        await a.send_json_to({"type": "message", "conversation": self.conv.id, "content": "welcome dave", "client_id": "m2"})
        ev = await d.receive_json_from()
        self.assertEqual(ev["message"]["content"], "welcome dave")  # the added member is in the fan-out
        await self.drain(a, b, d)

        await b.send_json_to({"type": "message", "conversation": self.conv.id, "content": "still here?", "client_id": "m3"})
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["client_id"]), ("error", "m3"))  # the leaver is refused despite the earlier cache
        self.assertTrue(await a.receive_nothing(0.2))
        contents = await database_sync_to_async(lambda: list(Message.objects.values_list("content", flat=True)))()
        self.assertEqual(contents, ["one", "welcome dave"])
        for comm in (a, b, d):
            await comm.disconnect()

    async def test_group_chat_does_not_need_friendship_over_websocket(self):
        b, c = await self.connect(self.b), await self.connect(self.c)  # bob and carol are not friends
        await self.drain(b, c)
        await b.send_json_to({"type": "message", "conversation": self.conv.id, "content": "hello carol", "client_id": "x"})
        for comm in (b, c):
            ev = await comm.receive_json_from()
            self.assertEqual(ev["type"], "message.new")
        await b.disconnect(); await c.disconnect()
