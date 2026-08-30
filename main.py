import os
import html as _html
import hmac
import hashlib
import json
import struct
import re
import ssl
import socket
import ipaddress
import time
import base64
import asyncio
import math
import logging
import random
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, parse_qsl, quote
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import httpx
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radio")

app = FastAPI()

# Ограничение частоты запросов — намеренно НЕ глобальное (не хотим случайно
# зацепить радио/контентные эндпоинты, у которых своя, непроверенная в этой
# сессии специфика нагрузки). Применяется точечно только к мутирующим
# эндпоинтам плана чтения (/plan/register, /plan/read и т.д. — см. ниже) —
# защита от одного "шумного" клиента (баг в его коде или намеренная
# нагрузка), а не от обычного использования: лимиты подобраны с большим
# запасом относительно того, сколько запросов реально делает один живой
# пользователь.
# Ключ лимита: на Render приложение стоит за reverse-proxy, поэтому
# request.client.host для ВСЕХ запросов — это IP прокси. С get_remote_address
# все пользователи делили бы ОДИН общий лимит и при умеренной нагрузке
# получали бы 429 впустую. Берём реальный IP из X-Forwarded-For.
def _real_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for")
    # Последний элемент цепочки ставит доверенный прокси Render — его клиент
    # подделать не может. Первый элемент присылает сам клиент и легко
    # рандомизируется для обхода лимитов.
    return fwd.split(",")[-1].strip() if fwd else get_remote_address(request)

limiter = Limiter(key_func=_real_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_methods=["POST", "GET", "HEAD"],
    allow_headers=["*"],
)

# ── Конфигурация ──────────────────────────────────────────────
BOT_TOKEN                 = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET             = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_SECRET               = os.environ.get("ADMIN_SECRET", "")
CHANNEL_ID                = os.environ.get("CHANNEL_ID", "@Chtenie_Preobrazenie")
# Linked чат для комментариев. Раньше был жёстко вшит в код — теперь берётся
# из окружения (тот же id по умолчанию, поведение не меняется), чтобы его
# можно было сменить без правки кода.
try:
    DISCUSSION_CHAT_ID        = int(os.environ.get("DISCUSSION_CHAT_ID", "-1002557846325"))
except ValueError:
    DISCUSSION_CHAT_ID        = -1002557846325
INIT_DATA_MAX_AGE_SECONDS = int(os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400"))
GITHUB_TOKEN              = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO               = os.environ.get("GITHUB_REPO", "maksjermy123/MyRadio")
GITHUB_BRANCH             = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE               = os.environ.get("GITHUB_FILE", "posts.json")
GITHUB_LINKS_FILE         = os.environ.get("GITHUB_LINKS_FILE", "links.json")
# Векторы Cohere живут ОТДЕЛЬНО от posts.json (см. /split_embeddings):
# клиент оглавления качает posts.json целиком, и эмбеддинги ему не нужны —
# раньше они составляли ~2.8 МБ из 3.4 МБ файла, замедляя первый экран
# мини-аппа и каждый поход в GitHub Contents API.
GITHUB_EMBEDDINGS_FILE    = os.environ.get("GITHUB_EMBEDDINGS_FILE", "embeddings.json")
GROQ_API_KEY              = os.environ.get("GROQ_API_KEY", "")
# Имя модели Groq вынесено в env: раз в квартал модель могут снимать —
# смена теперь не требует правки кода.
GROQ_MODEL                = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
COHERE_API_KEY            = os.environ.get("COHERE_API_KEY", "")
BOT_USERNAME              = os.environ.get("BOT_USERNAME", "preoradio_bot")
DEEPER_PAGE_URL           = f"https://maksjermy123.github.io/MyRadio/deeper.html"

# Операционные маршруты меняют Telegram/GitHub или запускают платный анализ.
# Они не должны быть доступны по одному только знанию Render URL.
ADMIN_PATHS = {
    "/analyze", "/analyze_range", "/analyze_all", "/reindex", "/reindex_all",
    "/remove_button", "/remove_all_buttons", "/send_button", "/cleanup",
    "/update_buttons", "/bulk_deeper", "/reload_theology", "/import_texts",
    "/set_webhook", "/check_webhook", "/set_webhook_bible", "/check_webhook_bible",
    "/debug_last", "/split_embeddings",
}

@app.middleware("http")
async def protect_admin_paths(request: Request, call_next):
    path = request.url.path
    protected = path in ADMIN_PATHS or any(
        path.startswith(prefix + "/") for prefix in ADMIN_PATHS
        if prefix in {"/analyze", "/remove_button", "/send_button"}
    )
    if protected:
        supplied = request.headers.get("x-admin-secret", "")
        if not ADMIN_SECRET or not hmac.compare_digest(supplied, ADMIN_SECRET):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)

TELEGRAM_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
COHERE_API    = "https://api.cohere.com/v2/embed"
COHERE_RERANK = "https://api.cohere.com/v2/rerank"

def _groq_headers():
    return {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

def _cohere_headers():
    return {"Authorization": f"Bearer {COHERE_API_KEY}", "Content-Type": "application/json"}

# ── Хэштеги ───────────────────────────────────────────────────
HASHTAG_MAP = {
    "#библия":          "📖 Библия и толкование",
    "#богословие":      "✝️ Богословие",
    "#теодицея":        "😔 Теодицея",
    "#книги":           "📚 Книги и авторы",
    "#жизнь":           "🌱 Христианская жизнь",
    "#молитва":         "🙏 Молитва и духовная жизнь",
    "#духовныйдневник": "📔 Духовный дневник",
    "#проповедь":       "🎤 Проповедь и семинар",
    "#челлендж":        "📅 Челлендж: Лука",
    "#история":         "🏛️ История и церковь",
    "#размышления":     "💬 Размышления и цитаты",
    "#цитата":          "💬 Размышления и цитаты",
    "#юмор":            "😄 Юмор",
    "#праздник":        "🎄 Праздники",
    "#анонс":           "📻 Анонсы канала",
    "#новости":         "📻 Анонсы канала",
}

SECONDARY_TAGS = {
    "#фильм", "#достоевский", "#солженицын", "#клайвльюис",
    "#чехов", "#лесков", "#толстой", "#семинар", "#лука",
}

# #продолжение убран из IGNORE_TAGS — он должен сохраняться в tags для определения первых частей пар
IGNORE_TAGS  = {"#отчтениякпреображению"}
SKIP_AI_TAGS = {"#анонс", "#новости", "#челлендж", "#лука", "#цитата", "#продолжение", "#духовныйдневник", "#юмор"}
# Теги которые отключают только кнопку «Глубже», но не AI-анализ
SKIP_BUTTON_TAGS = {"#без_глубже"}

# ── Карта книг Библии ─────────────────────────────────────────
BOOK_NUM = {
    "Бытие": 1, "Исход": 2, "Левит": 3, "Числа": 4, "Второзаконие": 5,
    "Иисус Навин": 6, "Судьи": 7, "Руфь": 8,
    "1 Царств": 9, "2 Царств": 10, "3 Царств": 11, "4 Царств": 12,
    "1 Паралипоменон": 13, "2 Паралипоменон": 14,
    "Ездра": 15, "Неемия": 16, "Есфирь": 17, "Иов": 18,
    "Псалтирь": 19, "Псалом": 19, "Притчи": 20,
    "Екклесиаст": 21, "Песня Песней": 22,
    "Исаия": 23, "Иеремия": 24, "Плач Иеремии": 25,
    "Иезекииль": 26, "Даниил": 27, "Осия": 28, "Иоиль": 29,
    "Амос": 30, "Авдий": 31, "Иона": 32, "Михей": 33,
    "Наум": 34, "Аввакум": 35, "Софония": 36, "Аггей": 37,
    "Захария": 38, "Малахия": 39,
    "Матфей": 40, "Марк": 41, "Лука": 42, "Иоанн": 43, "Деяния": 44,
    "Иакова": 45,
    "1 Петра": 46, "2 Петра": 47,
    "1 Иоанна": 48, "2 Иоанна": 49, "3 Иоанна": 50,
    "Иуды": 51,
    "Римлянам": 52,
    "1 Коринфянам": 53, "2 Коринфянам": 54,
    "Галатам": 55, "Ефесянам": 56, "Филиппийцам": 57, "Колоссянам": 58,
    "1 Фессалоникийцам": 59, "2 Фессалоникийцам": 60,
    "1 Тимофею": 61, "2 Тимофею": 62, "Титу": 63, "Филимону": 64,
    "Евреям": 65, "Откровение": 66,
}

BOOK_JSON_INDEX = {
    "Бытие": 0, "Исход": 1, "Левит": 2, "Числа": 3, "Второзаконие": 4,
    "Иисус Навин": 5, "Судьи": 6, "Руфь": 7,
    "1 Царств": 8, "2 Царств": 9, "3 Царств": 10, "4 Царств": 11,
    "1 Паралипоменон": 12, "2 Паралипоменон": 13,
    "Ездра": 14, "Неемия": 15, "Есфирь": 16, "Иов": 17,
    "Псалтирь": 18, "Псалом": 18, "Притчи": 19,
    "Екклесиаст": 20, "Песня Песней": 21,
    "Исаия": 22, "Иеремия": 23, "Плач Иеремии": 24,
    "Иезекииль": 25, "Даниил": 26, "Осия": 27, "Иоиль": 28,
    "Амос": 29, "Авдий": 30, "Иона": 31, "Михей": 32,
    "Наум": 33, "Аввакум": 34, "Софония": 35, "Аггей": 36,
    "Захария": 37, "Малахия": 38,
    "Матфей": 39, "Марк": 40, "Лука": 41, "Иоанн": 42, "Деяния": 43,
    "Римлянам": 44,
    "1 Коринфянам": 45, "2 Коринфянам": 46,
    "Галатам": 47, "Ефесянам": 48, "Филиппийцам": 49, "Колоссянам": 50,
    "1 Фессалоникийцам": 51, "2 Фессалоникийцам": 52,
    "1 Тимофею": 53, "2 Тимофею": 54, "Титу": 55, "Филимону": 56,
    "Евреям": 57,
    "Иакова": 58,
    "1 Петра": 59, "2 Петра": 60,
    "1 Иоанна": 61, "2 Иоанна": 62, "3 Иоанна": 63,
    "Иуды": 64,
    "Откровение": 65,
}

BOOK_ALIASES = {
    "Евангелие от Матфея": "Матфей", "Евангелие от Марка": "Марк",
    "Евангелие от Луки": "Лука", "Евангелие от Иоанна": "Иоанн",
    "Деяния апостолов": "Деяния", "Деяния Апостолов": "Деяния",
    "Послание к Римлянам": "Римлянам", "Послание Иакова": "Иакова",
    "1-е Коринфянам": "1 Коринфянам", "2-е Коринфянам": "2 Коринфянам",
    "1-е Петра": "1 Петра", "2-е Петра": "2 Петра",
    "1-е Иоанна": "1 Иоанна", "2-е Иоанна": "2 Иоанна",
    "1-е Тимофею": "1 Тимофею", "2-е Тимофею": "2 Тимофею",
    "1-е Фессалоникийцам": "1 Фессалоникийцам",
    "2-е Фессалоникийцам": "2 Фессалоникийцам",
    "Откровение Иоанна": "Откровение", "Апокалипсис": "Откровение",
    "Быт": "Бытие", "Исх": "Исход", "Лев": "Левит",
    "Чис": "Числа", "Втор": "Второзаконие",
    "Нав": "Иисус Навин", "Суд": "Судьи",
    "1Цар": "1 Царств", "2Цар": "2 Царств",
    "3Цар": "3 Царств", "4Цар": "4 Царств",
    "1Пар": "1 Паралипоменон", "2Пар": "2 Паралипоменон",
    "Езд": "Ездра", "Неем": "Неемия", "Есф": "Есфирь",
    "Пс": "Псалтирь", "Пс.": "Псалтирь", "Псалом": "Псалтирь",
    "Притч": "Притчи", "Прит": "Притчи",
    "Еккл": "Екклесиаст", "Песн": "Песня Песней",
    "Ис": "Исаия", "Иер": "Иеремия", "Плач": "Плач Иеремии",
    "Иез": "Иезекииль", "Дан": "Даниил",
    "Ос": "Осия", "Иоил": "Иоиль", "Ам": "Амос",
    "Авд": "Авдий", "Иона": "Иона", "Мих": "Михей",
    "Наум": "Наум", "Авв": "Аввакум", "Соф": "Софония",
    "Агг": "Аггей", "Зах": "Захария", "Мал": "Малахия",
    "Мф": "Матфей", "Мк": "Марк", "Лк": "Лука",
    "Ин": "Иоанн", "Ин.": "Иоанн",
    "Деян": "Деяния",
    "Рим": "Римлянам",
    "1Кор": "1 Коринфянам", "2Кор": "2 Коринфянам",
    "Гал": "Галатам", "Еф": "Ефесянам", "Флп": "Филиппийцам",
    "Кол": "Колоссянам",
    "1Фес": "1 Фессалоникийцам", "2Фес": "2 Фессалоникийцам",
    "1Тим": "1 Тимофею", "2Тим": "2 Тимофею",
    "Тит": "Титу", "Флм": "Филимону",
    "Евр": "Евреям", "Иак": "Иакова",
    "1Пет": "1 Петра", "2Пет": "2 Петра",
    "1Ин": "1 Иоанна", "2Ин": "2 Иоанна", "3Ин": "3 Иоанна",
    "Иуд": "Иуды", "Откр": "Откровение",
    "Иоанна": "Иоанн", "Матфея": "Матфей",
    "Марка": "Марк", "Луки": "Лука", "Иуды": "Иуды",
    "Послание к Ефесянам": "Ефесянам",
    "Послание к Галатам": "Галатам",
    "Послание к Евреям": "Евреям",
    "Послание к Колоссянам": "Колоссянам",
    "Послание к Филиппийцам": "Филиппийцам",
    "1-е послание к Коринфянам": "1 Коринфянам",
    "2-е послание к Коринфянам": "2 Коринфянам",
    "1-е послание к Фессалоникийцам": "1 Фессалоникийцам",
    "2-е послание к Фессалоникийцам": "2 Фессалоникийцам",
    "1-е послание к Тимофею": "1 Тимофею",
    "2-е послание к Тимофею": "2 Тимофею",
    "1-е послание Петра": "1 Петра", "2-е послание Петра": "2 Петра",
    "1-е послание Иоанна": "1 Иоанна",
}

# ── AI Промпты ────────────────────────────────────────────────
GROQ_PROMPT = """
Ты — вдумчивый богослов и библеист с глубоким знанием Священного Писания.
Ты внимательно читаешь текст христианского поста и помогаешь читателю
войти глубже в ту же мысль через Слово Божье.

━━━ ТЕКСТ ПОСТА ━━━
{post_text}

━━━ ТВОЙ ПРОЦЕСС ━━━

ШАГ 1 — ПОЙМИ ГЛАВНУЮ МЫСЛЬ
Прочитай пост целиком. В одном предложении сформулируй:
- Что именно автор утверждает или исследует? (не тему вообще, а конкретный тезис)
- Какой духовный вывод он делает или к которому ведёт?
Держи эту формулировку перед собой на каждом следующем шаге.

ШАГ 2 — ТЕКСТЫ АВТОРА (role="автора")
Найди тексты Писания которые автор явно цитирует, называет или на которые прямо опирается.
Если автор не даёт точную ссылку — установи её сам по содержанию.
Если автор не использует Писание — этот раздел пуст.

ШАГ 3 — ДОПОЛНИТЕЛЬНЫЕ ТЕКСТЫ (role="дополнительно")
Добавь 1-2 отрывка которые точно углубляют ГЛАВНУЮ МЫСЛЬ из ШАГ 1.
Каждый отрывок должен проходить проверку: «Если убрать этот текст — читатель потеряет важный угол зрения на тезис автора?» Если нет — не бери.
Предпочитай отрывки 3-6 стихов, а не одиночные стихи.

ЖЁСТКИЕ ЗАПРЕТЫ:
- Не бери тексты которые связаны с темой поста лишь по ключевым словам — только по смыслу тезиса
- Не бери тексты вырванные из контекста (например, псалмы покаяния — не про смирение в служении)
- Не используй второканонические книги (Товит, Маккавеи, Премудрость и др.)
- Не выдумывай и не искажай ссылки — только реально существующие стихи
- Не давай более 4 отрывков итого

ШАГ 4 — ВОПРОС ДЛЯ РАЗМЫШЛЕНИЯ
Сформулируй один личный вопрос — как продолжение тезиса автора.
Вопрос должен быть конкретным и вытекать из прочитанного, а не общим по теме.

━━━ ОТВЕТ ━━━

Только валидный JSON, без markdown, без пояснений вне JSON:
{{
  "bible_refs": [
    {{
      "ref": "Книга глава:стих — формат: 'Бытие 3:15' или 'Римлянам 8:18-25'. Никогда не пиши просто главу без стиха. Название в именительном падеже: Иоанн, Матфей, Лука, Римлянам, Псалтирь, 1 Коринфянам.",
      "theme": "одно предложение — почему этот текст точно соответствует тезису автора",
      "role": "автора или дополнительно"
    }}
  ],
  "reflection": "Личный вопрос продолжающий тезис автора"
}}
"""

_github_lock = asyncio.Lock()

# ── TTL-кэш GitHub-файлов ─────────────────────────────────────
# posts.json и links.json читаются с GitHub при каждом вебхуке, клике
# «Глубже» (/links/{id}) и на каждом шаге process_post - это 2 запроса
# к GitHub API (meta + blob) на каждое чтение и 300-800мс латентности.
# Кэш с TTL 45с: пользователь всегда видит данные максимум минутной давности,
# а число обращений к GitHub падает в разы (важно из-за rate limit 5000/ч).
# Инвалидация: github_put() сбрасывает кэш конкретного файла после записи,
# поэтому запись всегда видна сразу же - консистентность внутри процесса полная.
_GH_CACHE = {}          # filename -> {"data":..., "sha":..., "ts": float}
_GH_CACHE_TTL = 45.0    # секунды

def _gh_cache_get(filename: str):
    ent = _GH_CACHE.get(filename)
    if ent and (time.time() - ent["ts"]) < _GH_CACHE_TTL:
        return ent["data"], ent["sha"]
    return None, None

def _gh_cache_put(filename: str, data, sha) -> None:
    _GH_CACHE[filename] = {"data": data, "sha": sha, "ts": time.time()}

def _gh_cache_drop(filename: str) -> None:
    _GH_CACHE.pop(filename, None)


# ── GitHub API ────────────────────────────────────────────────
def _gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


async def github_get(client: httpx.AsyncClient, filename: str):
    # Сначала кэш: экономит 2 запроса к GitHub API и ~0.5с латентности на каждое
    # чтение. Свежесть данных - до _GH_CACHE_TTL секунд, запись всегда инвалидирует.
    data, sha = _gh_cache_get(filename)
    if data is not None:
        return data, sha
    meta_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    r = await client.get(meta_url, headers=_gh_headers(), params={"ref": GITHUB_BRANCH})
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    meta = r.json()
    sha = meta["sha"]
    file_size = meta.get("size", 0)

    if file_size < 900_000 and meta.get("content"):
        try:
            data = json.loads(base64.b64decode(meta["content"]).decode())
            # Кэшируем и быстрый путь — иначе все файлы < 900КБ (а это
            # posts.json и links.json) ходили бы в GitHub на КАЖДОЕ чтение,
            # и TTL-кэш не работал бы вовсе.
            _gh_cache_put(filename, data, sha)
            return data, sha
        except Exception:
            pass

    blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{sha}"
    blob_headers = {**_gh_headers(), "Accept": "application/vnd.github.v3.raw"}
    r2 = await client.get(blob_url, headers=blob_headers, timeout=30.0)
    r2.raise_for_status()
    try:
        data = r2.json()
    except Exception:
        data = json.loads(r2.text)
    _gh_cache_put(filename, data, sha)
    return data, sha


async def github_put(client: httpx.AsyncClient, filename: str, content: dict, sha, message: str):
    encoded = base64.b64encode(
        json.dumps(content, ensure_ascii=False, indent=2).encode()
    ).decode()
    body = {"message": message, "content": encoded, "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    for attempt in range(3):
        r = await client.put(url, headers=_gh_headers(), json=body)
        if r.status_code in (200, 201):
            result = r.json()
            # Запись успешна - обновляем кэш свежими данными и sha,
            # чтобы следующее чтение сразу увидело результат без похода в GitHub.
            _gh_cache_put(filename, content, result.get("content", {}).get("sha", body.get("sha")))
            return result
        if r.status_code == 409 and attempt < 2:
            _gh_cache_drop(filename)  # sha устарел - кэш недоверяем
            _, new_sha = await github_get(client, filename)
            if new_sha:
                body["sha"] = new_sha
            await asyncio.sleep(1)
            continue
        if r.status_code >= 500 and attempt < 2:
            # Временный сбой GitHub: повторяем, чтобы не потерять результат
            # уже потраченного AI-анализа (Groq/Cohere стоят денег).
            await asyncio.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()


# ── Парсинг хэштегов ─────────────────────────────────────────
def extract_hashtags(message: dict) -> list:
    tags = []
    for field in ("entities", "caption_entities"):
        entities = message.get(field) or []
        text_key = "text" if field == "entities" else "caption"
        text = message.get(text_key, "") or ""
        text_utf16 = text.encode("utf-16-le")
        for ent in entities:
            if ent.get("type") == "hashtag":
                offset = ent["offset"] * 2
                length = ent["length"] * 2
                try:
                    tag = text_utf16[offset: offset + length].decode("utf-16-le").lower()
                    tags.append(tag)
                except Exception:
                    pass
    return tags


def hashtags_to_topics(tags: list) -> list:
    topics, seen = [], set()
    for tag in tags:
        if tag in IGNORE_TAGS:
            continue
        cat = HASHTAG_MAP.get(tag)
        if cat and cat not in seen:
            topics.append(cat)
            seen.add(cat)
        elif not cat and tag not in SECONDARY_TAGS and tag not in SKIP_AI_TAGS:
            log.info(f"🆕 Новый тег: {tag} → добавлен в SECONDARY_TAGS")
            SECONDARY_TAGS.add(tag)
    return topics


def get_post_tags(tags: list) -> list:
    return [t for t in tags if t not in IGNORE_TAGS]


def extract_title_and_preview(message: dict) -> tuple:
    text = message.get("text", "") or message.get("caption", "") or ""
    clean = re.sub(r'\s*#\w+', '', text).strip()
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if not lines:
        return "", ""
    return lines[0][:120], (" ".join(lines[:4])[:300] if len(lines) > 1 else "")


def recalc_topics(posts: list) -> list:
    counts = {}
    for p in posts:
        for t in p.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


def should_process_ai(tags: list) -> bool:
    non_skip = [t for t in tags if t not in SKIP_AI_TAGS and t not in IGNORE_TAGS]
    return bool(non_skip)


def extract_tags_from_text(text: str) -> list:
    return [m.lower() for m in re.findall(r'#\w+', text)]


def resolve_post_tags(post: dict) -> list:
    """
    Единая точка получения тегов поста: сперва берём уже посчитанные теги
    (переданные явно, например из analyze_range/analyze_all/analyze/{id}),
    затем — из entities/caption_entities «живого» апдейта от Telegram,
    и только в последнюю очередь — грубое извлечение регуляркой из текста.
    Раньше process_post игнорировал уже переданный tags, из-за чего при
    пакетной переобработке (не через вебхук) не срабатывали проверки
    на "#продолжение" и SKIP_BUTTON_TAGS.
    """
    explicit = post.get("tags")
    if explicit:
        return explicit
    if "entities" in post or "caption_entities" in post:
        return extract_hashtags(post)
    text = post.get("text", "") or post.get("caption", "") or ""
    return extract_tags_from_text(text)


def smart_truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """
    Обрезает текст до limit символов, стараясь не разрывать слово посередине.
    Используется только для отображаемого текста (например, цитат богословов),
    и никак не влияет на то, что отправляется в Cohere Rerank — порог и
    релевантность подбора цитат остаются полностью прежними.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    # обрезаем по последнему пробелу, если это не отбрасывает слишком много текста
    if last_space >= int(limit * 0.6):
        cut = cut[:last_space]
    return cut.rstrip(" ,;:—-") + ellipsis


# ── Парсинг библейских ссылок ─────────────────────────────────
def normalize_book(name: str) -> str:
    return BOOK_ALIASES.get(name, name)


def normalize_book_for_text(name: str) -> str:
    """
    Более терпимая нормализация названия книги — используется ТОЛЬКО при
    получении синодального текста для отображения (fetch_bible_text).
    Ссылки на bible.by (make_translation_links / parse_ref) продолжают
    использовать исходную normalize_book() без каких-либо изменений.

    ИИ иногда выдаёт название книги в косвенном падеже (например,
    "Иеремии" вместо "Иеремия"), из-за чего normalize_book не находит
    точного совпадения. Здесь мы дополнительно пробуем сопоставить книгу
    по началу слова (падежные окончания в русском языке короткие),
    но только если точное совпадение не найдено — то есть для всех уже
    корректно работающих ссылок поведение остаётся тем же самым.
    """
    name = (name or "").strip()
    canonical = normalize_book(name)
    if canonical in BOOK_NUM:
        return canonical

    candidates = list(BOOK_NUM.keys()) + list(BOOK_ALIASES.keys())
    for cut in (1, 2):
        if len(name) <= cut + 3:
            continue
        stem = name[:-cut]
        for known in candidates:
            if known.startswith(stem) and len(stem) >= max(4, len(known) - 3):
                return normalize_book(known)
    return canonical


def parse_ref(ref: str):
    try:
        ref = ref.strip()
        ref = re.split(r' [—–-]{1,2} ', ref)[0].strip()
        m = re.search(r'(\d+:\d+(?:-\d+)?)$', ref)
        if m:
            cv = m.group(1)
            book_ru = normalize_book(ref[:m.start()].strip())
            book_num = BOOK_NUM.get(book_ru)
            if not book_num:
                return None
            chapter, verse_part = cv.split(":", 1)
            verse_start = verse_part.split("-")[0]
            return book_num, int(chapter), int(verse_start)
        m2 = re.search(r'(\d+)$', ref)
        if m2:
            chapter = m2.group(1)
            book_ru = normalize_book(ref[:m2.start()].strip())
            book_num = BOOK_NUM.get(book_ru)
            if book_num:
                return book_num, int(chapter), 1
    except Exception:
        pass
    return None


def make_translation_links(ref: str) -> dict:
    parsed = parse_ref(ref)
    if not parsed:
        return {}
    book_num, chapter, verse = parsed
    return {"📖 Читать все переводы": f"https://bible.by/verse/{book_num}/{chapter}/{verse}/"}


# ── Embeddings (Cohere) ───────────────────────────────────────
def cosine_similarity(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def get_embedding(text: str):
    if not COHERE_API_KEY:
        return None
    try:
        payload = {
            "texts": [text[:2000]],
            "model": "embed-multilingual-v3.0",
            "input_type": "search_document",
            "embedding_types": ["float"],
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(COHERE_API, headers=_cohere_headers(), json=payload)
            r.raise_for_status()
            return r.json()["embeddings"]["float"][0]
    except Exception as e:
        log.error(f"Embedding error: {e}")
        return None


def _extract_emb_map(emb_data) -> dict:
    """Нормализует содержимое embeddings.json в плоскую карту {id: [vec]}.
    Канонический формат — {"embeddings": {"443": [0.12, ...], ...}}; на
    переходный период допускается и просто плоский dict. None/мусор → {}."""
    if isinstance(emb_data, dict):
        inner = emb_data.get("embeddings")
        if isinstance(inner, dict):
            return inner
        return emb_data
    return {}


async def find_related_by_embedding(post_id: int, embedding: list, posts_data: dict, emb_map: dict = None, top_k: int = 2) -> list:
    # Вектор поста берётся из embeddings.json (каноническое место после
    # сплита) либо, для записей, ещё не прошедших сплит, из самого поста —
    # двойная совместимость на переходный период.
    scores = []
    for post in posts_data.get("posts", []):
        if post["id"] == post_id:
            continue
        emb = post.get("embedding")
        if not emb and emb_map:
            emb = emb_map.get(str(post["id"]))
        if not emb:
            continue
        sim = cosine_similarity(embedding, emb)
        scores.append((post["id"], sim))
    scores.sort(key=lambda x: x[1], reverse=True)
    result = [pid for pid, score in scores[:top_k] if score > 0.3]
    log.info(f"Vector search for {post_id}: top={[(pid, round(s,3)) for pid,s in scores[:3]]}")
    return result


async def update_related_bidirectional(post_id: int, related_ids: list, links_data: dict) -> None:
    for rel_id in related_ids:
        rel_key = str(rel_id)
        if rel_key not in links_data:
            continue
        current = links_data[rel_key].get("related_posts", [])
        if post_id not in current:
            links_data[rel_key]["related_posts"] = ([post_id] + current)[:2]


# ── Богословская база ─────────────────────────────────────────
_theology_cache = None
# Инвертированный индекс (слово → id записей) строится один раз при первой
# загрузке базы и служит ЛОКАЛЬНЫМ предфильтром перед Cohere Rerank.
# Раньше в Rerank уходили ВСЕ ~8038 записей (~3.2 млн символов) — это
# превышает лимит модели (1000 документов), Cohere возвращал ошибку,
# она глоталась, и блок «Богословы о теме» молча был всегда пустым.
_theology_index = None

_RU_STOPWORDS = {
    "и", "а", "но", "да", "же", "то", "бы", "ли", "как", "что", "это", "этот",
    "тот", "так", "такой", "такая", "такое", "для", "не", "ни", "на", "по", "о", "об", "от",
    "до", "из", "за", "под", "над", "у", "к", "ко", "с", "со", "в", "во",
    "при", "про", "или", "либо", "если", "чтобы", "который", "которая",
    "которое", "которые", "свой", "своей", "своё", "его", "её", "их", "мой",
    "моя", "моё", "мы", "вы", "они", "он", "она", "оно", "я", "ты", "есть",
    "быть", "был", "была", "было", "были", "будет", "будут", "зачем", "почему",
    "когда", "где", "куда", "какой", "какая", "какие", "весь", "вся", "всё",
    "the", "a", "an", "of", "to", "in", "is", "and", "or", "on", "for", "it",
}

# Длина корзины морфологии: токен приводится к 5-символьной основе-корзине
# («молитва/молитве/молитву» → «молит»), поэтому флексии русского языка
# ловятся без полноценного стеммера. Мелочь вроде «любовь/любви»
# (чередование ов/в) может не совпасть — это допустимо: предфильтр —
# сеть ПОЛНОТЫ из ~10-20 токенов запроса, а точность добирает rerank.
_THEO_STEM_LEN = 5


def _theology_stem(raw: str) -> str:
    return raw[:_THEO_STEM_LEN]


def _theology_tokens(text: str) -> set:
    """Корзины-основы запроса: нормализуем регистр, режем по не-буквам,
    отбрасываем стоп-слова и слишком короткие обрывки."""
    tokens = set()
    for raw in re.findall(r"[а-яёa-z0-9]+", (text or "").lower()):
        if len(raw) < 3 or raw in _RU_STOPWORDS:
            continue
        tokens.add(_theology_stem(raw))
    return tokens


def _build_theology_index(db: list) -> dict:
    idx = defaultdict(set)
    for i, rec in enumerate(db):
        for tok in _theology_tokens(rec.get("text", "")):
            idx[tok].add(i)
    return dict(idx)


async def get_theology_db() -> list:
    global _theology_cache, _theology_index
    if _theology_cache is not None:
        return _theology_cache
    all_records = []
    async with httpx.AsyncClient(timeout=30) as client:
        for part in range(1, 4):
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/theology_db_{part}.json"
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    records = r.json()
                    all_records.extend(records)
                    log.info(f"Theology DB part {part}: {len(records)} записей")
            except Exception as e:
                log.error(f"Theology DB part {part} error: {e}")
    _theology_cache = all_records
    _theology_index = _build_theology_index(all_records)
    log.info(f"Theology DB loaded: {len(all_records)} записей, индекс: {len(_theology_index)} токенов")
    return all_records


async def find_theology_quotes(query: str, top_n: int = 3) -> list:
    # Порог релевантности (top_score < 0.92, threshold = top_score * 0.85) и
    # формат результата НЕ изменились. Изменился ОБЪЁМ, уходящий в Cohere:
    # раньше туда шли все ~8038 записей (~3.2 млн символов) — больше лимита
    # rerank-multilingual-v3.0 (1000 документов). Cohere отвечал ошибкой,
    # исключение глоталось ниже, и блок «Богословы о теме» всегда был пуст.
    # Теперь двухступенчатый отбор: локальный предфильтр по инвертированному
    # индексу (топ-120 кандидатов) → Cohere только по ним.
    # Принцип релевантности: цитата появляется ТОЛЬКО пройдя все ворота
    # точности (лексическая связность с темой → rerank ≥ 0.92 → порог
    # 0.85 от top_score → дедуп авторов); иначе блок пуст — «лучше ничего,
    # чем нерелевантное».
    if not COHERE_API_KEY:
        return []
    try:
        db = await get_theology_db()
        if not db:
            return []
        qtokens = _theology_tokens(query)
        # Принцип «лучше ничего, чем нерелевантное»: без содержательных
        # токенов запрос не задаёт тему — произвольный срез базы в Cohere
        # НЕ отправляем. Раньше здесь уходило db[:1000] по порядку файла,
        # и неотфильтрованная запись могла случайно пройти порог 0.92.
        if not qtokens or not _theology_index:
            log.info("Theology: запрос не содержит содержательных токенов — цитаты не подбираем")
            return []
        scored = defaultdict(int)
        for tok in qtokens:
            for i in _theology_index.get(tok, ()):
                scored[i] += 1
        if not scored:
            log.info("Theology: предфильтр не нашёл совпадений по теме — Cohere не вызываем")
            return []
        # Связность-монотонная воронка: записи с БОЛЬШИМ числом совпавших
        # корзин запроса всегда занимают места в топ-120 раньше записей
        # с одиночным совпадением; при равенстве — устойчивый порядок.
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        candidates = [i for i, _ in ranked[:120]]
        sample = [db[i] for i in candidates]
        log.info(f"Theology: предфильтр отобрал {len(sample)}/{len(db)} записей для rerank")
        documents = [rec["text"][:400] for rec in sample]
        payload = {
            "model": "rerank-multilingual-v3.0",
            "query": query[:1000],
            "documents": documents,
            "top_n": top_n,
            "return_documents": True,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(COHERE_RERANK, headers=_cohere_headers(), json=payload)
            r.raise_for_status()
            results = r.json().get("results", [])
        if not results:
            return []
        top_score = results[0]["relevance_score"]
        if top_score < 0.92:
            log.info(f"Theology: top_score={top_score:.3f} < 0.92 — цитаты не релевантны, пропускаем")
            return []
        threshold = top_score * 0.85
        quotes = []
        seen_authors = set()
        for res in results:
            score = res["relevance_score"]
            if score < threshold:
                break
            rec = sample[res["index"]]
            if rec["author"] in seen_authors:
                continue
            seen_authors.add(rec["author"])
            quotes.append({
                "author": rec["author"],
                "title": rec.get("title", ""),
                # Раньше жёсткий срез [:500] более чем в 60% случаев обрывал
                # цитату прямо посреди слова. Порог/скор при этом не трогаем —
                # обрезаем только уже ОТОБРАННЫЙ текст для показа пользователю.
                "text": smart_truncate(rec["text"], 500),
                "score": round(score, 3)
            })
            if len(quotes) >= top_n:
                break
        log.info(f"Theology: {len(quotes)} quotes, top={top_score:.3f}, threshold={threshold:.3f}")
        return quotes
    except Exception as e:
        log.error(f"Theology search error: {e}")
        return []


# ── Синодальный перевод ───────────────────────────────────────
_bible_cache = None


async def get_bible_db(client: httpx.AsyncClient):
    global _bible_cache
    if _bible_cache is not None:
        return _bible_cache
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/ru_synodal.json"
    for attempt in range(2):
        try:
            r = await client.get(url, timeout=30)
            if r.status_code == 200:
                _bible_cache = r.json()
                log.info(f"Bible DB loaded: {len(_bible_cache)} books")
                return _bible_cache
        except Exception as e:
            log.error(f"Bible DB load error (attempt {attempt + 1}/2): {e}")
        if attempt == 0:
            await asyncio.sleep(1)
    return None


async def _get_bible_db_cached():
    """
    Отдаёт кэш Библии, при необходимости загружая его один раз.
    Если книга уже в кэше — клиент httpx вообще не создаётся
    (раньше он создавался на каждый вызов fetch_bible_text, даже
    когда кэш уже был заполнен).
    """
    global _bible_cache
    if _bible_cache is None:
        async with httpx.AsyncClient(timeout=15) as client:
            await get_bible_db(client)
    return _bible_cache


def _extract_verses(bible: list, book_idx: int, chapter: int, verse_start: int, verse_end: int) -> str:
    if chapter < 0 or book_idx is None or book_idx >= len(bible):
        return ""
    chapters = bible[book_idx].get("chapters", [])
    if chapter >= len(chapters):
        return ""
    selected = chapters[chapter][verse_start:verse_end]
    if not selected:
        return ""
    return " ".join(f"{verse_start + 1 + i} {v}" for i, v in enumerate(selected))


async def fetch_bible_text(ref: str) -> str:
    try:
        ref = re.split(r' [—–-]{1,2} ', ref.strip())[0].strip()
        m = re.search(r'(\d+:\d+(?:-\d+)?)$', ref)
        if not m:
            m2 = re.search(r'(\d+)$', ref)
            if not m2:
                return ""
            chapter = int(m2.group(1)) - 1
            book_ru = normalize_book_for_text(ref[:m2.start()].strip())
            book_idx = BOOK_JSON_INDEX.get(book_ru)
            if book_idx is None:
                return ""
            bible = await _get_bible_db_cached()
            if not bible:
                return ""
            chapters = bible[book_idx].get("chapters", [])
            if chapter >= len(chapters):
                return ""
            verses = chapters[chapter][:5]
            return " ".join(f"{i+1} {v}" for i, v in enumerate(verses))

        cv = m.group(1)
        book_ru = normalize_book_for_text(ref[:m.start()].strip())
        book_idx = BOOK_JSON_INDEX.get(book_ru)
        if book_idx is None:
            return ""
        chapter_str, verse_str = cv.split(":", 1)
        chapter = int(chapter_str) - 1
        if "-" in verse_str:
            v_start, v_end = verse_str.split("-", 1)
            verse_start = int(v_start) - 1
            verse_end = int(v_end)
        else:
            verse_start = int(verse_str) - 1
            verse_end = verse_start + 1

        bible = await _get_bible_db_cached()
        if not bible:
            return ""

        text = _extract_verses(bible, book_idx, chapter, verse_start, verse_end)

        # Синодальный перевод Псалтири пронумерован по Септуагинте и начиная
        # с 10-го псалма на 1 отстаёт от привычной ИИ масоретской/западной
        # нумерации (напр. англ. "Psalm 119:105" — это Пс.118:105 в Синодальном).
        # Если по указанному номеру ничего не нашли — пробуем на 1 псалом раньше.
        # На генерацию ссылок bible.by (make_translation_links) это не влияет.
        if not text and book_ru in ("Псалтирь", "Псалом") and chapter >= 9:
            text = _extract_verses(bible, book_idx, chapter - 1, verse_start, verse_end)

        return text
    except Exception as e:
        log.error(f"Bible fetch error for '{ref}': {e}")
        return ""


# ── Groq: анализ поста ────────────────────────────────────────
def _strip_json_fence(text: str) -> str:
    """
    Корректно убирает markdown-обрамление ```json ... ``` вокруг ответа модели.
    Раньше использовался text.lstrip("```json") — это удаляет с начала строки
    ЛЮБЫЕ символы из набора {`, j, s, o, n}, а не префикс "```json" целиком,
    что могло случайно обрезать валидные символы в начале настоящего JSON.
    """
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


async def analyze_post(post_text: str, topics: list):
    prompt = GROQ_PROMPT.format(post_text=post_text)
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(GROQ_URL, headers=_groq_headers(), json=payload)
        if r.status_code == 429:
            wait = 65 if attempt == 0 else 120
            log.warning(f"Groq 429 rate limit, attempt {attempt+1}/3, ждём {wait}s...")
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        text = _strip_json_fence(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Модель иногда ломает JSON (обрезка, лишний текст). Одна повторная
            # попытка дешевле, чем потерять весь вызов Groq.
            log.warning("Groq вернул невалидный JSON, повторная попытка...")
            continue
    raise Exception("Groq rate limit: все 3 попытки исчерпаны")


# ── Кнопка «Глубже» ───────────────────────────────────────────
async def send_deeper_button(post_id: int):
    """Отправляет кнопку «Глубже» в тред поста в чате комментариев."""
    bot_username = BOT_USERNAME.lstrip("@")
    miniapp_url = f"https://t.me/{bot_username}/deeper?startapp={post_id}"
    keyboard = {"inline_keyboard": [[
        {"text": "📚 Глубже — библейский контекст поста", "url": miniapp_url}
    ]]}

    async with httpx.AsyncClient(timeout=15) as client:

        # ── Шаг 1: быстрый путь — getDiscussionMessage ──────────
        disc_msg_id = None
        disc = await client.get(
            f"{TELEGRAM_API}/getDiscussionMessage",
            params={"chat_id": CHANNEL_ID, "message_id": post_id}
        )
        disc_data = disc.json()
        if disc_data.get("ok"):
            disc_msg_id = disc_data["result"]["message"]["message_id"]
            log.info(f"📨 Пост {post_id} → getDiscussionMessage OK, disc_msg_id={disc_msg_id}")

        # ── Шаг 2: резервный путь — getChat(DISCUSSION_CHAT_ID) ──
        if disc_msg_id is None:
            log.info(f"📨 Пост {post_id} → getDiscussionMessage 404, пробуем getChat...")
            gc = await client.get(
                f"{TELEGRAM_API}/getChat",
                params={"chat_id": DISCUSSION_CHAT_ID}
            )
            gc_data = gc.json()
            if gc_data.get("ok"):
                pinned = gc_data["result"].get("pinned_message", {})
                if pinned.get("forward_from_message_id") == post_id:
                    disc_msg_id = pinned["message_id"]
                    log.info(f"📨 Пост {post_id} → найден через pinned_message, disc_msg_id={disc_msg_id}")
                else:
                    found = False
                    for attempt in range(6):
                        wait = 10 * (attempt + 1)
                        log.warning(
                            f"⏳ pinned={pinned.get('forward_from_message_id')} ≠ {post_id}, "
                            f"ждём {wait}s (попытка {attempt+1}/6)..."
                        )
                        await asyncio.sleep(wait)
                        gc2 = await client.get(f"{TELEGRAM_API}/getChat", params={"chat_id": DISCUSSION_CHAT_ID})
                        gc2_data = gc2.json()
                        if gc2_data.get("ok"):
                            pinned2 = gc2_data["result"].get("pinned_message", {})
                            if pinned2.get("forward_from_message_id") == post_id:
                                disc_msg_id = pinned2["message_id"]
                                log.info(f"📨 Пост {post_id} → найден через pinned_message (retry), disc_msg_id={disc_msg_id}")
                                found = True
                                break
                    if not found:
                        log.error(f"❌ Не удалось найти disc_msg_id для поста {post_id} — добавьте вручную через /send_button")
                        return
            else:
                log.error(f"❌ getChat failed: {gc_data.get('description')}")
                return

        # ── Шаг 3: отправляем кнопку в тред ──────────────────────
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": DISCUSSION_CHAT_ID,
                "reply_to_message_id": disc_msg_id,
                "text": "📚 Библейский контекст и связи этого поста",
                "reply_markup": keyboard,
                "disable_notification": True
            }
        )
        result = r.json()
        if result.get("ok"):
            log.info(f"✅ Кнопка «Глубже» → тред поста {post_id} (disc_id={disc_msg_id})")
        else:
            desc = result.get("description", "")
            log.error(f"❌ sendMessage для поста {post_id}: {desc}")


def should_skip_button(post_tags: list) -> bool:
    """Единая проверка: нужно ли пропустить постановку кнопки «Глубже»."""
    tags = post_tags or []
    if "#продолжение" in tags:
        return True
    if set(tags) & SKIP_BUTTON_TAGS:
        return True
    return False


# ── Обработка поста AI ────────────────────────────────────────
async def process_post(post: dict, resend_button: bool = True):
    """
    resend_button=False позволяет обновить данные поста (bible_refs, цитаты,
    reflection, related_posts в links.json) БЕЗ отправки нового сообщения
    с кнопкой «Глубже» в тред — используется при переиндексации поста,
    у которого кнопка уже стоит на своём месте и трогать её не нужно:
    сама кнопка ведёт на deeper.html?post_id=..., который всегда подтягивает
    свежие данные из /links/{post_id}, так что повторно постить её незачем.
    """
    post_id = post.get("message_id") or post.get("id")
    text = post.get("text", "") or post.get("caption", "")
    if not text or not post_id:
        return

    tags = resolve_post_tags(post)
    topics = hashtags_to_topics(tags) if tags else post.get("topics", [])
    if not topics:
        log.info(f"process_post {post_id}: нет тем — пропускаем")
        return

    # Склейка с предыдущим постом если он помечен #продолжение
    prev_id = post_id - 1
    async with httpx.AsyncClient(timeout=15) as _cl:
        _posts_check, _ = await github_get(_cl, GITHUB_FILE)
    if _posts_check:
        _prev = next((p for p in _posts_check.get("posts", []) if p["id"] == prev_id), None)
        if _prev:
            _prev_text = _prev.get("text", "") or _prev.get("preview", "")
            _is_cont = (
                "Продолжение ниже" in _prev_text
                or "продолжение ниже" in _prev_text
            )
            if _is_cont and _prev_text:
                text = (_prev_text.rstrip() + "\n\n" + text)[:6000]
                log.info(f"📎 Пост {post_id} склеен с {prev_id} (суммарно {len(text)} симв.)")

    # Юмор: без Groq/Cohere
    if "😄 Юмор" in topics:
        humor_result = {
            "post_id": post_id, "topics": topics,
            "related_posts": [], "bible_refs": [], "quotes": [],
            "reflection": "", "humor": True,
            "humor_text": "«Серьёзность человека, обладающего чувством юмора, намного серьёзнее серьёзности серьёзного человека»",
            "humor_author": "А. П. Чехов"
        }
        async with _github_lock:
            async with httpx.AsyncClient(timeout=20) as client:
                links_data, links_sha = await github_get(client, GITHUB_LINKS_FILE)
                if links_data is None:
                    links_data = {}
                links_data[str(post_id)] = humor_result
                await github_put(client, GITHUB_LINKS_FILE, links_data, links_sha, f"Humor post {post_id}")
        if resend_button:
            await send_deeper_button(post_id)
        else:
            log.info(f"⏭ Пост {post_id} — resend_button=False, кнопку не трогаем")
        log.info(f"😄 Humor post {post_id} saved.")
        return

    # Нормальный пост.
    # Шаг 1: короткий lock ТОЛЬКО на чтение posts.json + embeddings.json.
    # Раньше lock держался на всё время платного вызова Cohere (15+ сек),
    # из-за чего параллельный пост канала ждал в очереди и рисковал
    # таймаутами/409 GitHub. Теперь: прочитали → отпустили → считаем.
    async with _github_lock:
        async with httpx.AsyncClient(timeout=20) as client:
            posts_data, _ = await github_get(client, GITHUB_FILE)
            emb_data, _ = await github_get(client, GITHUB_EMBEDDINGS_FILE)
    if posts_data is None:
        posts_data = {"posts": [], "topics": [], "total": 0, "updated": ""}
    existing = next((p for p in posts_data["posts"] if p["id"] == post_id), None)
    emb_map = _extract_emb_map(emb_data)
    # Вектор: сначала legacy (в самом посте), потом embeddings.json.
    embedding = (existing.get("embedding") if existing else None) or emb_map.get(str(post_id))

    # Шаг 2: Cohere embed — ВНЕ лока (медленный платный вызов).
    if not embedding:
        embedding = await get_embedding(text)

    # Шаг 3: новый вектор сохраняем ТОЛЬКО в embeddings.json — posts.json
    # качает клиент оглавления, и векторы там ему не нужны (после сплита).
    if embedding and emb_map.get(str(post_id)) != embedding:
        async with _github_lock:
            async with httpx.AsyncClient(timeout=20) as client:
                fresh_emb, fresh_sha = await github_get(client, GITHUB_EMBEDDINGS_FILE)
                fresh_map = _extract_emb_map(fresh_emb)
                fresh_map[str(post_id)] = embedding
                await github_put(
                    client, GITHUB_EMBEDDINGS_FILE,
                    {"embeddings": fresh_map,
                     "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")},
                    fresh_sha, f"embedding for post {post_id}")

    try:
        related_task = (find_related_by_embedding(post_id, embedding, posts_data, emb_map)
                        if embedding else asyncio.sleep(0))
        result, related = await asyncio.gather(
            analyze_post(text, topics), related_task
        )
        if not embedding:
            related = []
        theology_query = result.get("reflection") or text[:500]
        theology_query = f"{theology_query}\n\n{text[:500]}"
        theology_quotes = await find_theology_quotes(theology_query)
    except Exception as e:
        log.error(f"AI error for post {post_id}: {e}")
        return

    result["post_id"] = post_id
    result["topics"] = topics
    result["quotes"] = theology_quotes
    result["related_posts"] = related if related else []
    result["humor"] = False

    for bible_ref in result.get("bible_refs", []):
        ref_str = bible_ref.get("ref", "")
        bible_ref["text_syn"] = await fetch_bible_text(ref_str)
        bible_ref["translations"] = make_translation_links(ref_str)

    async with _github_lock:
        async with httpx.AsyncClient(timeout=20) as client:
            links_data, links_sha = await github_get(client, GITHUB_LINKS_FILE)
            if links_data is None:
                links_data = {}
            links_data[str(post_id)] = result
            if result["related_posts"]:
                await update_related_bidirectional(post_id, result["related_posts"], links_data)
            await github_put(client, GITHUB_LINKS_FILE, links_data, links_sha,
                             f"links for post {post_id}")

    log.info(f"✅ Post {post_id} processed. Related: {result['related_posts']}")

    if not resend_button:
        log.info(f"⏭ Пост {post_id} — resend_button=False, данные обновлены, кнопку не трогаем")
        return

    # Не отправляем кнопку для первых частей пар (#продолжение) и для #без_глубже
    if should_skip_button(tags):
        skipped_by = set(tags) & (SKIP_BUTTON_TAGS | {"#продолжение"})
        log.info(f"⏭ Пост {post_id} — тег {skipped_by}, кнопка «Глубже» отключена")
    else:
        await send_deeper_button(post_id)


# ── posts.json: добавление/обновление ────────────────────────
async def upsert_post_to_github(message: dict, is_edit: bool = False) -> str:
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN не задан")
        return "error_no_github_token"

    msg_id = message.get("message_id") or message.get("id")
    tags   = extract_hashtags(message)
    topics = hashtags_to_topics(tags)
    log.info(f"{'Правка' if is_edit else 'Пост'} {msg_id} | теги: {tags} | темы: {topics}")

    if not topics:
        log.info(f"Пост {msg_id}: нет известных тегов — пропускаем")
        return "no_topics"

    title, preview = extract_title_and_preview(message)
    text_full  = (message.get("text", "") or message.get("caption", "") or "")[:3000]
    date_raw   = message.get("date", 0)
    date_str   = datetime.fromtimestamp(date_raw, tz=timezone.utc).strftime("%Y-%m-%d")
    chan_user   = (message.get("chat") or {}).get("username") or CHANNEL_ID.lstrip("@")
    all_tags = get_post_tags(tags)
    new_post = {
        "id": msg_id, "date": date_str, "title": title,
        "preview": preview, "url": f"https://t.me/{chan_user}/{msg_id}",
        "topics": topics, "tags": all_tags, "text": text_full,
    }

    async with _github_lock:
        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(3):
                try:
                    posts_data, sha = await github_get(client, GITHUB_FILE)
                    if posts_data is None:
                        posts_data = {"posts": [], "topics": [], "total": 0,
                                      "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
                except Exception as e:
                    log.error(f"Чтение GitHub (попытка {attempt+1}): {e}")
                    await asyncio.sleep(1)
                    continue

                posts = posts_data.get("posts", [])
                existing_ids = {p["id"] for p in posts}

                if msg_id in existing_ids:
                    if is_edit:
                        old = next((p for p in posts if p["id"] == msg_id), {})
                        if old.get("embedding"):
                            new_post["embedding"] = old["embedding"]
                        posts_data["posts"] = [new_post if p["id"] == msg_id else p for p in posts]
                        action = "updated"
                    else:
                        log.info(f"Пост {msg_id} уже есть — пропускаем")
                        return "skipped_duplicate"
                else:
                    posts_data["posts"].append(new_post)
                    action = "added"

                posts_data["posts"].sort(key=lambda p: p["id"], reverse=True)
                posts_data["topics"]  = recalc_topics(posts_data["posts"])
                posts_data["total"]   = len(posts_data["posts"])
                posts_data["updated"] = date_str

                try:
                    await github_put(client, GITHUB_FILE, posts_data, sha,
                                     f"auto: posts [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]")
                    log.info(f"Пост {msg_id} — {action} ✓")
                    return action
                except Exception as e:
                    log.error(f"Запись GitHub (попытка {attempt+1}): {e}")
                    await asyncio.sleep(1)

    log.error(f"Пост {msg_id}: все попытки провалились")
    return "error_all_retries_failed"


# ── Проверка подписки ─────────────────────────────────────────
class VerifyRequest(BaseModel):
    init_data: str


def verify_telegram_init_data(init_data, bot_token, *, max_age_seconds=86400):
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    sk  = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    ch  = hmac.new(sk, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ch, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", 0))
    except ValueError:
        return None
    now = int(time.time())
    if max_age_seconds > 0 and (auth_date > now + 60 or now - auth_date > max_age_seconds):
        return None
    user_data = parsed.get("user")
    if not user_data:
        return None
    try:
        # parse_qsl уже один раз декодирует percent-encoding, поэтому
        # повторный unquote() здесь был лишним и мог испортить JSON,
        # если в данных пользователя случайно встречалась подстрока
        # вида "%xx" (например, в username).
        user = json.loads(user_data)
    except json.JSONDecodeError:
        return None
    return {"user": user, "auth_date": auth_date}


async def _host_is_public(host: str) -> bool:
    # getaddrinfo блокирует event loop на сотни мс (при проблемах DNS - секунды).
    # Уносим в отдельный поток, чтобы /metadata не тормозил остальные запросы.
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return bool(infos)


async def fetch_icy_metadata(stream_url: str):
    try:
        from urllib.parse import urlparse
        p = urlparse(stream_url)
        if p.scheme not in {"http", "https"} or not p.hostname:
            return None
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        if not await _host_is_public(host):
            return None
        ssl_ctx = ssl.create_default_context() if p.scheme == "https" else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx,
                                    server_hostname=host if ssl_ctx else None), timeout=5.0)
        writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nIcy-MetaData: 1\r\n"
                     f"User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        meta_interval = 0
        while True:
            line = (await asyncio.wait_for(reader.readline(), timeout=5.0)
                    ).decode("utf-8", errors="ignore").strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "icy-metaint":
                    meta_interval = int(v.strip())
        if meta_interval <= 0:
            writer.close(); return None
        audio = b""
        while len(audio) < meta_interval:
            chunk = await asyncio.wait_for(reader.read(meta_interval - len(audio)), timeout=5.0)
            if not chunk: break
            audio += chunk
        msb = await asyncio.wait_for(reader.read(1), timeout=3.0)
        if not msb:
            writer.close(); return None
        msize = struct.unpack("B", msb)[0] * 16
        if not msize:
            writer.close(); return None
        meta = b""
        while len(meta) < msize:
            chunk = await asyncio.wait_for(reader.read(msize - len(meta)), timeout=3.0)
            if not chunk: break
            meta += chunk
        writer.close()
        m = re.search(r"StreamTitle='([^']*)'",
                      meta.decode("utf-8", errors="ignore").rstrip("\x00"))
        if m:
            return m.group(1).strip() or None
    except Exception:
        return None


# ── Личные сообщения бота ─────────────────────────────────────
async def handle_user_message(message: dict):
    chat_id = message.get("chat", {}).get("id")
    text    = message.get("text", "")
    if not chat_id:
        return
    post_id = None
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            try:
                post_id = int(parts[1])
            except ValueError:
                pass
    deeper_url = "https://maksjermy123.github.io/MyRadio/deeper.html"
    if post_id:
        deeper_url += f"?post_id={post_id}"
    bot_username = BOT_USERNAME.lstrip("@")
    # web_app кнопки не работают в личных сообщениях — используем url
    payload = {
        "chat_id": chat_id,
        "text": "📚 Нажми чтобы открыть материалы поста:",
        "reply_markup": {"inline_keyboard": [[
            {"text": "📚 Глубже", "url": f"https://t.me/{bot_username}/deeper?startapp={post_id or ''}"}
        ]]}
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if r.json().get("ok"):
            log.info(f"✅ web_app кнопка → пользователю {chat_id}, пост {post_id}")
        else:
            log.warning(f"sendMessage error: {r.json().get('description')}")


# ═══════════════════════════════════════════════════
# ЭНДПОИНТЫ
# ═══════════════════════════════════════════════════

@app.head("/")
async def root_head():
    return Response(status_code=200)

@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio + Deeper Mini App Backend"}


@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    title = await fetch_icy_metadata(url)
    return {"title": title, "available": title is not None}


@app.post("/verify")
async def verify(request: VerifyRequest):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN not configured")
    if not CHANNEL_ID:
        raise HTTPException(500, "CHANNEL_ID not configured")
    if not request.init_data:
        raise HTTPException(403, "Missing init data")
    payload = verify_telegram_init_data(request.init_data, BOT_TOKEN,
                                        max_age_seconds=INIT_DATA_MAX_AGE_SECONDS)
    if payload is None:
        raise HTTPException(403, "Invalid init data")
    user_id = (payload.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(403, "No user id")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = None
        for attempt in range(2):
            resp = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                params={"chat_id": CHANNEL_ID, "user_id": user_id})
            if resp.status_code == 429 and attempt == 0:
                try:
                    ra = resp.json().get("parameters", {}).get("retry_after")
                except Exception:
                    ra = None
                if isinstance(ra, int) and 0 < ra <= 2:
                    await asyncio.sleep(ra)
                    continue
            break

    if resp is None:
        return {"allowed": False, "reason": "no_response"}
    if resp.status_code != 200:
        return {"allowed": False, "reason": f"http_{resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"allowed": False, "reason": "bad_json"}
    if not data.get("ok"):
        return {"allowed": False, "reason": data.get("description", "api_error")}
    status = data["result"].get("status", "")
    return {"allowed": status in {"member", "administrator", "creator"}, "status": status}


def _verify_webhook_secret(request: Request) -> bool:
    """Единственное, что реально подтверждает, что POST на /webhook или
    /webhook/bible пришёл от Telegram, а не от кого угодно, кто узнал URL.
    Без этой проверки sender_id/from.id из тела запроса — просто число,
    которое отправитель мог вписать любое, включая ID администратора, и
    получить доступ к /users, /stats, удалению любого пользователя и т.д.
    Telegram эхом присылает этот заголовок на каждый вебхук-запрос ТОЛЬКО
    если он был зарегистрирован через setWebhook с параметром secret_token
    (см. set_webhook/set_webhook_bible ниже) — значение известно только
    нам и Telegram, подделать его снаружи невозможно.

    Если WEBHOOK_SECRET не задан в окружении — ЗАКРЫВАЕМ вебхук (fail-closed):
    без секрета проверка подлинности невозможна в принципе, а открытый
    /webhook — это открытая снаружи админ-панель (/users, удаление любых
    пользователей от имени администратора, если известен его Telegram ID).
    Лучше бот молчит (ошибка видна в логах при первом же сообщении), чем
    админка доступна кому угодно. На Render секрет должен быть задан
    ВСЕГДА — см. DEPLOY_CHECKLIST.md."""
    if not WEBHOOK_SECRET:
        log.error("_verify_webhook_secret: WEBHOOK_SECRET не настроен — вебхук ОТКЛОНЯЕТ все запросы (fail-closed). Задай WEBHOOK_SECRET на Render и перерегистрируй вебхуки (/set_webhook, /set_webhook_bible).")
        return False
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    return hmac.compare_digest(header, WEBHOOK_SECRET)

@app.post("/webhook")
async def webhook(request: Request):
    if not _verify_webhook_secret(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid secret token"})
    try:
        update = await request.json()
    except Exception:
        log.warning("Webhook: невалидный JSON")
        return {"ok": False, "error": "invalid json"}

    update_id = update.get("update_id", "?")
    keys = [k for k in update if k != "update_id"]
    log.info(f"▶ update_id={update_id} | поля: {keys}")

    if update.get("message"):
        msg = update["message"]
        # Обрабатываем только личные сообщения боту (тип чата private).
        # Сообщения из чата комментариев тоже приходят как "message" —
        # их нужно игнорировать, иначе бот отвечает кнопкой на каждое.
        if (msg.get("chat") or {}).get("type") == "private":
            asyncio.create_task(handle_user_message(msg))
        return {"ok": True}

    message = update.get("channel_post")
    is_edit = False
    if not message:
        message = update.get("edited_channel_post")
        is_edit = True
    if not message:
        log.info(f"update_id={update_id}: не пост — пропускаем")
        return {"ok": True, "action": "ignored", "fields": keys}

    chat          = message.get("chat") or {}
    chat_username = chat.get("username", "")
    chat_id_num   = str(chat.get("id", ""))
    expected      = CHANNEL_ID.lstrip("@").lower()
    username_match = bool(chat_username and chat_username.lower() == expected)
    id_match       = bool(chat_id_num and (chat_id_num == expected or chat_id_num == CHANNEL_ID))
    if not username_match and not id_match:
        log.warning(f"update_id={update_id}: чужой чат @{chat_username} — пропускаем")
        return {"ok": True, "action": "ignored_wrong_chat"}

    # Раньше upsert_post_to_github await'ился до ответа Telegram: 2-4 запроса
    # к GitHub (до 20+ секунд с retry) удерживали вебхук, Telegram при таймауте
    # повторял доставку - лишние дубликаты апдейтов. Теперь сохранение поста
    # и AI-обработка живут в фоновых тасках, вебхук отвечает мгновенно.
    # Дедупликация по message_id внутри upsert уже защищает от повторов.
    async def _save_and_process():
        result = await upsert_post_to_github(message, is_edit=is_edit)
        if result == "added":
            tags = extract_hashtags(message)
            if should_process_ai(tags):
                await process_post(message)

    asyncio.create_task(_save_and_process())

    return {"ok": True, "action": "accepted", "post_id": message.get("message_id")}


@app.get("/links/{post_id}")
async def get_links(post_id: int):
    async with httpx.AsyncClient(timeout=15) as client:
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
    if not links_data or str(post_id) not in links_data:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return links_data[str(post_id)]


@app.post("/analyze/{post_id}")
@app.get("/analyze/{post_id}")
async def manual_analyze(post_id: int, resend_button: bool = True):
    """
    resend_button=false — переиндексировать данные поста (bible_refs, цитаты,
    reflection, related_posts), НЕ отправляя повторно кнопку «Глубже» в тред.
    Удобно, если кнопка у поста уже стоит на своём месте и трогать её не нужно —
    она и так откроет deeper.html, который всегда подтянет свежие данные из
    /links/{post_id}.
    Пример: /analyze/424?resend_button=false
    """
    async with httpx.AsyncClient(timeout=15) as client:
        posts_data, _ = await github_get(client, GITHUB_FILE)
    if not posts_data:
        return JSONResponse(status_code=404, content={"error": "posts.json not found"})
    post = next((p for p in posts_data["posts"] if p["id"] == post_id), None)
    if not post:
        return JSONResponse(status_code=404, content={"error": f"post {post_id} not found"})
    text = post.get("text") or post.get("preview") or post.get("title") or ""
    topics = post.get("topics", [])
    asyncio.create_task(process_post({
        "message_id": post_id,
        "text": text,
        "topics": topics,
        "tags": post.get("tags"),
        "date": 0,
        "chat": {"username": CHANNEL_ID.lstrip("@")}
    }, resend_button=resend_button))
    return {
        "ok": True,
        "message": f"Analysis started for post {post_id}",
        "text_len": len(text),
        "resend_button": resend_button,
    }


@app.get("/analyze_range")
async def analyze_range(from_id: int, to_id: int, delay: float = 20.0, skip_existing: bool = False):
    async with httpx.AsyncClient(timeout=30) as client:
        posts_data, _ = await github_get(client, GITHUB_FILE)
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
    if not posts_data:
        return {"error": "posts.json not found"}

    all_posts = sorted(posts_data.get("posts", []), key=lambda p: p["id"])
    to_analyze = [p for p in all_posts if from_id <= p["id"] <= to_id]
    if skip_existing:
        to_analyze = [p for p in to_analyze if str(p["id"]) not in (links_data or {})]

    log.info(f"analyze_range {from_id}-{to_id}: {len(to_analyze)} постов, delay={delay}s")

    async def _run():
        done = 0
        for post in to_analyze:
            pid = post["id"]
            post_tags = post.get("tags") or extract_tags_from_text(
                post.get("text","") or post.get("preview",""))
            if "#продолжение" in post_tags:
                log.info(f"analyze_range: пост {pid} — #продолжение, пропускаем")
                done += 1
                continue
            if not should_process_ai(post_tags):
                log.info(f"analyze_range: пост {pid} — нет AI-тегов, пропускаем")
                done += 1
                continue
            try:
                await process_post({
                    "message_id": pid,
                    "text": post.get("text") or post.get("preview") or post.get("title") or "",
                    "topics": post.get("topics", []),
                    "tags": post_tags,
                    "date": 0,
                    "chat": {"username": CHANNEL_ID.lstrip("@")}
                })
                done += 1
                log.info(f"analyze_range: [{done}/{len(to_analyze)}] пост {pid} готов")
            except Exception as e:
                log.error(f"analyze_range: пост {pid} ошибка: {e}")
            await asyncio.sleep(delay)
        log.info(f"analyze_range {from_id}-{to_id}: завершено {done}/{len(to_analyze)}")

    asyncio.create_task(_run())
    return {"ok": True, "range": f"{from_id}-{to_id}", "queued": len(to_analyze), "delay_seconds": delay}


@app.get("/analyze_all")
@limiter.limit("10/minute")
async def analyze_all(request: Request, delay: float = 5.0, skip_existing: bool = True):
    async with httpx.AsyncClient(timeout=20) as client:
        posts_data, _ = await github_get(client, GITHUB_FILE)
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)

    if not posts_data:
        return {"error": "posts.json not found"}

    links_data = links_data or {}
    posts = posts_data.get("posts", [])

    SKIP_TOPICS = {"📻 Анонсы канала", "📅 Челлендж: Лука", "📔 Духовный дневник", "😄 Юмор"}
    to_analyze = []
    for post in posts:
        post_topics = set(post.get("topics", []))
        if post_topics and post_topics.issubset(SKIP_TOPICS):
            continue
        if skip_existing and str(post["id"]) in links_data:
            continue
        text = post.get("text") or post.get("preview") or post.get("title") or ""
        if not text.strip():
            continue
        to_analyze.append(post)

    log.info(f"analyze_all: {len(to_analyze)} постов к анализу, delay={delay}s")

    async def _run():
        done = 0
        for post in to_analyze:
            pid = post["id"]
            post_tags = post.get("tags") or extract_tags_from_text(
                post.get("text","") or post.get("preview",""))
            if "#продолжение" in post_tags:
                log.info(f"analyze_all: пост {pid} — #продолжение, пропускаем")
                done += 1
                await asyncio.sleep(0.5)
                continue
            if not should_process_ai(post_tags):
                log.info(f"analyze_all: пост {pid} — нет AI-тегов, пропускаем")
                done += 1
                await asyncio.sleep(0.5)
                continue
            try:
                await process_post({
                    "message_id": pid,
                    "text": post.get("text") or post.get("preview") or post.get("title") or "",
                    "topics": post.get("topics", []),
                    "tags": post_tags,
                    "date": 0,
                    "chat": {"username": CHANNEL_ID.lstrip("@")}
                })
                done += 1
                log.info(f"analyze_all: [{done}/{len(to_analyze)}] пост {pid} готов")
            except Exception as e:
                log.error(f"analyze_all: пост {pid} ошибка: {e}")
            await asyncio.sleep(delay)
        log.info(f"analyze_all: завершено {done}/{len(to_analyze)}")

    asyncio.create_task(_run())
    return {
        "ok": True,
        "queued": len(to_analyze),
        "delay_seconds": delay,
        "message": f"Анализ запущен для {len(to_analyze)} постов. Следи за логами Render."
    }


# ── Фоновые админ-джобы ───────────────────────────────────────
# Раньше /reindex, /reindex_all, /cleanup, /remove_all_buttons и
# /import_texts делали всю работу СИНХРОННО внутри HTTP-запроса: Render
# обрезает такие запросы по таймауту (~30-100с), работа терялась на
# середине без всякой диагностики. Теперь каждый длинный роут мгновенно
# отвечает {ok, queued} и уводит работу в asyncio.create_task — как
# уже сделано раньше для /analyze_all и /analyze_range. Флаг в
# _ADMIN_JOBS не даёт запустить вторую копию той же job, пока жива
# первая (двойной /reindex = двойная трата Cohere и гонки записи).
_ADMIN_JOBS: set = set()


def _start_admin_job(name: str, coro) -> dict:
    if name in _ADMIN_JOBS:
        return {"ok": False, "error": f"{name} уже выполняется — дождитесь завершения (логи Render)"}
    _ADMIN_JOBS.add(name)

    async def _runner():
        try:
            await coro
        except Exception as e:
            log.error(f"admin job {name} failed: {e}")
        finally:
            _ADMIN_JOBS.discard(name)

    asyncio.create_task(_runner())
    log.info(f"admin job {name}: запущена в фоне")
    return {"ok": True, "queued": True, "job": name,
            "message": f"{name} запущена в фоне. Прогресс — в логах Render."}


@app.get("/reindex")
async def reindex_all():
    async def _job():
        async with httpx.AsyncClient(timeout=20) as client:
            posts_data, _ = await github_get(client, GITHUB_FILE)
            emb_data, emb_sha = await github_get(client, GITHUB_EMBEDDINGS_FILE)
        if not posts_data:
            log.error("reindex: posts.json not found")
            return
        emb_map = _extract_emb_map(emb_data)
        updated = 0
        for post in posts_data.get("posts", []):
            # Вектор ищем и в посте (legacy), и в embeddings.json —
            # отсутствует только там и там → считаем заново.
            if post.get("embedding") or emb_map.get(str(post["id"])):
                continue
            emb = await get_embedding(post.get("text", post.get("preview", "")))
            if emb:
                emb_map[str(post["id"])] = emb
                updated += 1
            await asyncio.sleep(0.5)
        if updated > 0:
            async with httpx.AsyncClient(timeout=20) as client:
                await github_put(
                    client, GITHUB_EMBEDDINGS_FILE,
                    {"embeddings": emb_map,
                     "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")},
                    emb_sha, f"reindex: added {updated} embeddings")
        log.info(f"reindex: добавлено {updated} эмбеддингов (всего постов {len(posts_data.get('posts', []))})")
    return _start_admin_job("reindex", _job())


@app.get("/split_embeddings")
async def split_embeddings():
    """Разовый перенос векторов Cohere из posts.json в embeddings.json.
    posts.json качает клиент оглавления целиком — до сплита ~2.8 МБ из
    3.4 МБ были векторами, которые фронту не нужны. После сплита файл
    сокращается до ~0.3-0.6 МБ, и первый экран мини-аппа грузится в разы
    быстрее. Повторный запуск безопасен: после первого же сплита векторов
    в posts.json не остаётся, updated=0."""
    async def _job():
        async with httpx.AsyncClient(timeout=30) as client:
            posts_data, posts_sha = await github_get(client, GITHUB_FILE)
            emb_data, emb_sha = await github_get(client, GITHUB_EMBEDDINGS_FILE)
        if not posts_data:
            log.error("split_embeddings: posts.json not found")
            return
        emb_map = _extract_emb_map(emb_data)
        moved = 0
        posts = posts_data.get("posts", [])
        for post in posts:
            emb = post.pop("embedding", None)
            if emb:
                emb_map[str(post["id"])] = emb
                moved += 1
        async with httpx.AsyncClient(timeout=30) as client:
            await github_put(
                client, GITHUB_EMBEDDINGS_FILE,
                {"embeddings": emb_map,
                 "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")},
                emb_sha, "split_embeddings: collect vectors")
            posts_data["posts"] = posts
            await github_put(client, GITHUB_FILE, posts_data, posts_sha,
                             f"split_embeddings: removed {moved} vectors from posts.json")
        log.info(f"split_embeddings: перенесено {moved} векторов, posts.json = {len(posts)} постов")
    return _start_admin_job("split_embeddings", _job())


@app.get("/reindex_all")
async def reindex_all_posts():
    async def _job():
        async with httpx.AsyncClient(timeout=30) as client:
            posts_data, sha = await github_get(client, GITHUB_FILE)
        if not posts_data:
            log.error("reindex_all: posts.json not found")
            return

        posts = posts_data.get("posts", [])
        updated = 0
        skipped = 0
        fetched_from_tg = 0

        for post in posts:
            raw_tags = post.get("tags")
            if not raw_tags:
                text = post.get("text", "") or post.get("preview", "")
                raw_tags = extract_tags_from_text(text)

            if not raw_tags:
                log.warning(f"reindex_all: пост {post['id']} — теги не найдены в тексте, пропускаем")

            if not raw_tags:
                skipped += 1
                continue

            new_topics = hashtags_to_topics(raw_tags)
            new_tags   = get_post_tags(raw_tags)

            if not new_topics:
                skipped += 1
                continue

            changed = (new_topics != post.get("topics") or new_tags != post.get("tags"))
            post["topics"] = new_topics
            post["tags"]   = new_tags
            if changed:
                updated += 1

        posts_data["topics"] = recalc_topics(posts)
        posts_data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=30) as client:
            _, fresh_sha = await github_get(client, GITHUB_FILE)
            await github_put(client, GITHUB_FILE, posts_data, fresh_sha,
                             f"reindex_all: updated {updated} posts")
        log.info(f"reindex_all: обновлено {updated}, из TG: {fetched_from_tg}, пропущено {skipped} из {len(posts)}")
    return _start_admin_job("reindex_all", _job())


@app.get("/remove_button/{post_id}")
async def remove_button(post_id: int):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{TELEGRAM_API}/editMessageReplyMarkup",
            json={"chat_id": CHANNEL_ID, "message_id": post_id,
                  "reply_markup": {"inline_keyboard": []}}
        )
    result = r.json()
    if result.get("ok"):
        log.info(f"✅ Кнопка удалена с поста {post_id}")
        return {"ok": True, "post_id": post_id}
    else:
        log.error(f"❌ Ошибка удаления кнопки {post_id}: {result.get('description')}")
        return {"ok": False, "error": result.get("description")}


@app.get("/remove_all_buttons")
async def remove_all_buttons(delay: float = 2.0):
    async def _job():
        async with httpx.AsyncClient(timeout=30) as client:
            links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
        if not links_data:
            log.error("remove_all_buttons: links.json not found")
            return

        removed = []
        skipped = []

        async with httpx.AsyncClient(timeout=10) as client:
            for post_id_str in sorted(links_data.keys(), key=lambda x: int(x)):
                pid = int(post_id_str)
                for attempt in range(3):
                    r = await client.post(
                        f"{TELEGRAM_API}/editMessageReplyMarkup",
                        json={"chat_id": CHANNEL_ID, "message_id": pid,
                              "reply_markup": {"inline_keyboard": []}}
                    )
                    rj = r.json()
                    if rj.get("ok"):
                        removed.append(pid)
                        log.info(f"🗑 Кнопка удалена с поста {pid}")
                        break
                    desc = rj.get("description", "")
                    if "Too Many Requests" in desc or r.status_code == 429:
                        wait = int(rj.get("parameters", {}).get("retry_after", 30))
                        log.warning(f"⏳ 429 на посту {pid}, ждём {wait}s...")
                        await asyncio.sleep(wait)
                    elif "message is not modified" in desc or "Bad Request" in desc:
                        skipped.append(pid)
                        log.info(f"ℹ️ Пост {pid}: кнопки уже не было")
                        break
                    else:
                        skipped.append(pid)
                        log.warning(f"⚠️ Пост {pid}: {desc}")
                        break
                await asyncio.sleep(delay)

        log.info(f"remove_all_buttons: удалено={len(removed)}, пропущено={len(skipped)}")
    return _start_admin_job("remove_all_buttons", _job())


@app.get("/send_button")
async def send_button_to_thread(post_id: int, disc_id: int):
    bot_username = BOT_USERNAME.lstrip("@")
    miniapp_url = f"https://t.me/{bot_username}/deeper?startapp={post_id}"
    keyboard = {"inline_keyboard": [[
        {"text": "📚 Глубже — библейский контекст поста", "url": miniapp_url}
    ]]}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": DISCUSSION_CHAT_ID,
                "reply_to_message_id": disc_id,
                "text": "📚 Библейский контекст и связи этого поста",
                "reply_markup": keyboard,
                "disable_notification": True
            }
        )
    result = r.json()
    if result.get("ok"):
        log.info(f"✅ Кнопка «Глубже» вручную → тред поста {post_id} (disc_id={disc_id})")
        return {"ok": True, "post_id": post_id, "disc_id": disc_id}
    else:
        desc = result.get("description", "")
        log.error(f"❌ send_button post={post_id} disc={disc_id}: {desc}")
        return {"ok": False, "error": desc}


@app.get("/cleanup")
async def cleanup(delay: float = 1.0):
    async def _job():
        async with httpx.AsyncClient(timeout=30) as client:
            posts_data, _ = await github_get(client, GITHUB_FILE)
            links_data, links_sha = await github_get(client, GITHUB_LINKS_FILE)
        if not posts_data or not links_data:
            log.error("cleanup: posts.json or links.json not found")
            return

        removed_buttons = []
        removed_links = []

        async with httpx.AsyncClient(timeout=10) as client:
            for post in posts_data.get("posts", []):
                pid = post["id"]
                post_tags = post.get("tags") or extract_tags_from_text(
                    post.get("text","") or post.get("preview",""))

                should_have_button = (
                    should_process_ai(post_tags)
                    and not should_skip_button(post_tags)
                    and str(pid) in links_data
                )

                if not should_have_button:
                    for attempt in range(3):
                        r = await client.post(
                            f"{TELEGRAM_API}/editMessageReplyMarkup",
                            json={"chat_id": CHANNEL_ID, "message_id": pid,
                                  "reply_markup": {"inline_keyboard": []}}
                        )
                        rj = r.json()
                        if rj.get("ok"):
                            removed_buttons.append(pid)
                            log.info(f"🗑 Кнопка удалена с поста {pid}")
                            break
                        desc = rj.get("description", "")
                        if "Too Many Requests" in desc or r.status_code == 429:
                            wait = int(rj.get("parameters", {}).get("retry_after", 30))
                            log.warning(f"⏳ 429 на посту {pid}, ждём {wait}s...")
                            await asyncio.sleep(wait)
                        elif "message is not modified" in desc or "Bad Request" in desc:
                            log.info(f"ℹ️ Пост {pid}: кнопки уже не было")
                            break
                        else:
                            log.warning(f"⚠️ Пост {pid}: {desc}")
                            break
                    if str(pid) in links_data:
                        del links_data[str(pid)]
                        removed_links.append(pid)
                    await asyncio.sleep(delay)

        if removed_links:
            async with httpx.AsyncClient(timeout=30) as client:
                _, fresh_sha = await github_get(client, GITHUB_LINKS_FILE)
                await github_put(client, GITHUB_LINKS_FILE, links_data, fresh_sha,
                                 f"cleanup: removed {len(removed_links)} entries")

        log.info(f"cleanup: кнопки удалены {len(removed_buttons)}, links удалены {len(removed_links)}")
    return _start_admin_job("cleanup", _job())


@app.get("/update_buttons")
async def update_buttons(delay: float = 1.5):
    async with httpx.AsyncClient(timeout=30) as client:
        posts_data, _ = await github_get(client, GITHUB_FILE)
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
    if not posts_data or not links_data:
        return {"error": "posts.json or links.json not found"}

    bot_username = BOT_USERNAME.lstrip("@")
    posts = posts_data.get("posts", [])
    queued = []

    for post in posts:
        pid = post["id"]
        post_tags = post.get("tags") or extract_tags_from_text(
            post.get("text","") or post.get("preview",""))
        if str(pid) not in links_data:
            continue
        if not should_process_ai(post_tags) or should_skip_button(post_tags):
            continue
        queued.append(pid)

    async def _run():
        done = 0
        async with httpx.AsyncClient(timeout=10) as client:
            for pid in queued:
                miniapp_url = f"https://t.me/{bot_username}/deeper?startapp={pid}"
                keyboard = {"inline_keyboard": [[
                    {"text": "📚 Глубже — библейский контекст поста", "url": miniapp_url}
                ]]}
                disc = await client.get(
                    f"{TELEGRAM_API}/getDiscussionMessage",
                    params={"chat_id": CHANNEL_ID, "message_id": pid}
                )
                disc_data = disc.json()
                if not disc_data.get("ok"):
                    log.error(f"❌ getDiscussionMessage {pid}: {disc_data.get('description')}")
                    done += 1
                    await asyncio.sleep(delay)
                    continue
                disc_msg_id = disc_data["result"]["message"]["message_id"]
                r = await client.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={
                        "chat_id": DISCUSSION_CHAT_ID,
                        "reply_to_message_id": disc_msg_id,
                        "text": "📚 Библейский контекст и связи этого поста",
                        "reply_markup": keyboard,
                        "disable_notification": True
                    }
                )
                result = r.json()
                if result.get("ok"):
                    log.info(f"✅ Кнопка → тред поста {pid} (disc={disc_msg_id})")
                else:
                    desc = result.get("description","")
                    if "Too Many Requests" in desc:
                        wait = 30
                        log.warning(f"⏳ 429 для {pid}, ждём {wait}s...")
                        await asyncio.sleep(wait)
                        r2 = await client.post(
                            f"{TELEGRAM_API}/sendMessage",
                            json={
                                "chat_id": DISCUSSION_CHAT_ID,
                                "reply_to_message_id": disc_msg_id,
                                "text": "📚 Библейский контекст и связи этого поста",
                                "reply_markup": keyboard,
                                "disable_notification": True
                            }
                        )
                        if r2.json().get("ok"):
                            log.info(f"✅ Кнопка (retry) → пост {pid}")
                        else:
                            log.error(f"❌ Пост {pid}: {r2.json().get('description')}")
                    else:
                        log.error(f"❌ Пост {pid}: {desc}")
                done += 1
                await asyncio.sleep(delay)
        log.info(f"update_buttons: добавлено комментариев {done} постам")

    asyncio.create_task(_run())
    return {"ok": True, "queued": len(queued), "delay_seconds": delay}


@app.get("/bulk_deeper")
async def bulk_deeper(delay: float = 1.5):
    async with httpx.AsyncClient(timeout=15) as client:
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
    if not links_data:
        return {"error": "links.json not found or empty"}
    post_ids = [int(k) for k in links_data.keys() if k.isdigit()]
    post_ids.sort(reverse=True)

    async def _run():
        done = 0
        for pid in post_ids:
            await send_deeper_button(pid)
            await asyncio.sleep(delay)
            done += 1
        log.info(f"bulk_deeper: добавил кнопки к {done} постам")

    asyncio.create_task(_run())
    return {"ok": True, "queued": len(post_ids), "delay_seconds": delay}


@app.get("/reload_theology")
async def reload_theology():
    global _theology_cache
    _theology_cache = None
    db = await get_theology_db()
    return {"ok": True, "loaded": len(db)}


@app.get("/set_webhook")
async def set_webhook(request: Request):
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN not set"}
    # Fail-closed: без secret_token Telegram будет слать запросы, которые
    # бэкенд теперь ОТКЛОНЯЕТ (см. _verify_webhook_secret) — бот молчал бы
    # без всякой диагностики. Регистрация без секрета запрещена.
    if not WEBHOOK_SECRET:
        return {"ok": False, "error": "WEBHOOK_SECRET не задан — вебхук не регистрируется (fail-closed). Задай переменную на Render и повтори."}
    webhook_url = str(request.base_url).rstrip("/") + "/webhook"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["channel_post", "edited_channel_post", "message"],
        "secret_token": WEBHOOK_SECRET,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json=payload)
        data = resp.json()
    log.info(f"setWebhook → {data}")
    return {"ok": data.get("ok"), "webhook_url": webhook_url, "telegram_response": data}


@app.get("/check_webhook")
async def check_webhook():
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        return (await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo")).json()


@app.get("/set_webhook_bible")
async def set_webhook_bible(request: Request):
    """Регистрирует вебхук бота «План чтения Библии» с явным allowed_updates,
    включающим callback_query — без этого поля Telegram мог быть настроен
    (например, вручную, ещё до появления инлайн-кнопок админ-панели) без
    доставки нажатий на инлайн-кнопки вообще. Достаточно один раз открыть
    этот адрес в браузере после деплоя."""
    if not BIBLE_BOT_TOKEN:
        return {"ok": False, "error": "BIBLE_BOT_TOKEN not set"}
    # Fail-closed — по той же причине, что и в /set_webhook выше.
    if not WEBHOOK_SECRET:
        return {"ok": False, "error": "WEBHOOK_SECRET не задан — вебхук не регистрируется (fail-closed). Задай переменную на Render и повтори."}
    webhook_url = str(request.base_url).rstrip("/") + "/webhook/bible"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "secret_token": WEBHOOK_SECRET,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BIBLE_BOT_TOKEN}/setWebhook",
            json=payload)
        data = resp.json()
    log.info(f"setWebhook (bible) → {data}")
    return {"ok": data.get("ok"), "webhook_url": webhook_url, "telegram_response": data}


@app.get("/check_webhook_bible")
async def check_webhook_bible():
    if not BIBLE_BOT_TOKEN:
        return {"ok": False, "error": "BIBLE_BOT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        return (await client.get(
            f"https://api.telegram.org/bot{BIBLE_BOT_TOKEN}/getWebhookInfo")).json()


@app.get("/import_texts")
async def import_texts():
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not set"}
    async def _job():
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                result_data, result_sha = await github_get(client, "result.json")
            except Exception as e:
                log.error(f"import_texts: result.json не найден на GitHub: {e}")
                return

            messages = result_data.get("messages", [])
            log.info(f"import_texts: {len(messages)} сообщений в result.json")

            tg_texts = {}
            for msg in messages:
                if msg.get("type") != "message":
                    continue
                mid = msg.get("id")
                raw = msg.get("text", "")
                if isinstance(raw, str):
                    text = raw
                elif isinstance(raw, list):
                    text = "".join(
                        p if isinstance(p, str) else p.get("text", "")
                        for p in raw
                    )
                else:
                    text = ""
                if mid and text.strip():
                    tg_texts[mid] = text[:3000]

            log.info(f"import_texts: текстов найдено: {len(tg_texts)}")

            posts_data, posts_sha = await github_get(client, GITHUB_FILE)
            posts = posts_data.get("posts", [])

            updated = 0
            for post in posts:
                pid = post["id"]
                if pid in tg_texts:
                    existing = post.get("text", "")
                    if not existing or len(existing) < 100:
                        post["text"] = tg_texts[pid]
                        updated += 1

            log.info(f"import_texts: обновлено {updated} постов")

            await github_put(client, GITHUB_FILE, posts_data, posts_sha,
                             f"import: full text for {updated} posts")

            try:
                del_body = {
                    "message": "cleanup: remove result.json",
                    "sha": result_sha,
                    "branch": GITHUB_BRANCH,
                }
                del_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/result.json"
                await client.delete(del_url, headers=_gh_headers(), json=del_body)
                log.info("import_texts: result.json удалён с GitHub")
            except Exception as e:
                log.warning(f"import_texts: не удалось удалить result.json: {e}")

        log.info(f"import_texts: готово, обновлено {updated} постов. Теперь /reindex и /analyze_all")
    return _start_admin_job("import_texts", _job())


@app.get("/debug_last")
async def debug_last():
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not set"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            data, _ = await github_get(client, GITHUB_FILE)
            posts = data.get("posts", [])
            return {
                "total": len(posts),
                "updated": data.get("updated"),
                "last_5": posts[:5],
            }
        except Exception as e:
            return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# BIBLE READING BOT
# ═══════════════════════════════════════════════════════════════

BIBLE_BOT_TOKEN    = os.environ.get("BIBLE_BOT_TOKEN", "")
BIBLE_BOT_USERNAME = os.environ.get("BIBLE_BOT_USERNAME", "mybible_reading_bot")
BIBLE_PAGES_URL    = os.environ.get("BIBLE_PAGES_URL", "https://maksjermy123.github.io/bible-reading-bot/")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")
CHANNEL_LINK       = os.environ.get("CHANNEL_LINK", "https://t.me/Chtenie_Preobrazenie")
CHANNEL_NAME       = os.environ.get("CHANNEL_NAME", "От чтения к Преображению")
# Telegram user_id (не username!) через запятую — те, кому в личке с ботом
# доступна команда /stats со статистикой пользователей. Узнать свой user_id
# можно, например, у бота @userinfobot. Если переменная не задана — команда
# /stats никому не отвечает (по умолчанию отключена, ничего не ломает).
BIBLE_ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("BIBLE_ADMIN_USER_IDS", "").replace(" ", "").split(",") if x
}

BIBLE_API  = f"https://api.telegram.org/bot{BIBLE_BOT_TOKEN}"
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}
MSK = ZoneInfo("Europe/Moscow")

bible_scheduler = AsyncIOScheduler(timezone=MSK)

# Общий httpx-клиент для всех Supabase-запросов кода плана чтения. Раньше
# каждый вызов создавал httpx.AsyncClient() заново — новое TCP/TLS-
# соединение на КАЖДЫЙ запрос к Supabase, вместо переиспользования уже
# открытого keep-alive соединения. При росте числа пользователей (и,
# соответственно, числа обращений к Supabase) это заметно бьёт по задержке.
# Намеренно НЕ трогаем остальные httpx.AsyncClient(timeout=...) в проекте
# (радио/анализ постов) — они написаны раньше, с осознанно подобранными
# таймаутами под конкретные внешние вызовы (GitHub, Telegram), и не были
# частью этой сессии — рефакторить их заодно было бы risky без отдельной
# проверки.
HTTP_CLIENT: httpx.AsyncClient = None


def _assert_webhook_routes():
    """Страховка от класса багов, который уже случался (см. DEPLOY_CHECKLIST.md):
    декоратор @app.post("/webhook/bible") «уезжал» не к той функции после
    правок — ast/импорт это не ловит, а боты молчат или отвечают не тем.
    Проверка без сети: у приложения должна быть ровно ОДНА POST-маршрутизация
    /webhook → webhook и /webhook/bible → bible_webhook. Дубликат или
    неправильная функция — процесс не поднимается вовсе (Render покажет
    crash в логах сразу при деплое, а не через день тишины)."""
    found = {}
    for r in app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if methods and "POST" in methods and path in ("/webhook", "/webhook/bible"):
            ep = getattr(r, "endpoint", None)
            name = getattr(ep, "__name__", repr(ep))
            if path in found:
                raise RuntimeError(
                    f"webhook route assert: дубликат POST {path} → {name} "
                    f"(уже зарегистрирован → {found[path]})")
            found[path] = name
    if found.get("/webhook") != "webhook" or found.get("/webhook/bible") != "bible_webhook":
        raise RuntimeError(
            f"webhook route assert: POST /webhook → {found.get('/webhook')!r}, "
            f"POST /webhook/bible → {found.get('/webhook/bible')!r} — вебхуки "
            f"повешены не на те функции. Деплой прерван.")


@app.on_event("startup")
async def bible_scheduler_startup():
    _assert_webhook_routes()
    global HTTP_CLIENT
    HTTP_CLIENT = httpx.AsyncClient(timeout=15.0)
    bible_scheduler.start()

@app.on_event("shutdown")
async def bible_http_client_shutdown():
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()

# ── Supabase: plan_progress ────────────────────────────────────

# ── Supabase: plan_progress ────────────────────────────────────
# ВАЖНО: с этой ревизии таблица plan_progress ведёт прогресс не "одна
# строка на пользователя", а "одна строка на (пользователь, план)" — это
# и есть требуемая поддержка нескольких одновременных планов с разным
# темпом чтения. Перед деплоем нужно один раз выполнить в Supabase SQL
# Editor (Table Editor → plan_progress → SQL, либо через "SQL Editor"):
#
#   alter table plan_progress drop constraint if exists plan_progress_user_id_key;
#   alter table plan_progress add constraint plan_progress_user_plan_key unique (user_id, plan_id);
#
# (имя старого constraint'а на user_id может отличаться — если команда
# выше не найдёт его по имени, откройте Table Editor → plan_progress →
# вкладку "Constraints", посмотрите точное имя uniq-констрейнта на
# user_id и подставьте его в DROP CONSTRAINT).

async def sb_get_all(user_id: int) -> list:
    """Все строки прогресса пользователя — по одной на каждый его план."""
    client = HTTP_CLIENT
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}"},
    )
    data = r.json()
    return data if isinstance(data, list) else []

async def sb_get_one(user_id: int, plan_id: str):
    """Прогресс по конкретному плану (или None, если пользователь его не регистрировал)."""
    client = HTTP_CLIENT
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}", "plan_id": f"eq.{plan_id}", "limit": "1"},
    )
    data = r.json()
    return data[0] if isinstance(data, list) and data else None


# PostgREST по умолчанию отдаёт МАКСИМУМ 1000 строк на запрос, молча обрезая
# остальное. На росте базы это означало бы: sb_get_due перестаёт видеть
# «хвост» пользователей (часть людей молча не получает напоминания),
# /users показывает не всех, онбординг-цепочка обрывается. _sb_fetch_all
# листает страницы (limit/offset) до конца и склеивает полный список.
_SB_PAGE = 1000
_SB_HARD_CAP = 200_000  # предохранитель от бесконечного цикла на битом фильтре


async def _sb_fetch_all(table: str, params: dict) -> list:
    client = HTTP_CLIENT
    out = []
    offset = 0
    while True:
        page_params = dict(params)
        page_params["limit"] = str(_SB_PAGE)
        page_params["offset"] = str(offset)
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=SB_HEADERS,
            params=page_params,
        )
        if r.status_code != 200:
            log.error(f"[_sb_fetch_all] {table} FAILED {r.status_code}: {r.text[:200]}")
            break
        try:
            rows = r.json()
        except Exception:
            rows = []
        if not isinstance(rows, list):
            log.error(f"[_sb_fetch_all] {table}: не список в ответе")
            break
        out.extend(rows)
        if len(rows) < _SB_PAGE or len(out) >= _SB_HARD_CAP:
            break
        offset += _SB_PAGE
    return out

async def sb_upsert(payload: dict) -> tuple:
    """Возвращает (ok, error). Раньше здесь ответ Supabase вообще не
    проверялся — если запись падала (например, ON CONFLICT (user_id,
    plan_id) не совпадает ни с одним реальным constraint'ом в таблице —
    именно так ведёт себя Postgres, если уникальность в plan_progress
    задана только по user_id), /plan/register всё равно молча отвечал
    {"ok": true}, будто второй план зарегистрировался. На деле строка не
    создавалась, и следующий /plan/read честно, но неожиданно для клиента
    отвечал "not registered" — план как будто испарялся."""
    client = HTTP_CLIENT
    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/plan_progress?on_conflict=user_id,plan_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json=payload,
    )
    if r.status_code >= 300:
        try:
            err = r.json()
        except Exception:
            err = r.text
        log.error(f"[sb_upsert] FAILED {r.status_code} for {payload.get('user_id')}/{payload.get('plan_id')}: {err}")
        return False, str(err)
    return True, None

async def sb_patch(user_id: int, plan_id: str, payload: dict) -> bool:
    """Возвращает True при успехе. Раньше ответ Supabase вообще не
    проверялся: при сбое (RLS, таймаут) сервер всё равно отвечал клиенту
    {"ok": true}, а прогресс молча не сохранялся. Теперь сбой виден
    в логах Render."""
    client = HTTP_CLIENT
    r = await client.patch(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}", "plan_id": f"eq.{plan_id}"},
        json=payload,
    )
    if r.status_code >= 300:
        try:
            err = r.json()
        except Exception:
            err = r.text[:300]
        log.error(f"[sb_patch] FAILED {r.status_code} for {user_id}/{plan_id}: {err}")
        raise HTTPException(status_code=503, detail="storage unavailable")
    return True

async def sb_get_due(hour: int, minute: int) -> list:
    """Точное совпадение часа И минуты — раньше проверялся только час
    (notify_hour_msk), поэтому время вроде 8:37 фактически округлялось
    до ближайшего часа. notify_minute_msk по умолчанию 0, поэтому все
    пользователи, ещё не задававшие точное время, продолжают получать
    напоминание ровно в начале часа — поведение для них не меняется.

    ВАЖНО: PostgREST-оператор neq НЕ матчит NULL. Раньше фильтр был просто
    last_read_date=neq.{today}, и планы, которые НИ РАЗУ не читали
    (last_read_date IS NULL), полностью выпадали из выборки — такие
    пользователи никогда не получали ежедневное напоминание.
    Теперь используем or=(is.null, neq.today).
    "сегодня" — по МСК: сервер живёт в UTC, и date.today() с 00:00 до 03:00 МСК
    возвращал бы ВЧЕРАШНИЙ день — прочитавшие после полуночи получали бы
    повторное напоминание тем же утром."""
    today = datetime.now(MSK).date().isoformat()
    rows = await _sb_fetch_all("plan_progress", {
        "notify_hour_msk": f"eq.{hour}",
        "notify_minute_msk": f"eq.{minute}",
        "notify_on": "eq.true",
        "or": f"(last_read_date.is.null,last_read_date.neq.{today})",
        "select": "user_id,plan_id,title,streak,start_date,days_done",
    })
    return rows if isinstance(rows, list) else []

async def _sb_count(client: httpx.AsyncClient, extra_params: dict) -> int:
    """Считает строки в plan_progress через PostgREST Prefer: count=exact —
    сам запрос данные не гоняет (select=user_id минимален), интересен только
    заголовок Content-Range вида '0-24/137', откуда и берём итоговое число.
    ПРИМЕЧАНИЕ: после перехода на одну строку на (пользователь, план) эти
    числа в /stats считают РЕГИСТРАЦИИ ПЛАНОВ, а не уникальных пользователей
    — пользователь с двумя активными планами даст здесь +2, а не +1."""
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers={**SB_HEADERS, "Prefer": "count=exact"},
        params={"select": "user_id", **extra_params},
    )
    cr = r.headers.get("content-range", "")
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[-1])
        except ValueError:
            pass
    try:
        return len(r.json())
    except Exception:
        return 0

async def bible_collect_stats() -> dict:
    """Статистика для /stats: сколько всего когда-либо зарегистрировали план
    (каждая строка plan_progress — один пользователь, upsert по user_id),
    сколько реально читали за последние 7 дней и сегодня, у скольких включены
    напоминания. «Активность» намеренно посчитана по факту чтения, а не по
    самому наличию строки — просто открыть бота один раз не значит читать.
    Даты тоже по МСК (см. комментарий в sb_get_due)."""
    today = datetime.now(MSK).date().isoformat()
    week_ago = (datetime.now(MSK).date() - timedelta(days=7)).isoformat()
    client = HTTP_CLIENT
    total          = await _sb_count(client, {})
    active_today   = await _sb_count(client, {"last_read_date": f"eq.{today}"})
    active_7d      = await _sb_count(client, {"last_read_date": f"gte.{week_ago}"})
    notify_enabled = await _sb_count(client, {"notify_on": "eq.true"})
    return {
        "total": total,
        "active_today": active_today,
        "active_7d": active_7d,
        "notify_enabled": notify_enabled,
    }

# ── Админ-панель: /users — список всех записей прогресса с удалением ──
# Даёт администратору увидеть КАЖДУЮ строку в plan_progress (включая любые
# "осиротевшие" записи, до которых не может дотянуться ни один обычный
# сценарий удаления в самом мини-аппе) и удалить любую из них вручную —
# нужно и для чистого старта при тестировании, и как способ найти запись,
# из-за которой могут продолжать приходить напоминания после того, как
# план вроде бы удалён в интерфейсе.

async def _fetch_all_progress_rows() -> list:
    return await _sb_fetch_all("plan_progress", {
        "select": "user_id,plan_id,title,streak,max_streak,last_read_date,notify_on,notify_hour_msk,notify_minute_msk",
        "order": "user_id",
    })

async def _fetch_orphan_app_state_users(known_user_ids: set) -> list:
    """Пользователи, у которых есть слепок app_state, но НИ ОДНОЙ строки в
    plan_progress. Раньше их вообще не было видно через /users — админ-панель
    строилась только по plan_progress, поэтому такого пользователя приходилось
    искать и удалять вручную через SQL Editor в Supabase (именно так и было
    сегодня с реальным призрачным app_state). Это ровно то состояние, которое
    возникает из-за бага «воскрешения» localStorage (см. checkAccountReset на
    клиенте): plan_progress уже пуст, а app_state — ещё нет."""
    rows = await _sb_fetch_all("app_state", {"select": "user_id", "order": "user_id"})
    seen = set()
    orphans = []
    for row in rows:
        uid = row.get("user_id")
        if uid is None or uid in known_user_ids or uid in seen:
            continue
        seen.add(uid)
        orphans.append(uid)
    return orphans

async def _fetch_onboarding_users(known_user_ids: set) -> list:
    """Пользователи на онбординг-этапе: «Старт» нажат (started_at заполнен),
    но ни одного плана нет и app_state тоже пуст. Раньше были невидимы для
    админа — их данные живут только в bible_accounts, которую /users не читал.
    Возвращает список user_id."""
    rows = await _sb_fetch_all("bible_accounts", {
        "started_at": "not.is.null",
        "select": "user_id",
        "order": "user_id",
    })
    result = []
    for row in rows:
        uid = row.get("user_id")
        if uid is None or uid in known_user_ids:
            continue
        result.append(uid)
    return result

async def _wipe_onboarding_account(user_id: int) -> None:
    """Полный сброс онбординг-аккаунта (без планов и app_state): ротация
    reset_token + очистка started_at и счётчика онбординг-напоминаний.
    Пользователь вернётся к состоянию «никогда не жал Старт»."""
    import uuid
    client = HTTP_CLIENT
    await client.post(
        f"{SUPABASE_URL}/rest/v1/bible_accounts?on_conflict=user_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json={
            "user_id": user_id,
            "reset_token": str(uuid.uuid4()),
            "started_at": None,
            "onboarding_nudges": 0,
            "onboarding_last_at": None,
        },
    )

_ADMIN_ROWS_LIMIT = 40

def _admin_users_text(rows: list, orphan_ids: list, onboarding_ids: list = None, note: str = "") -> str:
    by_user = {}
    for row in rows:
        by_user.setdefault(row.get("user_id"), []).append(row)
    # 🔕 — у пользователя все планы с notify_on=false (обычно после 403:
    # заблокировал бота или удалил аккаунт). Виден в списке, но напоминания
    # ему больше не уходят.
    muted = {uid for uid, urows in by_user.items()
             if urows and all(not r.get("notify_on", True) for r in urows)}
    total = len(by_user) + len(orphan_ids) + len(onboarding_ids or [])
    if not total:
        return (note + "\n\n" if note else "") + "👥 В plan_progress и app_state сейчас нет ни одной записи."
    text = (
        f"👥 <b>Пользователи</b> (всего {total}).\n"
        f"🔕 — напоминания отключены (часто: заблокировал бота).\n"
        f"🌫 — осиротевший app_state без планов.\n"
        f"🌱 — нажал «Старт», но план так и не выбрал.\n"
        f"Нажми на ID для деталей или 🗑 для удаления."
    )
    if (len(by_user) > _ADMIN_ROWS_LIMIT or len(orphan_ids) > _ADMIN_ROWS_LIMIT
            or len(onboarding_ids or []) > _ADMIN_ROWS_LIMIT):
        text += f"\n\nПоказаны первые {_ADMIN_ROWS_LIMIT} из каждой группы."
    return (note + "\n\n" + text) if note else text

def _admin_users_markup(rows: list, orphan_ids: list, onboarding_ids: list = None) -> dict:
    """Компактный список вместо простыни текста: одна строка на
    пользователя — ID (открывает детальный просмотр планов) и отдельная
    кнопка-корзина рядом (удаляет сразу, без захода внутрь). При росте
    числа пользователей полный список планов каждого прямо в одном
    сообщении стал бы нечитаемым полотном — детали теперь смотрятся по
    запросу, а не все сразу."""
    by_user = {}
    for row in rows:
        by_user.setdefault(row.get("user_id"), []).append(row)
    muted = {uid for uid, urows in by_user.items()
             if urows and all(not r.get("notify_on", True) for r in urows)}
    def _label(uid: int) -> str:
        s = str(uid)
        if uid in muted:
            s = f"🔕 {s}"
        return s
    buttons = [
        [
            {"text": _label(uid), "callback_data": f"adm_view:{uid}"},
            {"text": "🗑", "callback_data": f"adm_del:{uid}"},
        ]
        for uid in list(by_user.keys())[:_ADMIN_ROWS_LIMIT]
    ]
    buttons += [
        [
            {"text": f"🌫 {uid}", "callback_data": f"adm_view:{uid}"},
            {"text": "🗑", "callback_data": f"adm_del:{uid}"},
        ]
        for uid in orphan_ids[:_ADMIN_ROWS_LIMIT]
    ]
    # Онбординг-этап: Старт есть, плана нет. Данные только в bible_accounts,
    # поэтому раньше были невидимы для админа. Удаление здесь — полный сброс
    # аккаунта (reset_token ротируется, started_at очищается).
    for uid in (onboarding_ids or [])[:_ADMIN_ROWS_LIMIT]:
        buttons.append([
            {"text": f"🌱 {uid}", "callback_data": f"adm_view:{uid}"},
            {"text": "🗑", "callback_data": f"adm_obdel:{uid}"},
        ])
    return {"inline_keyboard": buttons}

def _admin_user_detail_text(uid: int, urows: list) -> str:
    if not urows:
        return (
            f"👤 <code>{uid}</code>\n\n"
            f"🌫 Планов в plan_progress нет — это осиротевшая запись app_state "
            f"(есть слепок мини-аппа, но ни одного зарегистрированного плана)."
        )
    lines = [f"👤 <b>Пользователь</b> <code>{uid}</code>\n"]
    for r in urows:
        lines.append(
            f"• {_plan_title(r)} <code>[{r.get('plan_id') or '—'}]</code>\n"
            f"  🔥 {r.get('streak', 0)} дней подряд (рекорд {r.get('max_streak', 0)}) · "
            f"последнее чтение: {r.get('last_read_date') or 'никогда'}"
        )
    return "\n".join(lines)

def _admin_user_detail_markup(uid: int) -> dict:
    return {"inline_keyboard": [
        [{"text": "🗑 Удалить этого пользователя", "callback_data": f"adm_del:{uid}"}],
        [{"text": "← Назад к списку", "callback_data": "adm_list"}],
    ]}

async def _delete_app_state_row(user_id: int) -> None:
    """Удаляет строку app_state (полный слепок мини-аппа: выбранные планы,
    их локальный прогресс, настройки) для пользователя. БЕЗ этого шага
    очистка одной только plan_progress не даёт настоящего чистого листа:
    при следующем открытии мини-аппа syncBackend() безусловно перезаписывает
    локальное состояние сохранённым на сервере app_state — и старые планы
    просто восстанавливаются заново, будто ничего не удаляли."""
    client = HTTP_CLIENT
    await client.delete(
        f"{SUPABASE_URL}/rest/v1/app_state",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}"},
    )

async def _wipe_user_completely(user_id: int) -> int:
    """Удаляет ВСЕ строки прогресса пользователя (по каждому его плану) и
    его app_state. Это единственное, что делает админ-панель /users —
    точечное удаление одного плана пользователь и так может сделать сам
    прямо в мини-аппе (кнопка 🗑 у плана), администратору нужно только
    полное удаление аккаунта целиком: для поддержки и для чистого старта
    при тестировании. После этого при следующей регистрации пользователь
    увидит стартовый экран, как будто зашёл впервые.
    Возвращает число реально удалённых строк plan_progress."""
    rows = await sb_get_all(user_id)
    deleted = 0
    for row in rows:
        ok, n = await _delete_progress_row(user_id, row.get("plan_id"))
        if ok:
            deleted += n
    await _delete_app_state_row(user_id)
    await sb_account_rotate_reset(user_id)
    return deleted

async def _handle_admin_callback(callback: dict):
    cq_id = callback.get("id")
    sender_id = (callback.get("from") or {}).get("id")
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data_str = callback.get("data") or ""

    if sender_id not in BIBLE_ADMIN_USER_IDS:
        await bible_answer_callback(cq_id, "Только для администратора")
        return
    if not chat_id or not message_id:
        await bible_answer_callback(cq_id)
        return

    parts = data_str.split(":")
    action = parts[0] if parts else ""

    if action == "adm_list":
        await bible_answer_callback(cq_id)
        rows = await _fetch_all_progress_rows()
        known = {r.get("user_id") for r in rows}
        orphans = await _fetch_orphan_app_state_users(known)
        onboarding = await _fetch_onboarding_users(known | set(orphans))
        await bible_edit_message(chat_id, message_id, _admin_users_text(rows, orphans, onboarding), _admin_users_markup(rows, orphans, onboarding))
        return

    if action == "adm_view" and len(parts) == 2:
        try:
            uid = int(parts[1])
        except ValueError:
            await bible_answer_callback(cq_id, "Ошибка данных")
            return
        await bible_answer_callback(cq_id)
        rows = await _fetch_all_progress_rows()
        urows = [r for r in rows if r.get("user_id") == uid]
        await bible_edit_message(chat_id, message_id, _admin_user_detail_text(uid, urows), _admin_user_detail_markup(uid))
        return

    if action == "adm_del" and len(parts) == 2:
        uid_s = parts[1]
        await bible_answer_callback(cq_id)
        await bible_edit_message(
            chat_id, message_id,
            f"⚠️ <b>Точно удалить пользователя {uid_s} целиком?</b>\n\n"
            f"Будут стёрты все его планы, стрики, дни чтения, закладки, "
            f"тема и шрифт — то есть при следующей регистрации он увидит "
            f"стартовый экран, как будто зашёл впервые.\n\n"
            f"Это действие необратимо.",
            {"inline_keyboard": [[
                {"text": "✅ Да, удалить", "callback_data": f"adm_delyes:{uid_s}"},
                {"text": "❌ Отмена", "callback_data": "adm_list"},
            ]]},
        )
        return

    if action == "adm_delyes" and len(parts) == 2:
        try:
            uid = int(parts[1])
        except ValueError:
            await bible_answer_callback(cq_id, "Ошибка данных")
            return
        deleted = await _wipe_user_completely(uid)
        await bible_answer_callback(cq_id, "Удалено ✅")
        note = f"✅ Пользователь <code>{uid}</code> полностью удалён ({deleted} план(ов) удалено, app_state очищен)."
        rows = await _fetch_all_progress_rows()
        known = {r.get("user_id") for r in rows}
        orphans = await _fetch_orphan_app_state_users(known)
        onboarding = await _fetch_onboarding_users(known | set(orphans))
        await bible_edit_message(chat_id, message_id, _admin_users_text(rows, orphans, onboarding, note), _admin_users_markup(rows, orphans, onboarding))
        return

    if action == "adm_obdel" and len(parts) == 2:
        # Удаление онбординг-аккаунта (Старт есть, планов нет): полный сброс
        # bible_accounts-строки. Подтверждение — как у обычного удаления.
        uid_s = parts[1]
        await bible_answer_callback(cq_id)
        await bible_edit_message(
            chat_id, message_id,
            f"⚠️ <b>Сбросить онбординг-аккаунт {uid_s}?</b>\n\n"
            f"У пользователя нет ни плана, ни app_state — будет очищена только "
            f"запись в bible_accounts (Старт, счётчик напоминаний). "
            f"Он снова увидит экран «нажми Старт» при следующем заходе.",
            {"inline_keyboard": [[
                {"text": "✅ Да, сбросить", "callback_data": f"adm_obdelyes:{uid_s}"},
                {"text": "❌ Отмена", "callback_data": "adm_list"},
            ]]},
        )
        return

    if action == "adm_obdelyes" and len(parts) == 2:
        try:
            uid = int(parts[1])
        except ValueError:
            await bible_answer_callback(cq_id, "Ошибка данных")
            return
        await _wipe_onboarding_account(uid)
        await bible_answer_callback(cq_id, "Сброшено ✅")
        note = f"✅ Онбординг-аккаунт <code>{uid}</code> сброшен."
        rows = await _fetch_all_progress_rows()
        known = {r.get("user_id") for r in rows}
        orphans = await _fetch_orphan_app_state_users(known)
        onboarding = await _fetch_onboarding_users(known | set(orphans))
        await bible_edit_message(chat_id, message_id, _admin_users_text(rows, orphans, onboarding, note), _admin_users_markup(rows, orphans, onboarding))
        return

    await bible_answer_callback(cq_id)

# ── Supabase: app_state (единый аккаунт на всех устройствах) ──

async def sb_state_get(user_id: int):
    client = HTTP_CLIENT
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/app_state",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}", "limit": "1"},
    )
    data = r.json()
    return data[0] if isinstance(data, list) and data else None

async def sb_state_upsert(user_id: int, data: dict):
    client = HTTP_CLIENT
    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/app_state?on_conflict=user_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": user_id, "data": data},
    )
    if r.status_code >= 300:
        log.error(f"[sb_state_upsert] FAILED {r.status_code} for {user_id}: {r.text[:300]}")
        raise HTTPException(status_code=503, detail="storage unavailable")
    try:
        rows = r.json()
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception:
        return None

# ── Supabase: bible_accounts (переживает удаление пользователя) ──
# Отдельная от plan_progress/app_state табличка-маячок: одна строка на
# user_id, которая НИКОГДА не удаляется при /users → 🗑, а только меняет
# reset_token. Нужна затем, что plan_progress и app_state после удаления
# исчезают полностью, и клиент не может отличить "я новый пользователь"
# от "меня только что стёрли" — оба случая выглядят как "на сервере
# ничего нет". Смена reset_token — единственный сигнал, который переживает
# сам факт удаления и позволяет клиенту понять, что его localStorage устарел
# и не должен "воскрешать" стёртые данные через syncBackend()/pushState().

async def sb_account_get(user_id: int):
    client = HTTP_CLIENT
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/bible_accounts",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}", "limit": "1"},
    )
    data = r.json()
    return data[0] if isinstance(data, list) and data else None

async def sb_account_ensure(user_id: int) -> dict:
    """Отдаёт строку аккаунта с её reset_token, создавая её при первом
    обращении (Prefer: resolution=merge-duplicates делает это безопасным
    upsert'ом — повторный вызов для уже существующего user_id ничего не
    сломает и не тронет уже сохранённый token благодаря on_conflict)."""
    row = await sb_account_get(user_id)
    if row:
        return row
    client = HTTP_CLIENT
    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/bible_accounts?on_conflict=user_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": user_id},
    )
    try:
        data = r.json()
    except Exception:
        data = None
    return data[0] if isinstance(data, list) and data else {"user_id": user_id, "reset_token": None}

async def sb_account_rotate_reset(user_id: int) -> None:
    """Вызывается ТОЛЬКО из админ-панели /users при полном удалении
    пользователя (_wipe_user_completely). Строка bible_accounts НЕ
    удаляется — только получает новый случайный reset_token И started_at
    сбрасывается в NULL (пользователь снова считается незарегистрированным,
    как будто никогда не жал /start — придётся сделать это заново). Именно
    смена токена (а не сам факт исчезновения app_state) — то, что клиент
    сверяет в checkAccountReset() перед тем, как синхронизировать
    состояние."""
    import uuid
    client = HTTP_CLIENT
    await client.post(
        f"{SUPABASE_URL}/rest/v1/bible_accounts?on_conflict=user_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": user_id, "reset_token": str(uuid.uuid4()), "started_at": None},
    )

async def _remove_plan_from_app_state(user_id: int, plan_id: str) -> None:
    """Хирургически убирает ОДИН план из app_state (локального слепка
    мини-аппа: выбранные планы, их локальный прогресс, настройки, темы,
    закладки), не трогая ничего остального у этого пользователя. Нужен,
    чтобы удаление конкретного плана (через /plan/unregister — как из
    самого мини-аппа, так и из админ-кнопки 🗑) не оставляло его "призрак"
    в app_state: это отдельное хранилище дублирует прогресс внутри
    app_state.data.progress[plan_id] независимо от канонической строки
    в plan_progress, и раньше при удалении только последней план оставался
    видимым (и рабочим лишь наполовину) в самом мини-аппе."""
    row = await sb_state_get(user_id)
    if not row:
        return
    blob = row.get("data") or {}
    changed = False
    if isinstance(blob.get("progress"), dict) and plan_id in blob["progress"]:
        del blob["progress"][plan_id]
        changed = True
    if isinstance(blob.get("customPlans"), dict) and plan_id in blob["customPlans"]:
        del blob["customPlans"][plan_id]
        changed = True
    if blob.get("activePlanId") == plan_id:
        blob["activePlanId"] = None
        changed = True
    if changed:
        await sb_state_upsert(user_id, blob)

# ── Telegram ──────────────────────────────────────────────────

async def bible_send(chat_id: int, text: str, reply_markup: dict = None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    client = HTTP_CLIENT
    r = await client.post(f"{BIBLE_API}/sendMessage", json=payload)
    try:
        return r.json()
    except Exception:
        return {}

async def bible_answer_callback(callback_query_id: str, text: str = None):
    """Гасит "часики" на нажатой инлайн-кнопке. text (если задан) показывается
    пользователю всплывающим тостом поверх экрана, а не отдельным сообщением."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    client = HTTP_CLIENT
    await client.post(f"{BIBLE_API}/answerCallbackQuery", json=payload)

async def bible_edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict = None):
    """Редактирует уже отправленное сообщение на месте — админ-панель
    (список → подтверждение → результат) живёт в одном сообщении, а не
    плодит новые при каждом нажатии."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
    client = HTTP_CLIENT
    await client.post(f"{BIBLE_API}/editMessageText", json=payload)

def _bible_url(plan_id: Optional[str] = None) -> str:
    """Добавляем метку времени к ссылке — иначе Telegram кэширует старую версию
    Mini App и не подгружает свежий index.html после обновлений (подтверждено
    на реальных устройствах: 10.07.2026 разные платформы показывали разные,
    несинхронизированные данные из-за того, что нативные приложения держали
    закэшированную старую версию index.html).

    plan_id (опционально) добавляется как query-параметр — иначе кнопка
    "Открыть план" в напоминании ВСЕГДА открывала мини-апп в его последнем
    локальном состоянии (app.activePlanId из localStorage), поэтому у двух
    напоминаний про разные планы кнопки вели в одно и то же место — в тот
    план, что открывали последним, а не в тот, о котором конкретно это
    напоминание."""
    import time, urllib.parse
    url = f"{BIBLE_PAGES_URL}?v={int(time.time())}"
    if plan_id:
        url += f"&plan={urllib.parse.quote(plan_id)}"
    return url

CHANNEL_BTN_TEXT = f"📻 {CHANNEL_NAME}"
SHARE_BTN_TEXT = "📤 Поделиться с другом"

BIBLE_WELCOME_TEXT = (
    "Привет! Читай Библию по плану — отмечай прочитанное и следи за числом дней подряд 🔥"
    "\n\nВыбери удобный план и начни сегодня:"
)

def bible_share_button() -> dict:
    """Кнопка Bot API у обычной (reply) клавиатуры физически не умеет сама
    открыть системное окно выбора получателя — так устроен сам Telegram:
    reply-кнопки могут быть только текстом, request_contact/location/poll
    или web_app. Настоящее системное окно "кому переслать" открывается
    только по инлайн-кнопке со ссылкой вида t.me/share/url. Поэтому нажатие
    "Поделиться с другом" на клавиатуре (см. bible_start_keyboard) отправляет
    этот текст как обычное сообщение, а бот в ответ (см. bible_webhook)
    присылает отдельное сообщение с ИНЛАЙН-кнопкой на эту ссылку — именно
    её нажатие и раскрывает системный список контактов/чатов.

    Ссылка ведёт в ЛИЧКУ С БОТОМ, а не сразу в Mini App (`.../plan`) — так
    приглашённый друг сначала естественно проходит /start (у него в чате
    появится нативная кнопка «START», плюс наша клавиатурная «Старт»), и
    только потом переходит в приложение через приветственное сообщение.
    Раньше ссылка вела прямо в Mini App, и человек без чата с ботом
    упирался в экран-гейт «Сначала открой бота» — лишний шаг для того, кто
    и так уже переходит по приглашению именно чтобы начать пользоваться."""
    bot_username = BIBLE_BOT_USERNAME.lstrip("@")
    app_link = f"https://t.me/{bot_username}"
    share_text = "📖 Нашёл удобный бот для чтения Библии по плану — с трекером дней подряд и напоминаниями. Попробуй!"
    share_url = f"https://t.me/share/url?url={quote(app_link, safe='')}&text={quote(share_text, safe='')}"
    return {"inline_keyboard": [[
        {"text": "👥 Выбрать, кому отправить", "url": share_url}
    ]]}

def bible_welcome_buttons() -> dict:
    """Инлайн-кнопки, прикреплённые к приветственному сообщению — та же
    структура, что и у кнопок напоминания (bible_reminder_button), для
    единообразия. Пересылаются заново при каждом обращении к боту, поэтому
    web_app-ссылка внутри всегда свежая (см. _bible_url)."""
    return {"inline_keyboard": [[
        {"text": "📖 Открыть план чтения", "web_app": {"url": _bible_url()}},
    ], [
        {"text": CHANNEL_BTN_TEXT, "url": CHANNEL_LINK},
    ]]}

def bible_start_keyboard(registered: bool = False) -> dict:
    """Постоянная клавиатура внизу чата. Для НОВОГО пользователя — «Старт» и
    «Поделиться с другом»: «Старт» — единственный способ создать чат с ботом
    (без него боту физически некуда слать напоминания, см.
    sb_account_mark_started). Для УЖЕ зарегистрированного — «Старт» больше
    не нужен (чат уже есть, started_at заполнен) и только запутывал бы,
    поэтому остаётся одна «Поделиться с другом» (см. bible_share_button())."""
    row = [{"text": SHARE_BTN_TEXT}] if registered else [{"text": "Старт"}, {"text": SHARE_BTN_TEXT}]
    return {
        "keyboard": [row],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def bible_reminder_button(plan_id: str) -> dict:
    """Инлайн-кнопка только для сообщений-напоминаний (не влияет на
    постоянную клавиатуру). Раньше сюда добавлялась ещё и кнопка канала —
    убрали: у ежедневного напоминания должно быть ровно одно чёткое
    действие (вернуться к чтению), а не конкурирующий второй CTA на
    сообщении, которое человек и так видит каждый день — упоминание
    канала теперь только в приветствии и на экране завершения плана,
    где оно уместно и не приедается."""
    return {"inline_keyboard": [[
        {"text": "📖 Открыть план", "web_app": {"url": _bible_url(plan_id)}},
    ]]}

# ── Models ────────────────────────────────────────────────────

def _require_bible_user(user_id: int, init_data: Optional[str]) -> None:
    """Проверяет, что запрос на /plan/*, /state, /account/status
    действительно пришёл от того самого Telegram-пользователя user_id, а не
    просто СОДЕРЖИТ его id в теле запроса. Раньше все эти эндпоинты
    полностью доверяли user_id из тела — Telegram ID не секрет (виден в
    пересланных сообщениях, ссылках, скриншотах), так что кто угодно мог
    подставить чужой id и менять/удалять чужой прогресс чтения. Механизм
    проверки подписи (verify_telegram_init_data, HMAC по WebAppData) уже
    используется для /verify — здесь тот же самый механизм, просто
    подключённый к остальным личным эндпоинтам.

    Если BIBLE_BOT_TOKEN не настроен — мутирующий запрос отклоняется,
    иначе любой клиент сможет подставить чужой user_id."""
    if not BIBLE_BOT_TOKEN:
        log.error(f"_require_bible_user: BIBLE_BOT_TOKEN не настроен — запрос отклонён для user_id={user_id}")
        raise HTTPException(status_code=503, detail="bible bot authentication unavailable")
    if not init_data:
        raise HTTPException(401, "missing init data")
    payload = verify_telegram_init_data(init_data, BIBLE_BOT_TOKEN, max_age_seconds=INIT_DATA_MAX_AGE_SECONDS)
    if payload is None:
        raise HTTPException(401, "invalid init data")
    real_user_id = (payload.get("user") or {}).get("id")
    if not real_user_id or int(real_user_id) != int(user_id):
        raise HTTPException(403, "user_id does not match init data")


def _verify_optional_bible_user(init_data: Optional[str], user_id: int) -> bool:
    """Для read-only эндпоинтов с двумя режимами (/plan/status, /account/status).
    init_data ОТСУТСТВУЕТ → False (публичный режим, минимальный срез данных).
    init_data ЕСТЬ → обязана быть валидной подписью конкретно user_id:
    невалидная/чужая подпись = 401/403, а не тихий откат в публичный режим.
    Валидная → True (полные данные). BIBLE_BOT_TOKEN не настроен — при
    переданной подписи отклоняем (fail-closed), без подписи — публичный режим."""
    if not init_data:
        return False
    if not BIBLE_BOT_TOKEN:
        log.error(f"_verify_optional_bible_user: BIBLE_BOT_TOKEN не настроен — подписанный запрос отклонён (user_id={user_id})")
        raise HTTPException(status_code=503, detail="bible bot authentication unavailable")
    payload = verify_telegram_init_data(init_data, BIBLE_BOT_TOKEN, max_age_seconds=INIT_DATA_MAX_AGE_SECONDS)
    if payload is None:
        raise HTTPException(401, "invalid init data")
    real_user_id = (payload.get("user") or {}).get("id")
    if not real_user_id or int(real_user_id) != int(user_id):
        raise HTTPException(403, "user_id does not match init data")
    return True

# Разумный верхний предел дней плана: все системные планы ≤ 365 дней,
# хроно-конструктор НЗ принимает до 1000. 1500 — с запасом, чтобы не
# отсечь ни один живой сценарий, но отвесить мусорные списки на 10⁵ элементов.
_DAYS_MAX_LEN = 1500


def _clean_days_done(raw) -> list:
    """Чистит присланный клиентом список дней: только целые 1.._DAYS_MAX_LEN,
    без дублей, лимит длины. Раньше days_done был list без ограничений —
    раздутый список уходил в plan_progress и раздувал строку/ответы."""
    out = []
    for d in (raw or [])[:_DAYS_MAX_LEN]:
        try:
            di = int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= di <= _DAYS_MAX_LEN and di not in out:
            out.append(di)
    return sorted(out)


class BibleRegisterBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    title: Optional[str] = Field(default=None, max_length=120)
    notify_hour_msk: int = Field(default=8, ge=0, le=23)
    notify_minute_msk: int = Field(default=0, ge=0, le=59)
    notify_on: bool = True
    init_data: Optional[str] = None

class BibleReadBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    day_number: int = Field(ge=1, le=_DAYS_MAX_LEN)
    local_date: Optional[str] = None  # YYYY-MM-DD по локальному времени пользователя
    init_data: Optional[str] = None

class BibleSettingsBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    notify_hour_msk: int = Field(ge=0, le=23)
    notify_minute_msk: int = Field(default=0, ge=0, le=59)
    notify_on: bool
    init_data: Optional[str] = None

class BibleMergeDaysBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    days_done: list
    title: Optional[str] = Field(default=None, max_length=120)
    init_data: Optional[str] = None

class BibleSetDaysBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    days_done: list
    init_data: Optional[str] = None

class BibleUnregisterBody(BaseModel):
    user_id: int
    plan_id: str = Field(max_length=64)
    init_data: Optional[str] = None

class StateBody(BaseModel):
    user_id: int
    data: dict
    # Метка серверной записи, от которой пишем клиент (opt-in 409-защита от
    # last-write-wins между двумя устройствами). None — старые клиенты.
    base_updated_at: Optional[str] = Field(default=None, max_length=64)
    init_data: Optional[str] = None

# ── Endpoints: планы чтения ────────────────────────────────────
# Все ниже — теперь на уровне (user_id, plan_id): пользователь может вести
# несколько планов одновременно, каждый со своим прогрессом, стриком и
# собственным временем напоминания.

@app.post("/plan/register")
@limiter.limit("30/minute")
async def bible_register(request: Request, body: BibleRegisterBody):
    _require_bible_user(body.user_id, body.init_data)
    # Аккаунт создаётся ТОЛЬКО мутациями с валидной подписью (и вебхуком
    # «Старт») — GET /account/status с этой ревизии строк больше не создаёт.
    await sb_account_ensure(body.user_id)
    payload = {
        "user_id": body.user_id,
        "plan_id": body.plan_id,
        # По МСК, а не date.today() сервера (UTC): иначе регистрация между
        # 00:00 и 03:00 МСК давала вчерашний start_date — первое же
        # напоминание говорило «отстаёшь на 1 день» только что зарегистрировавшемуся.
        "start_date": datetime.now(MSK).date().isoformat(),
        "notify_hour_msk": body.notify_hour_msk,
        "notify_minute_msk": body.notify_minute_msk,
        "notify_on": body.notify_on,
        "streak": 0, "max_streak": 0,
        "last_read_date": None, "days_done": [],
    }
    if body.title:
        payload["title"] = body.title
    # КРИТИЧНО: merge-duplicates upsert перезаписал бы ВСЕ поля. Если клиент
    # повторно вызывает register для уже зарегистрированного плана (reconnect,
    # гонка двух вкладок), прогресс пользователя молча обнулился бы.
    # Для существующего плана обновляем только настройки уведомлений.
    existing = await sb_get_one(body.user_id, body.plan_id)
    if existing:
        payload = {k: v for k, v in payload.items()
                   if k in ("user_id", "plan_id", "title",
                            "notify_hour_msk", "notify_minute_msk", "notify_on")}
    ok, err = await sb_upsert(payload)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True}

def _public_plan_row(row: dict) -> dict:
    """Срез строки plan_progress для НЕподписанных запросов (виджет «План»
    в радио). Остаётся только то, что виджет реально рисует: стрик, рекорд,
    даты, число прочитанных дней и серверный лаг. НЕ отдаётся: days_done[]
    (полный список дней — он и есть IDOR-утечка), notify_* (время напоминаний),
    last_reminder_sent_at. Лаг считает сервер тем же _days_behind, что и в
    напоминаниях, — числа в виджете и в боте гарантированно совпадают."""
    return {
        "plan_id": row.get("plan_id"),
        "title": row.get("title"),
        "streak": row.get("streak", 0),
        "max_streak": row.get("max_streak", 0),
        "last_read_date": row.get("last_read_date"),
        "start_date": row.get("start_date"),
        "days_count": len(row.get("days_done") or []),
        "lag": _days_behind(row),
    }


@app.get("/plan/status")
@limiter.limit("120/minute")
async def bible_status(request: Request, user_id: int, plan_id: Optional[str] = None,
                       init_data: Optional[str] = None):
    """Один эндпоинт, два режима:

    ПОДПИСАННЫЙ (мини-апп плана; initData бота «План чтения» в заголовке
    X-Telegram-Init-Data или query) — полный объект плана как раньше,
    включая days_done и notify_*.

    БЕЗ подписи (виджет «План» в радио — initData другого бота, подписать
    не может физически) — только публичный срез _public_plan_row.
    Раньше без подписи отдавался полный days_done[] и notify_* — любой,
    кто знает чужой Telegram ID, мог читать чужой прогресс целиком (IDOR).

    Инициализация init_data: сначала заголовок, потом query (query оставлен
    на один релиз для обратной совместимости старых фронтов, потом убрать)."""
    signed = _verify_optional_bible_user(
        request.headers.get("x-telegram-init-data") or init_data, user_id)
    if plan_id:
        row = await sb_get_one(user_id, plan_id)
        if not row:
            return {"registered": False}
        if signed:
            return {"registered": True, **row}
        return {"registered": True, **_public_plan_row(row)}
    rows = await sb_get_all(user_id)
    if signed:
        return {"registered": bool(rows), "plans": rows}
    return {"registered": bool(rows), "plans": [_public_plan_row(r) for r in rows]}

@app.post("/plan/read")
@limiter.limit("60/minute")
async def bible_read(request: Request, body: BibleReadBody):
    _require_bible_user(body.user_id, body.init_data)
    from datetime import date, timedelta
    row = await sb_get_one(body.user_id, body.plan_id)
    if not row:
        return {"ok": False, "error": "not registered"}
    # Доверяем локальной дате клиента, а не часовому поясу сервера (Render/UTC) —
    # иначе чтение поздно вечером/рано утром по местному времени пользователя
    # могло попасть не на тот "день" и сломать стрик без реальной причины.
    try:
        today = date.fromisoformat(body.local_date).isoformat() if body.local_date else date.today().isoformat()
    except ValueError:
        today = date.today().isoformat()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    last = row.get("last_read_date")
    if last == today and body.day_number in (row.get("days_done") or []):
        return {"ok": True, "streak": row["streak"], "already": True}
    streak = row.get("streak", 0)
    if last == yesterday:
        streak += 1
    elif last != today:
        streak = 1
    max_streak = max(row.get("max_streak", 0), streak)
    days_done = list(set((row.get("days_done") or []) + [body.day_number]))
    await sb_patch(body.user_id, body.plan_id, {
        "streak": streak, "max_streak": max_streak,
        "last_read_date": today, "days_done": days_done,
    })
    return {"ok": True, "streak": streak, "max_streak": max_streak}

async def _delete_progress_row(user_id: int, plan_id: str) -> tuple:
    """Удаляет строку прогресса (user_id, plan_id) из plan_progress и
    ПРОВЕРЯЕТ, что она реально исчезла, а не просто отправляет запрос и
    надеется на лучшее. Возвращает (ok, deleted_count). Раньше это было
    зашито прямо в /plan/unregister и всегда молча врало об успехе — теперь
    общая функция, которой пользуется и HTTP-эндпоинт, и админ-панель
    (/users), так что поведение гарантированно одинаковое в обоих местах."""
    existing = await sb_get_one(user_id, plan_id)
    if not existing:
        return True, 0
    client = HTTP_CLIENT
    r = await client.delete(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        params={"user_id": f"eq.{user_id}", "plan_id": f"eq.{plan_id}"},
    )
    try:
        deleted_rows = r.json() if r.status_code in (200, 204) else []
    except Exception:
        deleted_rows = []
    if not isinstance(deleted_rows, list):
        deleted_rows = []
    ok = bool(deleted_rows)
    if not ok:
        log.error(
            f"_delete_progress_row: DELETE не удалил строку user={user_id} "
            f"plan={plan_id} — status={r.status_code}, body={r.text[:300]!r}. "
            f"Возможно, в Supabase включён RLS без политики DELETE для plan_progress."
        )
    if ok:
        # Убираем этот же план и из app_state — иначе он остаётся "призраком"
        # в самом мини-аппе: видимым, но без канонической строки прогресса
        # (не отмечается прочитанным, не шлёт уведомления).
        await _remove_plan_from_app_state(user_id, plan_id)
    return ok, len(deleted_rows)


@app.post("/plan/unread")
@limiter.limit("30/minute")
async def bible_unread(request: Request, body: BibleReadBody):
    """Отмена отметки "Прочитал" (undo в мини-аппе). Убирает день из days_done
    и пересчитывает стрик по оставшимся дням: если после удаления дня не осталось
    ни одного прочитанного с датой today - стрик откатывается к значению до
    сегодняшнего чтения. Безопасно: не удаляет строку прогресса, только правит поля."""
    _require_bible_user(body.user_id, body.init_data)
    row = await sb_get_one(body.user_id, body.plan_id)
    if not row:
        return {"ok": False, "error": "not registered"}
    from datetime import date
    try:
        today = date.fromisoformat(body.local_date).isoformat() if body.local_date else date.today().isoformat()
    except ValueError:
        today = date.today().isoformat()
    days_done = [d for d in (row.get("days_done") or []) if d != body.day_number]
    if len(days_done) == len(row.get("days_done") or []):
        return {"ok": True, "already": True}
    # Пересчёт стрика: если today больше не в days_done - чтение сегодня отменено.
    # Стрик откатываем: убираем эффект сегодняшнего чтения.
    streak = row.get("streak", 0)
    last = row.get("last_read_date")
    patch = {"days_done": days_done}
    if last == today and body.day_number in (row.get("days_done") or []):
        # Сегодняшнее чтение отменено: восстанавливаем last_read_date как "вчера",
        # а стрик уменьшаем на 1 (не ниже 0). Точное восстановление предыдущего
        # значения невозможно без истории - это честный компромисс.
        from datetime import timedelta
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        patch["last_read_date"] = yesterday
        patch["streak"] = max(0, streak - 1)
        patch["max_streak"] = row.get("max_streak", 0)
    await sb_patch(body.user_id, body.plan_id, patch)
    return {"ok": True, "streak": patch.get("streak", streak), "days_done": days_done}


@app.post("/plan/unregister")
@limiter.limit("30/minute")
async def bible_unregister(request: Request, body: BibleUnregisterBody):
    _require_bible_user(body.user_id, body.init_data)
    """Полностью удаляет строку прогресса КОНКРЕТНОГО плана из plan_progress
    (остальные планы пользователя, если есть, не затрагиваются). Нужен,
    потому что удаление плана в самом мини-аппе (delPlan()) раньше чистило
    только локальное состояние (app_state) — канонические данные на сервере
    (streak, days_done и т.д.) оставались нетронутыми, из-за чего виджет
    "План" и push-уведомления продолжали показывать прогресс уже удалённого
    плана.

    ВАЖНО: раньше этот эндпоинт всегда возвращал {"ok": true}, даже если
    DELETE фактически не удалил ни одной строки (например, если в Supabase
    включён Row Level Security без политики на DELETE — тогда запрос
    отрабатывает без ошибки, но удаляет 0 строк). Из-за этого клиентский
    retry/toast при сбое никогда не срабатывал: сервер врал об успехе.
    Теперь мы явно проверяем, что строка (а) существовала и (б) реально
    исчезла после запроса."""
    ok, deleted = await _delete_progress_row(body.user_id, body.plan_id)
    return {"ok": ok, "deleted": deleted}

@app.post("/plan/merge_days")
@limiter.limit("30/minute")
async def bible_merge_days(request: Request, body: BibleMergeDaysBody):
    _require_bible_user(body.user_id, body.init_data)
    """Объединяет присланный клиентом список прочитанных дней с тем, что
    уже есть на сервере — и только это. Нужен, потому что days_done
    хранится в ДВУХ независимых местах (canonical plan_progress, которым
    пользуются виджет "План" и уведомления, и слепок app_state, которым
    пользуется сам мини-апп плана) — и если запрос /plan/read когда-то не
    долетел до сервера (обрыв сети сразу после нажатия "Прочитал"), эти два
    списка расходятся, и лаг/отставание в разных местах интерфейса
    показывает разные числа. Стрик и last_read_date здесь НЕ трогаем —
    это отдельная, более тонкая логика (см. /plan/read), которую не стоит
    задним числом пересчитывать при простом объединении списков дней."""
    row = await sb_get_one(body.user_id, body.plan_id)
    if not row:
        return {"ok": False, "error": "not registered"}
    client_days = _clean_days_done(body.days_done)
    merged = sorted(set((row.get("days_done") or [])) | set(client_days))
    patch = {}
    if merged != sorted(row.get("days_done") or []):
        patch["days_done"] = merged
    # Самовосстановление title: этот эндпоинт вызывается при КАЖДОМ открытии
    # мини-аппа для каждого плана пользователя — надёжная точка, чтобы
    # дозаполнить title у планов, зарегистрированных ещё до того, как сервер
    # научился его сохранять (раньше title обновлялся только при /plan/register,
    # то есть один раз при создании плана, и никогда — при обычном чтении).
    if body.title and body.title != row.get("title"):
        patch["title"] = body.title
    if patch:
        await sb_patch(body.user_id, body.plan_id, patch)
    return {"ok": True, "days_done": merged}

@app.post("/plan/set_days")
@limiter.limit("30/minute")
async def bible_set_days(request: Request, body: BibleSetDaysBody):
    _require_bible_user(body.user_id, body.init_data)
    """В отличие от /plan/merge_days — не объединяет, а ПЕРЕЗАПИСЫВАЕТ
    список прочитанных дней ровно тем, что прислали. Нужен для редактирования
    своего плана ("Составить свой план"): если пользователь уменьшает
    длительность плана, дни за пределами нового графика должны реально
    пропасть из прогресса, а не остаться висеть через union."""
    row = await sb_get_one(body.user_id, body.plan_id)
    if not row:
        return {"ok": False, "error": "not registered"}
    cleaned = _clean_days_done(body.days_done)
    await sb_patch(body.user_id, body.plan_id, {"days_done": cleaned})
    return {"ok": True, "days_done": cleaned}

@app.post("/plan/settings")
@limiter.limit("30/minute")
async def bible_settings(request: Request, body: BibleSettingsBody):
    _require_bible_user(body.user_id, body.init_data)
    await sb_patch(body.user_id, body.plan_id, {
        "notify_hour_msk": body.notify_hour_msk,
        "notify_minute_msk": body.notify_minute_msk,
        "notify_on": body.notify_on,
    })
    return {"ok": True}

# ── Endpoints: единый аккаунт (полное состояние приложения) ──

@app.get("/account/status")
@limiter.limit("120/minute")
async def account_status(request: Request, user_id: int, init_data: Optional[str] = None):
    """Клиент дергает это ПЕРЕД синхронизацией app_state (см. checkAccountReset
    в index.html) — а виджет "План" в Оглавлении/Radio дёргает это же для
    экрана-гейта "сначала зарегистрируйся в боте".

    ДВА режима (как у /plan/status):
    • БЕЗ подписи (виджет радио) — только {"registered": bool}, больше ничего.
    • С валидной подписью бота «План чтения» (мини-апп плана) — плюс
      reset_token, который клиент сверяет для детекта админ-сброса аккаунта.

    ГЛАВНОЕ изменение: строка аккаунта больше НЕ создаётся на GET. Раньше
    здесь звался sb_account_ensure, и ЛЮБОЙ вызов ?user_id=123 (в том числе
    перебор чужих ID) тихо создавал мусорные строки в bible_accounts со
    свежим reset_token. Теперь GET только читает: нет строки →
    {"registered": false, "reset_token": None}. Создание строки осталось
    ровно в двух легитимных местах: вебхук «Старт» (sb_account_mark_started)
    и первая мутация с валидной подписью (/plan/register)."""
    signed = _verify_optional_bible_user(
        request.headers.get("x-telegram-init-data") or init_data, user_id)
    row = await sb_account_get(user_id)
    result = {"registered": bool(row and row.get("started_at"))}
    if signed:
        result["reset_token"] = row.get("reset_token") if row else None
    return result

@app.get("/state")
async def get_state(request: Request, user_id: int, init_data: Optional[str] = None):
    # init_data теперь принимается и заголовком X-Telegram-Init-Data (query
    # оставлен как fallback на переходный период) — initData в URL попадал
    # в логи Render/прокси и Referer.
    _require_bible_user(user_id, request.headers.get("x-telegram-init-data") or init_data)
    row = await sb_state_get(user_id)
    if not row:
        return {"exists": False}
    return {"exists": True, "data": row.get("data"), "updated_at": row.get("updated_at")}

@app.post("/state")
@limiter.limit("30/minute")
async def save_state(request: Request, body: StateBody):
    _require_bible_user(body.user_id, body.init_data)
    # Лимит размера слепка: customPlans хранят полные расписания (хроно-НЗ
    # на 1000 дней — это сотни КБ), поэтому порог большой — 1 МБ; он ловит
    # только намеренно раздутые/garbage-пейлоады, а не живые состояния.
    try:
        body_size = len(json.dumps(body.data, ensure_ascii=False))
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "state not serializable"})
    if body_size > 1_000_000:
        return JSONResponse(status_code=413, content={"ok": False, "error": "state too large", "size": body_size})
    # Конфликтная защита last-write-wins (opt-in): если клиент прислал
    # base_updated_at, а на сервере с тех пор уже лежит более свежая запись
    # (другое устройство успело записать) — отвечаем 409 и НЕ затираем.
    # Старые клиенты base_updated_at не шлют — для них поведение прежнее.
    if body.base_updated_at:
        current = await sb_state_get(body.user_id)
        cur_updated = (current or {}).get("updated_at")
        if current and cur_updated and cur_updated != body.base_updated_at:
            return JSONResponse(status_code=409, content={
                "ok": False, "error": "conflict",
                "updated_at": cur_updated,
                "hint": "перезалей состояние сервера через GET /state и повтори при необходимости",
            })
    row = await sb_state_upsert(body.user_id, body.data)
    # updated_at возвращаем клиенту: тот держит метку для следующей
    # 409-проверки (opt-in last-write-wins защиты) без лишнего GET.
    return {"ok": True, "updated_at": (row or {}).get("updated_at")}

# ── Webhook ───────────────────────────────────────────────────

async def sb_account_mark_started(user_id: int) -> None:
    """Отмечает, что пользователь ДЕЙСТВИТЕЛЬНО открыл чат с ботом (а не
    просто зашёл в Mini App по прямой ссылке). Bot API не позволяет боту
    написать первым тому, кто с ним никогда не переписывался — открытие
    Mini App по t.me/bot/short?startapp= само по себе чат не создаёт.
    На Android Telegram, судя по всему, неявно шлёт скрытый /start при
    первом заходе в такую ссылку — на iOS это не срабатывает надёжно
    (совпадает с уже известным багом баннера Safari), из-за чего
    напоминаниям было физически некуда приходить. sb_upsert с
    merge-duplicates не трогает уже выставленный started_at повторными
    вызовами (это ЛЮБОЕ сообщение в чат, не только /start)."""
    client = HTTP_CLIENT
    existing = await client.get(
        f"{SUPABASE_URL}/rest/v1/bible_accounts",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}", "select": "started_at", "limit": "1"},
    )
    try:
        rows = existing.json()
    except Exception:
        rows = []
    if rows and rows[0].get("started_at"):
        return
    client = HTTP_CLIENT
    await client.post(
        f"{SUPABASE_URL}/rest/v1/bible_accounts?on_conflict=user_id",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"user_id": user_id, "started_at": datetime.now(timezone.utc).isoformat()},
    )

@app.post("/webhook/bible")
async def bible_webhook(request: Request):
    if not _verify_webhook_secret(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid secret token"})
    try:
        data = await request.json()
    except Exception:
        return {"ok": False}

    # Нажатия инлайн-кнопок (админ-панель /users) приходят отдельным типом
    # апдейта callback_query, а не message — раньше вебхук их вообще не
    # обрабатывал.
    callback = data.get("callback_query")
    if callback:
        await _handle_admin_callback(callback)
        return {"ok": True}

    message = data.get("message", {})
    if not message:
        return {"ok": True}
    # Только личные сообщения: если бота добавят в группу, без этого фильтра
    # бот отвечал бы приветствием на КАЖДОЕ сообщение группы и создавал
    # мусорные строки в bible_accounts с chat_id группы.
    if (message.get("chat") or {}).get("type") != "private":
        return {"ok": True}
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return {"ok": True}

    # /stats и /users — только для владельца/админа бота (BIBLE_ADMIN_USER_IDS),
    # обычным пользователям недоступны и никак не отражаются в их сценарии
    # приветствия.
    sender_id = (message.get("from") or {}).get("id")
    text = (message.get("text") or "").strip()
    if text == "/stats":
        if sender_id in BIBLE_ADMIN_USER_IDS:
            if not SUPABASE_URL:
                await bible_send(chat_id, "SUPABASE_URL не настроен — статистика недоступна.")
            else:
                s = await bible_collect_stats()
                await bible_send(
                    chat_id,
                    "📊 <b>Статистика «План чтения Библии»</b>\n\n"
                    f"👥 Всего зарегистрировано: <b>{s['total']}</b>\n"
                    f"🔥 Читали сегодня: <b>{s['active_today']}</b>\n"
                    f"📅 Читали за последние 7 дней: <b>{s['active_7d']}</b>\n"
                    f"🔔 С включёнными напоминаниями: <b>{s['notify_enabled']}</b>",
                )
        return {"ok": True}

    if text == "/users":
        if sender_id in BIBLE_ADMIN_USER_IDS:
            if not SUPABASE_URL:
                await bible_send(chat_id, "SUPABASE_URL не настроен.")
            else:
                rows = await _fetch_all_progress_rows()
                known = {r.get("user_id") for r in rows}
                orphans = await _fetch_orphan_app_state_users(known)
                onboarding = await _fetch_onboarding_users(known | set(orphans))
                await bible_send(chat_id, _admin_users_text(rows, orphans, onboarding), _admin_users_markup(rows, orphans, onboarding))
        return {"ok": True}

    if text == SHARE_BTN_TEXT:
        await bible_send(
            chat_id,
            "Отправь другу ссылку на бота одним нажатием:",
            bible_share_button(),
        )
        return {"ok": True}

    # Любое остальное сообщение в личке с ботом — будь то /start, нажатие
    # постоянной кнопки «Старт» или вообще что угодно ещё — приводит к одному
    # и тому же результату. Пользователю не нужно разбираться в командах
    # Telegram: что бы он ни отправил, он снова увидит приветствие с кнопками.
    #
    # Bot API не позволяет совместить инлайн-кнопки и постоянную клавиатуру
    # в ОДНОМ сообщении (reply_markup бывает только одного типа), поэтому
    # используются два быстрых сообщения подряд:
    #   1) приветствие с двумя инлайн-кнопками, прикреплёнными именно к нему
    #      (bible_welcome_buttons) — «Открыть план чтения» и «канал»;
    #   2) короткая реплика, которая (пере)устанавливает постоянную
    #      клавиатуру внизу — а на ней теперь только «Старт», без «Открыть
    #      план чтения», чтобы низ экрана не был перманентно занят.
    existing_account = await sb_account_get(chat_id)
    already_registered = bool(existing_account and existing_account.get("started_at"))
    await bible_send(chat_id, BIBLE_WELCOME_TEXT, bible_welcome_buttons())
    await bible_send(chat_id, "🙏", bible_start_keyboard(registered=already_registered))
    await sb_account_mark_started(chat_id)
    return {"ok": True}

@app.get("/bible/status")
async def bible_health():
    return {"ok": True, "bible_bot": BIBLE_BOT_USERNAME,
            "supabase": bool(SUPABASE_URL), "pages": BIBLE_PAGES_URL}

# ── Scheduler: проверяем каждую минуту, кому пора напомнить ────
# Раньше cron срабатывал только minute=0 (раз в час), а notify_hour_msk
# хранил лишь час — точное время вроде 8:37 негде было даже сохранить,
# фактически всегда округлялось до ближайшего часа. Теперь сверяем и
# час, и минуту (notify_minute_msk) — напоминание уходит ровно в
# заданную пользователем минуту. Нагрузка на Supabase минимальна:
# запрос лёгкий (условие по двум точным полям), таблица некрупная.

def _ru_day_word(n: int) -> str:
    """Склонение 'день/дня/дней' для целого n (n предполагается >= 0)."""
    if 11 <= n % 100 <= 19:
        return "дней"
    r = n % 10
    if r == 1:
        return "день"
    if 2 <= r <= 4:
        return "дня"
    return "дней"


# Названия системных планов (для сообщений напоминаний) — должны совпадать
# с id/title из массива PLANS в index.html (репозиторий bible-reading-bot).
# Сама разметка расписания (schedule) там не нужна — только человекочитаемое
# имя, чтобы при нескольких одновременных планах пользователь понимал, о
# каком из них уведомление.
PLAN_TITLES = {
    "nt_90": "Новый Завет за 90 дней",
    "chrono_365": "Хронологический план за год",
    "bible_365": "Библия за год",
}

def _plan_title(u) -> str:
    """u может быть строкой (голый plan_id, для обратной совместимости) или
    целой строкой из plan_progress (dict с полем title). Настоящее название
    плана, которое ввёл сам пользователь (или сгенерировал мини-апп для
    хронологического/своего плана), сохраняется в БД при регистрации и
    ВСЕГДА в приоритете — иначе для любого не-системного плана и здесь, и
    в виджете "План" в мини-аппе Радио, оставалось бы обезличенное
    "Свой план", даже если у пользователя их несколько одновременно и они
    совсем не похожи друг на друга."""
    if isinstance(u, dict):
        if u.get("title"):
            # title — свободный текст от клиента (/plan/register), а не
            # системная константа. Без экранирования сюда можно вписать
            # HTML-теги, которые отрисуются как настоящая разметка/ссылка и
            # в напоминании самому пользователю, и, что важнее, в панели
            # /users, которую видит администратор (parse_mode=HTML везде).
            return _html.escape(u["title"])
        plan_id = u.get("plan_id", "")
    else:
        plan_id = u or ""
    if plan_id in PLAN_TITLES:
        return PLAN_TITLES[plan_id]
    if plan_id and plan_id.startswith("c"):
        return "Свой план"
    return "план чтения"


def _next_unread_day(calendar_day: int, days_done) -> int:
    """Тот же алгоритм, что и nextUnreadDay() в мини-аппе: первый день от 1
    до calendar_day, которого нет среди прочитанных. Раньше сервер считал
    отставание совсем по другой формуле (см. ниже), из-за чего число в
    уведомлении и число в мини-аппе могли не совпадать при любых "дырах"
    в днях (например, если день был отмечен прочитанным не по порядку)."""
    done = set(days_done or [])
    for d in range(1, calendar_day + 1):
        if d not in done:
            return d
    return calendar_day


def _days_behind(u: dict) -> int:
    """Сколько дней пользователь отстаёт от графика — используем ТОТ ЖЕ
    подход, что и мини-апп (nextUnreadDay), а не отдельную "агрегатную"
    формулу (прошедшие дни минус общее число прочитанных). Прежняя формула
    calendar_day - len(days_done) давала другое число, чем мини-апп, как
    только дни читались не строго по порядку — а после активного
    тестирования это почти гарантированно так. Также "сегодня" теперь
    считается по МСК (как и остальная система напоминаний/cron), а не по
    часовому поясу сервера (обычно UTC на Render), чтобы не расходиться
    с реальным календарным днём около полуночи."""
    start_raw = u.get("start_date")
    if not start_raw:
        return 0
    from datetime import date
    try:
        start = date.fromisoformat(start_raw)
    except ValueError:
        return 0
    calendar_day = (datetime.now(MSK).date() - start).days + 1
    landing = _next_unread_day(calendar_day, u.get("days_done"))
    return max(0, calendar_day - landing)


async def _claim_reminder_slot(user_id: int, plan_id: str, now_iso: str, cutoff_iso: str) -> bool:
    """Атомарно 'застолбить' право отправить именно ЭТОМУ пользователю
    напоминание именно в эту минуту — проверка на уровне БД, а не только в
    памяти процесса. max_instances=1 у APScheduler уже не даёт ОДНОМУ и тому
    же процессу запустить пересекающийся забег, но если Render-сервис
    когда-нибудь запустят с несколькими воркерами (--workers N в Procfile),
    у каждого будет свой независимый планировщик — без проверки на уровне
    БД все они одновременно отправили бы одно и то же напоминание.
    Возвращает True, если именно этот вызов успел "застолбить" слот (можно
    слать), False — если кто-то другой уже сделал это в течение последней
    минуты (пропускаем, не дублируем)."""
    client = HTTP_CLIENT
    r = await client.patch(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        params={
            "user_id": f"eq.{user_id}",
            "plan_id": f"eq.{plan_id}",
            "or": f"(last_reminder_sent_at.is.null,last_reminder_sent_at.lt.{cutoff_iso})",
        },
        json={"last_reminder_sent_at": now_iso},
    )
    try:
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []
    return bool(rows)

async def _disable_reminders_for_blocked_user(user_id: int) -> None:
    """Telegram ответил 403 (бот заблокирован или пользователь удалил
    аккаунт) — дальше пытаться слать этому user_id ежедневные напоминания
    бессмысленно и просто впустую тратит вызовы API, а сам пользователь
    годами висел бы в /users как "активный", хотя давно недоступен.
    Отключаем notify_on для ВСЕХ его планов разом — прогресс и стрик не
    трогаем, только останавливаем попытки писать. Если человек когда-нибудь
    разблокирует бота, он сможет включить напоминания заново как обычно."""
    client = HTTP_CLIENT
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/plan_progress",
        headers=SB_HEADERS,
        params={"user_id": f"eq.{user_id}"},
        json={"notify_on": False},
    )
    log.info(f"bible: user_id={user_id} — 403 от Telegram (бот заблокирован), напоминания отключены для всех его планов")

@bible_scheduler.scheduled_job("cron", minute="*", max_instances=1,
                               misfire_grace_time=30, coalesce=True)
async def bible_send_reminders():
    if not SUPABASE_URL or not BIBLE_BOT_TOKEN:
        return
    now_msk = datetime.now(MSK)
    now_iso = now_msk.isoformat()
    cutoff_iso = (now_msk - timedelta(seconds=55)).isoformat()
    users = await sb_get_due(now_msk.hour, now_msk.minute)
    blocked_this_run = set()  # не бить по Supabase повторно, если у юзера несколько планов на одну минуту
    for u in users:
      # Сбой на ОДНОМ пользователе (таймаут Supabase, битая строка) не должен
      # прерывать рассылку всем остальным — иначе вся минута напоминаний
      # теряется целиком.
      try:
        claimed = await _claim_reminder_slot(u["user_id"], u["plan_id"], now_iso, cutoff_iso)
        if not claimed:
            continue
        streak = u.get("streak", 0)
        lag = _days_behind(u)
        title = _plan_title(u)
        days_done_n = len(u.get("days_done") or [])
        if days_done_n == 0:
            # План выбран, но не прочитано НИ ОДНОГО дня. Раньше такие
            # получали обычное "время читать" (lag=0, streak=0) — и это
            # выглядело как ошибка системы ("я же ещё не начинал!").
            # Теперь — мягкая онбординг-цепочка: день 1 после старта,
            # затем каждые 2 дня, пока человек не начнёт. Тексты дружелюбные,
            # без давления: цель — побудить к ПЕРВОМУ шагу.
            age_days = _plan_age_days(u)
            if age_days <= 0:
                continue  # план создан сегодня — ещё рано напоминать
            if age_days == 1:
                body = (f"🌱 «{title}»: план готов и ждёт тебя!\n"
                        f"Первый день — самый лёгкий. Открой план и прочитай первый отрывок — это займёт пару минут.")
            elif age_days <= 3:
                body = (f"📖 «{title}»: ты выбрал план, но ещё не начал.\n"
                        f"Начни с малого — один отрывок сегодня, и дальше пойдёт легче!")
            else:
                body = (f"⏳ «{title}»: ждёт тебя уже {age_days} {_ru_day_word(age_days)}.\n"
                        f"Не обязательно догонять график — просто открой и прочитай один отрывок сегодня.")
        elif lag > 0:
            word = _ru_day_word(lag)
            body = (
                f"⚠️ «{title}»: отстаёшь на {lag} {word} от плана чтения.\n"
                f"Не переживай — пропущенные дни никуда не делись. "
                f"Открой план и наверстай сразу несколько дней подряд!"
            )
        else:
            streak_text = f"🔥 {streak} дней подряд" if streak > 0 else "Начни сегодня!"
            body = f"📅 «{title}»: время читать Библию\n{streak_text}"
        result = await bible_send(
            u["user_id"],
            body,
            bible_reminder_button(u["plan_id"]),
        )
        if result.get("error_code") == 403 and u["user_id"] not in blocked_this_run:
            blocked_this_run.add(u["user_id"])
            await _disable_reminders_for_blocked_user(u["user_id"])
      except Exception as e:
        log.error(f"reminder failed for {u.get('user_id')}/{u.get('plan_id')}: {e}")
    # Онбординг-напоминания тем, кто нажал «Старт», но так и не выбрал план.
    await bible_send_onboarding_nudges(now_msk)


def _plan_age_days(u: dict) -> int:
    """Сколько полных дней прошло с создания плана (start_date)."""
    start_raw = u.get("start_date")
    if not start_raw:
        return 0
    from datetime import date
    try:
        return (datetime.now(MSK).date() - date.fromisoformat(start_raw)).days
    except ValueError:
        return 0


# ── Онбординг-напоминания: «Старт» есть, плана нет ───────────────────────
# Человек нажал «Старт» в боте (started_at заполнен), но plan_progress для
# него пуст — напоминать о чтении нечего, а следующий шаг (выбор плана)
# сам по себе не случится. Мягкая цепочка: день 2, затем каждые 3 дня,
# максимум 5 сообщений — ненавязчиво, но последовательно ведём к действию.
ONBOARDING_MAX_NUDGES = 5

def _as_aware(dt: datetime) -> datetime:
    """Supabase может вернуть timestamp БЕЗ таймзоны (если колонка не
    timestamptz) — наивный datetime при сравнении с aware now_msk бросает
    TypeError ВНЕ try-блока и роняет весь минутный cron: напоминания
    перестают уходить ВСЕМ. Нормализуем к aware (UTC по умолчанию)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

async def bible_send_onboarding_nudges(now_msk) -> None:
    # Пагинация вместо limit=500: PostgREST молча обрезает выборку до 1000
    # строк — на росте «хвост» пользователей переставал получать онбординг.
    accounts = await _sb_fetch_all("bible_accounts", {
        "started_at": "not.is.null",
        # ВАЖНО: onboarding_last_at обязан быть в select — без него
        # a.get("onboarding_last_at") всегда None, интервал «каждые
        # 3 дня» не работал бы, и после 3-го дня вся цепочка ушла бы
        # минута за минутой.
        "select": "user_id,started_at,onboarding_nudges,onboarding_last_at",
    })
    if not accounts:
        return
    # user_id тех, у кого УЖЕ есть хотя бы один план — их исключаем.
    # select=user_id лёгкий, но строк может быть больше 10000 — тоже пагинируем.
    with_plan = set()
    for row in await _sb_fetch_all("plan_progress", {"select": "user_id"}):
        with_plan.add(row.get("user_id"))
    today = now_msk.date()
    for a in accounts:
        uid = a.get("user_id")
        if uid in with_plan:
            continue
        sent_n = int(a.get("onboarding_nudges") or 0)
        if sent_n >= ONBOARDING_MAX_NUDGES:
            continue
        started_raw = a.get("started_at")
        try:
            started = _as_aware(datetime.fromisoformat(started_raw.replace("Z", "+00:00")))
        except Exception:
            continue
        # Интервал считается от ПОСЛЕДНЕГО напоминания (onboarding_last_at),
        # а не от started_at. Иначе для тех, кто зарегистрировался давно,
        # age_days навсегда больше любого порога графика — и вся цепочка
        # из 5 сообщений уходила бы подряд за 5 минут (реальный баг,
        # пойманный вживую: два сообщения минута в минуту).
        first_allowed = started + timedelta(days=2)
        last_raw = a.get("onboarding_last_at")
        if sent_n == 0:
            if now_msk < first_allowed:
                continue
        elif last_raw:
            try:
                last_sent = _as_aware(datetime.fromisoformat(last_raw.replace("Z", "+00:00")))
            except Exception:
                continue
            if now_msk < last_sent + timedelta(days=3):
                continue
        else:
            # Счётчик > 0, но метки нет: строка была инкрементирована СТАРОЙ
            # версией кода (она писала только счётчик). Без этого fallback
            # такие пользователи застревали навсегда: sent_n>0 требует метки,
            # которой никогда не появится. Откатываемся к расчёту от started_at:
            # первое "настоящее" напоминание придёт не раньше чем через 3 дня
            # после старта — безопасно против дублей (старая версия слала их
            # в первые минуты, значит 3 дня уже точно прошли).
            if now_msk < started + timedelta(days=3):
                continue
        # Атомарный claim ДО отправки: инкрементируем счётчик условным PATCH
        # (только если он всё ещё равен прочитанному значению) и проверяем,
        # что обновилась ровно наша строка. Раньше счётчик инкрементировался
        # ПОСЛЕ отправки — два минутных запуска cron подряд успевали оба
        # прочитать одно значение и отправить два сообщения с интервалом в
        # минуту. Теперь проигравший гонку увидит 0 строк и молча пропустит.
        client2 = HTTP_CLIENT
        claim = await client2.patch(
            f"{SUPABASE_URL}/rest/v1/bible_accounts",
            headers={**SB_HEADERS, "Prefer": "return=representation"},
            params={
                "user_id": f"eq.{uid}",
                "onboarding_nudges": f"eq.{sent_n}",
            },
            # Записываем и счётчик, И метку времени одним атомарным PATCH:
            # без метки следующий запуск cron не смог бы вычислить интервал
            # (реальный баг: сообщения уходили минута в минуту).
            json={
                "onboarding_nudges": sent_n + 1,
                "onboarding_last_at": now_msk.isoformat(),
            },
        )
        try:
            claimed_rows = claim.json() if claim.status_code == 200 else []
        except Exception:
            claimed_rows = []
        if not claimed_rows:
            continue  # другой воркер уже отправил — не дублируем
        texts = {
            0: ("👋 Привет! Ты открыл «План чтения Библии», но ещё не выбрал план.\n"
                "Выбери удобный темп — от 30 дней до года — и начни с одного короткого отрывка."),
            1: ("📖 Твой план чтения всё ещё ждёт выбора.\n"
                "Есть планы на 30, 90 и 365 дней — найди свой и сделай первый шаг."),
            2: ("🌱 Начать читать Библию проще, чем кажется.\n"
                "Один отрывок в день — и уже через неделю это станет привычкой. Выбери план?"),
            3: ("📚 Мы сохранили для тебя лучшие планы чтения.\n"
                "Загляни и выбери тот, что подходит по темпу — начать никогда не поздно."),
            4: ("🕊️ Последнее напоминание: план чтения Библии ждёт тебя.\n"
                "Если сейчас не время — не страшно, бот всегда рядом. Мир тебе!"),
        }
        text = texts.get(sent_n, texts[1])
        result = await bible_send(uid, text, {
            "inline_keyboard": [[
                {"text": "📅 Выбрать план", "web_app": {"url": _bible_url(None)}},
            ]],
        })
        if result.get("error_code") == 403:
            # Заблокировал бота — откатываем счётчик, чтобы при разблокировке
            # цепочка могла продолжиться корректно (и не тратим попытки зря:
            # планов нет → обычные напоминания ему и так не приходят).
            await client2.patch(
                f"{SUPABASE_URL}/rest/v1/bible_accounts",
                headers=SB_HEADERS,
                params={"user_id": f"eq.{uid}", "onboarding_nudges": f"eq.{sent_n + 1}"},
                json={"onboarding_nudges": sent_n},
            )
