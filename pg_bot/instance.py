# instance.py
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import API_TOKEN, TIMEZONE

# 1. Инициализируем бота
bot = Bot(token=API_TOKEN)

# 2. Инициализируем планировщик (чтобы он тоже был доступен в хендлерах)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)