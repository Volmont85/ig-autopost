"""Отчёт по вовлечённости опубликованных постов за последние N дней (по умолчанию 7).

Использование: python stats.py [дней]

Берёт media_id из queue/*.json (status: "done") и запрашивает у Instagram
базовые поля (лайки, комментарии) и insights (охват, показы, сохранения и
т.д. — набор метрик зависит от типа контента). У Stories insights доступны
только первые 24 часа после публикации — если сторис старше, Instagram
вернёт ошибку, это ожидаемо и не баг, просто нет данных.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ig_publish import Account, IGPublishError

QUEUE_DIR = Path(__file__).resolve().parent / "queue"

METRICS_BY_TYPE = {
    "reel": ["plays", "reach", "likes", "comments", "shares", "saved", "total_interactions"],
    "carousel": ["reach", "likes", "comments", "saved", "shares", "total_interactions"],
    "photo": ["reach", "likes", "comments", "saved", "shares", "total_interactions"],
    "story": ["reach", "exits", "replies", "taps_forward", "taps_back"],
}


def _log(message):
    print(message, flush=True)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    since = datetime.now(timezone.utc) - timedelta(days=days)

    accounts = {}
    rows = []

    for path in sorted(QUEUE_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)

        if entry.get("status") != "done" or not entry.get("media_id") or not entry.get("published_at"):
            continue

        published_at = datetime.fromisoformat(entry["published_at"])
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if published_at < since:
            continue

        account_name = entry["account"]
        if account_name not in accounts:
            accounts[account_name] = Account.from_env(account_name)
        account = accounts[account_name]

        media_id = entry["media_id"]
        entry_id = entry.get("id", path.stem)
        entry_type = entry.get("type", "photo")

        row = {
            "id": entry_id,
            "account": account_name,
            "type": entry_type,
            "trial": entry.get("trial", False),
            "published_at": entry["published_at"],
        }

        try:
            info = account.media_fields(media_id)
            row["like_count"] = info.get("like_count")
            row["comments_count"] = info.get("comments_count")
            row["permalink"] = info.get("permalink")
        except IGPublishError as exc:
            row["fields_error"] = str(exc)

        metrics = METRICS_BY_TYPE.get(entry_type, [])
        if metrics:
            try:
                data = account.media_insights(media_id, metrics)
                for item in data.get("data", []):
                    name = item.get("name")
                    values = item.get("values", [])
                    value = values[0].get("value") if values else None
                    row[name] = value
            except IGPublishError as exc:
                row["insights_error"] = str(exc)

        rows.append(row)
        _log(f"[{entry_id}] обработано")

    _log("---REPORT---")
    _log(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
