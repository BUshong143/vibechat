import time

from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.social.models import Friendship
from . import calls
from .consumers import ChatConsumer
from .models import Conversation, ConversationMember

PW = "pw-12345!"
CID = "call-0001-abcdef"


def befriend(a, b):
    return Friendship.objects.create(from_user=a, to_user=b, status=Friendship.ACCEPTED)


def direct(a, b):
    conv = Conversation.objects.create()
    for u in (a, b):
        ConversationMember.objects.create(conversation=conv, user=u)
    return conv


class CallRulesTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_call_ids_are_validated(self):
        self.assertTrue(calls.valid_call_id("abcd1234"))
        self.assertTrue(calls.valid_call_id("3f2b9c1e-7a1d-4a55-9b1e-0c7d1f2a3b4c"))
        for bad in ("short", "has space 123", "x" * 65, None, 12345678, "semi;colon-1"):
            self.assertFalse(calls.valid_call_id(bad), bad)

    def test_one_call_per_person_and_id_is_unique(self):
        rec, why = calls.start(1, 2, 10, "call-aaaa-0001", True, "chan-1")
        self.assertIsNone(why)
        self.assertEqual(calls.start(3, 2, 11, "call-aaaa-0002", True, "chan-3"), (None, "busy"))
        self.assertEqual(calls.start(1, 3, 12, "call-aaaa-0003", True, "chan-1"), (None, "in_call"))
        self.assertEqual(calls.start(4, 5, 13, "call-aaaa-0001", True, "chan-4"), (None, "exists"))
        self.assertIsNone(calls.current(3))  # the failed attempts left nothing behind

    def test_unanswered_call_times_out_and_frees_both_people(self):
        rec, _ = calls.start(1, 2, 10, CID, True, "chan-1")
        rec["ring_until"] = time.time() - 1
        cache.set(calls._ckey(CID), rec, 60)
        self.assertIsNone(calls.current(2))
        self.assertIsNone(calls.accept(CID, 2, "chan-2"))
        rec, why = calls.start(3, 2, 11, "call-bbbb-0001", True, "chan-3")
        self.assertIsNone(why)

    def test_only_the_callee_can_accept_and_only_once(self):
        calls.start(1, 2, 10, CID, True, "chan-1")
        self.assertIsNone(calls.accept(CID, 1, "chan-1"))   # the caller cannot answer their own call
        self.assertIsNone(calls.accept(CID, 9, "chan-9"))   # nor a stranger
        self.assertIsNotNone(calls.accept(CID, 2, "chan-2"))
        self.assertIsNone(calls.accept(CID, 2, "chan-2b"))  # a second tab is too late

    def test_end_reasons_and_strangers(self):
        calls.start(1, 2, 10, CID, True, "chan-1")
        self.assertEqual(calls.end(CID, 9), (None, None))
        self.assertEqual(calls.end(CID, 2)[1], "declined")
        self.assertIsNone(calls.current(1))
        calls.start(1, 2, 10, CID, True, "chan-1")
        self.assertEqual(calls.end(CID, 1)[1], "cancelled")
        calls.start(1, 2, 10, CID, True, "chan-1")
        calls.accept(CID, 2, "chan-2")
        self.assertEqual(calls.end(CID, 2)[1], "hangup")

    def test_signals_only_between_the_two_connections_carrying_the_call(self):
        calls.start(1, 2, 10, CID, True, "chan-1")
        self.assertIsNone(calls.signal_target(CID, 1, "chan-1"))  # not answered yet
        calls.accept(CID, 2, "chan-2")
        self.assertEqual(calls.signal_target(CID, 1, "chan-1"), "chan-2")
        self.assertEqual(calls.signal_target(CID, 2, "chan-2"), "chan-1")
        self.assertIsNone(calls.signal_target(CID, 2, "chan-other-tab"))
        self.assertIsNone(calls.signal_target(CID, 9, "chan-9"))

    def test_dropping_a_connection_ends_only_the_call_it_carried(self):
        calls.start(1, 2, 10, CID, True, "chan-1")
        calls.accept(CID, 2, "chan-2")
        self.assertIsNone(calls.drop_for_channel(2, "chan-other-tab"))
        self.assertIsNotNone(calls.current(2))
        self.assertEqual(calls.drop_for_channel(2, "chan-2")["id"], CID)
        self.assertIsNone(calls.current(1))

    @override_settings(CALL_STUN_URLS=["stun:s.example:3478"], CALL_TURN_URLS=[])
    def test_ice_servers_stun_only(self):
        self.assertEqual(calls.ice_servers(User(pk=1)), [{"urls": ["stun:s.example:3478"]}])

    @override_settings(CALL_TURN_URLS=["turn:t.example:3478"], CALL_TURN_SECRET="s3cret")
    def test_ice_servers_get_time_limited_turn_credentials(self):
        turn = calls.ice_servers(User(pk=7))[1]
        expiry, uid = turn["username"].split(":")
        self.assertEqual(uid, "7")
        self.assertGreater(int(expiry), time.time())
        self.assertTrue(turn["credential"])

    def test_config_endpoint_needs_login(self):
        self.assertEqual(self.client.get("/api/calls/config/").status_code, 302)
        User.objects.create_user("alice", password=PW)
        self.client.login(username="alice", password=PW)
        r = self.client.get("/api/calls/config/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("iceServers", r.json())


class CallSignallingTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user("alice", password=PW)
        self.b = User.objects.create_user("bob", password=PW)
        self.c = User.objects.create_user("carol", password=PW)
        befriend(self.a, self.b)
        befriend(self.a, self.c)
        self.ab = direct(self.a, self.b)
        self.ac = direct(self.a, self.c)
        self.group = Conversation.objects.create(type=Conversation.GROUP, title="Squad")
        for u in (self.a, self.b, self.c):
            ConversationMember.objects.create(conversation=self.group, user=u)

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

    async def invite(self, comm, conv, call_id=CID):
        await comm.send_json_to({"type": "call.invite", "conversation": conv.id, "call_id": call_id, "video": True})

    async def answered_call(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        await self.drain(a, b)
        await self.invite(a, self.ab)
        await a.receive_json_from(); await b.receive_json_from()
        await b.send_json_to({"type": "call.accept", "call_id": CID})
        await a.receive_json_from(); await b.receive_json_from()
        return a, b

    async def test_full_call_flow(self):
        a, b, c = await self.connect(self.a), await self.connect(self.b), await self.connect(self.c)
        await self.drain(a, b, c)
        await self.invite(a, self.ab)
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["call_id"]), ("call.ringing", CID))
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["call_id"], ev["user"], ev["conversation"], ev["video"]),
                         ("call.incoming", CID, "alice", self.ab.id, True))
        await b.send_json_to({"type": "call.accept", "call_id": CID})
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["call_id"]), ("call.accepted", CID))
        await a.send_json_to({"type": "call.signal", "call_id": CID, "data": {"sdp": {"type": "offer", "sdp": "v=0"}}})
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["data"]["sdp"]["type"]), ("call.signal", "offer"))
        await b.send_json_to({"type": "call.signal", "call_id": CID, "data": {"sdp": {"type": "answer", "sdp": "v=0"}}})
        ev = await a.receive_json_from()
        self.assertEqual((ev["type"], ev["data"]["sdp"]["type"]), ("call.signal", "answer"))
        await a.send_json_to({"type": "call.end", "call_id": CID})
        for comm in (a, b):
            ev = await comm.receive_json_from()
            self.assertEqual((ev["type"], ev["reason"]), ("call.ended", "hangup"))
        self.assertTrue(await c.receive_nothing(0.2))  # a third person hears nothing at all
        for comm in (a, b, c):
            await comm.disconnect()

    async def test_decline_and_cancel_and_missed(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        await self.drain(a, b)
        for who, expected, extra in ((b, "declined", {}), (a, "cancelled", {}), (a, "missed", {"reason": "missed"})):
            await self.invite(a, self.ab)
            await a.receive_json_from(); await b.receive_json_from()
            await who.send_json_to({"type": "call.end", "call_id": CID, **extra})
            for comm in (a, b):
                ev = await comm.receive_json_from()
                self.assertEqual((ev["type"], ev["reason"]), ("call.ended", expected))
        await a.disconnect(); await b.disconnect()

    async def test_busy_and_offline_and_group_and_not_friends(self):
        a, b, c = await self.connect(self.a), await self.connect(self.b), await self.connect(self.c)
        await self.drain(a, b, c)
        await self.invite(a, self.ab)
        await a.receive_json_from(); await b.receive_json_from()
        await self.invite(c, self.ac, "call-0002-abcdef")  # alice is already ringing with bob
        ev = await c.receive_json_from()
        self.assertEqual((ev["type"], ev["reason"]), ("call.failed", "busy"))
        self.assertTrue(await a.receive_nothing(0.2))
        await b.send_json_to({"type": "call.end", "call_id": CID})
        await self.drain(a, b, c)

        await self.invite(a, self.group, "call-0003-abcdef")
        self.assertEqual((await a.receive_json_from())["reason"], "group")

        await database_delete_friendship(self.a, self.b)
        await self.invite(a, self.ab, "call-0004-abcdef")
        self.assertEqual((await a.receive_json_from())["reason"], "not_friends")
        self.assertTrue(await b.receive_nothing(0.2))

        await c.disconnect()
        await self.drain(a)
        await self.invite(a, self.ac, "call-0005-abcdef")
        self.assertEqual((await a.receive_json_from())["reason"], "offline")
        await a.disconnect(); await b.disconnect()

    async def test_only_the_two_connections_can_signal(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        c = await self.connect(self.c)
        b2 = await self.connect(self.b)  # bob's second tab
        await self.drain(a, b, c, b2)
        await self.invite(a, self.ab)
        for comm in (a, b, b2):
            await comm.receive_json_from()
        frame = {"type": "call.signal", "call_id": CID, "data": {"candidate": {"candidate": "x"}}}
        await a.send_json_to(frame)                       # before it is answered: refused
        self.assertTrue(await b.receive_nothing(0.2))
        await b.send_json_to({"type": "call.accept", "call_id": CID})
        await self.drain(a, b, b2)
        await c.send_json_to(frame)                       # a bystander
        await b2.send_json_to(frame)                      # the wrong tab of a real participant
        self.assertTrue(await a.receive_nothing(0.2))
        await a.send_json_to(frame)
        ev = await b.receive_json_from()
        self.assertEqual(ev["type"], "call.signal")
        self.assertTrue(await b2.receive_nothing(0.2))    # the other tab never sees the call's set-up traffic
        await a.send_json_to({"type": "call.signal", "call_id": CID, "data": {"sdp": "x" * 30000}})
        self.assertTrue(await b.receive_nothing(0.2))     # oversized messages are dropped
        for comm in (a, b, b2, c):
            await comm.disconnect()

    async def test_two_tabs_ring_and_only_the_first_to_answer_wins(self):
        a, b1, b2 = await self.connect(self.a), await self.connect(self.b), await self.connect(self.b)
        await self.drain(a, b1, b2)
        await self.invite(a, self.ab)
        await a.receive_json_from()
        for comm in (b1, b2):
            self.assertEqual((await comm.receive_json_from())["type"], "call.incoming")
        await b1.send_json_to({"type": "call.accept", "call_id": CID})
        for comm in (a, b1, b2):
            self.assertEqual((await comm.receive_json_from())["type"], "call.accepted")
        await b2.send_json_to({"type": "call.accept", "call_id": CID})
        ev = await b2.receive_json_from()
        self.assertEqual((ev["type"], ev["reason"]), ("call.failed", "gone"))
        await a.disconnect(); await b1.disconnect(); await b2.disconnect()

    async def test_closing_the_tab_hangs_up(self):
        a, b = await self.answered_call()
        await a.disconnect()
        ev = await b.receive_json_from()
        self.assertEqual((ev["type"], ev["reason"]), ("call.ended", "dropped"))
        await b.disconnect()
        # and nobody is left "busy"
        self.assertIsNone(calls.current(self.a.id))
        self.assertIsNone(calls.current(self.b.id))

    async def test_bad_frames_are_ignored(self):
        a, b = await self.connect(self.a), await self.connect(self.b)
        await self.drain(a, b)
        for frame in ({"type": "call.invite"}, {"type": "call.invite", "call_id": "x"},
                      {"type": "call.invite", "call_id": CID, "conversation": "1"},
                      {"type": "call.accept", "call_id": 5}, {"type": "call.nonsense", "call_id": CID}):
            await a.send_json_to(frame)
        self.assertTrue(await a.receive_nothing(0.2))
        self.assertTrue(await b.receive_nothing(0.2))
        await a.disconnect(); await b.disconnect()


async def database_delete_friendship(a, b):
    from channels.db import database_sync_to_async
    await database_sync_to_async(Friendship.objects.all().delete)()
