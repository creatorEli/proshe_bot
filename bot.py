# 1 версия бота, будет тупо брать из топиков архива посты и кидать в указ. чат

import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import defaultdict

from config import ADMIN_ID, API_TOKEN, MAIN_SOURCE_CHAT_ID, TIMEZONE
from db_funcs import (
    init_db,
    add_route_db,
    get_all_routes,
    get_route_by_topic,
    save_post_db,
    mark_post_sent,
    reset_posts_for_route,
    get_random_unsent_post,
    get_route_by_id,
    delete_route_db
)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# Словарь для агрегации галерей
media_groups = defaultdict(list)
media_group_timers = {}

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

    message_ids = sorted(post['message_ids'])
    logging.info(f"Отправляем сообщения: {message_ids}")

    try:
        # Проверяем, доступен ли copy_messages (для галерей)
        if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
            await bot.copy_messages(
                chat_id=target_chat_id,
                from_chat_id=MAIN_SOURCE_CHAT_ID,
                message_ids=message_ids
            )
        else:
            for message_id in message_ids:
                await bot.copy_message(
                    chat_id=target_chat_id,
                    from_chat_id=MAIN_SOURCE_CHAT_ID,
                    message_id=message_id
                )
        mark_post_sent(post['id'])
        logging.info(f"Пост из {len(message_ids)} сообщений отправлен в чат {target_chat_id}")

    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")

    # try:
    #     await bot.copy_message(
    #         chat_id=target_chat_id,
    #         from_chat_id=MAIN_SOURCE_CHAT_ID,
    #         message_id=post['message_id']
    #     )
    #     mark_post_sent(post['id'])
    #     logging.info(f"Пост {post['message_id']} отправлен в чат {target_chat_id}")
    # except Exception as e:
    #     logging.error(f"Ошибка отправки: {e}")

@router.message(F.chat.id == MAIN_SOURCE_CHAT_ID)
async def collect_post(message: types.Message):
    # Игнорируем команды
    if message.text and message.text.startswith("/"):
        return

    # Игнорируем сообщения без контента
    if not (message.text or message.photo or message.video or message.document or message.animation):
        return
    
    # Слушаем главный чат. Если сообщение пришло в топик из нашего списка маршрутов - сохраняем
    topic_id = message.message_thread_id
    # если сообщение не в топике, игнорируем
    if not topic_id: 
        return

    # Проверяем, что этот топик в наших маршрутах
    route = get_route_by_topic(topic_id)
    if not route:
        return

    media_group_id = message.media_group_id

    if media_group_id:
        # Это часть галереи - добавляем в группу
        media_groups[media_group_id].append(message.message_id)

        # Отменяем предыдущий таймер если есть
        if media_group_id in media_group_timers:
            media_group_timers[media_group_id].cancel()

        # Запускаем новый таймер на 5 секунды
        async def save_media_group():
            await asyncio.sleep(5)

            message_ids = sorted(media_groups.pop(media_group_id, []))
            media_group_timers.pop(media_group_id, None)

            if(message_ids):
                save_post_db(route['id'], message_ids)
                logging.info(
                    f"Галерея из {len(message_ids)} сообщений сохранена для маршрута {route['id']}"
                )

        task = asyncio.create_task(save_media_group())
        media_group_timers[media_group_id] = task
        
    else:
        # Одиночное сообщение - сохраняем сразу
        save_post_db(route['id'], [message.message_id])
        logging.info(f"Пост {message.message_id} сохранен для маршрута {route['id']}")

    
# --- Админ команды для управления маршрутами ---

@router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message):
    # формат /add_route <ID_топика> <ID_целевого_чата> <время ЧЧ:ММ>
    assert message.text is not None
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

@router.message(Command("send_now"), F.from_user.id == ADMIN_ID)
async def cmd_send_now(message: types.Message):
    if not message.text:
        return

    args = message.text.split()

    if len(args) != 2:
        await message.answer(
            "Дорогой, используй формат:\n"
            "`/send_now <ID_маршрута>`\n\n"
            "Например такой:\n"
            "`/send_now 1`",
            parse_mode="Markdown"
        )
        return
    
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("Прости, но мне нужен числовой идентификатор маршрута.")
        return

    route = get_route_by_id(route_id)

    if not route:
        await message.answer(
            f"Я не смогла найти маршрут с ID {route_id}. Проверь `/routes`",
            parse_mode="Markdown"
        )
        return

    target_chat_id = route['target_chat_id']
    await message.answer(f"Пытаюсь отправить пост из маршрута {route_id}...")

    try:
        await send_random_post_job(route_id, target_chat_id)
        await message.answer("Готово! Проверь целевой чат :)")
    except Exception as e:
        await message.answer(f"Прости, я не смогла отправить сообщение. \nВот ошибка:\n{e}")
        logging.error(f"Ошибка мгновенной отправки: {e}")
    

@router.message(Command("delete_route"), F.from_user.id == ADMIN_ID)
async def cmd_delete_route(message: types.Message):
    if not message.text:
        return

    args = message.text.split()
    
    if len(args) != 2:
        await message.answer(
            "Дорогой, используй формат:\n"
            "`/delete_route <ID_маршрута>`\n\n"
            "Например так:\n"
            "`/delete_route 1`",
            parse_mode="Markdown"
        )
        return

    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("Прости, но мне нужно числовой идентификатор маршрута. Я не умею читать его словами :((")
        return

    job_id = f"route_{route_id}"

    # Сначала убираем задачу из планировщика, если она там есть
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logging.info(f"Планировщик: задача {job_id} удалена")

    route = get_route_by_id(route_id)

    if not route:
        await message.answer(
            f"Я не смогла найти маршрут с ID {route_id}. Мне очень жаль",
            parse_mode="Markdown"
        )
        return

    deleted = delete_route_db(route_id)

    if(deleted):
        await message.answer(
            f"Ура, я удалила маршрут {route_id}!\n"
            f"Топик `{route['source_topic_id']}` -> Чат `{route['target_chat_id']}`.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Не удалось удалить маршрут `{route_id}` из базы.",
            parse_mode="Markdown"
        )
    

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
