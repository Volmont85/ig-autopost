"""Удаляет медиафайлы уже опубликованных записей старше суток.

Instagram скачивает файл в момент публикации — после успешного паблиша
хранить исходник в репозитории больше не нужно ради Instagram, только ради
собственного удобства. Держим минимум сутки: если заметили, что что-то
странное с публикацией, есть время разобраться, пока источник ещё на месте.

Сама запись в queue/<id>.json не удаляется никогда — это малый по размеру
JSON, полезная история публикаций (защита от повторной публикации того же
контента, аудит). Удаляется только тяжёлый медиафайл, и то не раньше, чем
сутки прошли и файл не нужен какой-то другой, ещё не готовой к чистке записи
(бывают случаи, когда две записи ссылаются на один и тот же файл — например,
пробная и обычная версия одного ролика).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUEUE_DIR = Path(__file__).resolve().parent / "queue"
ROOT = Path(__file__).resolve().parent
MIN_AGE = timedelta(hours=24)


def _log(message):
    print(message, flush=True)


def _save_entry(path, entry):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _entry_media_paths(entry):
    paths = list(entry.get("media", []))
    if entry.get("cover"):
        paths.append(entry["cover"])
    return paths


def _is_eligible(entry, now_utc):
    if entry.get("status") != "done" or entry.get("media_deleted"):
        return False
    published_at_raw = entry.get("published_at")
    if not published_at_raw:
        return False
    published_at = datetime.fromisoformat(published_at_raw)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return now_utc - published_at.astimezone(timezone.utc) >= MIN_AGE


def main():
    now_utc = datetime.now(timezone.utc)
    entries = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            entries.append((path, json.load(f)))

    eligible_ids = {
        entry.get("id", path.stem) for path, entry in entries if _is_eligible(entry, now_utc)
    }

    # Файл защищён, если на него ссылается хоть одна запись, ещё не готовая к чистке.
    protected_paths = set()
    for path, entry in entries:
        entry_id = entry.get("id", path.stem)
        if entry_id in eligible_ids:
            continue
        protected_paths.update(_entry_media_paths(entry))

    removed_files = 0
    updated_entries = 0

    for path, entry in entries:
        entry_id = entry.get("id", path.stem)
        if entry_id not in eligible_ids:
            continue

        for media_path in _entry_media_paths(entry):
            if media_path in protected_paths:
                _log(f"[{entry_id}] пропущен {media_path}: используется другой записью, ещё не готовой к чистке")
                continue
            full_path = ROOT / media_path
            if full_path.exists():
                full_path.unlink()
                removed_files += 1
                _log(f"[{entry_id}] удалён {media_path}")

        entry["media_deleted"] = True
        entry["media_deleted_at"] = now_utc.isoformat()
        _save_entry(path, entry)
        updated_entries += 1

    if updated_entries:
        _log(f"Готово: удалено файлов {removed_files}, записей обновлено {updated_entries}")
    else:
        _log("Нечего чистить")


if __name__ == "__main__":
    main()
