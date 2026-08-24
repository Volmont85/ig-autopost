"""Тонкий вебхук-приёмник для Instagram Messaging (Meta Webhooks), деплоится
на Railway. Единственная причина существования — Meta отдаёт нажатие
Quick Reply кнопки (`message.quick_reply.payload`) ТОЛЬКО через вебхук, этого
поля физически нет при обычном GET-опросе `/{conversation-id}/messages`
(проверено по официальному Message Reference, 24.08.2026).

Вся реальная логика (стейт лид-магнита, что отправить в ответ, статистика)
здесь НЕ живёт — сервис только проверяет подпись и пересылает событие в
GitHub через repository_dispatch. Дальше это подхватывает обычный workflow
на GitHub Actions, как и весь остальной проект. Если Railway упадёт —
теряются только нажатия кнопок за время простоя, ничего необратимого:
пользователь просто не получит ответ на кнопку и может написать текстом
(Private Reply на первый комментарий всё ещё уходит через обычный опрос,
это отдельный, уже проверенный путь).

Формат payload сверен с официальной документацией Meta (Graph API Webhooks
getting-started + Messenger Platform webhooks): envelope object/entry/
messaging, подпись — HMAC-SHA256 от сырого тела запроса на APP_SECRET,
заголовок `X-Hub-Signature-256: sha256=<hex>`.
"""
import hashlib
import hmac
import json
import os

import requests
from flask import Flask, request

VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]
APP_SECRET = os.environ["META_APP_SECRET"].encode("utf-8")
GITHUB_TOKEN = os.environ["GH_DISPATCH_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Volmont85/ig-autopost")
DISPATCH_EVENT_TYPE = os.environ.get("DISPATCH_EVENT_TYPE", "ig_quick_reply")

app = Flask(__name__)


@app.get("/webhook")
def verify():
    """Handshake при подписке вебхука в Meta App Dashboard."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403


def _signature_valid(raw_body: bytes) -> bool:
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def _forward_to_github(client_payload: dict):
    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
        },
        json={"event_type": DISPATCH_EVENT_TYPE, "client_payload": client_payload},
        timeout=10,
    )
    resp.raise_for_status()


@app.post("/webhook")
def receive():
    raw_body = request.get_data()

    # Meta требует ответ 200 в любом случае, но подпись всё равно проверяем
    # строго — без валидной подписи тело не считаем нашим и ничего не шлём
    # дальше (не доверяем данным без проверки, даже если формат похож).
    if not _signature_valid(raw_body):
        app.logger.warning("подпись не совпала, тело проигнорировано")
        return "ok", 200

    # Не полагаемся на заголовок Content-Type запроса — request.get_json()
    # у Flask молча возвращает None, если он не ровно "application/json",
    # без единой ошибки в логе. Парсим JSON из сырого тела напрямую.
    try:
        data = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        app.logger.warning("тело не распарсилось как JSON, проигнорировано")
        return "ok", 200
    ig_id = data.get("id")  # id аккаунта-получателя из корня события Instagram Login webhook

    for entry in data.get("entry", []):
        for item in entry.get("messaging", []):
            message = item.get("message") or {}
            quick_reply = message.get("quick_reply")
            if not quick_reply:
                continue  # обычный текст — его читает уже существующий поллинг, не дублируем
            try:
                _forward_to_github({
                    "ig_id": ig_id or entry.get("id"),
                    "sender_id": (item.get("sender") or {}).get("id"),
                    "recipient_id": (item.get("recipient") or {}).get("id"),
                    "mid": message.get("mid"),
                    "text": message.get("text", ""),
                    "payload": quick_reply.get("payload"),
                    "timestamp": item.get("timestamp"),
                })
            except requests.RequestException as exc:
                # Не роняем ответ Meta из-за проблем на стороне GitHub — иначе
                # Meta начнёт ретраить вебхук и может временно отписать нас
                # при повторных ошибках. Теряем это конкретное нажатие, но
                # логируем, чтобы было видно в Railway logs.
                app.logger.error("не удалось переслать в GitHub: %s", exc)

    return "ok", 200


@app.get("/")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
