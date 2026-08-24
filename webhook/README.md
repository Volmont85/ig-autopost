# Вебхук-приёмник Quick Reply (Railway)

Зачем это существует: Meta отдаёт нажатие кнопки Quick Reply
(`message.quick_reply.payload`) только через вебхук — этого поля физически
нет при обычном `GET .../messages`, которым остальной проект читает
переписку (сверено по официальному Message Reference, 24.08.2026). Этот
сервис — тонкая прослойка: проверяет подпись Meta и пересылает нажатие
кнопки в GitHub через `repository_dispatch`. Вся реальная логика
(что ответить, стейт лид-магнита) остаётся в GitHub Actions, как и
everything else в проекте — сервис на Railway не хранит никакого состояния.

## Переменные окружения (задать в Railway → Variables)

- `META_VERIFY_TOKEN` — любая строка, которую сам придумываешь; вписывается
  туда же, в поле "Verify Token" при подписке вебхука в Meta App Dashboard.
- `META_APP_SECRET` — App Secret приложения в Meta for Developers (тот, что
  используется для обмена/продления токенов — не путать с самим IG-токеном).
- `GH_DISPATCH_TOKEN` — GitHub PAT с правом `repository_dispatch` на
  `Volmont85/ig-autopost` (для fine-grained PAT — доступ к репозиторию +
  permission "Actions: Read and write"). Отдельный от `GH_PAT`, которым
  пользуется `refresh.yml` — этому сервису не нужен доступ к секретам
  репозитория, только право дёрнуть dispatch, так что лучше не переиспользовать
  более широкий токен.
- `GITHUB_REPO` — опционально, по умолчанию `Volmont85/ig-autopost`.
- `DISPATCH_EVENT_TYPE` — опционально, по умолчанию `ig_quick_reply` (должно
  совпадать с `types:` в `.github/workflows/leadmagnet_ig_webhook.yml`).

## Деплой

1. В Railway: New Project → Deploy from GitHub repo → `Volmont85/ig-autopost`,
   **Root Directory: `webhook`** (важно — иначе Railway попытается собрать
   весь монорепозиторий целиком).
2. Проставить переменные окружения выше.
3. Railway сам подхватит `Procfile` (gunicorn) и `requirements.txt`.
4. После первого деплоя Railway выдаст публичный URL вида
   `https://<project>.up.railway.app`.

## Подписка вебхука в Meta App Dashboard

1. Meta for Developers → приложение → Webhooks (или Products → Webhooks,
   если продукт ещё не добавлен).
2. Callback URL: `https://<railway-url>/webhook`.
3. Verify Token: то же значение, что в `META_VERIFY_TOKEN`.
4. Подписаться на поле `messages` для объекта Instagram.
5. Meta сразу дёрнет `GET /webhook` с `hub.challenge` — если Railway отвечает
   корректно (см. `app.py`), подписка подтвердится автоматически.

## Проверка

- `GET https://<railway-url>/` → `ok` (health-check, без проверки подписи).
- Локальный прогон тестов сигнатуры/парсинга — см. историю разработки,
  проверялись сценарии: валидная подпись + quick_reply → форвардится;
  неверная подпись → игнорируется молча (200, но без пересылки); обычный
  текст без кнопки → не форвардится (его читает существующий поллинг,
  дублировать не нужно); отсутствие/неожиданный `Content-Type` в запросе —
  не влияет, JSON парсится из сырого тела напрямую, а не через
  `request.get_json()` (тот раньше молча ронял событие без единой ошибки
  в логе, если Content-Type не был ровно `application/json`).

## Что дальше, не сделано в этом файле

- `.github/workflows/leadmagnet_ig_webhook.yml` — принимающий workflow на
  `repository_dispatch: types: [ig_quick_reply]` — ещё не написан.
- `ig_leadmagnet.py` — сама логика сценария (стейт, что отправлять на каждый
  payload) — отдельная задача, см. `lead-magnet-igtg-setup-claude-code.md`.
