import json
import shutil
import tempfile
from io import BytesIO

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from PIL import Image

from apps.accounts.models import User
from apps.chat.consumers import ChatConsumer
from apps.chat.models import Conversation, ConversationMember, Message
from .models import Friendship
from . import services

PW = "pw-12345!"


def make_users(*names):
    return [User.objects.create_user(n, password=PW) for n in names]


def image_upload(size=(600, 400), fmt="PNG", mode="RGB", name="pic.png"):
    buf = BytesIO()
    Image.new(mode, size, "red").save(buf, fmt)
    return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")


class Base(TestCase):
    def setUp(self):
        self.a, self.b, self.c = make_users("alice", "bob", "carol")

    def login(self, name):
        self.client.logout()
        self.assertTrue(self.client.login(username=name, password=PW))

    def act(self, target, action):
        return self.client.post(f"/api/friends/{target}/{action}/", "{}", content_type="application/json")

    def status(self, viewer, other):
        return services.relationship(viewer, other)


class FriendRequestTests(Base):
    def test_send_accept(self):
        self.login("alice")
        self.assertEqual(self.act("bob", "request").json(), {"status": "outgoing"})
        self.assertEqual((self.status(self.a, self.b), self.status(self.b, self.a)), ("outgoing", "incoming"))
        self.login("bob")
        data = self.client.get("/api/friends/").json()
        self.assertEqual([u["username"] for u in data["incoming"]], ["alice"])
        self.assertEqual(self.act("alice", "accept").json(), {"status": "friends"})
        self.assertEqual((self.status(self.a, self.b), self.status(self.b, self.a)), ("friends", "friends"))
        self.assertEqual([u["username"] for u in self.client.get("/api/friends/").json()["friends"]], ["alice"])

    def test_decline_and_cancel_remove_the_row(self):
        self.login("alice"); self.act("bob", "request")
        self.assertEqual(self.act("bob", "cancel").json(), {"status": "none"})
        self.act("bob", "request")
        self.login("bob")
        self.assertEqual(self.act("alice", "decline").json(), {"status": "none"})
        self.assertEqual(Friendship.objects.count(), 0)
        self.login("alice")  # after a decline they may ask again
        self.assertEqual(self.act("bob", "request").json(), {"status": "outgoing"})

    def test_only_the_recipient_can_accept_or_decline(self):
        self.login("alice"); self.act("bob", "request")
        self.assertEqual(self.act("bob", "accept").status_code, 409)   # the sender can't accept their own request
        self.assertEqual(self.act("bob", "decline").status_code, 409)
        self.login("carol")
        self.assertEqual(self.act("alice", "accept").status_code, 409)  # nor can a bystander
        self.assertEqual(self.status(self.a, self.b), "outgoing")

    def test_only_the_sender_can_cancel(self):
        self.login("alice"); self.act("bob", "request")
        self.login("bob")
        self.assertEqual(self.act("alice", "cancel").status_code, 409)
        self.assertEqual(self.status(self.a, self.b), "outgoing")

    def test_request_is_idempotent(self):
        self.login("alice")
        self.act("bob", "request"); self.act("bob", "request")
        self.assertEqual(Friendship.objects.count(), 1)

    def test_crossed_requests_become_friends(self):
        self.login("alice"); self.act("bob", "request")
        self.login("bob")
        self.assertEqual(self.act("alice", "request").json(), {"status": "friends"})
        self.assertEqual(Friendship.objects.count(), 1)

    def test_request_when_already_friends(self):
        Friendship.objects.create(from_user=self.a, to_user=self.b, status="accepted")
        self.login("bob")
        self.assertEqual(self.act("alice", "request").json(), {"status": "friends"})
        self.assertEqual(Friendship.objects.count(), 1)

    def test_unfriend(self):
        Friendship.objects.create(from_user=self.a, to_user=self.b, status="accepted")
        self.login("bob")
        self.assertEqual(self.act("alice", "remove").json(), {"status": "none"})
        self.assertEqual(self.act("alice", "remove").status_code, 409)

    def test_cannot_befriend_yourself_or_ghosts(self):
        self.login("alice")
        self.assertEqual(self.act("alice", "request").status_code, 400)
        self.assertEqual(self.act("nobody", "request").status_code, 404)
        self.assertEqual(Friendship.objects.count(), 0)

    def test_inactive_user_not_found(self):
        User.objects.filter(pk=self.b.pk).update(is_active=False)
        self.login("alice")
        self.assertEqual(self.act("bob", "request").status_code, 404)

    def test_login_required_and_post_only(self):
        self.assertEqual(self.act("bob", "request").status_code, 302)
        self.assertEqual(self.client.get("/api/friends/").status_code, 302)
        self.login("alice")
        self.assertEqual(self.client.get("/api/friends/bob/request/").status_code, 405)
        self.assertEqual(self.act("bob", "explode").status_code, 404)

    def test_database_rejects_duplicate_pair_either_direction_and_self(self):
        Friendship.objects.create(from_user=self.a, to_user=self.b)
        for frm, to in ((self.a, self.b), (self.b, self.a)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Friendship.objects.create(from_user=frm, to_user=to)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.create(from_user=self.c, to_user=self.c)

    def test_lists_are_private_to_each_person(self):
        Friendship.objects.create(from_user=self.a, to_user=self.b, status="accepted")
        self.login("carol")
        data = self.client.get("/api/friends/").json()
        self.assertEqual((data["friends"], data["incoming"], data["outgoing"]), ([], [], []))


class SearchTests(Base):
    def test_search_shows_relationship_and_matches_display_name(self):
        User.objects.filter(pk=self.c.pk).update(display_name="Caroline Zed")
        Friendship.objects.create(from_user=self.a, to_user=self.b, status="accepted")
        self.login("alice")
        users = self.client.get("/api/users/search/?q=o").json()["users"]
        self.assertEqual({u["username"]: u["status"] for u in users}, {"bob": "friends", "carol": "none"})
        users = self.client.get("/api/users/search/?q=zed").json()["users"]
        self.assertEqual([(u["username"], u["name"]) for u in users], [("carol", "Caroline Zed")])
        self.assertNotIn("alice", [u["username"] for u in self.client.get("/api/users/search/?q=ali").json()["users"]])

    def test_search_statuses_incoming_outgoing(self):
        Friendship.objects.create(from_user=self.a, to_user=self.b)
        Friendship.objects.create(from_user=self.c, to_user=self.a)
        self.login("alice")
        users = {u["username"]: u["status"] for u in self.client.get("/api/users/search/?q=r").json()["users"]}
        self.assertEqual(users, {"carol": "incoming"})
        users = {u["username"]: u["status"] for u in self.client.get("/api/users/search/?q=b").json()["users"]}
        self.assertEqual(users, {"bob": "outgoing"})

    def test_search_does_not_leak_private_fields(self):
        self.login("alice")
        user = self.client.get("/api/users/search/?q=bob").json()["users"][0]
        self.assertEqual(set(user), {"username", "name", "avatar", "initial", "color", "status"})


class ChatGatingTests(Base):
    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_cannot_start_chat_with_non_friend(self):
        self.login("alice")
        self.assertEqual(self.post("/api/conversations/", {"username": "bob"}).status_code, 403)
        self.act("bob", "request")  # pending is not enough
        self.assertEqual(self.post("/api/conversations/", {"username": "bob"}).status_code, 403)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_can_start_chat_once_accepted(self):
        self.login("alice"); self.act("bob", "request")
        self.login("bob"); self.act("alice", "accept")
        r = self.post("/api/conversations/", {"username": "alice"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()["title"], r.json()["can_send"]), ("alice", True))

    def _chat(self):
        f = Friendship.objects.create(from_user=self.a, to_user=self.b, status="accepted")
        self.login("alice")
        cid = self.post("/api/conversations/", {"username": "bob"}).json()["id"]
        return f, cid

    def test_unfriend_locks_sending_but_keeps_history(self):
        f, cid = self._chat()
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "hi"}).status_code, 201)
        f.delete()
        self.assertEqual(self.post(f"/api/conversations/{cid}/messages/", {"content": "again"}).status_code, 403)
        self.assertEqual(Message.objects.count(), 1)
        msgs = self.client.get(f"/api/conversations/{cid}/messages/").json()["messages"]
        self.assertEqual([m["content"] for m in msgs], ["hi"])
        conv = self.client.get("/api/conversations/").json()["conversations"][0]
        self.assertFalse(conv["can_send"])
        self.assertEqual(self.post("/api/conversations/", {"username": "bob"}).status_code, 403)

    def test_conversation_list_uses_display_name_and_avatar_fields(self):
        User.objects.filter(pk=self.b.pk).update(display_name="Bobby B")
        self._chat()
        conv = self.client.get("/api/conversations/").json()["conversations"][0]
        self.assertEqual((conv["title"], conv["other"]), ("Bobby B", "bob"))
        self.assertEqual((conv["face"]["initial"], conv["face"]["avatar"]), ("B", ""))

    def test_group_chats_are_not_gated(self):
        g = Conversation.objects.create(type=Conversation.GROUP, title="Team")
        for u in (self.a, self.b):
            ConversationMember.objects.create(conversation=g, user=u)
        self.login("alice")
        self.assertEqual(self.post(f"/api/conversations/{g.id}/messages/", {"content": "hey"}).status_code, 201)
        self.assertTrue(self.client.get("/api/conversations/").json()["conversations"][0]["can_send"])


class ProfilePageTests(Base):
    def test_profile_page_states(self):
        self.login("alice")
        cases = [("alice", "Edit profile"), ("bob", "Add friend")]
        for who, text in cases:
            self.assertContains(self.client.get(f"/u/{who}/"), text)
        self.act("bob", "request")
        self.assertContains(self.client.get("/u/bob/"), "Cancel request")
        self.login("bob")
        page = self.client.get("/u/alice/")
        self.assertContains(page, "Accept"); self.assertContains(page, "Decline")
        self.act("alice", "accept")
        page = self.client.get("/u/alice/")
        self.assertContains(page, "Message"); self.assertContains(page, "Unfriend")

    def test_profile_shows_display_name_and_escapes_it(self):
        User.objects.filter(pk=self.b.pk).update(display_name="<script>alert(1)</script>")
        self.login("alice")
        page = self.client.get("/u/bob/")
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertContains(page, "&lt;script&gt;")
        self.assertContains(page, "@bob")

    def test_profile_requires_login_and_404s(self):
        self.assertEqual(self.client.get("/u/bob/").status_code, 302)
        self.login("alice")
        self.assertEqual(self.client.get("/u/nobody/").status_code, 404)
        self.assertNotContains(self.client.get("/u/bob/"), "@example")  # email is never shown


class ProfileEditTests(Base):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(lambda: (self.override.disable(), shutil.rmtree(self.media, ignore_errors=True)))
        self.login("alice")

    def save(self, **data):
        return self.client.post("/profile/edit/", data)

    def test_display_name(self):
        r = self.save(display_name="  Alice A.  ")
        self.assertRedirects(r, "/u/alice/", fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertEqual((self.a.display_name, self.a.name), ("Alice A.", "Alice A."))
        self.save(display_name="")
        self.a.refresh_from_db()
        self.assertEqual(self.a.name, "alice")

    def test_display_name_too_long(self):
        self.assertEqual(self.save(display_name="x" * 61).status_code, 200)
        self.a.refresh_from_db(); self.assertEqual(self.a.display_name, "")

    def test_avatar_is_cropped_and_reencoded(self):
        r = self.save(display_name="A", avatar=image_upload((900, 300)))
        self.assertEqual(r.status_code, 302)
        self.a.refresh_from_db()
        self.assertTrue(self.a.avatar_url.startswith("/media/avatars/"))
        img = Image.open(self.a.avatar.path)
        self.assertEqual((img.size, img.format), ((256, 256), "JPEG"))

    def test_transparent_png_and_gif_are_accepted(self):
        self.assertEqual(self.save(avatar=image_upload((50, 50), "PNG", "RGBA")).status_code, 302)
        self.assertEqual(self.save(avatar=image_upload((50, 50), "GIF", "P", "a.gif")).status_code, 302)

    def test_rejects_non_images_and_huge_files(self):
        fake = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        self.assertEqual(self.save(avatar=fake).status_code, 200)
        script = SimpleUploadedFile("x.html", b"<script>alert(1)</script>", content_type="text/html")
        self.assertEqual(self.save(avatar=script).status_code, 200)
        big = SimpleUploadedFile("big.png", b"\x89PNG" + b"0" * (5 * 1024 * 1024 + 1), content_type="image/png")
        self.assertEqual(self.save(avatar=big).status_code, 200)
        self.a.refresh_from_db(); self.assertFalse(self.a.avatar)

    def test_replacing_and_removing_deletes_the_old_file(self):
        import os
        self.save(avatar=image_upload())
        self.a.refresh_from_db(); first = self.a.avatar.path
        self.assertTrue(os.path.exists(first))
        self.save(avatar=image_upload((300, 300)))
        self.a.refresh_from_db(); second = self.a.avatar.path
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(first)); self.assertTrue(os.path.exists(second))
        self.save(remove_avatar="on", display_name="")
        self.a.refresh_from_db()
        self.assertFalse(self.a.avatar); self.assertFalse(os.path.exists(second))

    def test_keeping_the_photo_when_only_the_name_changes(self):
        self.save(avatar=image_upload())
        self.a.refresh_from_db(); before = self.a.avatar.name
        self.save(display_name="New name")
        self.a.refresh_from_db()
        self.assertEqual(self.a.avatar.name, before)

    def test_cannot_edit_someone_elses_profile(self):
        self.login("bob")
        self.save(display_name="Hacked")
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.a.display_name, self.b.display_name), ("", "Hacked"))  # only ever their own

    def test_edit_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/profile/edit/").status_code, 302)


class RealtimeFriendTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.a, self.b = make_users("alice", "bob")

    async def connect(self, user):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        comm.scope["user"] = user
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        snap = await comm.receive_json_from()
        comm.snapshot = snap["online"]
        return comm

    def post(self, who, target, action):
        self.client.logout(); self.client.login(username=who, password=PW)
        return self.client.post(f"/api/friends/{target}/{action}/", "{}", content_type="application/json")

    async def test_request_and_accept_notify_live(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        self.assertEqual(self.snapshot_of(a), [])
        await database_sync_to_async(self.post)("alice", "bob", "request")
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["event"], ev["user"]), ("friends.changed", "request", "alice"))
        ev = await a.receive_json_from()   # the sender's own tabs refresh too
        self.assertEqual((ev["type"], ev["event"]), ("friends.changed", "request"))
        await database_sync_to_async(self.post)("bob", "alice", "accept")
        ev = await a.receive_json_from()
        self.assertEqual((ev["event"], ev["user"], ev["name"]), ("accepted", "bob", "bob"))
        await b.receive_json_from()
        await a.disconnect(); await b.disconnect()

    def snapshot_of(self, comm):
        return comm.snapshot

    async def test_repeat_request_sends_no_second_notification(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        await database_sync_to_async(self.post)("alice", "bob", "request")
        await b.receive_json_from(); await a.receive_json_from()
        await database_sync_to_async(self.post)("alice", "bob", "request")
        self.assertTrue(await b.receive_nothing(0.3))
        await a.disconnect(); await b.disconnect()

    async def test_friends_see_each_others_presence_without_a_chat(self):
        await database_sync_to_async(Friendship.objects.create)(from_user=self.a, to_user=self.b, status="accepted")
        a = await self.connect(self.a)
        b = await self.connect(self.b)
        self.assertEqual(b.snapshot, ["alice"])
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["user"], ev["online"]), ("presence", "bob", True))
        await a.disconnect(); await b.disconnect()

    async def _direct(self, friends=True):
        conv = await database_sync_to_async(Conversation.objects.create)()
        for u in (self.a, self.b):
            await database_sync_to_async(ConversationMember.objects.create)(conversation=conv, user=u)
        if friends:
            await database_sync_to_async(Friendship.objects.create)(from_user=self.a, to_user=self.b, status="accepted")
        return conv

    async def test_websocket_message_rejected_when_not_friends(self):
        conv = await self._direct(friends=False)
        a, b = await self.connect(self.a), await self.connect(self.b)
        await a.receive_json_from()  # bob's presence, since they share a conversation
        await a.send_json_to({"type": "message", "conversation": conv.id, "content": "hi", "client_id": "k1"})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["client_id"]), ("error", "k1"))
        self.assertTrue(await b.receive_nothing(0.3))
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 0)
        await a.disconnect(); await b.disconnect()

    async def test_unfriend_takes_effect_on_an_open_connection(self):
        conv = await self._direct()
        a, b = await self.connect(self.a), await self.connect(self.b)
        await a.receive_json_from()
        await a.send_json_to({"type": "message", "conversation": conv.id, "content": "one", "client_id": "m1"})
        await a.receive_json_from(); await b.receive_json_from()
        await database_sync_to_async(self.post)("bob", "alice", "remove")
        await a.receive_json_from(); await b.receive_json_from()  # friends.changed for both
        await a.send_json_to({"type": "message", "conversation": conv.id, "content": "two", "client_id": "m2"})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["client_id"]), ("error", "m2"))
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 1)
        await a.disconnect(); await b.disconnect()
