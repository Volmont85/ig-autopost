"""Автоответы на комментарии и личные сообщения Instagram через Claude.

Использование:
    python ig_reply.py <account>

Опрашивает через Graph API комментарии под недавними публикациями (из
queue/*.json со status="done" для этого аккаунта) и личные сообщения,
для каждого нового — просит Claude решить: ответить (и как) или отложить
на ручной просмотр (спам, угрозы, самоповреждение, что угодно вне
компетенции автоответчика). Отправляет только то, что Claude пометил как
безопасное — это не approval-gate для обычных сообщений (по решению
пользователя автоответ отправляется сразу), а узкий предохранитель именно
для потенциально опасного контента.

Состояние (что уже обработано) хранится в файлах `replies/<account>/*.json`
и коммитится в репозиторий — без этого при каждом опросе слались бы
повторные ответы на одни и те же комментарии/сообщения.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from ig_publish import Account, IGPublishError

ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / "queue"
REPLIES_DIR = ROOT / "replies"

ANTHROPIC_MODEL = os.environ.get("REPLY_MODEL", "claude-sonnet-5")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Сколько дней назад считать публикации "недавними" для проверки комментариев —
# старые посты почти не получают новых комментариев, не тратим на них вызовы API.
MEDIA_LOOKBACK_DAYS = 14

DECIDE_TOOL = {
    "name": "decide_reply",
    "description": "Решить, как поступить с одним комментарием или сообщением",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["reply", "skip"]},
            "reply": {
                "type": "string",
                "description": "Текст ответа, если action=reply. Коротко, по-человечески, на языке обращения. Без обещаний скидок/сроков, если их не просили явно.",
            },
            "reason": {
                "type": "string",
                "description": "Почему skip (спам / угроза / самоповреждение / вне компетенции / уже отвечено по сути / нужен человек) или коротко почему такой ответ",
            },
        },
        "required": ["action", "reason"],
    },
}

SYSTEM_PROMPT = """Ты отвечаешь от лица Instagram-аккаунта {account} за автора бизнеса.
Твоя задача — коротко и по-человечески ответить на комментарий или личное
сообщение, ИЛИ отложить его на ручной просмотр (action=skip), если:
- это спам, реклама, попрошайничество, массовая рассылка;
- есть признаки угрозы, оскорбления, самоповреждения, домогательства;
- нужна информация, которой у тебя нет (цены, сроки, персональные данные,
  юридические обязательства, медицинские/финансовые советы);
- вопрос сложный и однозначно требует живого человека.

Если отвечаешь — пиши тепло, по делу, без канцелярита и без вопросительных
шаблонов "спасибо за ваш комментарий!". Не обещай ничего от лица бизнеса
(скидки, сроки, гарантии), если тебя прямо об этом не попросили и это не
очевидный факт. Один-два предложения, если не просят подробностей."""


def _log(message):
    print(message, flush=True)


def _state_path(account, kind):
    d = REPLIES_DIR / account
    d.mkdir(parents=True, exist_ok=True)
    return d / f"answered_{kind}.json"


def _load_state(account, kind):
    path = _state_path(account, kind)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _log(f"повреждён файл состояния {path}, начинаю с пустого")
        return {}


def _save_state(account, kind, state):
    _state_path(account, kind).write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _decide(account, kind, text, author):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "system": SYSTEM_PROMPT.format(account=account),
        "messages": [
            {
                "role": "user",
                "content": f"Тип: {kind}. Автор: {author or 'неизвестен'}.\nТекст: {text}",
            }
        ],
        "tools": [DECIDE_TOOL],
        "tool_choice": {"type": "tool", "name": "decide_reply"},
    }
    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "decide_reply":
            return block["input"]
    raise RuntimeError(f"Claude не вернул решение: {data}")


def _recent_media_ids(account):
    cutoff = datetime.now(timezone.utc) - timedelta(days=MEDIA_LOOKBACK_DAYS)
    ids = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if entry.get("account") != account or entry.get("status") != "done":
            continue
        media_id = entry.get("media_id")
        published_at = entry.get("published_at")
        if not media_id or not published_at:
            continue
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published >= cutoff:
            ids.append((entry["id"], media_id))
    return ids


def handle_comments(account_obj, account):
    state = _load_state(account, "comments")
    new_count = 0
    for post_id, media_id in _recent_media_ids(account):
        try:
            comments = account_obj.list_comments(media_id)
        except IGPublishError as exc:
            _log(f"[{post_id}] не удалось получить комментарии: {exc}")
            continue
        for comment in comments:
            comment_id = comment["id"]
            if comment_id in state:
                continue
            if comment.get("replies", {}).get("data"):
                # Уже есть хоть один ответ в ветке — считаем закрытым, чтобы
                # не отвечать поверх ручного ответа пользователя.
                state[comment_id] = {"skipped": "already_has_reply"}
                continue
            text = comment.get("text", "")
            author = comment.get("username")
            try:
                decision = _decide(account, "комментарий", text, author)
            except Exception as exc:
                _log(f"[{comment_id}] ошибка Claude: {exc}")
                continue
            if decision.get("action") == "reply" and decision.get("reply"):
                try:
                    account_obj.reply_to_comment(comment_id, decision["reply"])
                    state[comment_id] = {"replied": decision["reply"]}
                    new_count += 1
                    _log(f"[{comment_id}] ответил: {decision['reply'][:80]}")
                except IGPublishError as exc:
                    _log(f"[{comment_id}] ошибка отправки ответа: {exc}")
                    continue
            else:
                state[comment_id] = {"skipped": decision.get("reason", "")}
                _log(f"[{comment_id}] отложен: {decision.get('reason', '')}")
    _save_state(account, "comments", state)
    return new_count


def handle_messages(account_obj, account):
    state = _load_state(account, "messages")
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
        last_id = last["id"]
        from_id = last.get("from", {}).get("id")
        if from_id == account_obj.ig_id:
            continue  # последнее сообщение уже от нас
        if last_id in state:
            continue
        text = last.get("message", "")
        try:
            decision = _decide(account, "личное сообщение", text, last.get("from", {}).get("username"))
        except Exception as exc:
            _log(f"[{conv_id}] ошибка Claude: {exc}")
            continue
        if decision.get("action") == "reply" and decision.get("reply"):
            try:
                account_obj.send_message(from_id, decision["reply"])
                state[last_id] = {"replied": decision["reply"]}
                new_count += 1
                _log(f"[{conv_id}] ответил: {decision['reply'][:80]}")
            except IGPublishError as exc:
                _log(f"[{conv_id}] ошибка отправки: {exc}")
                continue
        else:
            state[last_id] = {"skipped": decision.get("reason", "")}
            _log(f"[{conv_id}] отложен: {decision.get('reason', '')}")
    _save_state(account, "messages", state)
    return new_count


def main():
    if len(sys.argv) < 2:
        print("Использование: python ig_reply.py <account>")
        sys.exit(1)
    account = sys.argv[1]
    account_obj = Account.from_env(account)
    comments = handle_comments(account_obj, account)
    messages = handle_messages(account_obj, account)
    _log(f"итого: {comments} ответов на комментарии, {messages} на сообщения")


if __name__ == "__main__":
    main()
