import os
import hmac
import hashlib
import json
from urllib.parse import unquote, parse_qsl
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI()

# CORS — разрешаем запросы от Mini App
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
    """Проверяет подпись initData от Telegram и возвращает данные пользователя."""
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    # Формируем строку для проверки
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    # Секретный ключ
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()

    # Считаем хэш
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    # Возвращаем данные пользователя
    user_data = parsed.get("user")
    if user_data:
        return json.loads(unquote(user_data))
    return {}


@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio Mini App Backend"}


@app.post("/verify")
async def verify(request: VerifyRequest):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")

    # 1. Проверяем подпись Telegram
    user_data = verify_telegram_init_data(request.init_data, BOT_TOKEN)
    if user_data is None:
        raise HTTPException(status_code=403, detail="Invalid init data")

    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="No user id")

    # 2. Проверяем подписку на канал
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
            params={"chat_id": CHANNEL_ID, "user_id": user_id}
        )

    if resp.status_code != 200:
        # Если Telegram не отвечает — не блокируем пользователя
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
