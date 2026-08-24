"""Лид-магнит по кодовому слову в Instagram — goszakupki.info + @goszakupki_help.

См. lead-magnet-igtg-setup-claude-code.md за полным сценарием диалога.
Коротко: комментарий с кодовым словом под постом → Private Reply (текст) +
публичный ответ под комментарием (случайный, не повторяющий последние 3) →
как только человек хоть что-то написал в ответ → кнопка "Да, пришли" →
две "проверки" подписки на followers_count (на самом деле не блокирующие,
это вовлекающий элемент, честно залогированный) → ссылка на Telegram, где
уже идёт настоящая проверка подписки на канал через getChatMember.

Два независимых входа:
    python ig_leadmagnet.py poll <account>
        Опрос: новые комментарии с кодовым словом (Part 1) + новые ответы
        в уже открытых диалогах, ожидающих первого сообщения (Part 2).
        Дёргается heartbeat'ом раз в 5 минут, как publish.yml/reply.yml.

    python ig_leadmagnet.py handle-quick-reply <account>
        Обработка ОДНОГО нажатия Quick Reply кнопки — payload приходит из
        переменной окружения LM_PAYLOAD (JSON), которую кладёт вебхук на
        Railway через repository_dispatch (см. webhook/app.py). Нажатия
        кнопок не видны через обычный опрос .../messages — это единственный
        способ их получить.

Состояние — leadmagnet/<account>/state.json (по IG user_id) и
leadmagnet/<account>/recent_reply_variants.json — коммитятся в репозиторий
тем же паттерном, что replies/ и stats/.
"""
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ig_publish import Account, IGPublishError
from ig_reply import _recent_media_ids, _load_state as _load_replies_state, _save_state as _save_replies_state

ROOT = Path(__file__).resolve().parent
LEADMAGNET_DIR = ROOT / "leadmagnet"
LEADMAGNETS_CONTENT_PATH = ROOT / "leadmagnets" / "goszakupki.json"
STATS_DIR = ROOT / "stats"
CONVERSION_LOG_PATH = STATS_DIR / "leadmagnet-conversion.jsonl"

# Как долго считаем публикацию "недавней" для проверки новых комментариев —
# тот же принцип и то же значение, что в ig_reply.py.
MEDIA_LOOKBACK_DAYS = 14

TELEGRAM_BOT_USERNAME = os.environ.get("TG_LEADMAGNET_BOT_USERNAME", "")

# Публичные ответы под комментарием — намеренно разные по формулировке,
# и почти все явно напоминают проверить "запросы" (Instagram часто кидает
# сообщения от аккаунтов, на которых ты не подписан/не переписывался, в
# отдельную папку "Запросы на сообщения", которую многие не проверяют по
# умолчанию — увиденный паттерн у dmitriymarketing, реально поднимает шанс,
# что человек найдёт сообщение).
PUBLIC_REPLY_VARIANTS = [
    "Привет! Скинул в Direct, проверьте запросы 🔥",
    "Отправил, проверяйте запросы в директе 👍",
    "Улетело в Direct, проверьте запросы 🤍",
    "Кинул вам в Direct, не забудьте заглянуть в запросы 🙃",
    "Проверяйте запросы в Direct",
    "Готово! Гляньте в Direct, скорее всего в запросах",
    "Отправил в личку — если не видно сразу, ищите в запросах",
    "Улетело! Проверьте запросы в Директе 📩",
    "Скинул вам в Direct, посмотрите в запросах",
    "Готово, отправил! Загляните в запросы на сообщения",
    "Кинул в директ — если не в основном, смотрите в запросах",
    "Отправил! Если не видно в ЛС — посмотрите в запросах 🔍",
    "Улетело в личку, проверьте вкладку «Запросы»",
    "Скинул! Иногда падает в запросы, гляньте там",
    "Готово, отправлено в Direct — проверьте запросы, если не видно",
    "Отправил вам в личные сообщения, ищите в запросах 🙌",
    "Кинул в директ, проверьте запросы на всякий случай",
    "Улетело! Если долго не появляется — это запросы 😉",
]

RECENT_VARIANTS_WINDOW = 3


def _log(message):
    print(message, flush=True)


def _account_dir(account):
    d = LEADMAGNET_DIR / account
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _log(f"повреждён файл состояния {path}, начинаю с {default!r}")
        return default


def _save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _load_state(account):
    return _load_json(_account_dir(account) / "state.json", {})


def _save_state(account, state):
    _save_json(_account_dir(account) / "state.json", state)


def _load_content():
    return _load_json(LEADMAGNETS_CONTENT_PATH, {})


def _pick_public_reply(account):
    """Случайный вариант ответа, не повторяющий последние N использованных
    (см. RECENT_VARIANTS_WINDOW) — чтобы под соседними комментариями не
    оказалось буквально одного и того же текста."""
    path = _account_dir(account) / "recent_reply_variants.json"
    data = _load_json(path, {"recent": []})
    recent = data.get("recent", [])
    choices = [i for i in range(len(PUBLIC_REPLY_VARIANTS)) if i not in recent]
    if not choices:  # защита на случай, если окно больше числа вариантов
        choices = list(range(len(PUBLIC_REPLY_VARIANTS)))
    idx = random.choice(choices)
    recent = (recent + [idx])[-RECENT_VARIANTS_WINDOW:]
    _save_json(path, {"recent": recent})
    return PUBLIC_REPLY_VARIANTS[idx]


def _find_keyword(text, content):
    """Точное совпадение слова (регистронезависимо), не подстрока —
    "ПРОВАЛ" не должно триггериться из "провалился". Возвращает (code, entry)
    первого найденного совпадения или (None, None)."""
    words = set(re.findall(r"[a-zA-Zа-яА-ЯёЁ]+", text.upper()))
    for code, entry in content.items():
        if entry.get("keyword", "").upper() in words:
            return code, entry
    return None, None


def _log_conversion(account, code, user_id, t0, t1, t2):
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "code": code,
        "user_id": user_id,
        "t0": t0,
        "t1": t1,
        "t2": t2,
        "delta_t0_t1": None if t0 is None or t1 is None else t1 - t0,
        "delta_t0_t2": None if t0 is None or t2 is None else t2 - t0,
    }
    with open(CONVERSION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --- Part 1: новые комментарии с кодовым словом ---

def handle_comments(account_obj, account):
    content = _load_content()
    if not content:
        _log("leadmagnets/<account>.json пуст или не найден — нечего искать")
        return 0

    replies_state = _load_replies_state(account, "comments")
    lm_state = _load_state(account)
    new_count = 0

    for post_id, media_id in _recent_media_ids(account):
        try:
            comments = account_obj.list_comments(media_id)
        except IGPublishError as exc:
            _log(f"[{post_id}] не удалось получить комментарии: {exc}")
            continue
        for comment in comments:
            comment_id = comment["id"]
            if comment_id in replies_state:
                continue
            code, entry = _find_keyword(comment.get("text", ""), content)
            if not code:
                continue  # не наш комментарий — пусть его видит общий ig_reply.py, если подключат

            title = entry.get("title", code)
            try:
                pr_response = account_obj.reply_private(
                    comment_id,
                    f"Привет! Материал по «{title}» готов. Напишите в ответ "
                    f"любое слово — пришлю условия получения.",
                )
                public_text = _pick_public_reply(account)
                account_obj.reply_to_comment(comment_id, public_text)
            except IGPublishError as exc:
                _log(f"[{comment_id}] ошибка отправки Private Reply/публичного ответа: {exc}")
                continue

            replies_state[comment_id] = {"skipped": "handled_by_leadmagnet"}
            # Ответ на Private Reply содержит recipient_id — реальный IG
            # user_id комментатора (подтверждено официальной документацией
            # Private Replies, 24.08.2026) — используем его сразу как ключ
            # состояния, без гадания в Part 2 по порядку диалогов.
            user_id = pr_response.get("recipient_id")
            if not user_id:
                _log(f"[{comment_id}] в ответе Private Reply нет recipient_id, пропускаю запись состояния: {pr_response}")
                continue
            lm_state[user_id] = {
                "code": code,
                "comment_id": comment_id,
                "stage": "awaiting_first_reply",
            }
            new_count += 1
            _log(f"[{comment_id}] кодовое слово «{entry.get('keyword')}» → Private Reply + публичный ответ отправлены (user_id={user_id})")

    _save_replies_state(account, "comments", replies_state)
    _save_state(account, lm_state)
    return new_count


# --- Part 2: первый ответ пользователя открывает диалог → кнопка "Да, пришли" ---

def handle_first_replies(account_obj, account):
    lm_state = _load_state(account)
    pending_user_ids = {
        uid for uid, rec in lm_state.items() if rec.get("stage") == "awaiting_first_reply"
    }
    if not pending_user_ids:
        return 0

    new_count = 0
    try:
        conversations = account_obj.list_conversations()
    except IGPublishError as exc:
        _log(f"не удалось получить список диалогов: {exc}")
        return 0

    for conv in conversations:
        conv_id = conv["id"]
        try:
            messages = account_obj.list_messages(conv_id, limit=5)
        except IGPublishError as exc:
            _log(f"[{conv_id}] не удалось получить сообщения: {exc}")
            continue
        if not messages:
            continue
        last = messages[0]
        from_id = last.get("from", {}).get("id")
        if from_id not in pending_user_ids:
            continue  # не наш диалог или уже прошёл дальше этой стадии
        if from_id == account_obj.ig_id:
            continue  # последнее сообщение — наше, ответа ещё не было

        rec = lm_state[from_id]
        title = _load_content().get(rec["code"], {}).get("title", rec["code"])
        try:
            account_obj.send_quick_replies(
                from_id,
                f"Прислать вам материал «{title}»?",
                [{"title": "✅ Да, пришли", "payload": "LM_YES"}],
            )
            rec["stage"] = "awaiting_yes"
            new_count += 1
            _log(f"[{from_id}] первый ответ получен → отправлена кнопка «Да, пришли»")
        except IGPublishError as exc:
            _log(f"[{from_id}] ошибка отправки кнопки: {exc}")
            continue

    _save_state(account, lm_state)
    return new_count


# --- Обработка нажатия Quick Reply кнопки (только через вебхук) ---

def handle_quick_reply(account_obj, account, payload):
    user_id = payload.get("sender_id")
    button = payload.get("payload")
    if not user_id or not button:
        _log(f"неполный payload, пропущено: {payload}")
        return

    lm_state = _load_state(account)
    rec = lm_state.get(user_id)
    if not rec:
        _log(f"[{user_id}] нет ожидающей записи лид-магнита для этой кнопки ({button}), игнорирую")
        return

    stage = rec.get("stage")
    content = _load_content()
    title = content.get(rec["code"], {}).get("title", rec["code"])

    if button == "LM_YES" and stage == "awaiting_yes":
        t0 = account_obj.account_fields(fields="followers_count").get("followers_count")
        rec["t0"] = t0
        rec["stage"] = "awaiting_check_1"
        account_obj.send_quick_replies(
            user_id,
            f"⚠️ Материал доступен только для подписчиков\n\n"
            f"Подпишитесь, чтобы получить доступ к «{title}» + материалам "
            f"канала @goszakupki_help\n\nПодписываетесь — жмёте кнопку — "
            f"доступ откроется 👇",
            [{"title": "✅ Подписка есть!", "payload": "LM_CHECK_1"}],
        )
        _log(f"[{user_id}] LM_YES: t0={t0}, отправлена первая проверка")

    elif button == "LM_CHECK_1" and stage == "awaiting_check_1":
        t1 = account_obj.account_fields(fields="followers_count").get("followers_count")
        rec["t1"] = t1
        rec["stage"] = "awaiting_check_2"
        account_obj.send_message(user_id, "Сек, проверяю...")
        account_obj.send_quick_replies(
            user_id,
            "Хитрите? Без подписки не работает 😏\n\nПодпишитесь и нажимайте 👇",
            [{"title": "🔁 Повторная проверка", "payload": "LM_CHECK_2"}],
        )
        _log(f"[{user_id}] LM_CHECK_1: t1={t1} (t0={rec.get('t0')}), отправлена вторая проверка")

    elif button == "LM_CHECK_2" and stage == "awaiting_check_2":
        t2 = account_obj.account_fields(fields="followers_count").get("followers_count")
        rec["t2"] = t2
        rec["stage"] = "done"
        link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={rec['code']}" if TELEGRAM_BOT_USERNAME else "(TG_LEADMAGNET_BOT_USERNAME не задан)"
        account_obj.send_message(user_id, f"Держите 🤍 {link}")
        _log_conversion(account, rec["code"], user_id, rec.get("t0"), rec.get("t1"), t2)
        _log(f"[{user_id}] LM_CHECK_2: t2={t2} (t0={rec.get('t0')}, t1={rec.get('t1')}) → ссылка на TG отправлена, stage=done")

    else:
        _log(f"[{user_id}] кнопка {button} не соответствует текущей стадии {stage}, игнорирую (защита от повторов/чужих нажатий)")
        return

    lm_state[user_id] = rec
    _save_state(account, lm_state)


def main():
    if len(sys.argv) < 3:
        print("Использование:")
        print("  python ig_leadmagnet.py poll <account>")
        print("  python ig_leadmagnet.py handle-quick-reply <account>")
        sys.exit(1)

    mode, account = sys.argv[1], sys.argv[2]
    account_obj = Account.from_env(account)

    if mode == "poll":
        comments = handle_comments(account_obj, account)
        first_replies = handle_first_replies(account_obj, account)
        _log(f"итого: {comments} новых кодовых слов, {first_replies} первых ответов обработано")
    elif mode == "handle-quick-reply":
        raw = os.environ.get("LM_PAYLOAD")
        if not raw:
            print("Ошибка: не задана переменная окружения LM_PAYLOAD")
            sys.exit(1)
        payload = json.loads(raw)
        handle_quick_reply(account_obj, account, payload)
    else:
        print(f"Неизвестный режим: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
