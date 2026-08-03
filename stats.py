"""Сбор статистики Instagram и построение отчёта по эффективности публикаций.

Использование:
    python stats.py            # обычный прогон: собрать замеры + пересобрать отчёт
    python stats.py --report   # только пересобрать отчёт из истории, без запросов к API
    python stats.py --all      # принудительно замерить всё, игнорируя расписание

Идея: одна цифра просмотров ничего не значит, потому что рилс копит охват
неделями. Поэтому храним ВРЕМЕННОЙ РЯД: при каждом прогоне дописываем в
`stats/history.jsonl` новый замер по каждому подходящему посту. Дальше посты
сравниваются между собой в одинаковом возрасте (например, views на 24 часа),
и только такое сравнение честное.

Что пишется на диск:
    stats/history.jsonl  append-only, одна строка = один замер одного поста.
                         Никогда не переписывается: git-конфликтов не создаёт.
    stats/latest.json    последний замер по каждому посту, для быстрого чтения.
    stats/report.md      человекочитаемый разбор: срезы по часу публикации,
                         дню недели, теме, типу, trial vs обычный.

Расписание замеров (чтобы не жечь лимиты Graph API на старых постах):
    возраст < 24 ч   - при каждом прогоне
    24 ч .. 7 дней   - не чаще раза в 20 часов
    7 .. 30 дней     - не чаще раза в 6 дней
    старше 30 дней   - больше не трогаем, цифры считаем финальными
    stories          - только первые 24 часа, дальше Instagram insights не отдаёт

Поля `topic` и `hook` в queue/<id>.json необязательные, но если их заполнять,
отчёт сможет сравнивать темы и формулировки хуков между собой.
"""
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ig_publish import Account, IGPublishError

ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / "queue"
STATS_DIR = ROOT / "stats"
HISTORY_PATH = STATS_DIR / "history.jsonl"
LATEST_PATH = STATS_DIR / "latest.json"
REPORT_PATH = STATS_DIR / "report.md"

MSK = timezone(timedelta(hours=3))
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

METRICS_BY_TYPE = {
    "reel": ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions"],
    "carousel": ["reach", "likes", "comments", "saved", "shares", "total_interactions"],
    "photo": ["reach", "likes", "comments", "saved", "shares", "total_interactions"],
    "story": ["reach", "replies", "navigation"],
}

# Главная метрика для сравнения постов между собой, по типам.
PRIMARY_METRIC = {"reel": "views", "carousel": "reach", "photo": "reach", "story": "reach"}

# Возраст в часах, на котором сравниваем посты друг с другом.
BENCHMARK_AGE_H = 24
BENCHMARK_TOLERANCE_H = 12


def _log(message):
    print(message, flush=True)


def _parse_dt(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _hashtags(caption):
    return [w.strip(".,!?;:()") for w in (caption or "").split() if w.startswith("#")]


def _topic(entry):
    """Тема поста: явное поле topic, иначе первый хэштег, иначе id без даты."""
    if entry.get("topic"):
        return entry["topic"]
    tags = _hashtags(entry.get("caption"))
    return tags[0].lstrip("#") if tags else "без темы"


def read_history():
    if not HISTORY_PATH.exists():
        return []
    rows = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    _log(f"пропущена битая строка истории: {line[:80]}")
    return rows


def read_queue_entries():
    entries = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
        except json.JSONDecodeError:
            _log(f"пропущен битый файл очереди: {path.name}")
            continue
        entry.setdefault("id", path.stem)
        entries.append(entry)
    return entries


def needs_measurement(entry_type, age_h, last_measured_age_h):
    """Пора ли снова замерять пост данного возраста."""
    if entry_type == "story":
        # Insights у сторис живут ровно 24 часа, дальше данных не будет никогда.
        return age_h < 24
    if age_h > 24 * 30:
        return False
    if last_measured_age_h is None:
        return True
    since_last = age_h - last_measured_age_h
    if age_h < 24:
        return since_last >= 3
    if age_h < 24 * 7:
        return since_last >= 20
    return since_last >= 24 * 6


def collect(force=False):
    """Опросить Instagram и дописать замеры в history.jsonl. Возвращает число замеров."""
    history = read_history()
    last_age = {}
    for row in history:
        rid, age = row.get("id"), row.get("age_hours")
        if rid is not None and age is not None:
            last_age[rid] = max(age, last_age.get(rid, 0))

    now = datetime.now(timezone.utc)
    accounts = {}
    new_rows = []

    for entry in read_queue_entries():
        if entry.get("status") != "done" or not entry.get("media_id") or not entry.get("published_at"):
            continue

        entry_id = entry["id"]
        entry_type = entry.get("type", "photo")
        published_at = _parse_dt(entry["published_at"])
        age_h = round((now - published_at).total_seconds() / 3600, 1)

        if not force and not needs_measurement(entry_type, age_h, last_age.get(entry_id)):
            continue

        account_name = entry["account"]
        if account_name not in accounts:
            accounts[account_name] = Account.from_env(account_name)
        account = accounts[account_name]

        published_msk = published_at.astimezone(MSK)
        row = {
            "id": entry_id,
            "account": account_name,
            "type": entry_type,
            "trial": bool(entry.get("trial", False)),
            "topic": _topic(entry),
            "hook": entry.get("hook") or (entry.get("caption") or "").split("\n")[0][:120],
            "hashtags": _hashtags(entry.get("caption")),
            "published_at": entry["published_at"],
            "publish_hour_msk": published_msk.hour,
            "weekday": published_msk.weekday(),
            "measured_at": now.isoformat(),
            "age_hours": age_h,
        }

        try:
            info = account.media_fields(entry["media_id"])
            row["like_count"] = info.get("like_count")
            row["comments_count"] = info.get("comments_count")
            row["permalink"] = info.get("permalink")
        except IGPublishError as exc:
            row["fields_error"] = str(exc)

        metrics = METRICS_BY_TYPE.get(entry_type, [])
        if metrics:
            try:
                data = account.media_insights(entry["media_id"], metrics)
                for item in data.get("data", []):
                    values = item.get("values", [])
                    row[item.get("name")] = values[0].get("value") if values else None
            except IGPublishError as exc:
                row["insights_error"] = str(exc)

        new_rows.append(row)
        primary = row.get(PRIMARY_METRIC.get(entry_type, "reach"))
        _log(f"[{entry_id}] возраст {age_h} ч, основная метрика: {primary}")

    if new_rows:
        STATS_DIR.mkdir(exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(new_rows)


def write_latest(history):
    latest = {}
    for row in history:
        rid = row.get("id")
        if rid and (rid not in latest or row.get("age_hours", 0) >= latest[rid].get("age_hours", 0)):
            latest[rid] = row
    STATS_DIR.mkdir(exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(latest.values(), key=lambda r: r.get("published_at", "")), f,
                  ensure_ascii=False, indent=2)
    return latest


def benchmark_rows(history):
    """По одному замеру на пост, максимально близкому к BENCHMARK_AGE_H часам.

    Именно эти цифры сравнимы между собой: пост суточной давности и пост
    недельной давности напрямую сравнивать нельзя.
    """
    by_id = defaultdict(list)
    for row in history:
        if row.get("id"):
            by_id[row["id"]].append(row)

    result = []
    for rid, rows in by_id.items():
        candidates = [r for r in rows
                      if abs(r.get("age_hours", 0) - BENCHMARK_AGE_H) <= BENCHMARK_TOLERANCE_H]
        if not candidates:
            continue
        best = min(candidates, key=lambda r: abs(r.get("age_hours", 0) - BENCHMARK_AGE_H))
        value = best.get(PRIMARY_METRIC.get(best.get("type", "photo"), "reach"))
        if isinstance(value, (int, float)):
            best = dict(best, primary=value)
            result.append(best)
    return result


def _slice_table(rows, key_fn, label):
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row["primary"])
    if not groups:
        return ""
    lines = [f"### {label}", "", f"| {label} | постов | медиана | среднее | максимум |",
             "|---|---:|---:|---:|---:|"]
    for key, values in sorted(groups.items(), key=lambda kv: -statistics.median(kv[1])):
        lines.append(f"| {key} | {len(values)} | {round(statistics.median(values))} "
                     f"| {round(statistics.mean(values))} | {max(values)} |")
    lines.append("")
    return "\n".join(lines)


def build_report(history):
    bench = benchmark_rows(history)
    now_msk = datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")

    out = [f"# Отчёт по эффективности публикаций", "",
           f"Собран: {now_msk}. Замеров в истории: {len(history)}. "
           f"Постов со сравнимой цифрой (возраст ~{BENCHMARK_AGE_H} ч): {len(bench)}.", "",
           "Сравниваются просмотры (рилсы) и охват (фото, карусели, сторис) на "
           f"{BENCHMARK_AGE_H}-й час жизни поста, допуск ±{BENCHMARK_TOLERANCE_H} ч. "
           "Медиана надёжнее среднего: один залетевший ролик не перекашивает картину.", ""]

    if not bench:
        out.append("Пока нет ни одного поста с замером в нужном возрасте. "
                   "Отчёт наполнится после суток работы сборщика.")
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text("\n".join(out), encoding="utf-8")
        return

    reels = [r for r in bench if r.get("type") == "reel"]

    def _hour(row):
        hour = row.get("publish_hour_msk")
        return f"{hour:02d}:00" if isinstance(hour, int) else "неизвестно"

    def _weekday(row):
        idx = row.get("weekday")
        return WEEKDAYS[idx] if isinstance(idx, int) and 0 <= idx < 7 else "неизвестно"

    out.append(_slice_table(bench, _hour, "Час публикации (МСК)"))
    out.append(_slice_table(bench, _weekday, "День недели"))
    out.append(_slice_table(bench, lambda r: r.get("topic", "без темы"), "Тема"))
    out.append(_slice_table(bench, lambda r: r.get("type", "?"), "Тип контента"))
    if reels:
        out.append(_slice_table(reels, lambda r: "пробный" if r.get("trial") else "обычный",
                                "Пробный рилс или обычный"))

    ranked = sorted(bench, key=lambda r: -r["primary"])
    for title, subset in (("Лучшие посты", ranked[:7]), ("Худшие посты", ranked[-7:][::-1])):
        out.append(f"### {title}", )
        out.append("")
        out.append("| пост | тип | когда | метрика | хук |")
        out.append("|---|---|---|---:|---|")
        for r in subset:
            when = _parse_dt(r["published_at"]).astimezone(MSK).strftime("%d.%m %H:%M")
            hook = (r.get("hook") or "").replace("|", "/")[:70]
            out.append(f"| {r['id']} | {r.get('type')} | {when} | {r['primary']} | {hook} |")
        out.append("")

    out.append("### Как читать")
    out.append("")
    out.append("- Срез по часу и дню недели показывает окно публикации, а не качество контента: "
               "если в группе меньше 5 постов, вывод пока случайный.")
    out.append("- Срез по теме отвечает на вопрос, о чём стоит снимать чаще.")
    out.append("- Строка «пробный» ниже «обычного» при одинаковых темах чаще всего означает "
               "перезалив уже опубликованного видеоряда: Instagram режет такому охват.")
    out.append("")

    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text("\n".join(out), encoding="utf-8")


def main():
    args = set(sys.argv[1:])
    if "--report" not in args:
        count = collect(force="--all" in args)
        _log(f"новых замеров: {count}")
    history = read_history()
    write_latest(history)
    build_report(history)
    _log(f"отчёт обновлён: {REPORT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
