import json
import os
import re
import asyncio
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode

# -----------------------------
# CONFIG
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "Chtenie_Preobrazenie"
POSTS_FILE = "posts.json"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables!")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# -----------------------------
# ТАБЛИЦА ХЭШТЕГОВ → КАТЕГОРИИ
# -----------------------------

HASHTAG_MAP = {
    "#библия": "📖 Библия",
    "#священноеписание": "📖 Библия",
    "#толкование": "📖 Библия",

    "#богословие": "✝️ Богословие",
    "#догматика": "✝️ Богословие",
    "#апологетика": "✝️ Богословие",

    "#теодицея": "😔 Теодицея",
    "#страдание": "😔 Теодицея",
    "#зло": "😔 Теодицея",

    "#молитва": "🙏 Молитва",
    "#псалом": "🙏 Молитва",
    "#духовнаяпрактика": "🙏 Молитва",

    "#жизнь": "🌱 Христианская жизнь",
    "#христианскаяжизнь": "🌱 Христианская жизнь",
    "#практика": "🌱 Христианская жизнь",

    "#размышления": "💬 Размышления",
    "#мысль": "💬 Размышления",

    "#цитата": "💬 Цитата",

    "#книги": "📚 Книги и авторы",
    "#книга": "📚 Книги и авторы",
    "#литература": "📚 Книги и авторы",
    "#автор": "📚 Книги и авторы",

    "#достоевский": "📚 Достоевский",
    "#фмд": "📚 Достоевский",

    "#солженицын": "📚 Книги и авторы",
    "#клайвльюис": "📚 Книги и авторы",
    "#фильм": "📚 Книги и авторы",

    "#челлендж": "📅 Серия",
    "#лука": "📅 Серия",

    "#духовныйдневник": "📔 Духовный дневник",

    "#проповедь": "🎤 Проповедь",
    "#семинар": "🎤 Семинар",
    "#лекция": "🎤 Семинар",

    "#история": "🏛️ История",
    "#церковь": "🏛️ История",
    "#традиция": "🏛️ История",

    "#праздник": "🎄 Праздники",
    "#рождество": "🎄 Праздники",
    "#пасха": "🎄 Праздники",

    "#юмор": "😄 Юмор",
    "#шутка": "😄 Юмор",

    "#новости": "📰 Новости",

    "#анонс": "📻 Анонсы",
    "#опрос": "📻 Анонсы",
    "#итоги": "📻 Анонсы",
}

# -----------------------------
# УТИЛИТЫ
# -----------------------------

def load_posts():
    if not os.path.exists(POSTS_FILE):
        return {"posts": [], "total": 0, "topics": []}

    with open(POSTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_posts(data):
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_hashtags(text: str):
    if not text:
        return []
    return re.findall(r"#\w+", text.lower())


def map_hashtags_to_categories(hashtags):
    categories = set()

    for tag in hashtags:
        if tag in HASHTAG_MAP:
            categories.add(HASHTAG_MAP[tag])

    if not categories:
        categories.add("💬 Размышления")

    return list(categories)

# -----------------------------
# ОБРАБОТКА НОВЫХ ПОСТОВ
# -----------------------------

@router.channel_post()
async def handle_new_post(message: types.Message):
    posts = load_posts()

    post_id = message.message_id
    date = datetime.utcfromtimestamp(message.date.timestamp()).strftime("%Y-%m-%d")

    text = message.text or message.caption or ""
    hashtags = extract_hashtags(text)
    categories = map_hashtags_to_categories(hashtags)

    preview = text[:200].replace("\n", " ") if text else ""

    url = f"https://t.me/{CHANNEL_USERNAME}/{post_id}"

    new_entry = {
        "id": post_id,
        "date": date,
        "title": preview if preview else "Пост",
        "preview": preview,
        "url": url,
        "topics": categories
    }

    posts["posts"].insert(0, new_entry)
    posts["total"] = len(posts["posts"])

    topic_counts = {}
    for p in posts["posts"]:
        for t in p["topics"]:
            topic_counts[t] = topic_counts.get(t, 0) + 1

    posts["topics"] = [{"name": t, "count": c} for t, c in topic_counts.items()]

    save_posts(posts)

# -----------------------------
# FASTAPI
# -----------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/posts")
async def get_posts():
    return JSONResponse(load_posts())

@app.get("/")
async def root():
    return {"status": "ok", "bot": "running"}

# -----------------------------
# ЗАПУСК БОТА В ФОНЕ
# -----------------------------

async def start_bot():
    dp.include_router(router)
    print("Starting bot polling...")
    await dp.start_polling(bot)

@app.on_event("startup")
async def on_startup():
    print("FastAPI started. Launching bot...")
    asyncio.create_task(start_bot())
