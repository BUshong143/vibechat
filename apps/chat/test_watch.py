import time

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.social.models import Friendship
from . import watch
from .consumers import ChatConsumer
from .models import Conversation, ConversationMember

PW = "pw-12345!"
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class ParseTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_supported_links(self):
        good = {
            YT: ("youtube", "dQw4w9WgXcQ"),
            "https://youtu.be/dQw4w9WgXcQ?t=42": ("youtube", "dQw4w9WgXcQ"),
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=x": ("youtube", "dQw4w9WgXcQ"),
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": ("youtube", "dQw4w9WgXcQ"),
            "https://www.youtube.com/embed/dQw4w9WgXcQ": ("youtube", "dQw4w9WgXcQ"),
            "https://vimeo.com/76979871": ("vimeo", "76979871"),
            "https://vimeo.com/76979871/abcdef1234": ("vimeo", "76979871/abcdef1234"),
            "https://player.vimeo.com/video/76979871?h=abcdef1234": ("vimeo", "76979871/abcdef1234"),
            "https://vimeo.com/channels/staffpicks/76979871": ("vimeo", "76979871"),
            "https://x.org/a/movie.MP4?token=1": ("file", "https://x.org/a/movie.MP4?token=1"),
            "https://x.org/v.webm": ("file", "https://x.org/v.webm"),
        }
        for url, expected in good.items():
            self.assertEqual(watch.parse(url), expected, url)

    @override_settings(DEBUG=False)
    def test_everything_else_is_refused(self):
        bad = ["", "   ", None, 5, "not a url", "http://x.org/v.mp4", "javascript:alert(1)",
               "https://evil.com/watch?v=dQw4w9WgXcQ", "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
               "https://www.youtube.com/watch?v=short", "https://www.youtube.com/", "https://user:pw@x.org/v.mp4",
               "https://x.org/page.html", "https://vimeo.com/about", "https://x.org/a b.mp4",
               "https://x.org/" + "a" * 600 + ".mp4", "file:///etc/passwd.mp4", "data:video/mp4;base64,AAAA",
               "https://x.org:99999/v.mp4", "https://player.vimeo.com/foo/76979871"]
        for url in bad:
            self.assertIsNone(watch.parse(url), url)

    @override_settings(DEBUG=True)
    def test_plain_http_only_while_developing(self):
        self.assertEqual(watch.parse("http://localhost:8000/v.mp4")[0], "file")

    def test_state_validation(self):
        self.assertTrue(watch.valid_state(True, 0) and watch.valid_state(False, 12.5))
        for bad in (("true", 1), (True, -1), (True, True), (True, 1e9), (True, None)):
            self.assertFalse(watch.valid_state(*bad), bad)


class SessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user("alice", password=PW)

    def test_position_moves_on_only_while_playing(self):
        watch.start(5, self.u, "youtube", "dQw4w9WgXcQ")
        self.assertEqual(watch.view(watch.get(5))["position"], 0)
        rec = watch.update(5, True, 10)
        rec["at"] -= 3
        self.assertAlmostEqual(watch.view(rec)["position"], 13, delta=0.3)
        rec = watch.update(5, False, 20)
        rec["at"] -= 3
        self.assertEqual(watch.view(rec)["position"], 20)

    def test_update_and_stop_need_a_session(self):
        self.assertIsNone(watch.update(99, True, 1))
        watch.start(5, self.u, "vimeo", "1")
        self.assertIsNotNone(watch.stop(5))
        self.assertIsNone(watch.stop(5))


class WatchSocketTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user("alice", password=PW)
        self.b = User.objects.create_user("bob", password=PW)
        self.c = User.objects.create_user("carol", password=PW)
        Friendship.objects.create(from_user=self.a, to_user=self.b, status=Friendship.ACCEPTED)
        self.conv = Conversation.objects.create()
        for u in (self.a, self.b):
            ConversationMember.objects.create(conversation=self.conv, user=u)
        self.group = Conversation.objects.create(type=Conversation.GROUP, title="Squad")
        for u in (self.a, self.b, self.c):
            ConversationMember.objects.create(conversation=self.group, user=u)

    async def connect(self, user):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        comm.scope["user"] = user
        ok, _ = await comm.connect()
        self.assertTrue(ok)
        self.assertEqual((await comm.receive_json_from())["type"], "presence.snapshot")
        return comm

    async def drain(self, *comms):
        for comm in comms:
            while not await comm.receive_nothing(0.15):
                await comm.receive_output()

    async def send(self, comm, kind, **extra):
        await comm.send_json_to({"type": kind, "conversation": self.conv.id, **extra})

    async def test_start_state_and_stop_reach_the_other_person_only(self):
        a, b, c = await self.connect(self.a), await self.connect(self.b), await self.connect(self.c)
        await self.drain(a, b, c)
        await self.send(a, "watch.start", url=YT)
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["provider"], ev["ref"], ev["by"], ev["playing"], ev["position"]),
                             ("watch.started", "youtube", "dQw4w9WgXcQ", "alice", False, 0))
        await self.send(b, "watch.state", playing=True, position=12.5)   # either person can control it
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["by"], ev["playing"], ev["position"]), ("watch.state", "bob", True, 12.5))
        self.assertTrue(await b.receive_nothing(0.2))                     # no echo to the sender
        await self.send(a, "watch.stop")
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["by"]), ("watch.stopped", "alice"))
        self.assertTrue(await c.receive_nothing(0.2))                     # a third person hears nothing
        for comm in (a, b, c):
            await comm.disconnect()

    async def test_joining_late_gets_the_current_moment(self):
        a = await self.connect(self.a)
        await self.drain(a)
        await self.send(a, "watch.sync")
        self.assertEqual((await a.receive_json_from())["type"], "watch.none")
        await self.send(a, "watch.start", url=YT)
        await a.receive_json_from()
        await self.send(a, "watch.state", playing=True, position=30)
        b = await self.connect(self.b)
        await self.drain(a, b)
        await self.send(b, "watch.sync")
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["sync"], ev["playing"]), ("watch.started", True, True))
        self.assertGreaterEqual(ev["position"], 30)
        await a.disconnect(); await b.disconnect()

    async def test_refusals(self):
        a, b, c = await self.connect(self.a), await self.connect(self.b), await self.connect(self.c)
        await self.drain(a, b, c)
        await self.send(a, "watch.start", url="https://evil.example/page.html")
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["reason"]), ("watch.failed", "bad_url"))
        await a.send_json_to({"type": "watch.start", "conversation": self.group.id, "url": YT})
        self.assertEqual((await a.receive_json_from())["reason"], "group")
        await c.send_json_to({"type": "watch.start", "conversation": self.conv.id, "url": YT})   # not a member
        self.assertTrue(await c.receive_nothing(0.2))
        self.assertTrue(await b.receive_nothing(0.2))
        await self.send(a, "watch.state", playing=True, position=5)                              # nothing playing yet
        self.assertTrue(await b.receive_nothing(0.2))
        await self.send(a, "watch.start", url=YT)
        await self.drain(a, b)
        for junk in ({"playing": "yes", "position": 5}, {"playing": True, "position": -3}, {"playing": True, "position": None}):
            await self.send(a, "watch.state", **junk)
        self.assertTrue(await b.receive_nothing(0.2))
        await database_sync_to_async(Friendship.objects.all().delete)()                          # unfriended
        await self.send(a, "watch.state", playing=True, position=5)
        self.assertEqual((await a.receive_json_from())["reason"], "not_friends")
        await self.send(b, "watch.start", url=YT)
        self.assertEqual((await b.receive_json_from())["reason"], "not_friends")
        for comm in (a, b, c):
            await comm.disconnect()

    async def test_state_updates_are_rate_limited(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        await self.send(a, "watch.start", url=YT)
        await self.drain(a, b)
        for pos in (1, 2, 3):
            await self.send(a, "watch.state", playing=True, position=pos)
        ev = await b.receive_json_from()
        self.assertEqual(ev["position"], 1)
        self.assertTrue(await b.receive_nothing(0.3))
        await a.disconnect(); await b.disconnect()
