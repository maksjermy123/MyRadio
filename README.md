# 📻 MyRadio + 📖 План чтения Библии — общий бэкенд

Один FastAPI-сервис на Render (`https://myradio-rrsk.onrender.com`)
обслуживает **два Telegram-бота** и их мини-аппы:

| Бот | Проект | Фронтенд |
|---|---|---|
| `@preoradio_bot` | Радио: посты, «Глубже», теологический поиск | `maksjermy123.github.io/MyRadio` (index.html, deeper.html) |
| `@mybible_reading_bot` | План чтения Библии | `maksjermy123.github.io/bible-reading-bot` |

## Состав репозитория

| Файл | Роль |
|---|---|
| `main.py` | Весь бэкенд: API, оба вебхука, планировщик напоминаний |
| `posts.json`, `links.json`, `result.json` | Живые данные — **бэкенд сам коммитит их сюда** через GitHub API |
| `disc_threads.json` | Кэш привязок «пост канала → тред в группе комментариев» — наполняется автоматически из вебхука (авто-пересылка поста в связанную группу), переживает рестарты |
| `theology_db_1..3.json` | База теологии для поиска (read-only для бэкенда) |
| `ru_synodal.json` | Текст Библии для админ-функций |
| `index.html`, `deeper.html` | Фронтенд радио (Оглавление/Радио/План + «Глубже») |
| `test-link.html` | Ручной тест диплинков |
| `DEPLOY_CHECKLIST.md` | Короткая памятка проверки после каждого деплоя |

## Переменные окружения (Render → Environment)

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` / `BIBLE_BOT_TOKEN` | Токены Telegram-ботов |
| `WEBHOOK_SECRET` | Secret-token обоих вебхуков (проверяется в каждом апдейте) |
| `ADMIN_SECRET` ⭐ | Пароль операционных маршрутов (см. раздел Безопасность) |
| `SUPABASE_URL`, `SUPABASE_KEY` | Таблицы `plan_progress`, `bible_accounts`, `app_state` |
| `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH` | Запись живых данных обратно в этот репозиторий |
| `GITHUB_THREADS_FILE` | Имя файла кэша тредов «Глубже» (по умолчанию `disc_threads.json`) |
| `GROQ_API_KEY`, `COHERE_API_KEY` | LLM-анализ и эмбеддинги |
| `CHANNEL_ID`, `CHANNEL_NAME`, `CHANNEL_LINK` | Канал проекта |
| `BOT_USERNAME`, `BIBLE_BOT_USERNAME` | Имена ботов для диплинков/web_app-ссылок |
| `BIBLE_PAGES_URL` | URL Pages библии (кнопки напоминаний) |
| `BIBLE_ADMIN_USER_IDS` | Telegram-ID владельцев (команды `/stats`, `/users`) |
| `ALLOWED_ORIGINS` | CORS; по умолчанию `*` |
| `INIT_DATA_MAX_AGE_SECONDS` | Максимальный возраст initData (по умолчанию 86400) |

## Безопасность

### 1. Операционные маршруты и ADMIN_SECRET
Маршруты, меняющие Telegram/GitHub или запускающие платный анализ, закрыты
middleware `protect_admin_paths`: без заголовка `x-admin-secret: <ADMIN_SECRET>`
любой запрос к ним возвращает **404 Not Found** (маскировка). Если переменная
не задана — доступ закрыт всегда (fail-closed). Сравнение — через
`hmac.compare_digest` (timing-safe).

Защищены (18): `/analyze/{id}`, `/analyze_range`, `/analyze_all`, `/reindex`,
`/reindex_all`, `/remove_button/{id}`, `/remove_all_buttons`, `/send_button`,
`/cleanup`, `/update_buttons`, `/bulk_deeper`, `/reload_theology`,
`/import_texts`, `/set_webhook`, `/check_webhook`, `/set_webhook_bible`,
`/check_webhook_bible`, `/debug_last`.

Пример:
```bash
curl -H "x-admin-secret: $SECRET" https://myradio-rrsk.onrender.com/check_webhook
```

Открыты сознательно (read-only / публичные): `/`, `/metadata`, `/verify`,
`/links/{post_id}`, `/bible/status` и все `/plan/*`.

### 2. Личные данные пользователей
Мутирующие эндпоинты `/plan/*`, `/state`, `/account/*` проверяют подпись
Telegram initData (HMAC-SHA256 по `WebAppData`, свежесть `auth_date`) через
`_require_bible_user` — подменить чужой user_id невозможно.

Кросс-мини-аппные read-only эндпоинты `/plan/status` и `/account/status`
(виджет «План» на вкладке План радио) — **fail-closed**: принимают подпись
ЛЮБОГО из двух ботов (`_verify_signed_user`: initData библ-аппа → полный
объект/reset_token; initData радио-аппа → публичный срез: стрик, рекорд,
лаг, число прочитанных дней; без валидной подписи → `registered:false`).
Раньше срез отдавался любому, кто знает перебираемый user_id.

### 3. Переход в канал из мини-аппов (iOS same-channel fix)
`index.html` и `deeper.html` открывают посты канала через
`openTelegramLink` + ожидание `visibilitychange` (1500 мс). Если апп открыт
ИЗ ТОГО ЖЕ канала, переход происходит в фоне без видимого сворачивания —
на iOS лечится `tg.close()` (пост уже открыт за аппом), страховка `openLink`
через 800 мс; Android/Desktop — прежний `openLink`. Ссылки на другие
мини-аппы (`/plan`, `/radio`, `/deeper`) идут без фолбэка и без close.

### 4. Прочее
Вебхуки сверяют secret-token Telegram; на мутациях стоят rate-limit'ы slowapi;
серверные SSRF-проверки (`_host_is_public`).

## Подсистема напоминаний (детально)

`AsyncIOScheduler`, cron **каждую минуту** (МСК), `max_instances=1`,
атомарный claim слота в Supabase (`last_reminder_sent_at`) — защита от дублей
и от рассылки при нескольких воркерах. Сбой на одном пользователе не роняет
рассылку остальным.

### Режим 1: «Старт» нажат, план не зарегистрирован
- Любое личное сообщение боту → приветствие + `bible_accounts.started_at`.
- Онбординг-цепочка (`bible_send_onboarding_nudges`): получателям с пустым
  `plan_progress` — максимум **5** сообщений; первое через **2 дня** после
  Стартa, далее каждые **3 дня**; счётчик и метка пишутся атомарно ДО отправки.
  Тексты 👋→📖→🌱→📚→🕊️, кнопка «Выбрать план». При 403 счётчик откатывается.
- Выбор плана переключает человека в режимы 2/3 автоматически.

### Режим 2: план зарегистрирован, ни одного дня не прочитано
- Напоминание приходит ежедневно в выбранное время (пока не прочитан первый день).
- Текст зависит от возраста плана: создан сегодня — молчим; 1 день — «план готов
  и ждёт»; 2–3 дня — мягкое приглашение; дольше — «план ждёт тебя уже N дней».
- После первого прочитанного дня человек переходит в обычный режим.

### Режим 3: обычный (есть прочитанные дни)
- Уже читал сегодня → напоминание НЕ отправляется (фильтр по МСК — сервер живёт
  в UTC, наивный date.today() давал повторные напоминания после полуночи).
- Не читал сегодня:
  - отстаёт по графику → «⚠️ Отстаёшь на N дней…» (N считается тем же алгоритмом,
    что и в мини-аппе — числа всегда совпадают);
  - на графике → «🔥 N дней подряд», при нулевом стрике — «Начни сегодня!».

### Прочее
403 от Telegram → `notify_on=false` для всех планов пользователя (в админке он
пометится 🔕). Время напоминания пользователь меняет в настройках мини-аппа
(`/plan/settings`). Админ `/users` показывает активных, 🔕, осиротевшие строки
и онбординг-аккаунты c возможностью полного удаления.

## Хранение данных

- **Supabase**: `plan_progress` (одна строка на пользователя×план),
  `bible_accounts` (Старт/reset_token/онбординг-счётчики), `app_state`
  (облачные настройки мини-аппа).
- **GitHub Contents API**: живые файлы этого же репозитория — `posts.json`,
  `links.json`. Бэкенд читает их с кэшем и коммитит изменения обратно
  (`github_get/github_put`), поэтому эти файлы руками не редактировать.

## Деплой

Render пересобирает сервис автоматически по пушу в `main`.

1. Локально: `git pull origin main` (подтянуть свежие posts.json!).
2. Заменить нужные файлы (`main.py`, фронтенд) — НЕ трогать posts.json,
   links.json, theology_db_*.json, ru_synodal.json.
3. `git push origin main` → дождаться статуса *Live* в Render.
4. Прогнать `DEPLOY_CHECKLIST.md`: startup-логи, оба бота отвечают,
   `/users` открывается, мини-апп сквозняком.
5. Откат при проблемах: Render → Deploys → Rollback.

## Чек-лист обязательных переменных (Render → Environment)

Без этих переменных отдельные подсистемы молча отключаются (fail-closed):
- `CHANNEL_ID` пуст → `/verify` отвечает «не подписан» **всему радио-аппу** (экран «Доступ закрыт» у всех);
- `BIBLE_BOT_TOKEN` пуст → все мутирующие запросы библ-аппа отклоняются (503);
- `BOT_TOKEN` пуст → радио-виджет «План» отклоняет подписанные запросы → «Сначала открой бота»;
- `SUPABASE_URL`/`SUPABASE_KEY` пусты → напоминания не работают, прогресс не пишется;
- `WEBHOOK_SECRET` пуст → **оба вебхука отклоняют все апдейты** (боты молчат).

Быстрая проверка после деплоя:
```bash
curl https://myradio-rrsk.onrender.com/bible/status
# {"ok":true,"bible_bot":"...","supabase":true,...} — supabase:true обязателен
```

## Административные команды (личка @mybible_reading_bot)

- `/stats` — регистрации, читали сегодня / за 7 дней, напоминания вкл.
- `/users` — интерактивная админ-панель (доступ только BIBLE_ADMIN_USER_IDS).
