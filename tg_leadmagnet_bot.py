"""Telegram-бот для выдачи лид-магнита goszakupki (Часть Б сценария).

НЕ путать с telegram_work/telegram_personal (MCP для личного/рабочего
аккаунта Александра, MTProto/Telethon) — это отдельный полноценный
Telegram-бот через Bot API, заведённый в @BotFather (@goszakupkiinfo_bot).

Поток:
    /start <CODE>  (пользователь переходит по ссылке из Instagram, код —
                    ключ из leadmagnets/goszakupki.json)
        -> предложить подписаться на @goszakupki_help + кнопка "Проверить подписку"
    нажатие кнопки -> getChatMember по каналу
        member/creator/administrator -> выдать текст лид-магнита по CODE
        left/kicked/restricted       -> честно "пока не вижу подписки", та
                                         же кнопка, можно жать сколько угодно раз

Опрос через getUpdates (без вебхука — Telegram Bot API отдаёт нажатия
инлайн-кнопок (callback_query) прямо в ответе поллинга, в отличие от
Instagram Graph API, где это возможно только через вебхук). Смещение
(offset) хранится в leadmagnet_tg/state.json и коммитится в репозиторий,
тем же паттерном, что остальные состояния в проекте.
"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
LEADMAGNETS_CONTENT_PATH = ROOT / "leadmagnets" / "goszakupki.json"
STATE_PATH = ROOT / "leadmagnet_tg" / "state.json"

BOT_TOKEN = os.environ.get("TG_LEADMAGNET_BOT_TOKEN")
CHANNEL_CHAT_ID = os.environ.get("TG_GOSZAKUPKI_HELP_CHAT_ID")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

CHECK_PREFIX = "CHECK:"


def _log(message):
    print(message, flush=True)


def _api(method, **params):
    response = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data}")
    return data["result"]


def _load_state():
    if not STATE_PATH.exists():
        return {"offset": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _log(f"повреждён {STATE_PATH}, начинаю с offset=0")
        return {"offset": 0}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_content():
    if not LEADMAGNETS_CONTENT_PATH.exists():
        return {}
    return json.loads(LEADMAGNETS_CONTENT_PATH.read_text(encoding="utf-8"))


def _check_button(code):
    return {
        "inline_keyboard": [[{"text": "✅ Проверить подписку", "callback_data": f"{CHECK_PREFIX}{code}"}]]
    }


def _handle_start(message, code):
    chat_id = message["chat"]["id"]
    content = _load_content()
    entry = content.get(code)
    if not entry:
        _api("sendMessage", chat_id=chat_id,
             text="Не нашёл этот материал — похоже, ссылка устарела. Напишите кодовое слово под постом в Instagram ещё раз.")
        _log(f"[{chat_id}] неизвестный код в /start: {code!r}")
        return

    _api(
        "sendMessage", chat_id=chat_id,
        text=f"Материал «{entry.get('title', code)}» доступен подписчикам @goszakupki_help.\n\n"
             f"Подпишитесь на канал и нажмите кнопку 👇",
        reply_markup=_check_button(code),
    )
    _log(f"[{chat_id}] /start {code} -> предложена подписка")


def _handle_bare_start(message):
    chat_id = message["chat"]["id"]
    _api("sendMessage", chat_id=chat_id,
         text="Привет! Чтобы получить материал — напишите кодовое слово под постом в Instagram, оттуда придёт ссылка сюда.")


def _answer_callback_safe(callback_id, **kwargs):
    """answerCallbackQuery — чисто косметика (снимает "часики" на кнопке у
    пользователя, показывает всплывашку), но у Telegram на неё жёсткий
    таймаут — на практике единицы секунд. Наш поллинг раз в 5 минут почти
    гарантированно его не успевает, и это НЕ ошибка обработки: подписка уже
    проверена, материал (если положен) уже отправлен. Поэтому здесь всегда
    best-effort — падение тут не должно прерывать функцию (см. инцидент
    24.08.2026: необработанное исключение здесь роняло весь скрипт ДО
    сохранения offset, из-за чего следующий прогон переспрашивал тот же
    callback и повторно слал материал — бесконечный цикл дублей)."""
    try:
        _api("answerCallbackQuery", callback_query_id=callback_id, **kwargs)
    except RuntimeError as exc:
        _log(f"answerCallbackQuery не прошла (не критично): {exc}")


def _handle_callback(callback_query):
    data = callback_query.get("data", "")
    if not data.startswith(CHECK_PREFIX):
        return
    code = data[len(CHECK_PREFIX):]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    callback_id = callback_query["id"]

    try:
        member = _api("getChatMember", chat_id=CHANNEL_CHAT_ID, user_id=user_id)
        status = member.get("status")
    except RuntimeError as exc:
        _log(f"[{user_id}] ошибка getChatMember: {exc}")
        _answer_callback_safe(callback_id, text="Не получилось проверить, попробуйте ещё раз")
        return

    if status in ("member", "creator", "administrator"):
        content = _load_content()
        entry = content.get(code, {})
        text = entry.get("text") or "Материал скоро добавим — код принят, но текста пока нет."
        _api("sendMessage", chat_id=chat_id, text=text)
        _answer_callback_safe(callback_id, text="Подписка подтверждена ✅")
        _log(f"[{user_id}] подписка подтверждена ({status}), материал «{code}» выдан")
    else:
        _answer_callback_safe(callback_id, text="Пока не вижу подписки", show_alert=True)
        _api(
            "sendMessage", chat_id=chat_id,
            text="Пока не вижу подписки на @goszakupki_help 🙂 Подпишитесь и нажмите ещё раз",
            reply_markup=_check_button(code),
        )
        _log(f"[{user_id}] подписки нет (status={status}), предложено повторить")


def main():
    if not BOT_TOKEN or not CHANNEL_CHAT_ID:
        print("Ошибка: не заданы TG_LEADMAGNET_BOT_TOKEN / TG_GOSZAKUPKI_HELP_CHAT_ID")
        sys.exit(1)

    state = _load_state()
    updates = _api("getUpdates", offset=state["offset"], timeout=0)

    processed = 0
    for update in updates:
        state["offset"] = update["update_id"] + 1

        try:
            message = update.get("message")
            if message and message.get("text", "").startswith("/start"):
                parts = message["text"].split(maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    _handle_start(message, parts[1].strip())
                else:
                    _handle_bare_start(message)
                processed += 1
                continue

            callback_query = update.get("callback_query")
            if callback_query:
                _handle_callback(callback_query)
                processed += 1
                continue
        except Exception as exc:
            # Один сломанный апдейт не должен ронять весь прогон и терять
            # offset для остальных, уже успешно обработанных, в этом же
            # батче — offset для ЭТОГО апдейта уже продвинут выше, значит
            # его не переспросят повторно, ошибка просто логируется.
            _log(f"[update_id={update['update_id']}] необработанная ошибка, пропускаю: {exc}")

    _save_state(state)
    _log(f"итого обработано апдейтов: {processed}")


if __name__ == "__main__":
    main()
