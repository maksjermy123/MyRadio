import os
import hmac
import hashlib
import json
import struct
import re
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Testovuj")


class VerifyRequest(BaseModel):
    init_data: str


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
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
    user_data = parsed.get("user")
    if user_data:
        return json.loads(unquote(user_data))
    return {}


async def fetch_icy_metadata(stream_url: str) -> str | None:
    """Читает ICY metadata из радиопотока."""
    try:
        # Парсим хост и путь из URL
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

        import asyncio
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

        # Читаем аудио данные до метаданных
        audio_chunk = b""
        while len(audio_chunk) < meta_interval:
            chunk = await asyncio.wait_for(
                reader.read(meta_interval - len(audio_chunk)),
                timeout=5.0
            )
            if not chunk:
                break
            audio_chunk += chunk

        # Читаем размер блока метаданных
        meta_size_byte = await asyncio.wait_for(reader.read(1), timeout=3.0)
        if not meta_size_byte:
            writer.close()
            return None

        meta_size = struct.unpack("B", meta_size_byte)[0] * 16
        if meta_size == 0:
            writer.close()
            return None

        # Читаем метаданные
        meta_data = b""
        while len(meta_data) < meta_size:
            chunk = await asyncio.wait_for(
                reader.read(meta_size - len(meta_data)),
                timeout=3.0
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


@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio Mini App Backend"}


@app.get("/metadata")
async def get_metadata(url: str = Query(...)):
    """Возвращает название текущего трека для радиопотока."""
    title = await fetch_icy_metadata(url)
    return {"title": title, "available": title is not None}


@app.post("/verify")
async def verify(request: VerifyRequest):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    user_data = verify_telegram_init_data(request.init_data, BOT_TOKEN)
    if user_data is None:
        raise HTTPException(status_code=403, detail="Invalid init data")

    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="No user id")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
            params={"chat_id": CHANNEL_ID, "user_id": user_id}
        )

    if resp.status_code != 200:
        return {"allowed": True, "reason": "telegram_api_error"}

    data = resp.json()
    if not data.get("ok"):
        return {"allowed": False, "reason": "not_found"}

    status = data["result"].get("status", "")
    allowed_statuses = {"member", "administrator", "creator"}

    return {
        "allowed": status in allowed_statuses,
        "status": status
    }
