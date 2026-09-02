# 1 версия бота, будет тупо брать из топиков архива посты и кидать в указ. чат

import asyncio
import json
import logging
import random
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
    delete_route_db,
    skip_next_publication,
    delete_post_db,
    update_route_schedule
)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class AddRouteStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_time = State()
    waiting_for_intervals = State()
    waiting_for_jitter = State()

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
    "Все команды управления доступны только администратору.\n"

    "<b>Основные команды:</b>\n"
    "/start - приветствие\n"
    "/help - эта справка\n"
    "/routes - показать сохранённые маршруты (только админ)\n"
    "/send_now &lt;ID маршрута&gt; - отправить пост из маршрута прямо сейчас (только админ)\n"
    "/delete_route &lt;ID маршрута&gt; - удалить маршрут (только админ)\n\n"

    "<b>Добавление маршрута:</b>\n"
    "<code>/add_route &lt;ID топика&gt; &lt;ID чата&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы через запятую ДД:ЧЧ:ММ:СС&gt;</code>\n"
    "или полный формат:\n"
    "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы ДД:ЧЧ:ММ:СС через запятую&gt;</code>\n\n"

    "<b>Пошаговое добавление маршрута:</b>\n"
    "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt;</code>\n"
    "После этого я отдельно спрошу название, время, интервал и разброс.\n\n"

    "<b>Отмена пошагового добавления:</b>\n"
    "/cancel\n\n"

    "Первый пост отправляется в указанное время, далее каждые &lt;интервал&gt; секунд.\n"
    "86400 сек = раз в сутки, 3600 = раз в час, 60 = раз в минуту\n\n"

    "<b>Условия:</b>\n"
    "<code>0</code> вместо чата источника - использовать основной чат из конфига\n"
    "<code>0</code> вместо топика источника - брать весь чат источника целиком\n"
    "<code>0</code> вместо целевого топика - отправлять без топика, в общий поток\n\n"

    "<b>Импорт старых сообщений:</b>\n"
    "/import_message &lt;ID маршрута&gt; &lt;ID сообщения&gt; - импортировать одно сообщение (только админ)\n"
    "/import_range &lt;ID маршрута&gt; &lt;начальный ID&gt; &lt;конечный ID&gt; - импортировать диапазон сообщений (только админ)\n"

    "<b>Пропуск следующего поста:</b>\n"
    "/skip_next &lt;ID маршрута&gt; - пропустить ближайшую публикацию (только админ)\n"
)



# --- Админ команды для управления маршрутами ---
@commands_router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@commands_router.message(Command("start"), F.from_user.id == ADMIN_ID)
async def cmd_start(message: types.Message):
    await message.answer("Привет милый! Используй /help если хочешь посмотреть список всех команд")


@commands_router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message, state: FSMContext):
    assert message.text is not None
    args = message.text.split()

    help_text = (
        "<b>Однострочный режим:</b>\n"
        "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt;</code>\n\n"
        "Интервалы — список через запятую в формате <code>ДД:ЧЧ:ММ:СС</code>\n"
        "Примеры:\n"
        "<code>/add_route 0 123 -100998877 0 15:30 00:09:00:00</code>\n"
        "<code>/add_route 0 123 -100998877 0 15:30 00:09:00:00,00:15:00:00</code>\n\n"
        "<b>Пошаговый режим:</b>\n"
        "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt;</code>\n"
        "Затем я спрошу название, время, интервалы и разброс."
    )

    try:
        if len(args) == 5:
            # Пошаговый режим
            source_chat_id = int(args[1])
            source_topic_id = int(args[2])
            target_chat_id = int(args[3])
            target_topic_id = int(args[4])
            await state.update_data(
                source_chat_id=source_chat_id,
                source_topic_id=source_topic_id,
                target_chat_id=target_chat_id,
                target_topic_id=target_topic_id
            )
            await state.set_state(AddRouteStates.waiting_for_name)
            await message.answer("Отлично! Теперь отправь название для этого маршрута.")

        elif len(args) == 7 or len(args) == 8:
            # Однострочный режим
            source_chat_id = int(args[1])
            source_topic_id = int(args[2])
            target_chat_id = int(args[3])
            target_topic_id = int(args[4])
            send_time = args[5]
            intervals_str = args[6]

            # Валидация времени
            h, m = map(int, send_time.split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Неверное время")

            # Парсим интервалы
            intervals = parse_intervals_list(intervals_str)
            intervals_json = json.dumps(intervals)

            # Разброс (опционально)
            jitter_seconds = int(args[7]) if len(args) == 8 else 0

            route_name = f"Маршрут {source_topic_id} -> {target_chat_id}"

            route_id = add_route_db(
                source_chat_id=source_chat_id,
                source_topic_id=source_topic_id,
                target_chat_id=target_chat_id,
                target_topic_id=target_topic_id,
                send_time=send_time,
                intervals_json=intervals_json,
                route_name=route_name,
                jitter_seconds=jitter_seconds
            )

            route = get_route_by_id(route_id)
            schedule_route_job(route)

            intervals_display = ', '.join(format_interval(i) for i in intervals)

            await message.answer(
                f"Маршрут {route_id} создан!\n"
                f"Название: {route_name}\n"
                f"Первый пост в {send_time}\n"
                f"Интервалы: [{intervals_display}]",
                parse_mode="HTML"
            )
        else:
            await message.answer(help_text, parse_mode="HTML")

    except Exception as e:
        error_text = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        await message.answer(
            f"Ошибка: {error_text}\n\n{help_text}",
            parse_mode="HTML"
        )


@commands_router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")


@commands_router.message(AddRouteStates.waiting_for_name, F.from_user.id == ADMIN_ID)
async def route_step_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Название маршрута нужно прислать обычным текстом.")
        return
    
    route_name = message.text

    await state.update_data(route_name=route_name)
    await state.set_state(AddRouteStates.waiting_for_time)

    await message.answer(f"Название сохранено: {route_name}\nТеперь отправь время первой отправки в формате ЧЧ:ММ")


@commands_router.message(AddRouteStates.waiting_for_time, F.from_user.id == ADMIN_ID)
async def route_step_time(message: types.Message, state: FSMContext):
    send_time = message.text
    try:
        h, m = map(int, send_time.split(':')) # type: ignore
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        await message.answer("Неверный формат времени. Используй ЧЧ:ММ, например 15:30")
        return

    await state.update_data(send_time=send_time)
    await state.set_state(AddRouteStates.waiting_for_intervals)
    await message.answer(
        "Время сохранено. Теперь отправь список интервалов в формате ДД:ЧЧ:ММ:СС через запятую.\n"
        "Например: 00:09:00:00,00:15:00:00"
    )


@commands_router.message(AddRouteStates.waiting_for_intervals, F.from_user.id == ADMIN_ID)
async def route_step_intervals(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Интервалы нужно прислать текстом в формате ДД:ЧЧ:ММ:СС")
        return

    try:
        intervals = parse_intervals_list(message.text.strip())
        if not intervals:
            raise ValueError("Список интервалов пуст")
    except ValueError as e:
        await message.answer(
            f"Ошибка: {e}\n"
            "Формат: ДД:ЧЧ:ММ:СС через запятую.\n"
            "Например: 00:09:00:00,00:15:00:00"
        )
        return

    await state.update_data(intervals=intervals)
    await state.set_state(AddRouteStates.waiting_for_jitter)

    intervals_display = ', '.join(format_interval(i) for i in intervals)
    await message.answer(
        f"Интервалы сохранены: [{intervals_display}]\n"
        "Теперь отправь разброс в секундах (0 если не нужен)"
    )


@commands_router.message(AddRouteStates.waiting_for_jitter, F.from_user.id == ADMIN_ID)
async def route_step_jitter(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer(
            "Разброс нужно прислать в формате ДД:ЧЧ:ММ:СС\n"
            "Или отправь 0, если разброс не нужен"
        )
        return

    text = message.text.strip()

    # Обрабатываем "0" отдельно, т.к. parse_interval не принимает ноль
    if text in ('0', '00:00:00:00'):
        jitter_seconds = 0
    else:
        try:
            jitter_seconds = parse_interval(text)  # ← ОДНО число, не список
        except ValueError as e:
            await message.answer(
                f"Ошибка: {e}\n"
                "Формат: ДД:ЧЧ:ММ:СС, например 00:00:05:00\n"
                "Или отправь 0, если разброс не нужен"
            )
            return

    data = await state.get_data()
    intervals = data['intervals']

    # Разброс должен быть меньше минимального интервала
    min_interval = min(intervals)
    if jitter_seconds >= min_interval:
        await message.answer(
            f"Разброс ({format_interval(jitter_seconds)}) должен быть меньше "
            f"минимального интервала ({format_interval(min_interval)}).\n"
            "Попробуй ещё раз"
        )
        return

    # Все данные собраны, создаём маршрут
    route_id = add_route_db(
        source_chat_id=data['source_chat_id'],
        source_topic_id=data['source_topic_id'],
        target_chat_id=data['target_chat_id'],
        target_topic_id=data['target_topic_id'],
        send_time=data['send_time'],
        intervals_json=json.dumps(intervals),
        route_name=data['route_name'],
        jitter_seconds=jitter_seconds  # ← теперь это int
    )

    route = get_route_by_id(route_id)
    schedule_route_job(route)

    await state.clear()

    intervals_display = ', '.join(format_interval(i) for i in intervals)
    jitter_text = f", разброс {format_interval(jitter_seconds)}" if jitter_seconds > 0 else ""

    await message.answer(
        f"Маршрут {route_id} успешно создан!\n"
        f"Название: {data['route_name']}\n"
        f"Первый пост в {data['send_time']}\n"
        f"Интервалы: [{intervals_display}]{jitter_text}"
    )


@commands_router.message(Command("routes"), F.from_user.id == ADMIN_ID)
async def cmd_list_routes(message: types.Message):
    routes = get_all_routes()

    if not routes:
        await message.answer("Маршрутов пока нет. Ты всегда можешь их добавить)")
        return

    text = "Маршруты, которые я сохранила для тебя:\n\n"

    for r in routes:
        route_name = r['route_name'] or f"Маршрут {r['id']}"
        intervals = json.loads(r['intervals_json'] or '[]')
        intervals_display = ', '.join(format_interval(i) for i in intervals) if intervals else "нет"

        source_chat_id = r['source_chat_id'] or MAIN_SOURCE_CHAT_ID
        source_topic_id = r['source_topic_id'] or 0
        target_chat_id = r['target_chat_id']
        target_topic_id = r['target_topic_id'] or 0

        source_text = f"чат <code>{source_chat_id}</code>"
        if source_topic_id:
            source_text += f", топик <code>{source_topic_id}</code>"
        else:
            source_text += " (весь чат)"

        target_text = f"чат <code>{target_chat_id}</code>"
        if target_topic_id:
            target_text += f", топик <code>{target_topic_id}</code>"

        text += (
            f"ID <code>{r['id']}</code>: {route_name}\n"
            f"  Источник: {source_text}\n"
            f"  Цель: {target_text}\n"
            f"  Первый пост: {r['send_time']}\n"
            f"  Интервалы: [{intervals_display}]\n"
            f"  Разброс: {r['jitter_seconds']} сек.\n\n"
        )

    await message.answer(text, parse_mode="HTML")


@commands_router.message(Command("send_now"), F.from_user.id == ADMIN_ID)
async def cmd_send_now(message: types.Message):
    if not message.text:
        return

    args = message.text.split()

    if len(args) != 2:
        await message.answer(
            "Дорогой, используй формат:\n"
            "<code>/send_now &lt;ID_маршрута&gt;</code>\n\n"
            "Например такой:\n"
            "<code>/send_now 1</code>",
            parse_mode="HTML"
        )
        return

    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("Прости, но мне нужен числовой идентификатор маршрута.", parse_mode="HTML")
        return

    route = get_route_by_id(route_id)

    if not route:
        await message.answer(
            f"Я не смогла найти маршрут с ID {route_id}. Проверь /routes",
            parse_mode="HTML"
        )
        return

    await message.answer(f"Пытаюсь отправить пост из маршрута {route_id}...")

    sent = await send_random_post_job(route_id, manual_send=True)

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
            "<code>/delete_route &lt;ID_маршрута&gt;</code>\n\n"
            "Например так:\n"
            "<code>/delete_route 1</code>",
            parse_mode="HTML"
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
            parse_mode="HTML"
        )
        return

    deleted = delete_route_db(route_id)

    if deleted:
        route_name = route['route_name'] or f"Маршрут {route_id}"
        await message.answer(
            f"Ура, я удалила маршрут {route_id} ({route_name})!",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"Не удалось удалить маршрут {route_id} из базы.",
            parse_mode="HTML"
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
            "<code>/import_message &lt;ID_маршрута&lt; &lt;ID_сообщения&lt;</code>\n\n"
            "Пример:\n"
            "<code>/import_message 1 456</code>",
            parse_mode="HTML"
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
            f"Я не смогла найти маршрут с ID <code>{route_id}</code>.\n"
            "Проверь список маршрутов через <code>/routes</code>",
            parse_mode="HTML"
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
            f"Ура! Сообщение <code>{message_id}</code> из чата <code>{source_chat_id}</code> импортировано в маршрут <code>{route_id}</code>!",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"Похоже, сообщение <code>{message_id}</code> уже было импортировано в маршрут <code>{route_id}</code>.",
            parse_mode="HTML"
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
            "<code>/import_range &lt;ID_маршрута&lt; &lt;начальный_ID&lt; &lt;конечный_ID&lt;</code>\n\n"
            "Пример:\n"
            "<code>/import_range 1 100 150</code>",
            parse_mode="HTML"
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
            f"Я не смогла найти маршрут с ID <code>{route_id}</code>.",
            parse_mode="HTML"
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


@commands_router.message(Command("skip_next"), F.from_user.id == ADMIN_ID)
async def cmd_skip_next(message: types.Message):
    if not message.text:
        return

    args = message.text.split()

    if len(args) != 2:
        await message.answer(
            "Формат: <code>/skip_next &lt;ID маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return

    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("ID маршрута должен быть числом.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    # Пропускаем публикацию и получаем новое время
    next_run = skip_next_publication(route_id)

    if next_run:
        job_id = f"route_{route_id}"
        jitter = route['jitter_seconds'] or 0
        if jitter > 0:
            actual_jitter = random.randint(0, jitter)
            next_run = next_run + timedelta(seconds=actual_jitter)

        scheduler.add_job(
            send_random_post_job,
            trigger='date',
            run_date=next_run,
            args=[route_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True
        )

        # Форматируем время для вывода админу
        # next_run уже содержит часовой пояс (timezone-aware datetime)
        # Показываем в часовом поясе, который задан в конфиге
        time_str = next_run.strftime('%Y-%m-%d %H:%M:%S %Z')
        
        # Также показываем в формате "через сколько" для наглядности
        now = datetime.now(ZoneInfo(TIMEZONE))
        delta = next_run - now
        total_seconds = int(delta.total_seconds())
        
        if total_seconds > 86400:
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            delta_str = f"{days} дн. {hours} ч."
        elif total_seconds > 3600:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            delta_str = f"{hours} ч. {minutes} мин."
        elif total_seconds > 60:
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            delta_str = f"{minutes} мин. {seconds} сек."
        else:
            delta_str = f"{total_seconds} сек."

        await message.answer(
            f"Ближайшая публикация маршрута <code>{route_id}</code> пропущена.\n"
            f"Следующая: <code>{time_str}</code>\n"
            f"(через {delta_str}, часовой пояс: {TIMEZONE})",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"Не удалось пропустить публикацию для маршрута {route_id}.\n"
            f"Возможно, у маршрута нет интервалов или он не найден."
        )



# ЛОГИКА БОТА

SOURCE_MESSAGE_MISSING_ERRORS = (
    'message to copy not found',
    'messages to copy not found',
    'message not found',
    'message to forward not found',
    'there are no messages to forward',
    'message_id_invalid',
    'wrong message id',
    'invalid message id',
    "message can't be copied",
    'was not forwarded',           # ДЛЯ УДАЛЁННОЙ ГАЛЕРЕИ, 
    # НО МОЖЕТ ВЫЗВАТЬ УДАЛЕНИЕ ВСЕХ СООБЩЕНИЙ ЕСЛИ ПРОБЕЛМА В ЧАТАХ!!!
    'failed to send message',
)


def is_missing_source_message_error(error: Exception) -> bool:
    """
    Проверяет, что ошибка связана с отсутствием/недоступностью
    исходного сообщения или галереи в чате-источнике.
    """
    error_str = str(error).lower()
    return any(marker in error_str for marker in SOURCE_MESSAGE_MISSING_ERRORS)


async def send_random_post_job(route_id: int, _attempt: int = 0, manual_send: bool = False) -> bool:
    """
    Задача планировщика для конкретного маршрута.
    При удалённом посте автоматически пытается отправить следующий.
    Максимум 5 попыток, чтобы избежать бесконечного цикла.
    
    manual_send=True - ручная отправка через /send_now, не обновляет расписание
    """
    MAX_ATTEMPTS = 5

    if _attempt >= MAX_ATTEMPTS:
        logging.warning(
            f"Маршрут {route_id}: достигнуто макс. число попыток ({MAX_ATTEMPTS}). "
            f"Все доступные посты, вероятно, удалены."
        )
        return False

    route = get_route_by_id(route_id)
    if not route:
        logging.warning(f"Маршрут {route_id} не найден. Пропускаю отправку.")
        return False

    source_chat_id = route['source_chat_id'] or MAIN_SOURCE_CHAT_ID
    target_chat_id = route['target_chat_id']
    target_topic_id = route['target_topic_id'] or 0

    post = get_random_unsent_post(route_id)

    if not post:
        logging.info("Все посты отправлены. Сбрасываем флаги для нового цикла")
        reset_posts_for_route(route_id)
        post = get_random_unsent_post(route_id)

    if not post:
        logging.warning(f"В маршруте {route_id} вообще нет постов!")
        return False

    message_ids = sorted(post['message_ids'])
    if not message_ids:
        logging.warning(f"Пост {post['id']} из маршрута {route_id} пустой. Удаляю.")
        delete_post_db(post['id'])
        # При ручной отправке не делаем рекурсию
        if manual_send:
            return False
        # Рекурсивно пробуем следующий пост
        return await send_random_post_job(route_id, _attempt + 1, manual_send=manual_send)

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

        # Планируем следующую публикацию (только если не ручная отправка)
        if not manual_send:
            _schedule_next_publication(route_id, route)

        logging.info(f"Пост отправлен для маршрута {route_id}.")
        return True

    except Exception as e:
        if is_missing_source_message_error(e):
            logging.warning(
                f"Пост {post['id']} недоступен в источнике. Удаляю и пробую следующий. "
                f"(попытка {_attempt + 1}/{MAX_ATTEMPTS})"
            )
            delete_post_db(post['id'])

            # При ручной отправке не делаем рекурсию
            if manual_send:
                return False

            # Пробуем следующий пост без ожидания
            return await send_random_post_job(route_id, _attempt + 1, manual_send=manual_send)

        # Остальные ошибки: сеть, лимиты, проблемы с целевым чатом и т.д.
        logging.error(f"Ошибка отправки маршрута {route_id}: {e}")
        return False


def _schedule_next_publication(route_id: int, route):
    intervals = json.loads(route['intervals_json'] or '[]')
    if not intervals:
        return

    current_index = route['interval_index'] or 0
    interval_seconds = intervals[current_index]
    next_index = (current_index + 1) % len(intervals)

    tz = ZoneInfo(TIMEZONE)
    next_run = datetime.now(tz) + timedelta(seconds=interval_seconds)

    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        actual_jitter = random.randint(0, jitter)
        next_run = next_run + timedelta(seconds=actual_jitter)

    update_route_schedule(route_id, next_index, next_run.isoformat())

    job_id = f"route_{route_id}"

    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=next_run,
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True
    )

    logging.info(
        f"Маршрут {route_id}: следующий пост через {format_interval(interval_seconds)}, "
        f"в {next_run}"
    )


def schedule_route_job(route):
    route_id = route['id']
    job_id = f"route_{route_id}"

    intervals = json.loads(route['intervals_json'] or '[]')
    send_time = route['send_time']

    if not intervals or not send_time:
        logging.warning(f"Маршрут {route_id}: нет интервалов или времени отправки")
        return

    saved_next_run = route['next_run_time']

    if saved_next_run:
        try:
            start_date = datetime.fromisoformat(saved_next_run)
            logging.info(f"Маршрут {route_id}: восстановлен next_run_time = {start_date}")
        except ValueError:
            start_date = _calculate_initial_start(send_time)
    else:
        start_date = _calculate_initial_start(send_time)
        logging.info(f"Маршрут {route_id}: первый запуск, start_date = {start_date}")

    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        actual_jitter = random.randint(0, jitter)
        start_date = start_date + timedelta(seconds=actual_jitter)
        logging.info(f"Маршрут {route_id}: добавлен разброс {actual_jitter} сек.")

    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=start_date,
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True
    )

    intervals_display = ', '.join(format_interval(i) for i in intervals)
    logging.info(
        f"Загружен маршрут {route_id}: старт {start_date}, "
        f"интервалы [{intervals_display}]"
    )


def _calculate_initial_start(send_time: str) -> datetime:
    """Вычисляет ближайший момент ЧЧ:ММ в часовом поясе TIMEZONE."""
    h, m = map(int, send_time.split(':'))
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_date = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start_date <= now:
        start_date += timedelta(days=1)
    return start_date


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
            types.BotCommand(command="skip_next", description="Пропустить ближайшую публикацию"),
        ]
    )


def parse_interval(interval_str: str) -> int:
    """
    Парсит строку формата ДД:ЧЧ:ММ:СС в секунды.
    Примеры: '00:09:00:00' -> 32400, '01:00:00:00' -> 86400
    """
    parts = interval_str.strip().split(':')
    if len(parts) != 4:
        raise ValueError(f"Неверный формат интервала: {interval_str}. Ожидается ДД:ЧЧ:ММ:СС")
    
    days, hours, minutes, seconds = map(int, parts)

    if days < 0:
        raise ValueError(f"Количество дней не может быть отрицательным: {interval_str}")
    
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        raise ValueError(f"Неверные значения в интервале: {interval_str}")
    
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    
    if total <= 0:
        raise ValueError(f"Интервал должен быть больше нуля: {interval_str}")
    
    return total


def format_interval(seconds: int) -> str:
    """
    Форматирует секунды в строку ДД:ЧЧ:ММ:СС.
    Пример: 32400 -> '00:09:00:00'
    """
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    secs = seconds % 60
    return f"{days:02d}:{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_intervals_list(intervals_str: str) -> list[int]:
    """
    Парсит список интервалов, разделённых запятыми.
    Пример: '00:09:00:00,00:15:00:00' -> [32400, 54000]
    """
    parts = [p.strip() for p in intervals_str.split(',')]
    return [parse_interval(p) for p in parts if p]



# LAUNCH
async def main():
    init_db()

    await set_bot_commands()

    # При старте бота подгружаем все маршруты из БД в планировщик
    routes = get_all_routes()

    for r in routes:
        schedule_route_job(r)
    
    scheduler.start()
    logging.info("Бот запущен и слушает топики...")
    await dp.start_polling(bot)
    logging.info("Бот был остановлен неизвестной силой!")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен!")
