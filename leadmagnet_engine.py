"""Privacy-conscious, dependency-injected Instagram lead-magnet workflow.

This module deliberately contains no HTTP, credentials, filesystem or SDK code.
Adapters supply the transport and store. Persistent records contain only keyed
SHA-256 identifiers, material codes, timestamps and workflow statuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
from typing import Any, Mapping, Optional, Protocol, Sequence


class Store(Protocol):
    def load(self) -> Mapping[str, Any]: ...
    def save(self, state: Mapping[str, Any]) -> None: ...


class Transport(Protocol):
    def get_follow_status(self, uid: str) -> Optional[bool]: ...
    def private_reply(self, comment_id: str, text: str) -> Any: ...
    def send_message(self, uid: str, text: str) -> Any: ...
    def recent_incoming(self, uid: str, since: datetime) -> Sequence[Mapping[str, Any]]: ...


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be an aware datetime or ISO-8601 string")
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _dt(value).isoformat()


def _key(secret: bytes, namespace: str, raw_id: str) -> str:
    return hmac.new(secret, (namespace + "\0" + str(raw_id)).encode(), hashlib.sha256).hexdigest()


def _has_word(text: str, keyword: str) -> bool:
    # Unicode-aware word boundary, with punctuation accepted around keyword.
    return re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text, re.IGNORECASE) is not None


@dataclass(frozen=True)
class EngineConfig:
    entries: Sequence[Mapping[str, str]]
    enabled_since: datetime
    allowlisted_media_ids: frozenset[str]
    hash_key: bytes
    interactive_buttons: bool = False


class LeadMagnetEngine:
    """Process a bounded poll of comments and pending conversation messages.

    Store keys never contain platform IDs or message/comment text. Send claims
    are saved before transport writes. Any send exception leaves an `uncertain`
    claim and is never retried automatically, avoiding duplicate delivery.
    """

    def __init__(self, config: EngineConfig, transport: Transport, store: Store):
        self.config = config
        self.transport = transport
        self.store = store
        self._validate_config()

    def _validate_config(self) -> None:
        if not isinstance(self.config.hash_key, bytes) or not self.config.hash_key:
            raise ValueError("hash_key must be non-empty bytes")
        codes = set()
        for entry in self.config.entries:
            if not all(isinstance(entry.get(k), str) and entry.get(k)
                       for k in ("code", "keyword", "title", "url")):
                raise ValueError("each entry requires code, keyword, title and url")
            if entry["code"] in codes:
                raise ValueError("material codes must be unique")
            codes.add(entry["code"])
        _dt(self.config.enabled_since)

    def _load(self) -> dict[str, Any]:
        loaded = self.store.load()
        loaded = loaded or {}
        # Fail closed on unexpected fields: a store may be shared with legacy
        # state, but this public-repository state must never re-persist IDs or
        # message/comment text that an older adapter happened to include.
        claims = {}
        for key, value in (loaded.get("claims", {}) or {}).items():
            if not re.fullmatch(r"(?:comment|incoming):[0-9a-f]{64}", str(key)):
                raise ValueError("Invalid durable record; reconciliation required")
            if not isinstance(value, Mapping):
                raise ValueError("Invalid durable record; reconciliation required")
            if (value.get("code") not in tuple(e["code"] for e in self.config.entries) or
                    value.get("kind") not in ("private_reply", "material_dm", "follow_prompt", "check_prompt") or
                    value.get("status") not in ("claiming", "sent", "uncertain") or
                    not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_hash", "")))):
                raise ValueError("Invalid durable record; reconciliation required")
            try:
                at = _stamp(_dt(value["at"]))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Invalid durable record; reconciliation required")
            claims[str(key)] = {"code": value["code"], "kind": value["kind"],
                                "status": value["status"], "at": at,
                                "source_hash": value["source_hash"]}
        users = {}
        codes = {e["code"] for e in self.config.entries}
        for key, value in (loaded.get("users", {}) or {}).items():
            if not re.fullmatch(r"[0-9a-f]{64}:.+", str(key)) or not isinstance(value, Mapping):
                raise ValueError("Invalid durable record; reconciliation required")
            if (not isinstance(value.get("code"), str) or value.get("code") not in codes or
                    value.get("status") not in ("claiming", "awaiting_reply", "delivered", "uncertain", "opted_out") or
                    value.get("follow_status") not in ("true", "false", "unknown") or
                    value.get("delivery_status") not in ("pending", "claiming", "sent", "uncertain")):
                raise ValueError("Invalid durable record; reconciliation required")
            try:
                created_at = _stamp(_dt(value["created_at"]))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Invalid durable record; reconciliation required")
            comment_claim = value.get("comment_claim")
            delivery_claim = value.get("delivery_claim")
            if not isinstance(comment_claim, str) or not re.fullmatch(r"comment:[0-9a-f]{64}", comment_claim):
                raise ValueError("Invalid durable record; reconciliation required")
            if delivery_claim is not None and (not isinstance(delivery_claim, str) or
                    not re.fullmatch(r"(?:comment|incoming):[0-9a-f]{64}", delivery_claim)):
                raise ValueError("Invalid durable record; reconciliation required")
            users[str(key)] = {"code": value["code"], "status": value["status"],
                               "comment_claim": comment_claim, "created_at": created_at,
                               "follow_status": value["follow_status"],
                               "delivery_status": value["delivery_status"],
                               "delivery_claim": delivery_claim}
            if value.get('last_inbox_checked_at'):
                users[str(key)]['last_inbox_checked_at'] = _stamp(_dt(value['last_inbox_checked_at']))
        dedupe = {}
        for key, value in (loaded.get("dedupe", {}) or {}).items():
            if not isinstance(value, Mapping):
                raise ValueError("Invalid durable record; reconciliation required")
            code = value.get("code")
            # Dedupe keys include the HMAC user hash and configured material
            # code. Match only the configured suffix, including codes with ':'.
            if (not isinstance(code, str) or code not in codes or
                    not re.fullmatch(r"[0-9a-f]{64}:" + re.escape(code), str(key))):
                raise ValueError("Invalid durable record; reconciliation required")
            if value.get("status") not in ("claiming", "sent", "uncertain"):
                raise ValueError("Invalid durable record; reconciliation required")
            claim = value.get("claim")
            if not isinstance(claim, str) or not re.fullmatch(r"comment:[0-9a-f]{64}", claim):
                raise ValueError("Invalid durable record; reconciliation required")
            dedupe[str(key)] = {"status": value["status"], "claim": claim, "code": code}
        consented = {}
        for key, value in (loaded.get('consented_users', {}) or {}).items():
            if not re.fullmatch(r'[0-9a-f]{64}', str(key)):
                raise ValueError('Invalid consent record')
            consented[key] = _stamp(_dt(value))
        state = {"claims": claims, "users": users, "dedupe": dedupe,
                 "consented_users": consented}
        # Shared EngineStore metadata used by the worker's poll throttle.
        # Preserve only a valid timezone-aware ISO timestamp.
        try:
            state["last_poll_started_at"] = _stamp(_dt(loaded["last_poll_started_at"]))
        except (KeyError, TypeError, ValueError):
            pass
        return state

    def _save(self, state: Mapping[str, Any]) -> None:
        self.store.save(state)

    def _entry_for(self, text: str) -> Optional[Mapping[str, str]]:
        for entry in self.config.entries:
            if _has_word(str(text or ""), entry["keyword"]):
                return entry
        return None

    def _send_incoming_response(self, uid, text, entry, kind):
        if not self.config.interactive_buttons:
            return self.transport.send_message(uid, text)
        if kind == 'material_dm':
            text = f"Готово! Материал «{entry['title']}» — по кнопке ниже."
            buttons = [{'type':'web_url','title':'Открыть рабочий лист','url':entry['url']}]
            if entry.get('telegram_url'):
                text += '\nВ Telegram — подробный разбор и другие рабочие материалы.'
                buttons.append({'type':'web_url','title':'Разбор в Telegram','url':entry['telegram_url']})
        else:
            buttons = []
            if kind == 'follow_prompt':
                buttons.append({'type':'web_url','title':'Открыть профиль','url':'https://www.instagram.com/goszakupki.info/'})
            buttons.append({'type':'postback','title':'✅ Готово' if kind == 'follow_prompt' else 'Проверить ещё',
                            'payload':'LM_CHECK:' + entry['code']})
        return self.transport.send_buttons(uid, text, buttons)

    @staticmethod
    def _material_text(entry):
        text = f"Материал «{entry['title']}»: {entry['url']}"
        if entry.get('telegram_url'):
            text += '\n\nПодробный разбор и другие материалы — в Telegram: ' + entry['telegram_url']
        return text

    @staticmethod
    def _fresh(timestamp: Any, now: datetime, since: datetime) -> bool:
        stamp = _dt(timestamp)
        return since <= stamp <= now and now - stamp <= timedelta(days=7)

    def _claim(self, state: dict[str, Any], claim_key: str, *, code: str,
               kind: str, at: datetime, source_hash: str) -> bool:
        if claim_key in state["claims"]:
            return False
        state["claims"][claim_key] = {
            "code": code, "kind": kind, "status": "claiming",
            "at": _stamp(at), "source_hash": source_hash,
        }
        self._save(state)  # Claim is durable before any external side effect.
        return True

    @staticmethod
    def _finish(state: dict[str, Any], claim_key: str, status: str) -> None:
        state["claims"][claim_key]["status"] = status

    def run(self, comments: Sequence[Mapping[str, Any]], now: datetime, *,
            incoming_events: Optional[Sequence[Mapping[str, Any]]] = None) -> dict[str, int]:
        """Handle supplied recent comments and poll messages for known users.

        Comment fields: id,user_id,media_id,text,timestamp. Incoming-message
        fields: id,created_time,text. Results are counters only, no IDs/text.
        """
        now = _dt(now)
        since = _dt(self.config.enabled_since)
        state = self._load()
        # None preserves the established poll adapter. A supplied list switches
        # to authenticated push events: never read comments or the inbox again.
        event_mode = incoming_events is not None
        incoming_events = incoming_events or []
        result = {"comments_seen": 0, "private_replies_sent": 0,
                  "messages_sent": 0, "skipped": 0, "uncertain": 0,
                  "errors": 0}
        for comment in comments:
            result["comments_seen"] += 1
            try:
                media_id = str(comment["media_id"])
                uid = str(comment["user_id"])
                cid = str(comment["id"])
                created = _dt(comment["timestamp"])
                entry = self._entry_for(str(comment.get("text", "")))
                if (media_id not in self.config.allowlisted_media_ids or
                        uid == "" or cid == "" or
                        not self._fresh(created, now, since) or not entry):
                    result["skipped"] += 1
                    continue
                user_hash = _key(self.config.hash_key, "user", uid)
                comment_hash = _key(self.config.hash_key, "comment", cid)
                dedupe = user_hash + ":" + entry["code"]
                # The per-user/material event claim survives even if a later
                # state write fails; another comment cannot bypass it.
                if dedupe in state["dedupe"]:
                    result["skipped"] += 1
                    continue
                try:
                    # A comment alone is not User Profile API consent. For new
                    # event leads, ask for a reply without wasting a failed read.
                    follows = (self.transport.get_follow_status(uid)
                               if not self.config.interactive_buttons and
                               (not event_mode or user_hash in state['consented_users']) else None)
                    if follows is not True and follows is not False:
                        follows = None
                except Exception:
                    follows = None
                claim = "comment:" + comment_hash
                if claim in state["claims"]:
                    result["skipped"] += 1
                    continue
                # Persist the comment claim, user/material dedupe and pending
                # user state together before the external private reply.
                state["claims"][claim] = {
                    "code": entry["code"], "kind": "private_reply", "status": "claiming",
                    "at": _stamp(now), "source_hash": comment_hash,
                }
                state["users"][dedupe] = {
                    "code": entry["code"], "status": "claiming",
                    "comment_claim": claim, "created_at": _stamp(now),
                    "follow_status": "true" if follows is True else ("false" if follows is False else "unknown"),
                    "delivery_status": "claiming" if follows is True else "pending",
                    "delivery_claim": claim if follows is True else None,
                }
                state["dedupe"][dedupe] = {"status": "claiming", "claim": claim, "code": entry["code"]}
                self._save(state)
                if follows is True:
                    text = self._material_text(entry)
                elif follows is False:
                    text = "Подпишитесь на аккаунт и ответьте словом ПОЛУЧИТЬ — проверю подписку и пришлю материал."
                else:
                    text = ("Чтобы получить рабочий материал, подпишитесь на аккаунт и ответьте словом ПОЛУЧИТЬ. После ответа проверю подписку и пришлю ссылку."
                            if event_mode else "Не получилось проверить подписку. Ответьте словом ПОЛУЧИТЬ — попробую ещё раз.")
                if self.config.interactive_buttons:
                    # First comment reply is a documented text Private Reply.
                    # A fresh human reply opens the 24-hour normal-DM window;
                    # only then send the button template, even for known users.
                    text = f"Подготовили материал «{entry['title']}». Прислать? Ответьте ПОЛУЧИТЬ — проверю подписку и пришлю рабочий лист."
                try:
                    self.transport.private_reply(cid, text)
                except Exception:
                    self._finish(state, claim, "uncertain")
                    state["users"][dedupe]["status"] = "uncertain"
                    state["dedupe"][dedupe]["status"] = "uncertain"
                    self._save(state)
                    result["uncertain"] += 1
                    continue
                self._finish(state, claim, "sent")
                state["users"][dedupe]["status"] = "delivered" if follows is True else "awaiting_reply"
                state["dedupe"][dedupe]["status"] = "sent"
                if follows is True:
                    state["users"][dedupe]["delivery_status"] = "sent"
                self._save(state)
                result["private_replies_sent"] += 1
            except Exception:
                # A bad event must not stop polling later comments.
                result["errors"] += 1

        # Only users with a known code are queried. The adapter must return only
        # incoming messages in its supplied 24-hour window.
        inbox_progress = False
        for dedupe, user in list(state["users"].items()):
            if user.get("status") != "awaiting_reply":
                continue
            entry = next((e for e in self.config.entries if e["code"] == user.get("code")), None)
            if entry is None:
                continue
            # Recover the platform ID only transiently from a matching supplied
            # comment; raw identifiers are never persisted.
            uid = next((str(c.get("user_id")) for c in comments
                        if _key(self.config.hash_key, "user", str(c.get("user_id", ""))) == dedupe.split(":", 1)[0]), None)
            if uid is None and event_mode:
                uid = next((str(m.get('user_id')) for m in incoming_events
                            if _key(self.config.hash_key, 'user', str(m.get('user_id', ''))) == dedupe.split(':', 1)[0]), None)
            if uid is None:
                continue
            created_at = _dt(user["created_at"])
            window_start = max(created_at, now - timedelta(hours=24))
            if user.get('last_inbox_checked_at'):
                window_start = max(window_start, _dt(user['last_inbox_checked_at']))
            if now - created_at > timedelta(days=7):
                continue
            try:
                incoming = ([{'id': m['id'], 'created_time': m['timestamp'], 'text': m.get('text', '')}
                             for m in incoming_events if str(m.get('user_id')) == uid]
                            if event_mode else self.transport.recent_incoming(uid, window_start))
            except Exception:
                result["errors"] += 1
                continue
            # A later refusal in the same poll must take precedence over an
            # earlier request. Do not send the link while walking older rows.
            if any(re.search(r"(?<!\w)(?:стоп|stop|отмена|отписка|не присылай(?:те)?|не нужно|не надо)(?!\w)",
                             str(m.get("text", "")), re.IGNORECASE)
                   for m in incoming if window_start <= _dt(m["created_time"]) <= now):
                user["status"] = "opted_out"
                self._save(state)
                continue
            for message in incoming:
                try:
                    mid = str(message["id"])
                    mt = _dt(message["created_time"])
                    if mt < window_start or mt > now:
                        continue
                    message_hash = _key(self.config.hash_key, "message", mid)
                    if "incoming:" + message_hash in state["claims"]:
                        continue
                    delivery = user.get("delivery_status")
                    if delivery == "sent":
                        break
                    if event_mode:
                        state['consented_users'][_key(self.config.hash_key, 'user', uid)] = _stamp(mt)
                    # A new user reply is the basis for checking and, only on
                    # true, an ordinary DM inside the open 24-hour window.
                    try:
                        follows = self.transport.get_follow_status(uid)
                    except Exception:
                        follows = None
                    if follows is True:
                        # A distinct incoming-message ID must not bypass an
                        # ambiguous or partially persisted link delivery.
                        if user.get("delivery_status") != "pending":
                            continue
                        text = self._material_text(entry)
                        kind = "material_dm"
                    elif follows is False:
                        text = "Пока не вижу подписку. Подпишитесь и ответьте ГОТОВО — проверю ещё раз."
                        kind = "follow_prompt"
                    else:
                        text = "Не получилось проверить подписку. Ответьте ПРОВЕРИТЬ, и попробую ещё раз."
                        kind = "check_prompt"
                    claim = "incoming:" + message_hash
                    if follows is True:
                        if claim in state["claims"]:
                            continue
                        # Persist the inbound-event and per-user link claims in
                        # one write, before any material DM can be attempted.
                        state["claims"][claim] = {
                            "code": entry["code"], "kind": kind, "status": "claiming",
                            "at": _stamp(now), "source_hash": message_hash,
                        }
                        user["delivery_status"] = "claiming"
                        user["delivery_claim"] = claim
                        self._save(state)
                    elif not self._claim(state, claim, code=entry["code"], kind=kind,
                                         at=now, source_hash=message_hash):
                        continue
                    try:
                        self._send_incoming_response(uid, text, entry, kind)
                    except Exception:
                        self._finish(state, claim, "uncertain")
                        user["status"] = "uncertain"
                        if follows is True:
                            user["delivery_status"] = "uncertain"
                        self._save(state)
                        result["uncertain"] += 1
                        continue
                    self._finish(state, claim, "sent")
                    if follows is True:
                        user["delivery_status"] = "sent"
                    user["status"] = "delivered" if follows is True else "awaiting_reply"
                    user["follow_status"] = "true" if follows is True else ("false" if follows is False else "unknown")
                    self._save(state)
                    result["messages_sent"] += 1
                except Exception:
                    result["errors"] += 1
            # Overlap by one minute for rounded provider timestamps. Claimed
            # message IDs prevent duplicate handling in that overlap.
            user['last_inbox_checked_at'] = _stamp(max(created_at, now-timedelta(minutes=1)))
            inbox_progress = True
        if inbox_progress:
            self._save(state)
        return result

