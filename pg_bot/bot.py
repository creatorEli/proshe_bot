# bot.py - главный файл

import asyncio
import logging
from aiogram import Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

from config import ADMIN_ID

from db_funcs import (
    init_db, get_all_routes
)
from scheduler_utils import (
    schedule_route_job,
)

# Импортируем глобальные объекты из instance.py
from instance import bot, scheduler

# Импортируем роутеры из модулей
from handlers import chats, routes, imports, singular, fsm_steps, collector

dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# ПОДКЛЮЧЕНИЕ РОУТЕРОВ
# ==========================================
# Порядок важен! Сначала команды, потом коллектор
dp.include_router(chats.commands_router)
dp.include_router(routes.commands_router)
dp.include_router(singular.commands_router)
dp.include_router(imports.commands_router)
dp.include_router(fsm_steps.commands_router)

# router = Router()
# dp.include_router(router)

commands_router = Router()
# collector_router = Router()
# dp.include_router(commands_router)
# dp.include_router(collector_router)

logging.basicConfig(level=logging.INFO)


from texts import HELP_TEXT



@commands_router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@commands_router.message(Command("start"), F.from_user.id == ADMIN_ID)
async def cmd_start(message: types.Message):
    await message.answer("Привет милый! Используй /help если хочешь посмотреть список всех команд")

dp.include_router(commands_router)

# Коллектор ВСЕГДА последним, чтобы не перехватывать команды и FSM
dp.include_router(collector.collector_router) 

logging.basicConfig(level=logging.INFO)

# ==========================================
# ЗАПУСК
# ==========================================

async def set_bot_commands():
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Запуск бота"),
        types.BotCommand(command="help", description="Справка"),
        types.BotCommand(command="routes", description="Список маршрутов"),
        types.BotCommand(command="add_route", description="Добавить маршрут"),
        types.BotCommand(command="send_now", description="Отправить пост сейчас"),
        types.BotCommand(command="delete_route", description="Удалить маршрут"),
        types.BotCommand(command="freeze_route", description="Заморозить маршрут"),
        types.BotCommand(command="unfreeze_route", description="Разморозить маршрут"),
        types.BotCommand(command="edit_time", description="Изменить время отправки"),
        types.BotCommand(command="edit_intervals", description="Изменить интервалы"),
        types.BotCommand(command="edit_jitter", description="Изменить разброс"),
        types.BotCommand(command="import_message", description="Импортировать одно сообщение"),
        types.BotCommand(command="import_range", description="Импортировать диапазон"),
        types.BotCommand(command="skip_next", description="Пропустить публикацию"),
        types.BotCommand(command="add_target", description="Добавить цель к маршруту"),
        types.BotCommand(command="remove_target", description="Убрать цель из маршрута"),
        types.BotCommand(command="toggle_random", description="Вкл/выкл случайную рассылку"),
        types.BotCommand(command="add_singular", description="Создать singular-маршрут"),
        types.BotCommand(command="list_singular", description="Список singular-маршрутов"),
        types.BotCommand(command="set_rounds", description="Установить количество кругов"),
        types.BotCommand(command="extend_route", description="Продлить singular-маршрут"),
        types.BotCommand(command="get_rounds", description="Показать статус кругов"),
        types.BotCommand(command="import_singular", description="Импорт поста/галереи через пересылку"),
    ])


async def main():
    init_db()
    # await set_bot_commands()
    all_routes = get_all_routes(active_only=False)  # Загружаем все маршруты
    for r in all_routes:
        schedule_route_job(r)
    scheduler.start()
    logging.info("Бот запущен и слушает топики...")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот был остановлен неизвестной силой!")