"""Telegram subscription gate for the existing goszakupkiinfo_bot.

This file is prepared for deployment as ``tg_leadmagnet_bot.py`` at the
repository root. It reuses leadmagnet_worker.GitHubStore for CAS persistence.
No Telegram user/chat/message IDs or text are written to GitHub state or logs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

try:
    import requests
except ImportError:  # Subscription/state logic remains testable without optional HTTP client.
    requests = None

try:
    from leadmagnet_worker import GitHubStore
except ImportError:  # Lets the prepared module be tested from its staging folder.
    GitHubStore = None


EXPECTED_BOT_USERNAME = "goszakupkiinfo_bot"
EXPECTED_CHANNEL_USERNAME = "goszakupki_help"
CHECK_PREFIX = "CHECK:"
MAX_TEXT_LENGTH = 4096
STATE_PATH = "leadmagnet_tg/state.json"
CONTENT_PATH = Path("leadmagnets/goszakupki.json")
CAMPAIGN_CONFIG_PATH = Path("leadmagnets/composio.json")
ALLOWED_CODES = frozenset({
    "VYPISKA", "PROVAL", "ZAKAZCHIK", "SKIDKA", "PORYADOK", "SHTRAF",
    "OTKAZ", "TENDER", "AVANS",
})


class TelegramAPIError(RuntimeError):
    """Safe API failure; the Bot API URL and token are intentionally omitted."""

    def __init__(self, method: str, code: Optional[int] = None, reason: str = "request failed"):
        self.method = method
        self.code = code
        suffix = f", code {code}" if code is not None else ""
        super().__init__(f"Telegram {method} {reason}{suffix}")


def api_for_token(token: str, requests_module=None) -> Callable[..., Any]:
    if not token:
        raise RuntimeError("TG_LEADMAGNET_BOT_TOKEN is missing")
    if requests_module is None:
        if requests is None:
            raise RuntimeError("The requests package is required for Telegram API access")
        requests_module = requests

    def api(method: str, **params: Any) -> Any:
        # The token-bearing URL is transient and never copied into exceptions/logs.
        try:
            response = requests_module.post(
                f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30
            )
        except Exception:
            raise TelegramAPIError(method, reason="network outcome unavailable") from None
        status = getattr(response, "status_code", None)
        try:
            payload = response.json()
        except Exception:
            raise TelegramAPIError(method, status, "returned invalid JSON") from None
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            code = payload.get("error_code") if isinstance(payload, Mapping) else None
            code = code if type(code) is int else None
            raise TelegramAPIError(method, code, "was rejected")
        if not 200 <= (status if type(status) is int else 200) < 300:
            raise TelegramAPIError(method, status if type(status) is int else None,
                                   "returned an HTTP error")
        return payload.get("result")

    return api


def check_only(api: Callable[..., Any], channel_id: str,
               expected_bot: str = EXPECTED_BOT_USERNAME,
               expected_channel: str = EXPECTED_CHANNEL_USERNAME) -> dict[str, Any]:
    """Read-only Bot API preflight: exactly getMe/getChat/getChatMember/getWebhookInfo."""
    if not channel_id:
        raise RuntimeError("TG_GOSZAKUPKI_HELP_CHAT_ID is missing")
    me = api("getMe")
    chat = api("getChat", chat_id=channel_id)
    if not isinstance(me, Mapping) or type(me.get("id")) is not int:
        raise RuntimeError("Telegram getMe returned an invalid bot profile")
    if not isinstance(chat, Mapping):
        raise RuntimeError("Telegram getChat returned an invalid channel profile")
    bot_username = str(me.get("username", "")).removeprefix("@").lower()
    channel_username = str(chat.get("username", "")).removeprefix("@").lower()
    if bot_username != expected_bot.lower():
        raise RuntimeError("Telegram bot username does not match the expected bot")
    if chat.get("type") != "channel" or channel_username != expected_channel.lower():
        raise RuntimeError("Telegram channel username does not match the expected channel")
    own_member = api("getChatMember", chat_id=channel_id, user_id=me["id"])
    webhook = api("getWebhookInfo")
    if not isinstance(own_member, Mapping):
        raise RuntimeError("Telegram could not verify the bot's channel role")
    is_admin = own_member.get("status") in ("administrator", "creator")
    if not is_admin:
        raise RuntimeError("Telegram bot is not an administrator of the expected channel")
    if not isinstance(webhook, Mapping):
        raise RuntimeError("Telegram returned invalid webhook status")
    polling = not bool(webhook.get("url"))
    if not polling:
        raise RuntimeError("Telegram webhook is configured; getUpdates polling is unavailable")
    return {"mode": "check_only", "bot_username": "@" + bot_username,
            "channel_username": "@" + channel_username,
            "bot_admin": True, "polling_available": True}


def _root_fingerprint(hash_key: bytes) -> str:
    # Matches leadmagnet_worker.py's root-key fingerprint convention.
    return hashlib.sha256(hash_key).hexdigest()


def _aware_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _clean_state(store: Any, hash_key: bytes) -> dict[str, Any]:
    """Load state and migrate the old {offset} format without resetting offset."""
    state = store.load()
    if not isinstance(state, Mapping):
        raise RuntimeError("Invalid durable Telegram state; refusing to reset")
    expected = _root_fingerprint(hash_key)
    old_fingerprint = state.get("hash_key_id")
    if old_fingerprint is not None and old_fingerprint != expected:
        raise RuntimeError("Telegram state hash key changed; migration required")
    offset = state.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise RuntimeError("Invalid Telegram update offset; refusing to reset")
    unknown = set(state) - {"offset", "hash_key_id", "claims"}
    if unknown:
        raise RuntimeError("Unexpected Telegram state fields; refusing to overwrite")
    raw_claims = state.get("claims", {})
    if not isinstance(raw_claims, Mapping):
        raise RuntimeError("Invalid Telegram claims; refusing to reset")
    if old_fingerprint is None and raw_claims:
        raise RuntimeError("Telegram state has claims without a key fingerprint; migration required")
    claims: dict[str, dict[str, str]] = {}
    for key, claim in raw_claims.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(key)) or not isinstance(claim, Mapping):
            raise RuntimeError("Invalid Telegram claim; refusing to reset")
        if (claim.get("code") not in ALLOWED_CODES or
                claim.get("status") not in ("claiming", "sent", "uncertain") or
                not _aware_iso(claim.get("claimed_at"))):
            raise RuntimeError("Invalid Telegram claim; refusing to reset")
        claims[str(key)] = {"code": claim["code"], "status": claim["status"],
                            "claimed_at": claim["claimed_at"]}
    normalized = {"offset": offset, "hash_key_id": expected, "claims": claims}
    # The legacy state held only the polling offset. Preserve it and atomically
    # upgrade the same Contents file before reading updates or sending anything.
    if old_fingerprint is None or "claims" not in state:
        store.hash_key_id = expected
        store.save(normalized)
    else:
        store.hash_key_id = expected
    return normalized


def _claim_key(hash_key: bytes, uid: Any, code: str) -> str:
    payload = b"telegram-user-code\0" + str(uid).encode("utf-8") + b"\0" + code.encode("utf-8")
    return hmac.new(hash_key, payload, hashlib.sha256).hexdigest()


def _channel_link() -> str:
    return "https://t.me/" + EXPECTED_CHANNEL_USERNAME


def load_materials(root: Path = Path(".")) -> dict[str, dict[str, Any]]:
    """Keep legacy material codes and add AVANS only from its real configured URL."""
    content_path = root / CONTENT_PATH
    try:
        content = json.loads(content_path.read_text(encoding="utf-8")) if content_path.exists() else {}
    except Exception:
        raise RuntimeError("Lead-magnet content could not be loaded") from None
    if not isinstance(content, dict):
        raise RuntimeError("Lead-magnet content has an invalid format")
    content = dict(content)
    # Current AVANS config carries the verified real page URL, while the older
    # eight-code bot file has no AVANS entry. Do not invent or label a public
    # page exclusive: state plainly that the link itself is public.
    if "AVANS" not in content:
        campaign_path = root / CAMPAIGN_CONFIG_PATH
        try:
            cfg = json.loads(campaign_path.read_text(encoding="utf-8")) if campaign_path.exists() else {}
        except Exception:
            cfg = {}
        entry = next((e for e in cfg.get("entries", [])
                      if isinstance(e, Mapping) and e.get("code") == "AVANS"), None)
        if entry and isinstance(entry.get("title"), str) and isinstance(entry.get("url"), str):
            url = entry["url"]
            if url.startswith("https://goszakupki.info/"):
                content["AVANS"] = {
                    "keyword": "АВАНС", "title": entry["title"],
                    "text": (f"Рабочий лист «{entry['title']}»:\n{url}\n\n"
                             "Это открытая страница goszakupki.info. Подписка проверяется "
                             "перед выдачей ссылки через бота; сама страница общедоступна."),
                }
    return content


def _usable_entry(content: Mapping[str, Any], code: str) -> Optional[Mapping[str, str]]:
    if code not in ALLOWED_CODES:
        return None
    entry = content.get(code)
    if not isinstance(entry, Mapping):
        return None
    title, text = entry.get("title"), entry.get("text")
    if not isinstance(title, str) or not title.strip() or not isinstance(text, str) or not text.strip():
        return None
    if "TODO" in title.upper() or "TODO" in text.upper():
        return None
    if len(text) > MAX_TEXT_LENGTH:
        return None
    return {"title": title, "text": text}


def _keyboard(code: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "Открыть канал @goszakupki_help", "url": _channel_link()}],
        [{"text": "✅ Проверить подписку", "callback_data": CHECK_PREFIX + code}],
    ]}


class TelegramLeadMagnetBot:
    def __init__(self, api: Callable[..., Any], store: Any,
                 hash_key: bytes, channel_id: str,
                 content: Optional[Mapping[str, Any]] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(hash_key, bytes) or len(hash_key) < 32:
            raise RuntimeError("LEADMAGNET_HASH_KEY must contain at least 32 bytes")
        if not channel_id:
            raise RuntimeError("TG_GOSZAKUPKI_HELP_CHAT_ID is missing")
        self.api, self.store, self.hash_key = api, store, hash_key
        self.channel_id = channel_id
        self.content = dict(content if content is not None else load_materials())
        self.clock = clock
        self.state = _clean_state(store, hash_key)

    def _save(self) -> None:
        self.store.save(self.state)

    def _answer(self, callback_id: Any, text: str, show_alert: bool = False) -> None:
        try:
            self.api("answerCallbackQuery", callback_query_id=callback_id,
                     text=text, show_alert=show_alert)
        except TelegramAPIError:
            # Callback UI acknowledgement is best effort; never log IDs/URLs.
            print("Telegram callback acknowledgement unavailable", file=sys.stderr)

    def _send_start_prompt(self, message: Mapping[str, Any], code: str) -> None:
        chat_id = message["chat"]["id"]
        entry = _usable_entry(self.content, code)
        if entry is None:
            self.api("sendMessage", chat_id=chat_id,
                     text="Для этого кода материал пока не подготовлен. Напишите автору канала.")
            return
        self.api("sendMessage", chat_id=chat_id,
                 text=(f"Материал «{entry['title']}» выдаётся после проверки подписки "
                       f"на @{EXPECTED_CHANNEL_USERNAME}. Подпишитесь и нажмите кнопку ниже."),
                 reply_markup=_keyboard(code))

    @staticmethod
    def _is_member(member: Any) -> Optional[bool]:
        if not isinstance(member, Mapping):
            return None
        status = member.get("status")
        if status in ("member", "administrator", "creator"):
            return True
        if status in ("left", "kicked"):
            return False
        if status == "restricted":
            value = member.get("is_member")
            return value if type(value) is bool else None
        return None

    def _handle_callback(self, callback: Mapping[str, Any]) -> None:
        data = callback.get("data", "")
        if not isinstance(data, str) or not data.startswith(CHECK_PREFIX):
            return
        code = data[len(CHECK_PREFIX):]
        entry = _usable_entry(self.content, code)
        if entry is None:
            self._answer(callback.get("id"), "Не нашёл подготовленный материал для этого кода.", True)
            return
        user = callback.get("from")
        message = callback.get("message")
        if not isinstance(user, Mapping) or user.get("id") is None or not isinstance(message, Mapping):
            self._answer(callback.get("id"), "Не получилось проверить подписку. Нажмите ещё раз.", True)
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("id") is None:
            self._answer(callback.get("id"), "Не получилось открыть чат с ботом. Нажмите /start ещё раз.", True)
            return
        try:
            member = self.api("getChatMember", chat_id=self.channel_id, user_id=user["id"])
        except TelegramAPIError:
            self._answer(callback.get("id"), "Не получилось проверить подписку. Нажмите ещё раз.", True)
            return
        follows = self._is_member(member)
        if follows is None:
            self._answer(callback.get("id"), "Не получилось проверить подписку. Нажмите ещё раз.", True)
            return
        if follows is False:
            self._answer(callback.get("id"), "Пока не вижу подписки.", True)
            self.api("sendMessage", chat_id=chat["id"],
                     text=(f"Пока не вижу подписки на @{EXPECTED_CHANNEL_USERNAME}. "
                           "Подпишитесь и нажмите «Проверить подписку» ещё раз."),
                     reply_markup=_keyboard(code))
            return

        key = _claim_key(self.hash_key, user["id"], code)
        existing = self.state["claims"].get(key)
        if existing is not None:
            self._answer(callback.get("id"),
                         "Материал уже отправлялся или исход отправки неясен; автоматический повтор отключён.",
                         True)
            return
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Clock must return a timezone-aware datetime")
        self.state["claims"][key] = {
            "code": code, "status": "claiming", "claimed_at": now.astimezone(timezone.utc).isoformat(),
        }
        self._save()  # Durable HMAC user+code claim before material delivery.
        try:
            self.api("sendMessage", chat_id=chat["id"], text=entry["text"])
        except TelegramAPIError:
            self.state["claims"][key]["status"] = "uncertain"
            self._save()  # If this CAS fails, the persisted claiming status still blocks resends.
            self._answer(callback.get("id"),
                         "Не могу подтвердить отправку. Проверьте чат; автоматного повтора не будет.", True)
            return
        self.state["claims"][key]["status"] = "sent"
        self._save()
        self._answer(callback.get("id"), "Подписка подтверждена. Материал отправлен.")

    def _handle_update(self, update: Mapping[str, Any]) -> None:
        message = update.get("message")
        if isinstance(message, Mapping):
            command = message.get("text", "")
            if isinstance(command, str) and command.startswith("/start"):
                parts = command.split(maxsplit=1)
                code = parts[1].strip() if len(parts) == 2 else ""
                if code:
                    self._send_start_prompt(message, code)
                else:
                    chat_id = message["chat"]["id"]
                    self.api("sendMessage", chat_id=chat_id,
                             text="Чтобы получить материал, откройте ссылку из Instagram и нажмите Start.")
                return
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            self._handle_callback(callback)

    def run_once(self) -> dict[str, int]:
        updates = self.api("getUpdates", offset=self.state["offset"], timeout=0)
        if not isinstance(updates, list):
            raise TelegramAPIError("getUpdates", reason="returned invalid updates")
        counts = {"updates": 0}
        for update in updates:
            if not isinstance(update, Mapping) or type(update.get("update_id")) is not int:
                raise RuntimeError("Telegram returned an invalid update; state was not advanced")
            new_offset = update["update_id"] + 1
            if new_offset <= self.state["offset"]:
                continue
            self.state["offset"] = new_offset
            self._save()  # Persist offset before any update handler/API write.
            counts["updates"] += 1
            # The caller may inspect only counters; IDs/text are never returned.
            self._handle_update(update)
        return counts


def main() -> int:
    token = os.environ.get("TG_LEADMAGNET_BOT_TOKEN", "")
    channel_id = os.environ.get("TG_GOSZAKUPKI_HELP_CHAT_ID", "")
    try:
        api = api_for_token(token)
        if os.environ.get("TG_LEADMAGNET_CHECK_ONLY", "").lower() == "true":
            print(json.dumps(check_only(api, channel_id), ensure_ascii=False, sort_keys=True))
            return 0
        env = {name: os.environ.get(name, "") for name in (
            "GITHUB_TOKEN", "GITHUB_REPOSITORY", "LEADMAGNET_HASH_KEY")}
        if any(not value for value in env.values()):
            raise RuntimeError("GITHUB_TOKEN, GITHUB_REPOSITORY and LEADMAGNET_HASH_KEY are required")
        hash_key = env["LEADMAGNET_HASH_KEY"].encode("utf-8")
        if len(hash_key) < 32:
            raise RuntimeError("LEADMAGNET_HASH_KEY must contain at least 32 bytes")
        if GitHubStore is None:
            raise RuntimeError("leadmagnet_worker.GitHubStore is unavailable")
        store = GitHubStore(env["GITHUB_REPOSITORY"], env["GITHUB_TOKEN"],
                            path=STATE_PATH, hash_key_id=None)
        bot = TelegramLeadMagnetBot(api, store, hash_key, channel_id)
        print(json.dumps(bot.run_once(), ensure_ascii=False, sort_keys=True))
        return 0
    except TelegramAPIError as exc:
        # Error strings contain only the API method and numeric status/code.
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Never print arbitrary request, response, URL, chat or user values.
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Telegram bot stopped safely: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

