# 📻 Радио Mini App для Telegram

Христианское радио как Telegram Mini App с проверкой подписки на канал.

---

## Файлы проекта

```
index.html      — фронтенд (Mini App плеер)
main.py         — бэкенд (проверка подписки)
requirements.txt — зависимости Python
Procfile        — инструкция запуска для Railway
```

---

## ШАГ 1 — Создать бота в Telegram

1. Открой Telegram, найди **@BotFather**
2. Напиши `/newbot`
3. Придумай имя бота (например: `Моё Радио`)
4. Придумай username бота (должен заканчиваться на `bot`, например: `moy_radio_bot`)
5. BotFather пришлёт **токен** — сохрани его, он выглядит так:
   ```
   1234567890:AAGxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

6. Добавь бота в канал как **администратора**:
   - Зайди в настройки канала @Testovuj
   - Администраторы → Добавить администратора
   - Найди своего бота по username
   - Дай право **"Добавление участников"** (или просто подтверди минимальные права)

---

## ШАГ 2 — Загрузить код на GitHub

1. Зайди на **github.com** (ты уже там зарегистрирован)
2. Нажми **"New repository"** (зелёная кнопка)
3. Название: `radio-miniapp`
4. Выбери **Private** (приватный)
5. Нажми **"Create repository"**
6. На следующей странице нажми **"uploading an existing file"**
7. Перетащи все 4 файла: `index.html`, `main.py`, `requirements.txt`, `Procfile`
8. Нажми **"Commit changes"**

---

## ШАГ 3 — Задеплоить бэкенд на Railway

1. Зайди на **railway.app**
2. Нажми **"Start a New Project"**
3. Выбери **"Deploy from GitHub repo"**
4. Авторизуй Railway через GitHub (кнопка "Authorize Railway")
5. Выбери репозиторий `radio-miniapp`
6. Railway начнёт деплой — подожди 1-2 минуты

### Добавить переменные окружения:
7. В проекте нажми на сервис → вкладка **Variables**
8. Нажми **"New Variable"** и добавь две:
   ```
   BOT_TOKEN = 1234567890:AAGxxxxxxxxxxxx  (твой токен от BotFather)
   CHANNEL_ID = @Testovuj
   ```
9. Railway автоматически перезапустит сервис

### Получить URL бэкенда:
10. Вкладка **Settings** → раздел **Domains** → нажми **"Generate Domain"**
11. Появится URL вида: `radio-miniapp-production.up.railway.app`
12. **Скопируй этот URL** — он понадобится на следующем шаге

---

## ШАГ 4 — Вставить URL бэкенда в index.html

1. Открой файл `index.html` на GitHub
2. Нажми карандашик (Edit)
3. Найди строку:
   ```javascript
   const BACKEND_URL = 'https://ТВОЙ_BACKEND_URL.up.railway.app';
   ```
4. Замени `ТВОЙ_BACKEND_URL.up.railway.app` на твой реальный URL из шага 3
5. Нажми **"Commit changes"**

---

## ШАГ 5 — Задеплоить фронтенд на Vercel

1. Зайди на **vercel.com**
2. Нажми **"Add New Project"**
3. Выбери **"Import Git Repository"** → выбери `radio-miniapp`
4. Нажми **"Deploy"** — всё автоматически
5. После деплоя скопируй URL вида: `radio-miniapp.vercel.app`

---

## ШАГ 6 — Подключить Mini App к боту

1. Вернись к **@BotFather** в Telegram
2. Напиши `/mybots` → выбери своего бота
3. Выбери **"Bot Settings"** → **"Menu Button"**
4. Нажми **"Configure menu button"**
5. Введи URL: `https://radio-miniapp.vercel.app`
6. Введи текст кнопки: `📻 Радио`

---

## ШАГ 7 — Добавить кнопку в канал

1. В Telegram зайди в свой канал @Testovuj
2. Нажми на имя канала вверху → **"Управление каналом"**
3. **"Администраторы"** → найди своего бота
4. Убедись что бот есть среди администраторов

Теперь когда пользователь открывает бота через канал — он видит кнопку меню, кликает и попадает в плеер. Если он не подписан на канал — видит экран с предложением подписаться.

---

## Проверка работы

Открой в браузере: `https://ТВОЙ_URL.up.railway.app/`
Должен увидеть: `{"status": "ok", "service": "Radio Mini App Backend"}`

Если видишь это — бэкенд работает ✓
