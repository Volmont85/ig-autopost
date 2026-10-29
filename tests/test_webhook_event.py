import base64
import copy
import unittest
from datetime import datetime, timedelta, timezone

from webhook.webhook_event import decrypt_events, encrypt_events, parse_events

NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
KEY = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


class ParseEventsTests(unittest.TestCase):
    def setUp(self):
        self.data = {"id": "100", "entry": [{"id": "100", "time": 1793210400,
            "changes": [{"field": "comments", "value": {"id": "200", "from_id": "300",
                "media": {"id": "400"}, "text": "Нужна ВЫПИСКА", "timestamp": 1793210400}}]}]}

    def test_change_and_direct_entry_forms_and_milliseconds(self):
        self.data["entry"].append({"id": "100", "field": "comments", "value": {
            "id": "201", "user_id": "301", "media_id": "400", "text": "выПИСКА",
            "timestamp": 1793210400000}})
        out = parse_events(self.data, "100", {"400"}, ["выписка"])
        self.assertEqual([x["id"] for x in out], ["200", "201"])
        self.assertEqual(out[0]["timestamp"], "2026-10-28T18:00:00Z")

    def test_messages_are_not_keyword_filtered_but_echoes_and_self_ignored(self):
        self.data["entry"][0]["messaging"] = [
            {"sender": {"id": "300"}, "recipient": {"id": "100"}, "timestamp": 1793210400,
             "message": {"mid": "500", "text": "stop"}},
            {"sender": {"id": "100"}, "recipient": {"id": "100"}, "timestamp": 1793210400,
             "message": {"mid": "501", "text": "self"}},
            {"sender": {"id": "300"}, "recipient": {"id": "100"}, "timestamp": 1793210400,
             "message": {"mid": "502", "text": "echo", "is_echo": True}},
        ]
        out = parse_events(self.data, "100", {"400"}, ["выписка"])
        self.assertEqual([x["id"] for x in out], ["200", "500"])
        self.assertEqual(out[1]["text"], "stop")

    def test_bad_items_are_skipped_and_missing_comment_timestamp_uses_entry_time(self):
        self.data["entry"][0]["changes"] = [None, {"field": "comments", "value": {"id": "bad"}},
            {"field": "comments", "value": {"id": "202", "from_id": "302", "media": {"id": "400"},
                "text": "ВЫПИСКА"}}]
        out = parse_events(self.data, "100", {"400"}, ["выписка"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["timestamp"], "2026-10-28T18:00:00Z")

    def test_rejects_wrong_accounts_media_substrings_and_invalid_values(self):
        self.data["entry"].extend([
            {"id": "999", "changes": self.data["entry"][0]["changes"]},
            {"id": "100", "changes": [{"field": "comments", "value": {"id": "203", "from_id": "303", "media": {"id": "401"}, "text": "ВЫПИСКА", "timestamp": 1793210400}}]},
            {"id": "100", "changes": [{"field": "comments", "value": {"id": "204", "from_id": "304", "media": {"id": "400"}, "text": "ВЫПИСКАМИ", "timestamp": 1793210400}}]},
            {"id": "100", "changes": [{"field": "comments", "value": {"id": "205", "from_id": "305", "media": {"id": "400"}, "text": "ВЫПИСКА", "timestamp": float("nan")}}]},
        ])
        wrong_root = copy.deepcopy(self.data)
        wrong_root["id"] = "998"
        self.assertEqual([e["id"] for e in parse_events(wrong_root, "100", {"400"}, ["выписка"])], ["200"])

    def test_event_limit_fails_closed(self):
        entry = {"id": "100", "changes": [{"field": "comments", "value": {"id": str(1000+i), "from_id": "300", "media": {"id": "400"}, "text": "ВЫПИСКА", "timestamp": 1793210400}} for i in range(501)]}
        with self.assertRaises(ValueError):
            parse_events({"entry": [entry]}, "100", {"400"}, ["выписка"])


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.events = [{"kind": "comment", "id": "200", "user_id": "300", "media_id": "400",
                        "text": "ВЫПИСКА", "timestamp": "2026-10-28T18:00:00Z"}]

    def test_round_trip_shape_and_random_nonce(self):
        a = encrypt_events(self.events, KEY, NOW)
        b = encrypt_events(self.events, KEY, NOW)
        self.assertEqual(set(a), {"version", "nonce", "ciphertext"})
        self.assertNotEqual(a["nonce"], b["nonce"])
        self.assertEqual(decrypt_events(a, KEY, NOW), self.events)

    def test_tampering_wrong_key_and_bad_key_rejected(self):
        envelope = encrypt_events(self.events, KEY, NOW)
        broken = copy.deepcopy(envelope)
        raw = bytearray(base64.urlsafe_b64decode(broken["ciphertext"] + "=="))
        raw[-1] ^= 1
        broken["ciphertext"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with self.assertRaises(ValueError): decrypt_events(broken, KEY, NOW)
        with self.assertRaises(ValueError): decrypt_events(envelope, base64.urlsafe_b64encode(b"x"*32).decode(), NOW)
        with self.assertRaises(ValueError): encrypt_events(self.events, "bad-key", NOW)

    def test_expiry_and_future_issue_rejected(self):
        env = encrypt_events(self.events, KEY, NOW)
        with self.assertRaises(ValueError): decrypt_events(env, KEY, NOW + timedelta(days=7))
        with self.assertRaises(ValueError): decrypt_events(encrypt_events(self.events, KEY, NOW + timedelta(days=1)), KEY, NOW)

    def test_invalid_events_rejected(self):
        with self.assertRaises(ValueError): encrypt_events([{"kind": "unknown"}], KEY, NOW)


if __name__ == "__main__":
    unittest.main()
