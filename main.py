import os
import hmac
import hashlib
import json
import re
import httpx
from urllib.parse import parse_qsl
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@Testovuj")
CHANNEL_URL = f"https://t.me/{CHANNEL_ID.replace('@', '')}"

class VerifyRequest(BaseModel):
    init_data: str

def verify_data(init_data, token):
    try:
        parsed = dict(parse_qsl(init_data))
        h = parsed.pop("hash")
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        hmac_res = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        return json.loads(parsed["user"]) if hmac.compare_digest(hmac_res, h) else None
    except: return None

@app.get("/")
async def root(): return {"status": "ok"}

@app.post("/verify")
async def verify(req: VerifyRequest):
    user = verify_data(req.init_data, BOT_TOKEN)
    if not user: return {"allowed": False, "error": "no_auth", "invite_link": CHANNEL_URL}
    
    async with httpx.AsyncClient(timeout=6.0) as client:
        try:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember", 
                                 params={"chat_id": CHANNEL_ID, "user_id": user["id"]})
            data = r.json()
            if r.status_code == 200 and data.get("ok"):
                status = data.get("result", {}).get("status")
                if status in ["member", "administrator", "creator", "restricted"]:
                    return {"allowed": True}
        except: pass
    return {"allowed": False, "error": "not_member", "invite_link": CHANNEL_URL}

@app.get("/metadata")
async def get_meta(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            async with c.stream("GET", url, headers={"Icy-MetaData": "1"}) as r:
                mi = int(r.headers.get("icy-metaint", 0))
                if mi:
                    await r.aread(mi)
                    lb = await r.aread(1)
                    if lb:
                        sz = ord(lb) * 16
                        m = (await r.aread(sz)).decode(errors='ignore')
                        res = re.search(r"StreamTitle='(.*?)';", m)
                        return {"title": res.group(1) if res else None}
    except: pass
    return {"title": None}
