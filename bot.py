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
    get_route_by_id,
    get_routes_for_source,
    save_post_db,
    mark_post_sent,
    reset_posts_for_route,
    get_random_unsent_post,
    delete_route_db
)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

commands_router = Router()
collector_router = Router()

dp.include_router(commands_router)
dp.include_router(collector_router)


logging.basicConfig(level=logging.INFO)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Словарь для агрегации галерей
media_groups = defaultdict(list)
media_group_timers = {}


HELP_TEXT = (
    "Привет! Я бот для автоматической отправки постов по маршрутам.\n"
    "Используй команды ниже.\n\n"

    "Основные команды:\n"
    "`/start` - приветствие\n"
    "`/help` - эта справка\n"
    "`/routes` - показать сохранённые маршруты (только админ)\n\n"

    "Добавление маршрута:\n"
    "Старый формат:\n"
    "`/add_route <ID топика> <ID чата> <ЧЧ:ММ>`\n\n"
    "Новый формат:\n"
    "`/add_route <ID чата источника> <ID топика источника> <ID целевого чата> <ID целевого топика> <ЧЧ:ММ>`\n\n"

    "Условия:\n"
    "`0` вместо чата источника - использовать основной чат из конфига\n"
    "`0` вместо топика источника - брать весь чат источника целиком\n"
    "`0` вместо целевого топика - отправлять без топика, в общий поток\n\n"

    "Примеры:\n"
    "`/add_route 123 -100998877 15:30`\n"
    "`/add_route 0 123 -100998877 0 15:30`\n"
    "`/add_route -100111222333 0 -100998877 45 18:20`\n\n"

    "Управление маршрутами:\n"
    "`/send_now <ID маршрута>` - отправить пост из маршрута прямо сейчас (только админ)\n"
    "`/delete_route <ID маршрута>` - удалить маршрут (только админ)\n\n"

    "Импорт старых сообщений:\n"
    "`/import_message <ID маршрута> <ID сообщения>` - импортировать одно сообщение (только админ)\n"
    "`/import_range <ID маршрута> <начальный ID> <конечный ID>` - импортировать диапазон сообщений (только админ)\n"
)



# --- Админ команды для управления маршрутами ---
@commands_router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="Markdown")

@commands_router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет милый! Используй /help если хочешь посмотреть список всех команд")


@commands_router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message):
    assert message.text is not None

    args = message.text.split()

    help_text = (
        "Дорогой, используй один из форматов.\n\n"
        "Старый формат:\n"
        "`/add_route <ID_топика> <ID_чата> <ЧЧ:ММ>`\n\n"
        "Новый формат:\n"
        "`/add_route <ID_чата_источника> <ID_топика_источника> <ID_целевого_чата> <ID_целевого_топика> <ЧЧ:ММ>`\n\n"
        "Условия:\n"
        "`0` вместо чата-источника — использовать MAIN_SOURCE_CHAT_ID\n"
        "`0` вместо топика-источника — брать весь чат\n"
        "`0` вместо целевого топика — отправлять без топика / в общий поток\n\n"
        "Примеры:\n"
        "`/add_route 123 -100998877 15:30`\n"
        "`/add_route 0 123 -100998877 0 15:30`\n"
        "`/add_route -100111222333 0 -100998877 45 18:20`"
    )

    try:
        if len(args) == 4:
            # Старый формат:
            # /add_route <source_topic_id> <target_chat_id> <HH:MM>
            source_chat_id = 0
            source_topic_id = int(args[1])
            target_chat_id = int(args[2])
            target_topic_id = 0
            send_time = args[3]

        elif len(args) == 6:
            # Новый формат:
            # /add_route <source_chat_id> <source_topic_id> <target_chat_id> <target_topic_id> <HH:MM>
            source_chat_id = int(args[1])
            source_topic_id = int(args[2])
            target_chat_id = int(args[3])
            target_topic_id = int(args[4])
            send_time = args[5]

        else:
            await message.answer(help_text, parse_mode="Markdown")
            return

        h, m = map(int, send_time.split(':'))

        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Неверное время")

        route_id = add_route_db(
            source_chat_id=source_chat_id,
            source_topic_id=source_topic_id,
            target_chat_id=target_chat_id,
            target_topic_id=target_topic_id,
            send_time=send_time
        )

        # Динамически добавляем задачу в планировщик
        scheduler.add_job(
            send_random_post_job,
            trigger='cron',
            hour=h,
            minute=m,
            args=[route_id],
            id=f"route_{route_id}",
            replace_existing=True
        )

        effective_source_chat = source_chat_id or MAIN_SOURCE_CHAT_ID

        if source_topic_id == 0:
            source_description = f"чат `{effective_source_chat}` (весь чат)"
        else:
            source_description = f"чат `{effective_source_chat}`, топик `{source_topic_id}`"

        if target_topic_id == 0:
            target_description = f"чат `{target_chat_id}`"
        else:
            target_description = f"чат `{target_chat_id}`, топик `{target_topic_id}`"

        await message.answer(
            f"Я добавила маршрут {route_id}!\n"
            f"Источник: {source_description}\n"
            f"Цель: {target_description}\n"
            f"Время: {send_time}. Всё как ты сказал :)",
            parse_mode="Markdown"
        )

    except Exception as e:
        await message.answer(f"Дорогой, ты, кажется, ошибся: {e}\n\n{help_text}", parse_mode="Markdown")


@commands_router.message(Command("routes"), F.from_user.id == ADMIN_ID)
async def cmd_list_routes(message: types.Message):
    routes = get_all_routes()

    if not routes:
        await message.answer("Маршрутов пока нет. Ты всегда можешь их добавить)")
        return

    text = "Маршруты, которые я сохранила для тебя:\n\n"

    for r in routes:
        source_chat_id = r['source_chat_id'] or MAIN_SOURCE_CHAT_ID
        source_topic_id = r['source_topic_id'] or 0
        target_chat_id = r['target_chat_id']
        target_topic_id = r['target_topic_id'] or 0

        if source_topic_id == 0:
            source_text = f"чат `{source_chat_id}` (весь чат)"
        else:
            source_text = f"чат `{source_chat_id}`, топик `{source_topic_id}`"

        if target_topic_id == 0:
            target_text = f"чат `{target_chat_id}`"
        else:
            target_text = f"чат `{target_chat_id}`, топик `{target_topic_id}`"

        text += (
            f"ID `{r['id']}`:\n"
            f"Источник: {source_text}\n"
            f"Цель: {target_text}\n"
            f"Время: {r['send_time']}\n\n"
        )

    await message.answer(text, parse_mode="Markdown")


@commands_router.message(Command("send_now"), F.from_user.id == ADMIN_ID)
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

    await message.answer(f"Пытаюсь отправить пост из маршрута {route_id}...")

    sent = await send_random_post_job(route_id)

    if sent:
        await message.answer("Готово! Проверь целевой чат :)")
    else:
        await message.answer("Не получилось отправить пост. Подробности можно посмотреть в логах.")

    # try:
    #     await send_random_post_job(route_id)
    #     await message.answer("Готово! Проверь целевой чат :)")
    # except Exception as e:
    #     await message.answer(f"Прости, я не смогла отправить сообщение.\nВот ошибка:\n{e}")
    #     logging.error(f"Ошибка мгновенной отправки: {e}")


@commands_router.message(Command("delete_route"), F.from_user.id == ADMIN_ID)
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

    if deleted:
        source_chat_id = route['source_chat_id'] or MAIN_SOURCE_CHAT_ID
        source_topic_id = route['source_topic_id'] or 0
        target_chat_id = route['target_chat_id']
        target_topic_id = route['target_topic_id'] or 0

        if source_topic_id == 0:
            source_text = f"чат `{source_chat_id}` (весь чат)"
        else:
            source_text = f"чат `{source_chat_id}`, топик `{source_topic_id}`"

        if target_topic_id == 0:
            target_text = f"чат `{target_chat_id}`"
        else:
            target_text = f"чат `{target_chat_id}`, топик `{target_topic_id}`"

        await message.answer(
            f"Ура, я удалила маршрут {route_id}!\n"
            f"{source_text} -> {target_text}",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Не удалось удалить маршрут `{route_id}` из базы.",
            parse_mode="Markdown"
        )


@commands_router.message(Command("import_message"), F.from_user.id == ADMIN_ID)
async def cmd_import_message(message: types.Message):
    """
    Импортирует конкретное сообщение по ID маршрута и ID сообщения.
    Формат: /import_message <ID_маршрута> <ID_сообщения>
    """
    if not message.text:
        return
    
    args = message.text.split()
    
    if len(args) != 3:
        await message.answer(
            "Дорогой, используй формат:\n"
            "`/import_message <ID_маршрута> <ID_сообщения>`\n\n"
            "Пример:\n"
            "`/import_message 1 456`",
            parse_mode="Markdown"
        )
        return
    
    try:
        route_id = int(args[1])
        message_id = int(args[2])
    except ValueError:
        await message.answer("Прости, но ID маршрута и ID сообщения должны быть числами.")
        return
    
    # Проверяем, существует ли такой маршрут
    route = get_route_by_id(route_id)
    
    if not route:
        await message.answer(
            f"Я не смогла найти маршрут с ID `{route_id}`.\n"
            "Проверь список маршрутов через `/routes`",
            parse_mode="Markdown"
        )
        return
    
    # Определяем чат-источник для этого маршрута
    source_chat_id = route['source_chat_id'] or MAIN_SOURCE_CHAT_ID

    sent_msg = await bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=source_chat_id,
        message_id=message_id
    )

    try:
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=sent_msg.message_id
        )

    except Exception as e:
        logging.warning(f"Не удалось удалить временное сообщение после проверки: {e}")

    if save_post_db(route_id, [message_id]):
        await message.answer(
            f"Ура! Сообщение `{message_id}` из чата `{source_chat_id}` импортировано в маршрут `{route_id}`!",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"Похоже, сообщение `{message_id}` уже было импортировано в маршрут `{route_id}`.",
            parse_mode="Markdown"
        )
    
    # # Пытаемся скопировать сообщение, чтобы проверить его существование и доступность
    # try:
    #     # Копируем сообщение тебе в чат для проверки
    #     sent_msg = await bot.copy_message(
    #         chat_id=message.chat.id,
    #         from_chat_id=source_chat_id,
    #         message_id=message_id
    #     )
        
    #     # И сразу удаляем его, чтобы не засорять чат
    #     await bot.delete_message(
    #         chat_id=message.chat.id,
    #         message_id=sent_msg.message_id
    #     )
        
    #     # Если всё успешно, сохраняем в базу для этого маршрута
    #     save_post_db(route_id, [message_id])
        
    #     await message.answer(
    #         f"Ура! Сообщение `{message_id}` из чата `{source_chat_id}` импортировано в маршрут `{route_id}`!",
    #         parse_mode="Markdown"
    #     )
        
    # except Exception as e:
    #     await message.answer(
    #         f"Не удалось импортировать сообщение.\n"
    #         f"Возможно, бот не имеет к нему доступа, либо оно было удалено.\n\n"
    #         f"Ошибка:\n`{e}`",
    #         parse_mode="Markdown"
    #     )


@commands_router.message(Command("import_range"), F.from_user.id == ADMIN_ID)
async def cmd_import_range(message: types.Message):
    """
    Импортирует диапазон сообщений по ID маршрута.
    Формат: /import_range <ID_маршрута> <начальный_ID> <конечный_ID>
    """
    if not message.text:
        return
    
    args = message.text.split()
    
    if len(args) != 4:
        await message.answer(
            "Дорогой, используй формат:\n"
            "`/import_range <ID_маршрута> <начальный_ID> <конечный_ID>`\n\n"
            "Пример:\n"
            "`/import_range 1 100 150`",
            parse_mode="Markdown"
        )
        return
    
    try:
        route_id = int(args[1])
        start_id = int(args[2])
        end_id = int(args[3])
    except ValueError:
        await message.answer("Прости, но все параметры должны быть числами.")
        return
    
    if start_id > end_id:
        await message.answer("Начальный ID должен быть меньше конечного.")
        return
    
    # Проверяем маршрут
    route = get_route_by_id(route_id)
    
    if not route:
        await message.answer(
            f"Я не смогла найти маршрут с ID `{route_id}`.",
            parse_mode="Markdown"
        )
        return
    
    # Определяем чат-источник
    source_chat_id = route['source_chat_id'] or MAIN_SOURCE_CHAT_ID
    
    await message.answer(f"Начинаю импорт сообщений с {start_id} по {end_id} для маршрута {route_id}... Это может занять немного времени.")
    
    imported_count = 0
    failed_count = 0
    skipped_count = 0
    
    for msg_id in range(start_id, end_id + 1):
        try:
            sent_msg = await bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=source_chat_id,
                message_id=msg_id
            )

            try:
                await bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=sent_msg.message_id
                )
            except Exception as e:
                logging.warning(f"Не удалось удалить временное сообщение: {e}")

            if save_post_db(route_id, [msg_id]):
                imported_count += 1
            else:
                skipped_count += 1

        except Exception:
            failed_count += 1

        await asyncio.sleep(0.15)

    await message.answer(
        f"Импорт для маршрута `{route_id}` завершён!\n"
        f"Успешно импортировано: {imported_count}\n"
        f"Пропущено/уже было: {skipped_count}\n"
        f"Не удалось: {failed_count}"
    )



# ЛОГИКА БОТА

async def send_random_post_job(route_id: int):
    # задача планировщика для конкретного маршрута
    route = get_route_by_id(route_id)
    if not route:
        logging.warning(f"Маршрут {route_id} не найден. Пропускаю отправку.")
        return

    logging.info(f"Сработка маршрута {route_id}. Ищем пост...")

    source_chat_id = route['source_chat_id'] or MAIN_SOURCE_CHAT_ID
    target_chat_id = route['target_chat_id']
    target_topic_id = route['target_topic_id'] or 0

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
    if not message_ids:
        logging.warning(f"Пост {post['id']} из маршрута {route_id} пустой.")
        return False

    logging.info(f"Отправляем сообщения: {message_ids}")

    # Если указан целевой топик, добавляем message_thread_id
    send_kwargs = {}
    if target_topic_id:
        send_kwargs['message_thread_id'] = target_topic_id

    try:
        # Проверяем, доступен ли copy_messages (для галерей)
        if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
            await bot.copy_messages(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_ids=message_ids,
                **send_kwargs
            )
        else:
            for message_id in message_ids:
                await bot.copy_message(
                    chat_id=target_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=message_id,
                    **send_kwargs
                )
        mark_post_sent(post['id'])
        logging.info(
            f"Пост из {len(message_ids)} сообщений отправлен "
            f"в чат {target_chat_id} (topic={target_topic_id or 'нет'})"
        )
        return True

    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")
        return False


@collector_router.message()
async def collect_post(message: types.Message):
    """
    Слушаем сообщения из чатов и сохраняем их, если есть подходящий маршрут.
    """
    # Приватные диалоги с ботом не используем как источник постов
    if message.chat.type == 'private':
        return

    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        return

    # Игнорируем сообщения без контента
    if not (
        message.text or
        message.photo or
        message.video or
        message.document or
        message.animation
    ):
        return

    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0

    # Ищем маршруты, которые подходят для этого чата и топика
    routes = get_routes_for_source(chat_id, topic_id)

    if not routes:
        return

    media_group_id = message.media_group_id

    if media_group_id:
        # Это часть галереи.
        # Ключ теперь включает маршрут, чтобы поддерживать несколько маршрутов.
        for route in routes:
            key = (route['id'], media_group_id)

            media_groups[key].append(message.message_id)

            # Отменяем предыдущий таймер, если он уже был
            if key in media_group_timers:
                media_group_timers[key].cancel()

            # Запускаем новый таймер.
            # Дефолтные аргументы нужны, чтобы функция правильно запомнила текущий маршрут.
            async def save_media_group(key=key, route_id=route['id']):
                await asyncio.sleep(5)

                message_ids = sorted(media_groups.pop(key, []))
                media_group_timers.pop(key, None)

                if message_ids:
                    save_post_db(route_id, message_ids)
                    logging.info(
                        f"Галерея из {len(message_ids)} сообщений "
                        f"сохранена для маршрута {route_id}"
                    )

            task = asyncio.create_task(save_media_group())
            media_group_timers[key] = task

    else:
        # Одиночное сообщение - сохраняем сразу для всех подходящих маршрутов
        for route in routes:
            save_post_db(route['id'], [message.message_id])
            logging.info(
                f"Пост {message.message_id} сохранён для маршрута {route['id']}"
            )

async def set_bot_commands():
    await bot.set_my_commands(
        [
            types.BotCommand(command="start", description="Запуск бота"),
            types.BotCommand(command="help", description="Справка по командам"),
            types.BotCommand(command="routes", description="Список маршрутов"),
            types.BotCommand(command="add_route", description="Добавить маршрут"),
            types.BotCommand(command="send_now", description="Отправить пост сейчас"),
            types.BotCommand(command="delete_route", description="Удалить маршрут"),
            types.BotCommand(command="import_message", description="Импортировать одно сообщение"),
            types.BotCommand(command="import_range", description="Импортировать диапазон сообщений"),
        ]
    )
    
# LAUNCH
async def main():
    init_db()

    await set_bot_commands()

    # При старте бота подгружаем все маршруты из БД в планировщик
    routes = get_all_routes()

    for r in routes:
        h, m = map(int, r['send_time'].split(":"))
        scheduler.add_job(
            send_random_post_job,
            trigger='cron',
            hour=h,
            minute=m,
            args=[r['id']],
            id=f"route_{r['id']}",
            replace_existing=True
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
