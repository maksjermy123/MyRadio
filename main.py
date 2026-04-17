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
from datetime import datetime, timezone
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Chtenie_Preobrazenie")
INIT_DATA_MAX_AGE_SECONDS = int(os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400"))

# GitHub settings для автообновления posts.json
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "maksjermy123/MyRadio")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "posts.json")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# Хэштег → категория оглавления
HASHTAG_MAP = {
    "#библия":         "📖 Библия и толкование",
    "#богословие":     "✝️ Богословие",
    "#теодицея":       "😔 Теодицея",
    "#фильм":          "😔 Теодицея",
    "#книги":          "📚 Книги и авторы",
    "#достоевский":    "📚 Достоевский",
    "#солженицын":     "📚 Книги и авторы",
    "#клайвльюис":     "📚 Книги и авторы",
    "#чехов":          "📚 Книги и авторы",
    "#лесков":         "📚 Книги и авторы",
    "#толстой":        "📚 Книги и авторы",
    "#филиппянси":     "📚 Книги и авторы",
    "#жизнь":          "🌱 Христианская жизнь",
    "#молитва":        "🙏 Молитва и духовная жизнь",
    "#духовныйдневник":"📔 Духовный дневник",
    "#проповедь":      "🎤 Проповедь и семинар",
    "#семинар":        "🎤 Проповедь и семинар",
    "#челлендж":       "📅 Челлендж: Лука",
    "#лука":           "📅 Челлендж: Лука",
    "#история":        "🏛️ История и церковь",
    "#размышления":    "💬 Размышления и цитаты",
    "#цитата":         "💬 Размышления и цитаты",
    "#юмор":           "😄 Юмор",
    "#праздник":       "🎄 Праздники",
    "#анонс":          "📻 Анонсы канала",
    "#новости":        "📻 Анонсы канала",
}

# Служебные теги — игнорируем, не добавляем в оглавление
IGNORE_TAGS = {"#отчтениякпреображению"}


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def extract_hashtags(message: dict) -> list[str]:
    """Извлекает все хэштеги из сообщения Telegram."""
    tags = []
    entities = message.get("entities", []) or message.get("caption_entities", [])
    text = message.get("text", "") or message.get("caption", "") or ""
    for ent in entities:
        if ent.get("type") == "hashtag":
            offset = ent["offset"]
            length = ent["length"]
            tag = text[offset:offset + length].lower()
            tags.append(tag)
    return tags


def hashtags_to_topics(tags: list[str]) -> list[str]:
    """Конвертирует хэштеги в категории оглавления."""
    topics = []
    seen = set()
    for tag in tags:
        if tag in IGNORE_TAGS:
            continue
        cat = HASHTAG_MAP.get(tag)
        if cat and cat not in seen:
            topics.append(cat)
            seen.add(cat)
    return topics


def extract_title_and_preview(message: dict) -> tuple[str, str]:
    """Достаёт заголовок и превью из текста поста."""
    text = message.get("text", "") or message.get("caption", "") or ""
    # Убираем хэштеги с конца
    clean = re.sub(r'\s*#\w+', '', text).strip()
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if not lines:
        return "", ""
    title = lines[0][:120]
    preview = " ".join(lines[:4])[:300] if len(lines) > 1 else ""
    return title, preview


async def fetch_posts_json_from_github() -> dict:
    """Скачивает текущий posts.json с GitHub через API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers, params={"ref": GITHUB_BRANCH})
        if resp.status_code == 200:
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content), data["sha"]
        elif resp.status_code == 404:
            # Файл ещё не существует — возвращаем пустую структуру
            empty = {"posts": [], "topics": [], "total": 0,
                     "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            return empty, None
        else:
            raise Exception(f"GitHub API error: {resp.status_code} {resp.text}")


async def push_posts_json_to_github(posts_data: dict, sha: str | None) -> bool:
    """Загружает обновлённый posts.json на GitHub."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    content_b64 = base64.b64encode(
        json.dumps(posts_data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": f"auto: update posts.json [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha  # нужен для обновления существующего файла

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.put(url, headers=headers, json=payload)
        return resp.status_code in (200, 201)


def recalc_topics(posts: list) -> list:
    """Пересчитывает список тем и количество постов в каждой."""
    counts = {}
    for p in posts:
        for t in p.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


async def add_post_to_github(message: dict) -> str:
    """
    Основная логика: получает сообщение из канала,
    парсит его и добавляет в posts.json на GitHub.
    Возвращает статус: 'added', 'skipped', 'no_topics', 'error'
    """
    if not GITHUB_TOKEN:
        return "error_no_github_token"

    msg_id = message.get("message_id") or message.get("id")
    tags = extract_hashtags(message)
    topics = hashtags_to_topics(tags)

    if not topics:
        return "no_topics"  # пост без известных хэштегов — не добавляем

    title, preview = extract_title_and_preview(message)
    date_raw = message.get("date", 0)
    date_str = datetime.fromtimestamp(date_raw, tz=timezone.utc).strftime("%Y-%m-%d")

    channel_username = CHANNEL_ID.lstrip("@")
    url = f"https://t.me/{channel_username}/{msg_id}"

    new_post = {
        "id": msg_id,
        "date": date_str,
        "title": title,
        "preview": preview,
        "url": url,
        "topics": topics,
    }

    try:
        posts_data, sha = await fetch_posts_json_from_github()
    except Exception as e:
        return f"error_fetch: {e}"

    # Проверяем, нет ли уже такого поста (по id)
    existing_ids = {p["id"] for p in posts_data.get("posts", [])}
    if msg_id in existing_ids:
        return "skipped_duplicate"

    posts_data["posts"].append(new_post)
    posts_data["posts"].sort(key=lambda p: p["id"])
    posts_data["topics"] = recalc_topics(posts_data["posts"])
    posts_data["total"] = len(posts_data["posts"])
    posts_data["updated"] = date_str

    try:
        ok = await push_posts_json_to_github(posts_data, sha)
        return "added" if ok else "error_push"
    except Exception as e:
        return f"error_push: {e}"


# ──────────────────────────────────────────────
# Проверка подписки (без изменений)
# ──────────────────────────────────────────────

class VerifyRequest(BaseModel):
    init_data: str


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> dict | None:
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw:
        return None
    try:
        auth_date = int(auth_date_raw)
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
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


async def fetch_icy_metadata(stream_url: str) -> str | None:
    try:
        from urllib.parse import urlparse
        import asyncio

        parsed_url = urlparse(stream_url)
        if parsed_url.scheme not in {"http", "https"}:
            return None
        if not parsed_url.hostname:
            return None

        host = parsed_url.hostname
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        path = parsed_url.path or "/"
        if parsed_url.query:
            path += "?" + parsed_url.query

        if not _host_is_public(host):
            return None

        ssl_context = None
        if parsed_url.scheme == "https":
            ssl_context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context,
                                    server_hostname=host if ssl_context else None),
            timeout=5.0
        )

        request = (
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {host}\r\n"
            f"Icy-MetaData: 1\r\n"
            f"User-Agent: Mozilla/5.0\r\n"
            f"Connection: close\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()

        headers = {}
        meta_interval = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            line = line.decode("utf-8", errors="ignore").strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
                if k.strip().lower() == "icy-metaint":
                    meta_interval = int(v.strip())

        if meta_interval <= 0:
            writer.close()
            return None

        audio_chunk = b""
        while len(audio_chunk) < meta_interval:
            chunk = await asyncio.wait_for(
                reader.read(meta_interval - len(audio_chunk)), timeout=5.0)
            if not chunk:
                break
            audio_chunk += chunk

        meta_size_byte = await asyncio.wait_for(reader.read(1), timeout=3.0)
        if not meta_size_byte:
            writer.close()
            return None

        meta_size = struct.unpack("B", meta_size_byte)[0] * 16
        if meta_size == 0:
            writer.close()
            return None

        meta_data = b""
        while len(meta_data) < meta_size:
            chunk = await asyncio.wait_for(
                reader.read(meta_size - len(meta_data)), timeout=3.0)
            if not chunk:
                break
            meta_data += chunk

        writer.close()
        meta_str = meta_data.decode("utf-8", errors="ignore").rstrip("\x00")
        match = re.search(r"StreamTitle='([^']*)'", meta_str)
        if match:
            title = match.group(1).strip()
            return title if title else None

    except Exception:
        return None
    return None


# ──────────────────────────────────────────────
# Эндпоинты
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio Mini App Backend"}


@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    title = await fetch_icy_metadata(url)
    return {"title": title, "available": title is not None}


@app.post("/verify")
async def verify(request: VerifyRequest):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")
    if not CHANNEL_ID:
        raise HTTPException(status_code=500, detail="CHANNEL_ID not configured")
    if not request.init_data:
        raise HTTPException(status_code=403, detail="Missing init data")

    payload = verify_telegram_init_data(
        request.init_data, BOT_TOKEN,
        max_age_seconds=INIT_DATA_MAX_AGE_SECONDS,
    )
    if payload is None:
        raise HTTPException(status_code=403, detail="Invalid init data")

    user = payload.get("user") or {}
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="No user id")

    async with httpx.AsyncClient(timeout=10.0) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
        resp = None
        for attempt in range(2):
            resp = await client.get(url, params={"chat_id": CHANNEL_ID, "user_id": user_id})
            if resp.status_code == 429 and attempt == 0:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after")
                except Exception:
                    retry_after = None
                if isinstance(retry_after, int) and 0 < retry_after <= 2:
                    import asyncio
                    await asyncio.sleep(retry_after)
                    continue
            break

    if resp is None:
        return {"allowed": False, "reason": "telegram_api_no_response"}
    if resp.status_code != 200:
        return {"allowed": False, "reason": f"telegram_api_http_{resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"allowed": False, "reason": "telegram_api_bad_json"}

    if not data.get("ok"):
        return {"allowed": False, "reason": data.get("description", "telegram_api_error")}

    status = data["result"].get("status", "")
    allowed_statuses = {"member", "administrator", "creator"}
    return {"allowed": status in allowed_statuses, "status": status}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Telegram шлёт сюда все обновления канала.
    Мы ловим новые посты и добавляем их в posts.json на GitHub.
    """
    try:
        update = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    # Новый пост в канале приходит как channel_post
    message = update.get("channel_post")

    if not message:
        # Не пост канала — игнорируем (личные сообщения, edited и т.д.)
        return {"ok": True, "action": "ignored"}

    result = await add_post_to_github(message)
    return {"ok": True, "action": result}


@app.get("/set_webhook")
async def set_webhook(request: Request):
    """
    Одноразовый эндпоинт для регистрации webhook в Telegram.
    Открой в браузере: https://ВАШ_RENDER_URL/set_webhook
    """
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN not set"}

    # Определяем наш публичный URL автоматически
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhook"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["channel_post"]},
        )
        data = resp.json()

    return {"ok": data.get("ok"), "webhook_url": webhook_url, "telegram_response": data}


@app.get("/check_webhook")
async def check_webhook():
    """Показывает текущий статус webhook."""
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
        )
        return resp.json()
