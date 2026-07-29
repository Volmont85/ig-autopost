# ig-autopost

Автопостинг в два Instagram-аккаунта (`personal`, `goszakupki`) через официальный
Instagram Graph API. Публикация идёт по расписанию через GitHub Actions
(каждые 30 минут проверяет `queue.json`), медиафайлы раздаются через GitHub Pages.

Репозиторий публичный: github.com/Volmont85/ig-autopost. Токены и ID лежат
только в GitHub Secrets, в коде и в `queue.json` их быть не должно.

## Как добавить публикацию (основной сценарий)

1. Положить медиафайл в `media/<account>/` (`personal` или `goszakupki`).
   - Фото/карусель: **только JPEG**, соотношение сторон 4:5–1.91:1.
   - Reels/видео в Stories: mp4, 9:16, 5–90 сек, H.264/HEVC + AAC.
   - Фото в Stories: JPEG, обычно 9:16.
2. Добавить запись в `queue.json` (это массив, дописать новый объект):

```json
{
  "id": "уникальный-id",
  "account": "personal",
  "type": "photo",
  "publish_at": "2026-08-01T09:00:00+03:00",
  "caption": "Текст с #хэштегами",
  "media": ["media/personal/foto.jpg"],
  "status": "pending"
}
```

   `type`: `photo` | `carousel` | `reel` | `story`.
   `media`: массив путей относительно корня репозитория (для carousel — 2–10 файлов).
   Для `reel` опционально: `"cover"` (путь к обложке), `"share_to_feed": false`
   (по умолчанию true), `"trial": true` + `"graduation_strategy": "MANUAL"` или
   `"SS_PERFORMANCE"` — публикация как **пробный рилс** (см. ниже).
   Для `story` подписи (`caption`) не поддерживаются самим Instagram — можно
   оставить `""`, она будет проигнорирована.

3. Закоммитить и запушить в `main`. Дальше система сама всё сделает: раз в
   30 минут (или по ручному запуску, см. ниже) GitHub Actions проверит, чья
   `publish_at` уже наступила (сравнение в UTC), проверит доступность файла
   по HTTPS, опубликует и допишет в ту же запись `status: "done"`,
   `published_at`, `media_id` (или `status: "failed"`, `error` при неудаче).

Это можно делать и без терминала — прямо в вебе на github.com: загрузить файл
через Add file → Upload files, отредактировать `queue.json` через встроенный
редактор, закоммитить.

## Опубликовать прямо сейчас, не дожидаясь расписания

```bash
gh workflow run publish.yml --repo Volmont85/ig-autopost
```

(Сеть до api.github.com у пользователя нестабильная — TLS-таймауты частые,
нормально повторить команду 3-5 раз подряд.) Проверить статус запуска:

```bash
gh run list --repo Volmont85/ig-autopost --workflow=publish.yml --limit 5
gh run view <run-id> --repo Volmont85/ig-autopost --log
```

## Пробные рилсы (Trial Reels)

Подтверждено реальной публикацией: параметр `trial_params` со значением
`{"graduation_strategy": "MANUAL"}` работает через API — реализовано в
`Account.publish_reel(..., trial=True, graduation_strategy="MANUAL")`.

Ограничение — не наше, а Instagram: функция должна быть доступна конкретному
аккаунту (на `goszakupki`, новом/пустом аккаунте, API вернул
`Application does not have permission for this action` — вероятно, из-за
малого числа подписчиков или того, что функция там ещё не разблокирована).
На `personal` сработало без проблем. Если тест на новом аккаунте не проходит —
это ожидаемо, не баг.

## Локальный тест (если нужно проверить руками)

```bash
cd ~/Projects/ig-autopost
source .venv/bin/activate
python ig_publish.py personal quota   # проверить квоту публикаций
```

Для реальной публикации из Python — `Account.from_env('personal')` и методы
`publish_photo` / `publish_carousel` / `publish_reel` / `publish_story`.
Токены читаются из переменных окружения `IG_<NAME>_ID` / `IG_<NAME>_TOKEN` —
их нет в этой сессии по умолчанию, пользователь должен экспортировать их сам
в терминале (см. ниже про секреты).

## Важно — секреты и подтверждения

- **Никогда не проси и не выводи в чат значения токенов/ID/App Secret.**
  Если нужно их куда-то передать — только через `gh secret set` (интерактивный
  ввод в терминале пользователя) или напрямую в GitHub Actions env.
- Любая публикация через `run_queue.py` / `workflow_dispatch` — это **реальный
  пост в Instagram**, необратимое действие (даже Stories видны 24 часа всем
  подписчикам). Всегда явно подтверждай с пользователем перед запуском, если
  не попросили действовать без подтверждений.
- `git push` в этот публичный репозиторий — тоже подтверждать, если не давали
  постоянного разрешения действовать автономно.

## Прочее

- `refresh.yml` раз в неделю продлевает токены через `refresh_token.py`
  (`ig_refresh_token`, секрет не нужен) и обновляет GitHub Secrets через
  `gh secret set` (использует секрет `GH_PAT`).
- Квота публикаций проверяется перед стартом (`Account.quota()`), примерно
  100 публикаций в сутки на аккаунт.
- Зависимость только одна — `requests` (см. `requirements.txt`).
