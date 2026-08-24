"""Вебхук-приёмник для Instagram Messaging (Meta Webhooks) + heartbeat для
всего проекта ig-autopost, деплоится на Railway.

Вебхук: Meta отдаёт нажатие Quick Reply кнопки (`message.quick_reply.payload`)
ТОЛЬКО через вебхук, этого поля физически нет при обычном GET-опросе
`/{conversation-id}/messages` (проверено по официальному Message Reference,
24.08.2026). Проверяет подпись и пересылает событие в GitHub через
repository_dispatch — вся реальная логика остаётся в GitHub Actions.

Heartbeat (добавлено 24.08.2026, до этого жил на Маке — см. CLAUDE.md,
инцидент 5 про ненадёжный `schedule` GitHub, ~70% дропа тиков): фоновый
поток каждые HEARTBEAT_INTERVAL_SECONDS дёргает workflow_dispatch для
publish.yml/reply.yml/leadmagnet_ig.yml/leadmagnet_tg.yml напрямую через
GitHub API. Railway — постоянно работающий процесс (в отличие от Мака,
который может быть выключен/спать), поэтому переезд сюда убирает
зависимость всей автоматизации от конкретного железа пользователя. Ничего
не персистится — если Railway перезапустится, просто пропустится один тик,
это не очередь и не состояние, восстанавливается само на следующей
итерации.

Формат payload сверен с официальной документацией Meta (Graph API Webhooks
getting-started + Messenger Platform webhooks): envelope object/entry/
messaging, подпись — HMAC-SHA256 от сырого тела запроса на APP_SECRET,
заголовок `X-Hub-Signature-256: sha256=<hex>`.
"""
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request

VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]
APP_SECRET = os.environ["META_APP_SECRET"].encode("utf-8")
GITHUB_TOKEN = os.environ["GH_DISPATCH_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Volmont85/ig-autopost")
DISPATCH_EVENT_TYPE = os.environ.get("DISPATCH_EVENT_TYPE", "ig_quick_reply")
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "120"))

# Что и с какими входами дёргать каждый тик. account для reply.yml/
# leadmagnet_ig.yml захардкожен под текущий набор аккаунтов — см. те же
# ограничения, что уже были у ~/ig-autopost-heartbeat.sh на Маке.
HEARTBEAT_WORKFLOWS = [
    {"workflow": "publish.yml"},
    {"workflow": "reply.yml", "inputs": {"account": "realbuiltbyone"}},
    {"workflow": "leadmagnet_ig.yml", "inputs": {"account": "goszakupki"}},
    {"workflow": "leadmagnet_tg.yml"},
]

app = Flask(__name__)
_heartbeat_status = {"last_tick_at": None, "last_errors": []}


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


def _dispatch_workflow(workflow_file, inputs=None):
    body = {"ref": "main"}
    if inputs:
        body["inputs"] = inputs
    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


def _heartbeat_tick():
    """Один проход по всем воркфлоу — вынесено отдельно от бесконечного
    цикла, чтобы можно было протестировать без реальных sleep()/сети."""
    errors = []
    for wf in HEARTBEAT_WORKFLOWS:
        try:
            _dispatch_workflow(wf["workflow"], wf.get("inputs"))
        except requests.RequestException as exc:
            # Один неудавшийся dispatch не должен останавливать остальные
            # в этом же тике — тот же принцип, что был у heartbeat.sh на
            # Маке (publish/reply/leadmagnet триггеры независимы друг от друга).
            errors.append(f"{wf['workflow']}: {exc}")
            app.logger.error("heartbeat: не удалось dispatch %s: %s", wf["workflow"], exc)
    _heartbeat_status["last_tick_at"] = datetime.now(timezone.utc).isoformat()
    _heartbeat_status["last_errors"] = errors


def _heartbeat_loop():
    while True:
        _heartbeat_tick()
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def _start_heartbeat_thread():
    thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    thread.start()


if os.environ.get("DISABLE_HEARTBEAT") != "1":
    _start_heartbeat_thread()


@app.get("/")
def health():
    return {"status": "ok", "heartbeat": _heartbeat_status}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
