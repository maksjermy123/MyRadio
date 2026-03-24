import os, hmac, hashlib, json, re, httpx, time
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Разрешаем запросы с твоего фронтенда
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
    except: return None

@app.post("/verify")
async def verify(req: VerifyRequest):
    user = verify_tg_data(req.init_data, BOT_TOKEN)
    if not user: return {"allowed": False}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
                params={"chat_id": CHANNEL_ID, "user_id": user["id"]}
            )
            res = r.json()
            status = res.get("result", {}).get("status")
            # Разрешаем доступ участникам и админам
            if status in ["member", "administrator", "creator", "restricted"]:
                return {"allowed": True}
        except: pass
    return {"allowed": False}

@app.get("/metadata")
async def get_meta(url: str = Query(...)):
    now = time.time()
    # Если в кэше есть данные не старее 30 секунд - отдаем их
    if url in meta_cache and now - meta_cache[url]['time'] < 30:
        return {"title": meta_cache[url]['title']}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            async with client.stream("GET", url, headers={"Icy-MetaData": "1"}) as r:
                metaint = int(r.headers.get("icy-metaint", 0))
                if metaint:
                    await r.aread(metaint)
                    length_byte = await r.aread(1)
                    if length_byte:
                        size = ord(length_byte) * 16
                        meta_raw = (await r.aread(size)).decode(errors='ignore')
                        title = re.search(r"StreamTitle='(.*?)';", meta_raw)
                        if title:
                            res_title = title.group(1)
                            meta_cache[url] = {'title': res_title, 'time': now}
                            return {"title": res_title}
    except: pass
    return {"title": None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
