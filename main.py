import os
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel

BOT_TOKEN = os.getenv("8210823079:AAG-AC-dpZliPtlpAqAovu67OXThc_gPAqE")  # токен бота
CHANNEL_ID = os.getenv("-1002547160941")  # пример: @Testovuj

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Radio Mini App Backend")

# CORS для фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class VerifyRequest(BaseModel):
    init_data: str

@app.get("/")
async def root():
    return {"status": "ok", "service": "Radio Mini App Backend"}

@app.post("/verify")
async def verify_sub(req: VerifyRequest):
    # Проверка init_data
    if not req.init_data:
        return {"allowed": False, "reason": "init_data missing"}
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
            params = {"chat_id": CHANNEL_ID, "user_id": extract_user_id(req.init_data)}
            r = await client.get(url, params=params, timeout=5.0)
            data = r.json()
            # user status
            status = data.get("result", {}).get("status", "left")
            allowed = status in ("creator", "administrator", "member")
            logging.info(f"user_id={extract_user_id(req.init_data)} status={status}")
            return {"allowed": allowed, "status": status}
    except Exception as e:
        logging.error(f"Error verifying subscription: {e}")
        return {"allowed": False, "reason": "verification failed"}

def extract_user_id(init_data: str) -> int:
    # Telegram WebApp init_data безопасно содержит user id в query string
    import urllib.parse, hmac, hashlib
    data_items = dict(urllib.parse.parse_qsl(init_data))
    user_id = int(data_items.get("user", 0))
    return user_id
