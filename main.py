import os
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
from datetime import datetime, timezone
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radio")

app = FastAPI()

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
CHANNEL_ID                = os.environ.get("CHANNEL_ID", "@Chtenie_Preobrazenie")
INIT_DATA_MAX_AGE_SECONDS = int(os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400"))
GITHUB_TOKEN              = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO               = os.environ.get("GITHUB_REPO", "maksjermy123/MyRadio")
GITHUB_BRANCH             = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE               = os.environ.get("GITHUB_FILE", "posts.json")
GITHUB_LINKS_FILE         = os.environ.get("GITHUB_LINKS_FILE", "links.json")
GROQ_API_KEY              = os.environ.get("GROQ_API_KEY", "")
COHERE_API_KEY            = os.environ.get("COHERE_API_KEY", "")
BOT_USERNAME              = os.environ.get("BOT_USERNAME", "preoradio_bot")
DEEPER_PAGE_URL           = f"https://maksjermy123.github.io/MyRadio/deeper.html"

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
    "#фильм":           "😔 Теодицея",
    "#книги":           "📚 Книги и авторы",
    "#достоевский":     "📚 Достоевский",
    "#солженицын":      "📚 Книги и авторы",
    "#клайвльюис":      "📚 Книги и авторы",
    "#чехов":           "📚 Книги и авторы",
    "#лесков":          "📚 Книги и авторы",
    "#толстой":         "📚 Книги и авторы",
    "#филиппянси":      "📚 Книги и авторы",
    "#жизнь":           "🌱 Христианская жизнь",
    "#молитва":         "🙏 Молитва и духовная жизнь",
    "#духовныйдневник": "📔 Духовный дневник",
    "#проповедь":       "🎤 Проповедь и семинар",
    "#семинар":         "🎤 Проповедь и семинар",
    "#челлендж":        "📅 Челлендж: Лука",
    "#лука":            "📅 Челлендж: Лука",
    "#история":         "🏛️ История и церковь",
    "#размышления":     "💬 Размышления и цитаты",
    "#цитата":          "💬 Размышления и цитаты",
    "#юмор":            "😄 Юмор",
    "#праздник":        "🎄 Праздники",
    "#анонс":           "📻 Анонсы канала",
    "#новости":         "📻 Анонсы канала",
}
IGNORE_TAGS  = {"#отчтениякпреображению", "#продолжение"}
SKIP_AI_TAGS = {"#анонс", "#новости", "#челлендж", "#лука", "#цитата", "#продолжение"}

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
    "Иакова": 44,
    "1 Петра": 45, "2 Петра": 46,
    "1 Иоанна": 47, "2 Иоанна": 48, "3 Иоанна": 49,
    "Иуды": 50,
    "Римлянам": 51,
    "1 Коринфянам": 52, "2 Коринфянам": 53,
    "Галатам": 54, "Ефесянам": 55, "Филиппийцам": 56, "Колоссянам": 57,
    "1 Фессалоникийцам": 58, "2 Фессалоникийцам": 59,
    "1 Тимофею": 60, "2 Тимофею": 61, "Титу": 62, "Филимону": 63,
    "Евреям": 64, "Откровение": 65,
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


# ── GitHub API ────────────────────────────────────────────────
def _gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


async def github_get(client: httpx.AsyncClient, filename: str):
    """
    Читает файл с GitHub.
    Для файлов > 1 МБ GitHub API не возвращает content через Contents API —
    используем Git Blobs API который не имеет ограничения по размеру.
    """
    # Сначала получаем SHA через Contents API (быстро, без содержимого)
    meta_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    r = await client.get(meta_url, headers=_gh_headers(), params={"ref": GITHUB_BRANCH})
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    meta = r.json()
    sha = meta["sha"]
    file_size = meta.get("size", 0)

    # Если файл маленький — берём content прямо из ответа
    if file_size < 900_000 and meta.get("content"):
        try:
            data = json.loads(base64.b64decode(meta["content"]).decode())
            return data, sha
        except Exception:
            pass  # fallback на blob API

    # Для больших файлов — Git Blobs API (без ограничения размера)
    blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{sha}"
    blob_headers = {**_gh_headers(), "Accept": "application/vnd.github.v3.raw"}
    r2 = await client.get(blob_url, headers=blob_headers, timeout=30.0)
    r2.raise_for_status()
    # vnd.github.v3.raw возвращает сырой текст файла
    try:
        data = r2.json()
    except Exception:
        # Если вернулся raw content как текст
        data = json.loads(r2.text)
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
            return r.json()
        if r.status_code == 409 and attempt < 2:
            _, new_sha = await github_get(client, filename)
            if new_sha:
                body["sha"] = new_sha
            await asyncio.sleep(1)
            continue
        r.raise_for_status()


# ── Парсинг хэштегов ─────────────────────────────────────────
def extract_hashtags(message: dict) -> list:
    """Парсинг хэштегов с учётом UTF-16 (эмодзи = 2 единицы)."""
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
    return topics


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
    """Нужно ли запускать AI для этого набора тегов."""
    non_skip = [t for t in tags if t not in SKIP_AI_TAGS and t not in IGNORE_TAGS]
    return bool(non_skip)


# ── Парсинг библейских ссылок ─────────────────────────────────
def normalize_book(name: str) -> str:
    return BOOK_ALIASES.get(name, name)


def parse_ref(ref: str):
    """Разбираем ref на (book_num, chapter, verse_start) или None."""
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


async def find_related_by_embedding(post_id: int, embedding: list, posts_data: dict, top_k: int = 2) -> list:
    scores = []
    for post in posts_data.get("posts", []):
        if post["id"] == post_id:
            continue
        emb = post.get("embedding")
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


async def get_theology_db() -> list:
    global _theology_cache
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
    log.info(f"Theology DB loaded: {len(all_records)} записей")
    return all_records


async def find_theology_quotes(post_text: str, top_n: int = 3) -> list:
    if not COHERE_API_KEY:
        return []
    try:
        db = await get_theology_db()
        if not db:
            return []
        # Берём все записи — не случайную выборку, чтобы не пропустить релевантное
        sample = db[:]
        documents = [rec["text"][:400] for rec in sample]
        payload = {
            "model": "rerank-multilingual-v3.0",
            "query": post_text[:1000],
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
        # Высокий порог: если лучшая цитата не очень близка — не показываем ничего
        if top_score < 0.4:
            log.info(f"Theology: top_score={top_score:.3f} < 0.4 — цитаты не релевантны, пропускаем")
            return []
        # Берём только цитаты близкие к лучшей (не ниже 70% от топа)
        threshold = top_score * 0.70
        quotes = []
        seen_authors = set()
        for res in results:
            score = res["relevance_score"]
            if score < threshold:
                break
            rec = sample[res["index"]]
            # Не более одной цитаты от одного автора
            if rec["author"] in seen_authors:
                continue
            seen_authors.add(rec["author"])
            quotes.append({
                "author": rec["author"],
                "title": rec.get("title", ""),
                "text": rec["text"][:500],
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
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/ru_synodal.json"
        r = await client.get(url, timeout=30)
        if r.status_code == 200:
            _bible_cache = r.json()
            log.info(f"Bible DB loaded: {len(_bible_cache)} books")
            return _bible_cache
    except Exception as e:
        log.error(f"Bible DB load error: {e}")
    return None


async def fetch_bible_text(ref: str) -> str:
    try:
        ref = re.split(r' [—–-]{1,2} ', ref.strip())[0].strip()
        m = re.search(r'(\d+:\d+(?:-\d+)?)$', ref)
        if not m:
            m2 = re.search(r'(\d+)$', ref)
            if not m2:
                return ""
            chapter = int(m2.group(1)) - 1
            book_ru = normalize_book(ref[:m2.start()].strip())
            book_idx = BOOK_JSON_INDEX.get(book_ru)
            if book_idx is None:
                return ""
            async with httpx.AsyncClient(timeout=15) as client:
                bible = await get_bible_db(client)
            if not bible or chapter >= len(bible[book_idx]["chapters"]):
                return ""
            verses = bible[book_idx]["chapters"][chapter][:5]
            return " ".join(f"{i+1} {v}" for i, v in enumerate(verses))
        cv = m.group(1)
        book_ru = normalize_book(ref[:m.start()].strip())
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
        async with httpx.AsyncClient(timeout=15) as client:
            bible = await get_bible_db(client)
        if not bible:
            return ""
        chapters = bible[book_idx].get("chapters", [])
        if chapter >= len(chapters):
            return ""
        selected = chapters[chapter][verse_start:verse_end]
        if not selected:
            return ""
        return " ".join(f"{verse_start + 1 + i} {v}" for i, v in enumerate(selected))
    except Exception as e:
        log.error(f"Bible fetch error for '{ref}': {e}")


# ── Groq: анализ поста ────────────────────────────────────────
async def analyze_post(post_text: str, topics: list):
    prompt = GROQ_PROMPT.format(post_text=post_text)
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(GROQ_URL, headers=_groq_headers(), json=payload)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(text)


# ── Кнопка «Глубже» ───────────────────────────────────────────
async def send_deeper_button(post_id: int, use_web_app: bool = False):
    # Прямая ссылка на deeper.html с post_id в URL параметре.
    # url-кнопка → Telegram принимает через editMessageReplyMarkup.
    # Открывается как Mini App внутри Telegram (домен привязан к боту).
    # post_id читается из window.location.search — надёжно на всех платформах.
    direct_url = f"{DEEPER_PAGE_URL}?post_id={post_id}"
    btn = {"text": "📚 Глубже", "url": direct_url}
    keyboard = {"inline_keyboard": [[btn]]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{TELEGRAM_API}/editMessageReplyMarkup",
            json={"chat_id": CHANNEL_ID, "message_id": post_id, "reply_markup": keyboard}
        )
        result = r.json()
        if result.get("ok"):
            log.info(f"✅ Кнопка «Глубже» добавлена к посту {post_id}")
        else:
            log.error(f"❌ Ошибка кнопки для {post_id}: {result.get('description')}")


# ── Обработка поста AI ────────────────────────────────────────
async def process_post(post: dict):
    """Полный AI-пайплайн: embedding → Groq → Cohere Rerank → Bible text → links.json."""
    post_id = post.get("message_id") or post.get("id")
    text = post.get("text", "") or post.get("caption", "")
    if not text or not post_id:
        return

    tags = extract_hashtags(post) if ("entities" in post or "caption_entities" in post) else []
    topics = hashtags_to_topics(tags) if tags else post.get("topics", [])
    if not topics:
        log.info(f"process_post {post_id}: нет тем — пропускаем")
        return

    # Склейка с предыдущим постом если он помечен #продолжение
    # (текущий пост — вторая и финальная часть)
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
        await send_deeper_button(post_id, use_web_app=True)
        log.info(f"😄 Humor post {post_id} saved.")
        return

    # Нормальный пост
    embedding = None
    async with _github_lock:
        async with httpx.AsyncClient(timeout=20) as client:
            posts_data, posts_sha = await github_get(client, GITHUB_FILE)
            if posts_data is None:
                posts_data = {"posts": [], "topics": [], "total": 0, "updated": ""}
            existing = next((p for p in posts_data["posts"] if p["id"] == post_id), None)
            embedding = existing.get("embedding") if existing else None
            if not embedding:
                embedding = await get_embedding(text)
                if existing and embedding:
                    existing["embedding"] = embedding
                    await github_put(client, GITHUB_FILE, posts_data, posts_sha,
                                     f"embedding for post {post_id}")

    try:
        related_task = (find_related_by_embedding(post_id, embedding, posts_data)
                        if embedding else asyncio.sleep(0))
        result, related, theology_quotes = await asyncio.gather(
            analyze_post(text, topics), related_task, find_theology_quotes(text)
        )
        if not embedding:
            related = []
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
    await send_deeper_button(post_id, use_web_app=True)


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
    new_post = {
        "id": msg_id, "date": date_str, "title": title,
        "preview": preview, "url": f"https://t.me/{chan_user}/{msg_id}",
        "topics": topics, "text": text_full,
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
        user = json.loads(unquote(user_data))
    except json.JSONDecodeError:
        return None
    return {"user": user, "auth_date": auth_date}


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
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
        if not _host_is_public(host):
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
    payload = {
        "chat_id": chat_id,
        "text": "📚 Нажми чтобы открыть материалы поста:",
        "reply_markup": {"inline_keyboard": [[
            {"text": "📚 Глубже", "web_app": {"url": deeper_url}}
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


@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        log.warning("Webhook: невалидный JSON")
        return {"ok": False, "error": "invalid json"}

    update_id = update.get("update_id", "?")
    keys = [k for k in update if k != "update_id"]
    log.info(f"▶ update_id={update_id} | поля: {keys}")

    # Личные сообщения пользователей боту
    if update.get("message"):
        asyncio.create_task(handle_user_message(update["message"]))
        return {"ok": True}

    # Новый пост канала
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

    result = await upsert_post_to_github(message, is_edit=is_edit)

    # Запускаем AI только для новых подходящих постов
    if result == "added":
        tags = extract_hashtags(message)
        if should_process_ai(tags):
            asyncio.create_task(process_post(message))

    return {"ok": True, "action": result, "post_id": message.get("message_id")}


@app.get("/links/{post_id}")
async def get_links(post_id: int):
    async with httpx.AsyncClient(timeout=15) as client:
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)
    if not links_data or str(post_id) not in links_data:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return links_data[str(post_id)]


@app.post("/analyze/{post_id}")
@app.get("/analyze/{post_id}")
async def manual_analyze(post_id: int):
    """Ручной запуск AI-анализа для существующего поста (GET и POST)."""
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
        "date": 0,
        "chat": {"username": CHANNEL_ID.lstrip("@")}
    }))
    return {"ok": True, "message": f"Analysis started for post {post_id}", "text_len": len(text)}


@app.get("/analyze_all")
async def analyze_all(delay: float = 5.0, skip_existing: bool = True):
    """
    Запускает AI-анализ для всех постов.
    skip_existing=true — пропускает посты у которых уже есть links.
    Открой в браузере и жди — анализ идёт в фоне.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        posts_data, _ = await github_get(client, GITHUB_FILE)
        links_data, _ = await github_get(client, GITHUB_LINKS_FILE)

    if not posts_data:
        return {"error": "posts.json not found"}

    links_data = links_data or {}
    posts = posts_data.get("posts", [])

    # Фильтруем что нужно анализировать
    SKIP_TOPICS = {"📻 Анонсы канала", "📅 Челлендж: Лука"}
    to_analyze = []
    for post in posts:
        post_topics = set(post.get("topics", []))
        # Пропускаем анонсы и челленджи
        if post_topics and post_topics.issubset(SKIP_TOPICS):
            continue
        # Пропускаем если уже есть аналитика
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
            try:
                await process_post({
                    "message_id": post["id"],
                    "text": post.get("text") or post.get("preview") or post.get("title") or "",
                    "topics": post.get("topics", []),
                    "date": 0,
                    "chat": {"username": CHANNEL_ID.lstrip("@")}
                })
                done += 1
                log.info(f"analyze_all: [{done}/{len(to_analyze)}] пост {post['id']} готов")
            except Exception as e:
                log.error(f"analyze_all: пост {post['id']} ошибка: {e}")
            await asyncio.sleep(delay)
        log.info(f"analyze_all: завершено {done}/{len(to_analyze)}")

    asyncio.create_task(_run())
    return {
        "ok": True,
        "queued": len(to_analyze),
        "delay_seconds": delay,
        "message": f"Анализ запущен для {len(to_analyze)} постов. Следи за логами Render."
    }


@app.get("/reindex")
async def reindex_all():
    """Добавляет embeddings для постов у которых их нет."""
    async with httpx.AsyncClient(timeout=20) as client:
        posts_data, posts_sha = await github_get(client, GITHUB_FILE)
    if not posts_data:
        return {"error": "posts.json not found"}
    updated = 0
    for post in posts_data.get("posts", []):
        if post.get("embedding"):
            continue
        emb = await get_embedding(post.get("text", post.get("preview", "")))
        if emb:
            post["embedding"] = emb
            updated += 1
        await asyncio.sleep(0.5)
    if updated > 0:
        async with httpx.AsyncClient(timeout=20) as client:
            _, sha = await github_get(client, GITHUB_FILE)
            await github_put(client, GITHUB_FILE, posts_data, sha,
                             f"reindex: added {updated} embeddings")
    return {"ok": True, "updated": updated, "total": len(posts_data.get("posts", []))}


@app.get("/bulk_deeper")
async def bulk_deeper(delay: float = 1.5):
    """Добавляет кнопку «Глубже» ко всем постам у которых уже есть links."""
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
    webhook_url = str(request.base_url).rstrip("/") + "/webhook"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": ["channel_post", "edited_channel_post", "message"],
            })
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



@app.get("/import_texts")
async def import_texts():
    """
    Читает result.json с GitHub, добавляет полный текст в posts.json,
    затем удаляет result.json с GitHub.
    """
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not set"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Читаем result.json
        try:
            result_data, result_sha = await github_get(client, "result.json")
        except Exception as e:
            return {"error": f"result.json не найден на GitHub: {e}"}

        messages = result_data.get("messages", [])
        log.info(f"import_texts: {len(messages)} сообщений в result.json")

        # Строим словарь id → полный текст
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

        # Читаем posts.json
        posts_data, posts_sha = await github_get(client, GITHUB_FILE)
        posts = posts_data.get("posts", [])

        # Обновляем тексты
        updated = 0
        for post in posts:
            pid = post["id"]
            if pid in tg_texts:
                existing = post.get("text", "")
                if not existing or len(existing) < 100:
                    post["text"] = tg_texts[pid]
                    updated += 1

        log.info(f"import_texts: обновлено {updated} постов")

        # Сохраняем posts.json
        await github_put(client, GITHUB_FILE, posts_data, posts_sha,
                         f"import: full text for {updated} posts")

        # Удаляем result.json с GitHub
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

    return {
        "ok": True,
        "messages_in_export": len(tg_texts),
        "posts_updated": updated,
        "message": f"Готово! Обновлено {updated} постов. Теперь запусти /reindex и /analyze_all"
    }

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
