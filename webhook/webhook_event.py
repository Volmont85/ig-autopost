"""Strict parsing and authenticated encryption for Instagram lead events."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"ig-leadmagnet-events-v1"
VERSION = 1
MAX_EVENTS = 500
MAX_TEXT = 10_000
MAX_ID = 128
MAX_ENVELOPE_BYTES = 48 * 1024
_WORD = re.compile(r"[\w]+", re.UNICODE)
_NUMERIC_ID = re.compile(r"[0-9]{1,128}\Z")


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result if len(result) <= MAX_ID and _NUMERIC_ID.fullmatch(result) else None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and 'T' in value:
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is not None and 2000 <= dt.year <= 2100:
                return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        except ValueError:
            pass
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number > 100_000_000_000:
        number /= 1000.0
    try:
        dt = datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if not 2000 <= dt.year <= 2100:
        return None
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _matches_keyword(text: str, keywords: Iterable[str]) -> bool:
    tokens = {m.group(0).casefold() for m in _WORD.finditer(text)}
    return any(isinstance(k, str) and k and k.casefold() in tokens for k in keywords)


def parse_events(data: Any, own_id: Any, allowed_media_ids: Iterable[Any],
                 keywords: Iterable[str]) -> list[dict[str, str]]:
    """Normalize supported comments and DMs; malformed individual items are skipped."""
    own = _id(own_id)
    if own is None or not isinstance(data, Mapping):
        return []
    allowed = {_id(x) for x in allowed_media_ids}
    allowed.discard(None)
    keys = tuple(keywords)
    entries = data.get("entry", [])
    if not isinstance(entries, list):
        return []
    events: list[dict[str, str]] = []

    def add(event: dict[str, str]) -> None:
        events.append(event)
        if len(events) > MAX_EVENTS:
            raise ValueError("webhook event limit exceeded")

    def comment(value: Any, fallback: Any) -> None:
        if not isinstance(value, Mapping):
            return
        cid = _id(value.get("id"))
        author = value.get('from')
        uid = _id(author.get('id')) if isinstance(author, Mapping) else _id(value.get("from_id", value.get("user_id")))
        media = value.get("media")
        mid = _id(media.get("id")) if isinstance(media, Mapping) else _id(value.get("media_id"))
        text = value.get("text")
        stamp = _timestamp(value.get("timestamp", value.get("created_time", fallback)))
        if (not cid or not uid or uid == own or not mid or mid not in allowed or
                not isinstance(text, str) or not text or len(text) > MAX_TEXT or
                not stamp or not _matches_keyword(text, keys)):
            return
        add({"kind": "comment", "id": cid, "user_id": uid, "media_id": mid,
             "text": text, "timestamp": stamp})

    def message(item: Any, fallback: Any) -> None:
        if not isinstance(item, Mapping) or item.get("is_echo") is True:
            return
        sender, recipient, msg = item.get("sender"), item.get("recipient"), item.get("message")
        if not isinstance(sender, Mapping) or not isinstance(recipient, Mapping) or not isinstance(msg, Mapping):
            return
        if msg.get("is_echo") is True:
            return
        uid, recipient_id = _id(sender.get("id")), _id(recipient.get("id"))
        mid, text = _message_id(msg.get("mid")), msg.get("text")
        stamp = _timestamp(item.get("timestamp", fallback))
        if (not uid or uid == own or recipient_id != own or not mid or
                not isinstance(text, str) or not text or len(text) > MAX_TEXT or not stamp):
            return
        add({"kind": "message", "id": mid, "user_id": uid, "text": text, "timestamp": stamp})

    def messaging_event(item: Any, fallback: Any) -> None:
        if not isinstance(item, Mapping) or 'postback' not in item:
            message(item, fallback)
            return
        sender, recipient, postback = item.get('sender'), item.get('recipient'), item.get('postback')
        if not all(isinstance(x, Mapping) for x in (sender, recipient, postback)):
            return
        uid = _id(sender.get('id'))
        stamp = _timestamp(item.get('timestamp', fallback))
        payload = postback.get('payload')
        commands = {'LM_YES:AVANS':'ПОЛУЧИТЬ', 'LM_CHECK:AVANS':'ГОТОВО', 'LM_STOP:AVANS':'СТОП'}
        if (not uid or uid == own or _id(recipient.get('id')) != own or not stamp or
                not isinstance(payload, str) or payload not in commands or item.get('is_echo') is True):
            return
        mid = _message_id(postback.get('mid'))
        if mid is None:
            identity = json.dumps([own, uid, str(item.get('timestamp', fallback)), payload], separators=(',', ':'))
            mid = 'postback:' + hashlib.sha256(identity.encode()).hexdigest()
        add({'kind':'message', 'id':mid, 'user_id':uid, 'text':commands[payload], 'timestamp':stamp})

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        account = _id(entry.get('id')) if entry.get('id') is not None else _id(data.get('id'))
        if account != own:
            continue
        fallback = entry.get("time")
        changes = entry.get("changes", [])
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, Mapping) and change.get("field") == "comments":
                    comment(change.get("value"), fallback)
                elif isinstance(change, Mapping) and change.get('field') in ('messages', 'messaging_postbacks', 'messaging_postback'):
                    messaging_event(change.get('value'), fallback)
        if entry.get("field") == "comments":
            comment(entry.get("value"), fallback)
        elif entry.get('field') in ('messages', 'messaging_postbacks', 'messaging_postback'):
            messaging_event(entry.get('value'), fallback)
        messaging = entry.get("messaging", [])
        if isinstance(messaging, list):
            for item in messaging:
                messaging_event(item, fallback)
    return events


def _key(key_b64url: str) -> bytes:
    if not isinstance(key_b64url, str):
        raise ValueError("key must be base64url text")
    try:
        raw = base64.urlsafe_b64decode(key_b64url + "=" * (-len(key_b64url) % 4))
    except Exception as exc:
        raise ValueError("invalid key encoding") from exc
    if len(raw) != 32:
        raise ValueError("key must decode to 32 bytes")
    return raw


def _iso(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid envelope time")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid envelope time") from exc
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def _message_id(value: Any) -> str | None:
    # Meta message IDs are opaque, often base64; only user/media/comment IDs
    # are numeric. Do not discard genuine DM events by applying numeric rules.
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:.=+/-]{1,4096}', value):
        return value
    return None


def _valid_event(event: Any) -> bool:
    if not isinstance(event, dict) or event.get("kind") not in ("comment", "message"):
        return False
    fields = {"kind", "id", "user_id", "text", "timestamp"}
    if event["kind"] == "comment":
        fields.add("media_id")
    return (set(event) == fields and (_id(event.get('id')) if event['kind'] == 'comment' else _message_id(event.get('id'))) is not None and
            _id(event.get("user_id")) is not None and
            (event["kind"] != "comment" or _id(event.get("media_id")) is not None) and
            isinstance(event.get("text"), str) and 0 < len(event["text"]) <= MAX_TEXT and
            _canonical_stamp(event.get("timestamp")))


def _canonical_stamp(value: Any) -> bool:
    try:
        dt = _iso(value)
        return dt.year >= 2000 and dt.year <= 2100 and dt.isoformat(timespec="seconds").replace("+00:00", "Z") == value
    except (TypeError, ValueError):
        return False


def encrypt_events(events: list[dict[str, str]], key_b64url: str,
                   now: datetime | None = None) -> dict[str, Any]:
    """Return compact AES-GCM envelope; issue and expiry timestamps are encrypted."""
    if not isinstance(events, list) or len(events) > MAX_EVENTS or not all(_valid_event(e) for e in events):
        raise ValueError("invalid event list")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    issued = now.astimezone(timezone.utc).replace(microsecond=0)
    payload = json.dumps({"issued_at": issued.isoformat().replace("+00:00", "Z"),
                          "expires_at": (issued + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                          "events": events}, ensure_ascii=False, separators=(",", ":")).encode()
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(_key(key_b64url)).encrypt(nonce, payload, AAD)
    envelope = {"version": VERSION,
                "nonce": base64.urlsafe_b64encode(nonce).decode().rstrip("="),
                "ciphertext": base64.urlsafe_b64encode(cipher).decode().rstrip("=")}
    if len(json.dumps(envelope, separators=(",", ":")).encode()) > MAX_ENVELOPE_BYTES:
        raise ValueError("encrypted envelope too large")
    return envelope


def decrypt_events(envelope: Any, key_b64url: str,
                   now: datetime | None = None) -> list[dict[str, str]]:
    """Authenticate, validate and return unexpired events from an envelope."""
    if not isinstance(envelope, Mapping) or set(envelope) != {"version", "nonce", "ciphertext"} or envelope.get("version") != VERSION:
        raise ValueError("invalid envelope")
    try:
        nonce_text, cipher_text = envelope["nonce"], envelope["ciphertext"]
        if not isinstance(nonce_text, str) or not isinstance(cipher_text, str):
            raise ValueError("invalid envelope encoding")
        nonce = base64.urlsafe_b64decode(nonce_text + "=" * (-len(nonce_text) % 4))
        cipher = base64.urlsafe_b64decode(cipher_text + "=" * (-len(cipher_text) % 4))
    except Exception as exc:
        raise ValueError("invalid envelope encoding") from exc
    if len(nonce) != 12 or len(cipher) > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid envelope size")
    try:
        payload = json.loads(AESGCM(_key(key_b64url)).decrypt(nonce, cipher, AAD))
    except Exception as exc:
        raise ValueError("envelope authentication failed") from exc
    if not isinstance(payload, dict) or set(payload) != {"issued_at", "expires_at", "events"}:
        raise ValueError("invalid encrypted payload")
    issued, expires = _iso(payload["issued_at"]), _iso(payload["expires_at"])
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    now = now.astimezone(timezone.utc)
    if issued > now + timedelta(minutes=5) or expires <= now or expires - issued != timedelta(days=7):
        raise ValueError("envelope expired or issued in the future")
    events = payload["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS or not all(_valid_event(e) for e in events):
        raise ValueError("invalid event payload")
    return events
