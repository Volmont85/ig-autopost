"""Исполнитель очереди публикаций: читает queue/*.json и публикует записи, время которых наступило.

Каждая запись хранится в отдельном файле queue/<id>.json, а не в одном общем
массиве queue.json — это устраняет git-конфликты, когда GitHub Actions
обновляет статус одной записи одновременно с тем, как локальный автопуш
добавляет или меняет другую: разные записи теперь физически разные файлы.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from ig_publish import Account, IGPublishError

REPO_DIR = Path(__file__).resolve().parent
QUEUE_DIR = REPO_DIR / "queue"

# Если прогон захватил запись (status: publishing), но не успел её завершить
# за это время — считаем прогон умершим (упал/убит) и разрешаем перезахват.
# С запасом больше самого длинного таймаута публикации (300с для видео/reels).
CLAIM_STALE_SECONDS = 15 * 60


def _log(message):
    print(message, flush=True)


def _load_entries():
    entries = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        entries.append((path, entry))
    return entries


def _save_entry(path, entry):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _is_due(entry, now_utc):
    publish_at_raw = entry.get("publish_at")
    if not publish_at_raw:
        # Нет publish_at -> публиковать как можно скорее (как только запись
        # дойдёт до GitHub) — это и есть протокол "опубликуй сейчас".
        return True
    publish_at = datetime.fromisoformat(publish_at_raw)
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    return publish_at.astimezone(timezone.utc) <= now_utc


def _parse_dt(raw):
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_claimable(entry, now_utc):
    status = entry.get("status")
    if status == "pending":
        return True
    if status != "publishing":
        return False
    claimed_at_raw = entry.get("claimed_at")
    if not claimed_at_raw:
        return True
    return (now_utc - _parse_dt(claimed_at_raw)).total_seconds() > CLAIM_STALE_SECONDS


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True
    )


def _ensure_git_identity():
    if not _git("config", "user.email").stdout.strip():
        _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
        _git("config", "user.name", "github-actions[bot]")


def _commit_and_push(rel_path, message):
    """Коммитит и пушит один файл очереди с ретраями fetch+rebase — тот же
    паттерн, что раньше жил только в workflow YAML (см. CLAUDE.md, инцидент
    2026-08-06). Возвращает True, если наш коммит доехал до origin/main.

    На конфликте при rebase (кто-то другой запушил изменение того же файла)
    сразу отступаем, не тратя оставшиеся попытки, — вызывающий код сам
    решит, что делать, перечитав файл с диска."""
    _ensure_git_identity()
    for attempt in range(6):
        _git("add", str(rel_path))
        _git("commit", "-m", message)  # если уже закоммичено локально с прошлой попытки — просто no-op
        if _git("push").returncode == 0:
            return True
        _git("fetch", "origin", "main")
        if _git("rebase", "origin/main").returncode != 0:
            _git("rebase", "--abort")
            break
        time.sleep(3)
    # Синхронизируемся с origin/main перед выходом — иначе брошенный локальный
    # коммит (наша проигранная заявка) может уехать в push следующей записи
    # цикла и затереть чужой реальный статус (например, "done" победителя).
    _git("reset", "--hard", "origin/main")
    return False


def _claim_entry(path, entry, now_utc):
    """Помечает запись 'publishing' и пушит это в git ДО вызова Instagram API.

    Если пуш проигран другому прогону — значит кто-то уже забрал эту запись,
    и мы её пропускаем вместо повторной публикации. Раньше запись читалась
    один раз в начале джобы, а коммит/пуш финального статуса происходил
    только в самом конце workflow — при одновременном старте нескольких
    прогонов (как случилось 2026-08-06 при выходе из Major Outage GitHub
    Actions: скопилось ~20 отложенных запусков, стартовавших разом) каждый
    из них видел status=pending и публиковал независимо — очередь ушла в
    Instagram 9 раз подряд. Захват здесь закрывает именно эту гонку: кто
    первым запушил 'publishing', тот и публикует.
    """
    rel_path = path.relative_to(REPO_DIR)
    entry["status"] = "publishing"
    entry["claimed_at"] = now_utc.isoformat()
    _save_entry(path, entry)
    entry_id = entry.get("id", path.stem)

    if _commit_and_push(rel_path, f"queue: claim {entry_id} [skip ci]"):
        return True
    return False


def _media_url(base_url, media_path):
    return f"{base_url.rstrip('/')}/{media_path.lstrip('/')}"


def _check_url(url, attempts=8, delay=15):
    # GitHub Pages нужно время на билд/деплой после push (обычно 30-90 сек).
    # Особенно актуально при триггере publish.yml по push — он срабатывает
    # быстрее, чем Pages успевает выложить новый файл, поэтому одна попытка
    # HEAD-запроса без повторов даёт ложный "недоступен" на свежих файлах.
    for attempt in range(attempts):
        try:
            response = requests.head(url, timeout=15, allow_redirects=True)
            if response.status_code < 400:
                return True
        except requests.RequestException:
            pass
        if attempt < attempts - 1:
            time.sleep(delay)
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

    QUEUE_DIR.mkdir(exist_ok=True)
    entries = _load_entries()
    now_utc = datetime.now(timezone.utc)

    accounts = {}
    quota_exhausted = {}
    processed = 0

    for path, _stale_entry in entries:
        # Перечитываем с диска, а не доверяем entries из _load_entries() в
        # начале джобы: между стартом и этим моментом локальный git мог уйти
        # вперёд — например, _commit_and_push() для ДРУГОЙ, более ранней по
        # алфавиту записи мог проиграть гонку и сделать `git reset --hard
        # origin/main`, который тихо обновил файл ЭТОЙ записи на диске тоже
        # (см. инцидент 2026-08-18: reel-16aug-povtor опубликован дважды —
        # второй прогон после такого reset увидел свежий "done" на диске, но
        # продолжил работать со старым "pending" из памяти и затёр его).
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except FileNotFoundError:
            continue

        if not _is_claimable(entry, now_utc) or not _is_due(entry, now_utc):
            continue

        account_name = entry["account"]
        entry_id = entry.get("id", path.stem)
        rel_path = path.relative_to(REPO_DIR)

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

            processed += 1
            if not _claim_entry(path, entry, now_utc):
                _log(f"[{entry_id}] пропущено: запись уже забрал другой прогон")
                continue

            _log(f"[{entry_id}] публикация в '{account_name}' ({entry['type']})...")
            media_id = _publish_entry(account, entry, base_url)

            entry["status"] = "done"
            entry["published_at"] = now_utc.isoformat()
            entry["media_id"] = media_id
            _save_entry(path, entry)
            _commit_and_push(rel_path, f"queue: published {entry_id} [skip ci]")
            _log(f"[{entry_id}] опубликовано, media_id={media_id}")

        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            _save_entry(path, entry)
            _commit_and_push(rel_path, f"queue: failed {entry_id} [skip ci]")
            _log(f"[{entry_id}] ошибка: {exc}")

    if processed == 0:
        _log("Нет записей для публикации")


if __name__ == "__main__":
    main()
