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
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException, Query
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
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Testovuj")
INIT_DATA_MAX_AGE_SECONDS = int(
    os.environ.get("INIT_DATA_MAX_AGE_SECONDS", "86400")
)


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

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True


async def fetch_icy_metadata(stream_url: str) -> str | None:
    """Читает ICY metadata из радиопотока."""
    try:
        from urllib.parse import urlparse

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

        import asyncio
        ssl_context = None
        if parsed_url.scheme == "https":
            ssl_context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context, server_hostname=host if ssl_context else None),
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
    if not CHANNEL_ID:
        raise HTTPException(status_code=500, detail="CHANNEL_ID not configured")

    if not request.init_data:
        raise HTTPException(status_code=403, detail="Missing init data")

    payload = verify_telegram_init_data(
        request.init_data,
        BOT_TOKEN,
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
            resp = await client.get(
                url,
                params={"chat_id": CHANNEL_ID, "user_id": user_id},
            )
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

    return {
        "allowed": status in allowed_statuses,
        "status": status
    }
