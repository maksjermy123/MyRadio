import os, hmac, hashlib, json, re, time, struct, asyncio
from urllib.parse import parse_qsl, unquote
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Testovuj")

# Кеш метаданных — не дёргаем поток каждую секунду
meta_cache = {}
CACHE_TTL = 30  # секунд


class VerifyRequest(BaseModel):
    init_data: str


def verify_tg_data(init_data: str, token: str):
    """Проверяет подпись initData от Telegram. Возвращает данные пользователя или None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_val = parsed.pop("hash", None)
        if not hash_val:
            return None

        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, check_str.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, hash_val):
            return None

        user_str = parsed.get("user")
        if user_str:
            return json.loads(unquote(user_str))
        return {}
    except Exception:
        return None


@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio Mini App Backend"}


@app.post("/verify")
async def verify(req: VerifyRequest):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    # Проверяем подпись
    user = verify_tg_data(req.init_data, BOT_TOKEN)
    if not user:
        return {"allowed": False, "reason": "invalid_signature"}

    user_id = user.get("id")
    if not user_id:
        return {"allowed": False, "reason": "no_user_id"}

    # Спрашиваем у Telegram
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                params={"chat_id": CHANNEL_ID, "user_id": user_id}
            )
        data = r.json()

        if not data.get("ok"):
            # Telegram не нашёл пользователя — скорее всего не подписан
            return {"allowed": False, "reason": "not_found"}

        status = data["result"].get("status", "")
        # member, administrator, creator, restricted — разрешаем
        # left, kicked — блокируем
        allowed = status in ("member", "administrator", "creator", "restricted")
        return {"allowed": allowed, "status": status}

    except Exception:
        # При ошибке сети — блокируем (безопаснее чем пускать всех)
        return {"allowed": False, "reason": "network_error"}


@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    """Читает ICY metadata (название трека) из радиопотока."""
    now = time.time()

    # Отдаём из кеша если свежий
    if url in meta_cache and now - meta_cache[url]["time"] < CACHE_TTL:
        return {"title": meta_cache[url]["title"], "cached": True}

    title = await fetch_icy_title(url)
    meta_cache[url] = {"title": title, "time": now}
    return {"title": title, "available": title is not None}


async def fetch_icy_title(stream_url: str):
    """Подключается к потоку и читает ICY metadata."""
    try:
        url_clean = stream_url.replace("https://", "").replace("http://", "")
        parts = url_clean.split("/", 1)
        host_port = parts[0]
        path = "/" + parts[1] if len(parts) > 1 else "/"

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 443 if stream_url.startswith("https") else 80

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
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

        # Читаем заголовки
        meta_interval = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            line = line.decode("utf-8", errors="ignore").strip()
            if not line:
                break
            if line.lower().startswith("icy-metaint:"):
                meta_interval = int(line.split(":", 1)[1].strip())

        if meta_interval <= 0:
            writer.close()
            return None

        # Читаем аудио до метаданных
        audio_data = b""
        while len(audio_data) < meta_interval:
            chunk = await asyncio.wait_for(
                reader.read(meta_interval - len(audio_data)), timeout=5.0
            )
            if not chunk:
                break
            audio_data += chunk

        # Читаем размер блока
        size_byte = await asyncio.wait_for(reader.read(1), timeout=3.0)
        if not size_byte:
            writer.close()
            return None

        meta_size = struct.unpack("B", size_byte)[0] * 16
        if meta_size == 0:
            writer.close()
            return None

        # Читаем метаданные
        meta_data = b""
        while len(meta_data) < meta_size:
            chunk = await asyncio.wait_for(
                reader.read(meta_size - len(meta_data)), timeout=3.0
            )
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
