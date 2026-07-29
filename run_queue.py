"""Исполнитель очереди публикаций: читает queue.json и публикует записи, время которых наступило."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from ig_publish import Account, IGPublishError

QUEUE_PATH = Path(__file__).resolve().parent / "queue.json"


def _log(message):
    print(message, flush=True)


def _load_queue():
    with open(QUEUE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_queue(queue):
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _is_due(entry, now_utc):
    publish_at = datetime.fromisoformat(entry["publish_at"])
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    return publish_at.astimezone(timezone.utc) <= now_utc


def _media_url(base_url, media_path):
    return f"{base_url.rstrip('/')}/{media_path.lstrip('/')}"


def _check_url(url):
    try:
        response = requests.head(url, timeout=15, allow_redirects=True)
        return response.status_code < 400
    except requests.RequestException:
        return False


def _guess_story_media_type(media_path):
    return "video" if media_path.lower().endswith((".mp4", ".mov")) else "photo"


def _publish_entry(account, entry, base_url):
    urls = [_media_url(base_url, path) for path in entry["media"]]

    for url in urls:
        if not _check_url(url):
            raise IGPublishError(f"Медиафайл недоступен: {url}")

    entry_type = entry["type"]
    caption = entry.get("caption", "")

    if entry_type == "photo":
        return account.publish_photo(urls[0], caption)
    if entry_type == "carousel":
        return account.publish_carousel(urls, caption)
    if entry_type == "reel":
        cover_path = entry.get("cover")
        cover_url = _media_url(base_url, cover_path) if cover_path else None
        return account.publish_reel(
            urls[0], caption, cover=cover_url, share_to_feed=entry.get("share_to_feed", True),
            trial=entry.get("trial", False), graduation_strategy=entry.get("graduation_strategy", "MANUAL")
        )
    if entry_type == "story":
        return account.publish_story(urls[0], media_type=_guess_story_media_type(entry["media"][0]))

    raise IGPublishError(f"Неизвестный тип публикации: {entry_type}")


def main():
    base_url = os.environ.get("PAGES_BASE_URL")
    if not base_url:
        _log("Ошибка: не задана переменная окружения PAGES_BASE_URL")
        sys.exit(1)

    queue = _load_queue()
    now_utc = datetime.now(timezone.utc)

    accounts = {}
    quota_exhausted = {}
    changed = False

    for entry in queue:
        if entry.get("status") != "pending" or not _is_due(entry, now_utc):
            continue

        account_name = entry["account"]
        entry_id = entry.get("id", "?")

        try:
            if account_name not in accounts:
                accounts[account_name] = Account.from_env(account_name)
            account = accounts[account_name]

            if account_name not in quota_exhausted:
                quota = account.quota()
                usage = quota.get("quota_usage")
                limit = quota.get("config", {}).get("quota_total")
                quota_exhausted[account_name] = limit is not None and usage is not None and usage >= limit

            if quota_exhausted[account_name]:
                _log(f"[{entry_id}] пропущено: суточная квота аккаунта '{account_name}' исчерпана")
                continue

            _log(f"[{entry_id}] публикация в '{account_name}' ({entry['type']})...")
            media_id = _publish_entry(account, entry, base_url)

            entry["status"] = "done"
            entry["published_at"] = now_utc.isoformat()
            entry["media_id"] = media_id
            changed = True
            _log(f"[{entry_id}] опубликовано, media_id={media_id}")

        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            changed = True
            _log(f"[{entry_id}] ошибка: {exc}")

    if changed:
        _save_queue(queue)
        _log("queue.json обновлён")
    else:
        _log("Нет записей для публикации")


if __name__ == "__main__":
    main()
