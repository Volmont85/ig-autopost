"""Offline unit tests for the staged Telegram subscription gate."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("tg_leadmagnet_bot.py.prepared")
if not MODULE_PATH.exists():
    MODULE_PATH = Path(__file__).resolve().parents[1] / "tg_leadmagnet_bot.py"
loader = importlib.machinery.SourceFileLoader("tg_leadmagnet_bot_prepared", str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
botmod = importlib.util.module_from_spec(spec)
loader.exec_module(botmod)

HASH_KEY = b"test-key-material-with-at-least-32-bytes"
CHANNEL_ID = "-100fakechannel"
CONTENT = {"AVANS": {"title": "Рабочий лист", "text": "https://goszakupki.info/materialy/avans-checklist.html"}}


class FakeStore:
    def __init__(self, state=None, fail_on_save=None):
        self.state = state if state is not None else {
            "offset": 0, "hash_key_id": hashlib.sha256(HASH_KEY).hexdigest(), "claims": {}
        }
        self.hash_key_id = None
        self.saves = 0
        self.fail_on_save = fail_on_save

    def load(self):
        return self.state.copy()

    def save(self, state):
        self.saves += 1
        if self.saves == self.fail_on_save:
            raise RuntimeError("simulated CAS failure")
        self.state = {**state, "claims": {k: dict(v) for k, v in state.get("claims", {}).items()}}
        if self.hash_key_id:
            self.state["hash_key_id"] = self.hash_key_id


_DEFAULT_MEMBERSHIP = object()


class FakeAPI:
    def __init__(self, membership=_DEFAULT_MEMBERSHIP, send_error=None):
        if membership is _DEFAULT_MEMBERSHIP:
            membership = {"status": "member"}
        self.membership = membership
        self.send_error = send_error
        self.calls = []

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "getChatMember":
            if isinstance(self.membership, Exception):
                raise self.membership
            return self.membership
        if method == "sendMessage" and params.get("text") == CONTENT["AVANS"]["text"] and self.send_error:
            raise self.send_error
        if method == "getUpdates":
            return []
        if method == "getMe":
            return {"id": 11, "username": "goszakupkiinfo_bot"}
        if method == "getChat":
            return {"id": CHANNEL_ID, "type": "channel", "username": "goszakupki_help"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "answerCallbackQuery":
            return True
        if method == "sendMessage":
            return {"message_id": 1}
        raise AssertionError(f"unexpected Bot API method {method}")


def callback(update_id=1, user_id=123, callback_id="cb-1"):
    return {"update_id": update_id, "callback_query": {
        "id": callback_id, "data": "CHECK:AVANS", "from": {"id": user_id},
        "message": {"chat": {"id": user_id}},
    }}


def make_bot(api=None, store=None):
    api = api or FakeAPI()
    store = store or FakeStore()
    bot = botmod.TelegramLeadMagnetBot(api, store, HASH_KEY, CHANNEL_ID, CONTENT,
                                       clock=lambda: datetime(2026, 10, 28, tzinfo=timezone.utc))
    return bot, api, store


class TelegramLeadMagnetTests(unittest.TestCase):
    def test_membership_status_semantics(self):
        for member in ({"status": "member"}, {"status": "administrator"},
                       {"status": "creator"}, {"status": "restricted", "is_member": True}):
            self.assertIs(botmod.TelegramLeadMagnetBot._is_member(member), True)
        for member in ({"status": "left"}, {"status": "kicked"},
                       {"status": "restricted", "is_member": False}):
            self.assertIs(botmod.TelegramLeadMagnetBot._is_member(member), False)
        self.assertIsNone(botmod.TelegramLeadMagnetBot._is_member({"status": "restricted"}))
        self.assertIsNone(botmod.TelegramLeadMagnetBot._is_member({"status": "unexpected"}))

    def test_restricted_member_is_accepted_and_legacy_codes_remain_available(self):
        bot, api, store = make_bot(FakeAPI(membership={"status": "restricted", "is_member": True}))
        bot._handle_update(callback())
        self.assertTrue(any(p.get("text") == CONTENT["AVANS"]["text"]
                            for method, p in api.calls if method == "sendMessage"))
        self.assertTrue({"VYPISKA", "PROVAL", "ZAKAZCHIK", "SKIDKA", "PORYADOK",
                         "SHTRAF", "OTKAZ", "TENDER"}.issubset(botmod.ALLOWED_CODES))

    def test_claims_without_root_fingerprint_fail_closed(self):
        state = {"offset": 4, "claims": {"a" * 64: {"code": "AVANS", "status": "sent",
                 "claimed_at": "2026-10-28T00:00:00+00:00"}}}
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            botmod.TelegramLeadMagnetBot(FakeAPI(), FakeStore(state), HASH_KEY, CHANNEL_ID, CONTENT)

    def test_true_subscription_sends_once_and_persists_claim(self):
        bot, api, store = make_bot()
        bot._handle_update(callback())
        sends = [p for method, p in api.calls if method == "sendMessage"]
        self.assertEqual([p["text"] for p in sends], [CONTENT["AVANS"]["text"]])
        claim = next(iter(store.state["claims"].values()))
        self.assertEqual(claim["status"], "sent")
        self.assertEqual(claim["code"], "AVANS")
        self.assertNotIn("123", repr(store.state))

    def test_false_and_unknown_never_send_material(self):
        for membership in ({"status": "left"}, None, {"status": "unexpected"},
                           botmod.TelegramAPIError("getChatMember", 403)):
            with self.subTest(membership=membership):
                bot, api, store = make_bot(FakeAPI(membership=membership))
                bot._handle_update(callback())
                sent = [p["text"] for method, p in api.calls if method == "sendMessage"]
                self.assertFalse(any(text == CONTENT["AVANS"]["text"] for text in sent))
                self.assertFalse(store.state["claims"])

    def test_repeated_callback_and_restart_are_idempotent(self):
        bot, api, store = make_bot()
        bot._handle_update(callback())
        bot2, api2, _ = make_bot(FakeAPI(), store)
        bot2._handle_update(callback(update_id=2, callback_id="new-callback"))
        self.assertFalse(any(p.get("text") == CONTENT["AVANS"]["text"] for m, p in api2.calls if m == "sendMessage"))

    def test_ambiguous_send_timeout_blocks_new_callback_after_restart(self):
        timed_out = FakeAPI(send_error=botmod.TelegramAPIError("sendMessage", reason="network outcome unavailable"))
        bot, _, store = make_bot(timed_out)
        bot._handle_update(callback())
        self.assertEqual(next(iter(store.state["claims"].values()))["status"], "uncertain")
        next_api = FakeAPI()
        restarted, _, _ = make_bot(next_api, store)
        restarted._handle_update(callback(update_id=2, callback_id="brand-new"))
        self.assertFalse(any(p.get("text") == CONTENT["AVANS"]["text"] for m, p in next_api.calls if m == "sendMessage"))

    def test_claim_cas_failure_prevents_material_send_and_propagates(self):
        store = FakeStore(fail_on_save=1)
        bot, api, _ = make_bot(store=store)
        with self.assertRaisesRegex(RuntimeError, "CAS failure"):
            bot._handle_callback(callback()["callback_query"])
        self.assertFalse(any(p.get("text") == CONTENT["AVANS"]["text"] for m, p in api.calls if m == "sendMessage"))

    def test_offset_is_saved_before_handler_and_legacy_offset_preserved(self):
        legacy = FakeStore({"offset": 23})
        bot, api, _ = make_bot(FakeAPI(), legacy)
        self.assertEqual(bot.state["offset"], 23)
        self.assertEqual(legacy.state["offset"], 23)
        class UpdatesAPI(FakeAPI):
            def __call__(self, method, **params):
                if method == "getUpdates":
                    self.calls.append((method, params))
                    return [{"update_id": 28, "message": {"text": "/start AVANS", "chat": {"id": 42}}}]
                return super().__call__(method, **params)

        api = UpdatesAPI()
        bot, _, store = make_bot(api, FakeStore())
        bot.run_once()
        self.assertEqual(store.state["offset"], 29)
        self.assertEqual([m for m, _ in api.calls].index("sendMessage") > [m for m, _ in api.calls].index("getUpdates"), True)

    def test_check_only_uses_only_four_read_methods_and_no_store(self):
        api = FakeAPI(membership={"status": "administrator"})
        result = botmod.check_only(api, CHANNEL_ID)
        self.assertEqual([m for m, _ in api.calls], ["getMe", "getChat", "getChatMember", "getWebhookInfo"])
        self.assertEqual(result, {"mode": "check_only", "bot_username": "@goszakupkiinfo_bot",
                                  "channel_username": "@goszakupki_help", "bot_admin": True,
                                  "polling_available": True})

    def test_check_only_rejects_wrong_bot_channel_role_and_webhook(self):
        class BadAPI(FakeAPI):
            def __init__(self, bad):
                super().__init__(membership={"status": "member"})
                self.bad = bad

            def __call__(self, method, **params):
                result = super().__call__(method, **params)
                if method == "getMe" and self.bad == "bot":
                    return {"id": 11, "username": "other_bot"}
                if method == "getChat" and self.bad == "channel":
                    return {"id": CHANNEL_ID, "type": "channel", "username": "other_channel"}
                if method == "getChatMember" and self.bad == "admin":
                    return {"status": "member"}
                if method == "getWebhookInfo" and self.bad == "webhook":
                    return {"url": "https://example.invalid/webhook"}
                return result

        for bad in ("bot", "channel", "admin", "webhook"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                botmod.check_only(BadAPI(bad), CHANNEL_ID)

    def test_http_network_exception_does_not_leak_token_url(self):
        token = "123456:secret-token-value"

        class Requests:
            @staticmethod
            def post(*args, **kwargs):
                raise OSError("secret URL " + str(args[0]))

        api = botmod.api_for_token(token, Requests)
        with self.assertRaises(botmod.TelegramAPIError) as raised:
            api("getMe")
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("getMe", str(raised.exception))

    def test_prepared_workflow_keeps_cron_removes_git_state_commit_and_adds_check_mode(self):
        workflow = (MODULE_PATH.parent / "leadmagnet_tg.yml.prepared").read_text(encoding="utf-8")
        self.assertIn('cron: "17,47 * * * *"', workflow)
        self.assertIn("check_only:", workflow)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("LEADMAGNET_HASH_KEY: ${{ secrets.LEADMAGNET_HASH_KEY }}", workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("Commit state", workflow)


if __name__ == "__main__":
    unittest.main()
