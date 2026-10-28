import copy
import unittest
from datetime import datetime, timedelta, timezone

from leadmagnet_engine import EngineConfig, LeadMagnetEngine


UTC = timezone.utc
NOW = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
ENTRY = {"code": "AVANS", "keyword": "ВЫПИСКА", "title": "Чек-лист аванса",
         "url": "https://example.test/avans"}


class MemoryStore:
    def __init__(self, state=None, fail_at=()):
        self.state = copy.deepcopy(state or {})
        self.fail_at = set(fail_at)
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.saves += 1
        if self.saves in self.fail_at:
            raise OSError("synthetic store failure")
        self.state = copy.deepcopy(state)


class FakeTransport:
    def __init__(self, follows=True):
        self.follows = follows
        self.follow_error = None
        self.private_error = None
        self.send_error = None
        self.incoming = []
        self.private_calls = []
        self.send_calls = []
        self.incoming_calls = []

    def get_follow_status(self, uid):
        if self.follow_error:
            raise self.follow_error
        return self.follows

    def private_reply(self, comment_id, text):
        self.private_calls.append((comment_id, text))
        if self.private_error:
            raise self.private_error

    def send_message(self, uid, text):
        self.send_calls.append((uid, text))
        if self.send_error:
            raise self.send_error

    def recent_incoming(self, uid, since):
        self.incoming_calls.append((uid, since))
        return copy.deepcopy(self.incoming)


def make_comment(*, at=None, text="Нужна ВЫПИСКА", media="media-1", uid="user-1", cid="comment-1"):
    return {"id": cid, "user_id": uid, "media_id": media,
            "text": text, "timestamp": at or NOW}


def build(transport=None, store=None):
    return LeadMagnetEngine(
        EngineConfig(entries=[ENTRY], enabled_since=NOW - timedelta(days=8),
                     allowlisted_media_ids=frozenset({"media-1"}), hash_key=b"test-only-secret"),
        transport or FakeTransport(), store or MemoryStore())


class LeadMagnetTests(unittest.TestCase):
    def test_refusal_after_request_prevents_delivery(self):
        t, s = FakeTransport(False), MemoryStore()
        e = build(t, s)
        e.run([make_comment()], NOW)
        t.follows = True
        t.incoming = [
            {"id":"request", "created_time":NOW+timedelta(minutes=1), "text":"ПОЛУЧИТЬ"},
            {"id":"cancel", "created_time":NOW+timedelta(minutes=2), "text":"Не присылайте"}]
        e.run([make_comment()], NOW+timedelta(minutes=3))
        e.run([make_comment()], NOW+timedelta(minutes=4))
        self.assertEqual(t.send_calls, [])

    def test_corrupt_prior_claim_fails_closed(self):
        s = MemoryStore({'claims': {'comment:bad': {'status':'uncertain'}}})
        t = FakeTransport(True)
        with self.assertRaises(ValueError): build(t, s).run([make_comment()], NOW)
        self.assertEqual(t.private_calls, [])

    def test_follow_true_sends_link_once_and_never_opens_standard_dm_path(self):
        t, s = FakeTransport(True), MemoryStore()
        e = build(t, s)
        result = e.run([make_comment()], NOW)
        self.assertEqual(result["private_replies_sent"], 1)
        self.assertIn(ENTRY["url"], t.private_calls[0][1])
        self.assertEqual(t.send_calls, [])
        e.run([make_comment(cid="comment-2")], NOW + timedelta(minutes=1))
        self.assertEqual(len(t.private_calls), 1)
        self.assertNotIn("user-1", repr(s.state))
        self.assertNotIn("comment-1", repr(s.state))
        self.assertNotIn("Нужна ВЫПИСКА", repr(s.state))

    def test_false_and_unknown_get_only_honest_invitation_until_incoming_reply(self):
        for follows, expected in ((False, "Подпишитесь"), (None, "Не получилось проверить")):
            with self.subTest(follows=follows):
                t = FakeTransport(follows)
                build(t).run([make_comment()], NOW)
                self.assertIn(expected, t.private_calls[0][1])
                self.assertNotIn(ENTRY["url"], t.private_calls[0][1])
                self.assertEqual(t.send_calls, [])

    def test_incoming_rechecks_false_then_true_and_sends_link_in_24h(self):
        t, s = FakeTransport(False), MemoryStore()
        e = build(t, s)
        e.run([make_comment()], NOW)
        t.incoming = [{"id": "msg-1", "created_time": NOW + timedelta(minutes=1), "text": "ПОЛУЧИТЬ"}]
        e.run([make_comment()], NOW + timedelta(minutes=2))
        self.assertIn("Пока не вижу подписку", t.send_calls[0][1])
        self.assertNotIn(ENTRY["url"], t.send_calls[0][1])
        t.follows = True
        t.incoming = [{"id": "msg-2", "created_time": NOW + timedelta(minutes=3), "text": "ГОТОВО"}]
        e.run([make_comment()], NOW + timedelta(minutes=4))
        self.assertIn(ENTRY["url"], t.send_calls[-1][1])
        t.incoming.append({"id": "msg-3", "created_time": NOW + timedelta(minutes=5), "text": "ещё"})
        e.run([make_comment()], NOW + timedelta(minutes=6))
        self.assertEqual(sum(ENTRY["url"] in text for _, text in t.send_calls), 1)

    def test_comment_time_media_exact_keyword_and_7day_boundaries(self):
        t = FakeTransport(True)
        e = build(t)
        comments = [
            make_comment(cid="at-enabled", at=NOW - timedelta(days=1)),
            make_comment(cid="at-cutoff", uid="cutoff-user", at=NOW - timedelta(days=7)),
            make_comment(cid="too-old", at=NOW - timedelta(days=7, seconds=1)),
            make_comment(cid="future", at=NOW + timedelta(microseconds=1)),
            make_comment(cid="wrong-media", media="other"),
            make_comment(cid="substring", text="ВЫПИСКАМИ"),
        ]
        result = e.run(comments, NOW)
        self.assertEqual(result["private_replies_sent"], 2)
        self.assertEqual({cid for cid, _ in t.private_calls}, {"at-enabled", "at-cutoff"})

    def test_enabled_since_is_inclusive(self):
        t = FakeTransport(True)
        enabled = NOW - timedelta(hours=3)
        e = LeadMagnetEngine(EngineConfig([ENTRY], enabled, frozenset({"media-1"}), b"k"), t, MemoryStore())
        result = e.run([make_comment(at=enabled), make_comment(at=enabled - timedelta(microseconds=1), cid="before")], NOW)
        self.assertEqual(result["private_replies_sent"], 1)
        self.assertEqual(t.private_calls[0][0], "comment-1")

    def test_follow_check_error_is_unknown_not_false_negative(self):
        t = FakeTransport(True)
        t.follow_error = RuntimeError("API unavailable")
        build(t).run([make_comment()], NOW)
        self.assertIn("Не получилось проверить", t.private_calls[0][1])

    def test_send_timeout_is_uncertain_and_not_retried_after_restart(self):
        t, s = FakeTransport(True), MemoryStore()
        t.private_error = TimeoutError("ambiguous delivery")
        build(t, s).run([make_comment()], NOW)
        t.private_error = None
        result = build(t, s).run([make_comment(cid="comment-new-id")], NOW + timedelta(minutes=1))
        self.assertEqual(result["private_replies_sent"], 0)
        self.assertEqual(len(t.private_calls), 1)
        self.assertTrue(any(x["status"] == "uncertain" for x in s.state["claims"].values()))

    def test_claim_store_failure_prevents_send(self):
        t, s = FakeTransport(True), MemoryStore(fail_at={1})
        result = build(t, s).run([make_comment()], NOW)
        self.assertEqual(t.private_calls, [])
        self.assertEqual(result["errors"], 1)

    def test_valid_worker_poll_throttle_metadata_survives_engine_save(self):
        for initial, should_keep in (("2026-10-28T17:50:00Z", True), ("not-a-time", False)):
            with self.subTest(initial=initial):
                s = MemoryStore({"last_poll_started_at": initial, "unexpected_raw_id": "private"})
                build(FakeTransport(True), s).run([make_comment()], NOW)
                self.assertEqual("last_poll_started_at" in s.state, should_keep)
                if should_keep:
                    self.assertEqual(s.state["last_poll_started_at"], "2026-10-28T17:50:00+00:00")
                self.assertNotIn("unexpected_raw_id", repr(s.state))

    def test_partial_failure_after_send_never_repeats_side_effect(self):
        # The initial claim+user snapshot is save 1; final sent-state save is 2.
        t, s = FakeTransport(True), MemoryStore(fail_at={2})
        build(t, s).run([make_comment()], NOW)
        self.assertEqual(len(t.private_calls), 1)
        build(t, s).run([make_comment(cid="comment-repolled")], NOW + timedelta(minutes=1))
        self.assertEqual(len(t.private_calls), 1)

    def test_incoming_link_timeout_cannot_be_bypassed_by_new_message_id(self):
        t, s = FakeTransport(False), MemoryStore()
        e = build(t, s)
        e.run([make_comment()], NOW)
        t.follows = True
        t.send_error = TimeoutError("ambiguous delivery")
        t.incoming = [{"id": "reply-1", "created_time": NOW + timedelta(minutes=1), "text": "ГОТОВО"}]
        e.run([make_comment()], NOW + timedelta(minutes=2))
        t.send_error = None
        t.incoming = [{"id": "reply-2", "created_time": NOW + timedelta(minutes=3), "text": "ПРОВЕРИТЬ"}]
        build(t, s).run([make_comment()], NOW + timedelta(minutes=4))
        self.assertEqual(sum(ENTRY["url"] in text for _, text in t.send_calls), 1)

    def test_incoming_link_final_store_failure_keeps_delivery_claim(self):
        # Initial state = 2 saves. Incoming event plus durable link claim is
        # save 3; post-send status save 4 fails.
        t, s = FakeTransport(False), MemoryStore(fail_at={4})
        e = build(t, s)
        e.run([make_comment()], NOW)
        t.follows = True
        t.incoming = [{"id": "reply-1", "created_time": NOW + timedelta(minutes=1), "text": "ГОТОВО"}]
        e.run([make_comment()], NOW + timedelta(minutes=2))
        t.incoming = [{"id": "reply-2", "created_time": NOW + timedelta(minutes=3), "text": "ПРОВЕРИТЬ"}]
        build(t, s).run([make_comment()], NOW + timedelta(minutes=4))
        self.assertEqual(sum(ENTRY["url"] in text for _, text in t.send_calls), 1)

    def test_duplicate_comment_and_same_user_code_are_deduped(self):
        t = FakeTransport(False)
        e = build(t)
        e.run([make_comment(), make_comment()], NOW)
        e.run([make_comment(cid="another-comment")], NOW + timedelta(minutes=1))
        self.assertEqual(len(t.private_calls), 1)

    def test_incoming_message_at_24h_boundary_is_allowed_but_later_is_not(self):
        t, s = FakeTransport(False), MemoryStore()
        e = build(t, s)
        e.run([make_comment()], NOW)
        t.follows = True
        t.incoming = [{"id": "boundary", "created_time": NOW + timedelta(hours=24), "text": "ГОТОВО"}]
        e.run([make_comment()], NOW + timedelta(hours=24))
        self.assertEqual(len(t.send_calls), 1)
        self.assertIn(ENTRY["url"], t.send_calls[0][1])

    def test_no_public_comment_replies_and_no_unallowlisted_media(self):
        t = FakeTransport(True)
        result = build(t).run([make_comment(media="not-allowed")], NOW)
        self.assertEqual(result["private_replies_sent"], 0)
        self.assertEqual(t.private_calls, [])
        self.assertEqual(t.send_calls, [])


if __name__ == "__main__":
    unittest.main()
