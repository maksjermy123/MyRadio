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
import logging
from datetime import datetime, timezone
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
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
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

BOT_TOKEN                 = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID                = os.environ.get("CHANNEL_ID", "@Chtenie_Preobrazenie")
INIT_DATA_MAX_AGE_SECONDS = int(os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400"))
GITHUB_TOKEN              = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO               = os.environ.get("GITHUB_REPO", "maksjermy123/MyRadio")
GITHUB_FILE               = os.environ.get("GITHUB_FILE", "posts.json")
GITHUB_BRANCH             = os.environ.get("GITHUB_BRANCH", "main")

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
IGNORE_TAGS = {"#отчтениякпреображению"}

_github_lock = asyncio.Lock()


def extract_hashtags(message: dict) -> list:
    tags = []
    for field in ("entities", "caption_entities"):
        entities = message.get(field) or []
        text_key = "text" if field == "entities" else "caption"
        text = message.get(text_key, "") or ""
        for ent in entities:
            if ent.get("type") == "hashtag":
                tag = text[ent["offset"]: ent["offset"] + ent["length"]].lower()
                tags.append(tag)
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


def _gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


async def fetch_posts_json(client: httpx.AsyncClient) -> tuple:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    resp = await client.get(url, headers=_gh_headers(), params={"ref": GITHUB_BRANCH})
    if resp.status_code == 200:
        data = resp.json()
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]
    if resp.status_code == 404:
        return {"posts": [], "topics": [], "total": 0,
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d")}, None
    raise Exception(f"GitHub {resp.status_code}: {resp.text[:200]}")


async def push_posts_json(client: httpx.AsyncClient, data: dict, sha) -> bool:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    b64 = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()
    payload = {"message": f"auto: posts [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]",
               "content": b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    resp = await client.put(url, headers=_gh_headers(), json=payload)
    return resp.status_code in (200, 201)


def recalc_topics(posts: list) -> list:
    counts = {}
    for p in posts:
        for t in p.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


async def add_post_to_github(message: dict) -> str:
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN не задан")
        return "error_no_github_token"

    msg_id = message.get("message_id") or message.get("id")
    tags   = extract_hashtags(message)
    topics = hashtags_to_topics(tags)
    log.info(f"Пост {msg_id} | теги: {tags} | темы: {topics}")

    if not topics:
        log.info(f"Пост {msg_id}: нет тегов — пропускаем")
        return "no_topics"

    title, preview = extract_title_and_preview(message)
    date_str = datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc).strftime("%Y-%m-%d")
    new_post = {"id": msg_id, "date": date_str, "title": title, "preview": preview,
                "url": f"https://t.me/{CHANNEL_ID.lstrip('@')}/{msg_id}", "topics": topics}

    async with _github_lock:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for attempt in range(3):
                try:
                    posts_data, sha = await fetch_posts_json(client)
                except Exception as e:
                    log.error(f"Чтение GitHub (попытка {attempt+1}): {e}")
                    await asyncio.sleep(1)
                    continue

                if msg_id in {p["id"] for p in posts_data.get("posts", [])}:
                    log.info(f"Пост {msg_id} уже есть")
                    return "skipped_duplicate"

                posts_data["posts"].append(new_post)
                posts_data["posts"].sort(key=lambda p: p["id"])
                posts_data["topics"]  = recalc_topics(posts_data["posts"])
                posts_data["total"]   = len(posts_data["posts"])
                posts_data["updated"] = date_str

                try:
                    if await push_posts_json(client, posts_data, sha):
                        log.info(f"Пост {msg_id} добавлен ✓")
                        return "added"
                    log.warning(f"Конфликт sha, попытка {attempt+1}/3")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.error(f"Запись GitHub (попытка {attempt+1}): {e}")
                    await asyncio.sleep(1)

    log.error(f"Пост {msg_id}: все 3 попытки провалились")
    return "error_all_retries_failed"


# ── Проверка подписки (без изменений) ─────────────────────────────

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
            asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=host if ssl_ctx else None),
            timeout=5.0)
        writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nIcy-MetaData: 1\r\n"
                     f"User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        meta_interval = 0
        while True:
            line = (await asyncio.wait_for(reader.readline(), timeout=5.0)).decode("utf-8", errors="ignore").strip()
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
        m = re.search(r"StreamTitle='([^']*)'", meta.decode("utf-8", errors="ignore").rstrip("\x00"))
        if m:
            return m.group(1).strip() or None
    except Exception:
        return None


# ── Эндпоинты ─────────────────────────────────────────────────────

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

    # Принимаем channel_post (основной) и message (запасной вариант)
    message = update.get("channel_post") or update.get("message")

    if not message:
        log.info(f"update_id={update_id}: не пост — пропускаем")
        return {"ok": True, "action": "ignored", "fields": keys}

    # Проверяем что пост из нашего канала
    chat_username = (message.get("chat") or {}).get("username", "")
    expected = CHANNEL_ID.lstrip("@").lower()
    if chat_username and chat_username.lower() != expected:
        log.warning(f"update_id={update_id}: чужой чат @{chat_username} — пропускаем")
        return {"ok": True, "action": "ignored_wrong_chat"}

    result = await add_post_to_github(message)
    return {"ok": True, "action": result, "post_id": message.get("message_id")}


@app.get("/set_webhook")
async def set_webhook(request: Request):
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN not set"}
    webhook_url = str(request.base_url).rstrip("/") + "/webhook"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["channel_post", "message"]})
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


@app.get("/debug_last")
async def debug_last():
    """Последние 5 постов в индексе — для проверки."""
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not set"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            data, _ = await fetch_posts_json(client)
            posts = data.get("posts", [])
            return {"total": len(posts), "updated": data.get("updated"), "last_5": posts[-5:]}
        except Exception as e:
            return {"error": str(e)}
