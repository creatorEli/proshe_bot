# 1 версия бота, будет тупо брать из топиков архива посты и кидать в указ. чат

import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db_funcs import init_db, add_route_db, get_all_routes, get_route_by_topic, save_post_db, mark_post_sent, reset_posts_for_route, get_random_unsent_post
from config import ADMIN_ID, API_TOKEN, MAIN_SOURCE_CHAT_ID, TIMEZONE


bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)
scheduler = AsyncIOScheduler(TIMEZONE)


# ЛОГИКА БОТА

async def send_random_post_job(route_id: int, target_chat_id: int):
    # задача планировщика для конкретного маршрута
    logging.info(f"Сработка маршрута {route_id}. Ищем пост...")
    post = get_random_unsent_post(route_id)

    # Если все посты из базы уже отправлены, сбрасываем флаги и берём заново
    if not post:
        logging.info("Все посты отправлены. Сбрасываем флаги для нового цикла")
        reset_posts_for_route(route_id)
        post = get_random_unsent_post(route_id)
    
    if not post:
        logging.warning(f"В маршруте {route_id} вообще нет постов!")
        return
    
    try:
        await bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=MAIN_SOURCE_CHAT_ID,
            message_id=post['message_id']
        )
        mark_post_sent(post[id])
        logging.info(f"Пост {post['message_id']} отправлен в чат {target_chat_id}")
    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")

@router.message(F.chat.id == MAIN_SOURCE_CHAT_ID)
async def collect_post(message: types.Message):
    # Слушаем главный чат. Если сообщение пришло в топик из нашего списка маршрутов - сохраняем
    topic_id = message.message_thread_id

    # если сообщение не в топике, игнорируем
    if not topic_id: 
        return

    # Проверяем, что этот топик в наших маршрутах
    route = get_route_by_topic(topic_id)
    if route:
        # Сохраняем только если есть контент
        if message.text or message.photo or message.video or message.document or message.animation:
            save_post_db(route['id'], message.message_id)
            logging.info(f"Пост {message.message_id} сохранен для маршрута {route['id']}")
    
# -- Админ команды для управления маршрутами ---

@router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message):
    # формат /add_route <ID_топика> <ID_целевого_чата> <время ЧЧ:ММ>
    args = message.text.split()
    if len(args) != 4:
        await message.answer("Дорогой, используй такой формат: `/add_route <ID_топика> <ID_чата> <ЧЧ:ММ>`\nПример: `/add_route 000 -100998877 15:30`", parse_mode="Markdown")
        return

    try:
        topic_id = int(args[1])
        target_chat = int(args[2])
        send_time = args[3]

        h, m = map(int, send_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        
        route_id = add_route_db(topic_id, target_chat, send_time)
        #dynamically adding task in scheduler
        scheduler.add_job(
            send_random_post_job,
            trigger='cron',
            hour=h, minute=m,
            args=[route_id, target_chat],
            id=f"route_{route_id}"
        )

        await message.answer(f"Я добавила маршрут {route_id}! Топик {topic_id} -> чат {target_chat} в {send_time}. Всё как ты сказал :)", parse_mode="Markdown")

    except Exception as e:
        await message.answer(f"Дорогой, ты, кажется, ошибся: {e}")

@router.message(Command("routes"), F.from_user.id == ADMIN_ID)
async def cmd_list_routes(message: types.Message):
    routes = get_all_routes()
    if not routes:
        await message.answer("Маршрутов пока нет. Ты всегда можешь их добавить)")
        return
    
    text = "Маршруты, которые я сохранила для тебя: \n\n"

    for r in routes:
        text += f"ID `{r['id']}`: Топик `{r['source_topic_id']}` -> Чат `{r['target_chat_id']}` (в {r['send_time']})\n"
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет милый! Используй /routes если хочешь посмотреть все маршруты")

# LAUNCH

async def main():
    init_db()

    # При старте бота подгружаем все маршруты из БД в планировщик
    routes = get_all_routes()

    for r in routes:
        h, m = map(int, r['send_time'].split(":"))
        scheduler.add_job(
            send_random_post_job,
            trigger='cron',
            hour=h, minute=m,
            args=[r['id'], r['target_chat_id']],
            id=f"route_{r['id']}"
        )
        logging.info(f"Загружен маршрут {r['id']} на {r['send_time']}")
    
    scheduler.start()
    logging.info("Бот запущен и слушает топики...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен!")
