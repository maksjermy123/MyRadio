import os, hmac, hashlib, json, re, httpx, time
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Разрешаем запросы с фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Testovuj")

# Кэш для названий песен (чтобы не дергать поток каждую секунду)
meta_cache = {}

class VerifyRequest(BaseModel):
    init_data: str

def verify_tg_data(init_data: str, token: str):
    try:
        parsed = dict(parse_qsl(init_data))
        hash_val = parsed.pop("hash")
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, check_str.encode(), hashlib.sha256).hexdigest()
        return json.loads(parsed["user"]) if hmac.compare_digest(computed, hash_val) else None
    except:
        return None

@app.post("/verify")
async def verify(req: VerifyRequest):
    user = verify_tg_data(req.init_data, BOT_TOKEN)
    if not user:
        return {"allowed": False}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                params={"chat_id": CHANNEL_ID, "user_id": user["id"]}
            )
            res = r.json()
            status = res.get("result", {}).get("status")
            if status in ["member", "administrator", "creator", "restricted"]:
                return {"allowed": True}
        except:
            pass
    return {"allowed": False}

@app.get("/metadata")
async def get_meta(url: str = Query(...)):
    now = time.time()
    # Проверяем кэш
    if url in meta_cache and now - meta_cache[url]["time"] < 300:
        return meta_cache[url]["data"]

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=10.0)
            if r.status_code == 200:
                content = r.text
                title = re.search(r'<title>(.*?)</title>', content)
                artist = re.search(r'<artist>(.*?)</artist>', content)

                title = title.group(1) if title else "Unknown"
                artist = artist.group(1) if artist else "Unknown"

                # Сохраняем в кэш
                meta_cache[url] = {"data": {"title": title, "artist": artist}, "time": now}
                return {"title": title, "artist": artist}
        except:
            pass

    return {"title": "Unknown", "artist": "Unknown"}
