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
    add_chat_topic, get_chat_topic_by_id, get_chat_topic_by_tg_ids,
    add_route, get_route_by_id, get_all_routes, update_route_schedule,
    delete_route, skip_next_publication,
    add_route_target, get_route_targets, get_note_for_target,
    save_post, mark_post_sent, reset_posts_for_route,
    get_random_unsent_post, delete_post,
    get_routes_for_source,  # <-- новая функция из db_funcs
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

# ==========================================
# ТЕКСТЫ И УТИЛИТЫ
# ==========================================

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
    "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы ДД:ЧЧ:ММ:СС&gt;</code>\n"
    "или пошагово:\n"
    "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; &lt;цель_топик&gt;</code>\n"
    "После этого я отдельно спрошу название, время, интервал и разброс.\n\n"
    "<b>Отмена:</b> /cancel\n\n"
    "<b>Условия:</b>\n"
    "<code>0</code> вместо чата источника - использовать основной чат из конфига\n"
    "<code>0</code> вместо топика источника - брать весь чат целиком\n"
    "<code>0</code> вместо целевого топика - отправлять в общий поток (без топика)\n\n"
    "<b>Импорт сообщений:</b>\n"
    "/import_message &lt;ID маршрута&gt; &lt;ID сообщения&gt;\n"
    "/import_range &lt;ID маршрута&gt; &lt;начальный ID&gt; &lt;конечный ID&gt;\n\n"
    "<b>Пропуск:</b>\n"
    "/skip_next &lt;ID маршрута&gt; - пропустить ближайшую публикацию\n"
)


def parse_interval(interval_str: str) -> int:
    parts = interval_str.strip().split(':')
    if len(parts) != 4:
        raise ValueError(f"Неверный формат: {interval_str}. Ожидается ДД:ЧЧ:ММ:СС")
    days, hours, minutes, seconds = map(int, parts)
    if days < 0 or not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        raise ValueError(f"Неверные значения в интервале: {interval_str}")
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"Интервал должен быть больше нуля: {interval_str}")
    return total


def format_interval(seconds: int) -> str:
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    secs = seconds % 60
    return f"{days:02d}:{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_intervals_list(intervals_str: str) -> list[int]:
    parts = [p.strip() for p in intervals_str.split(',')]
    return [parse_interval(p) for p in parts if p]


def _resolve_source_chat(raw_chat_id: int) -> int:
    """Заменяет 0 на MAIN_SOURCE_CHAT_ID (совместимость со старым поведением)."""
    return MAIN_SOURCE_CHAT_ID if raw_chat_id == 0 else raw_chat_id


# ==========================================
# КОМАНДЫ АДМИНА
# ==========================================

@commands_router.message(Command("help"), F.from_user.id == ADMIN_ID)
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@commands_router.message(Command("start"), F.from_user.id == ADMIN_ID)
async def cmd_start(message: types.Message):
    await message.answer("Привет милый! Используй /help если хочешь посмотреть список всех команд")


@commands_router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message, state: FSMContext):
    if not message.text:
        return
    args = message.text.split()

    help_text = (
        "<b>Однострочный режим:</b>\n"
        "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; "
        "&lt;цель_топик&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt;</code>\n\n"
        "<b>Пошаговый режим:</b>\n"
        "<code>/add_route &lt;исх_чат&gt; &lt;исх_топик&gt; &lt;цель_чат&gt; "
        "&lt;цель_топик&gt;</code>\n"
        "Затем я спрошу название, время, интервалы и разброс."
    )

    try:
        if len(args) == 5:
            # ---- Пошаговый режим ----
            src_chat = _resolve_source_chat(int(args[1]))
            src_topic = int(args[2])
            tgt_chat = int(args[3])
            tgt_topic = int(args[4])

            source_ct_id = add_chat_topic(
                src_chat, src_topic,
                name=f"Source {src_chat}:{src_topic}"
            )
            target_ct_id = add_chat_topic(
                tgt_chat, tgt_topic,
                name=f"Target {tgt_chat}:{tgt_topic}",
                sendable=True
            )

            await state.update_data(
                source_ct_id=source_ct_id,
                target_ct_id=target_ct_id,
                src_chat=src_chat, src_topic=src_topic,
                tgt_chat=tgt_chat, tgt_topic=tgt_topic,
            )
            await state.set_state(AddRouteStates.waiting_for_name)
            await message.answer("Отлично! Чаты зарегистрированы. Теперь отправь название для этого маршрута.")

        elif len(args) in (7, 8):
            # ---- Однострочный режим ----
            src_chat = _resolve_source_chat(int(args[1]))
            src_topic = int(args[2])
            tgt_chat = int(args[3])
            tgt_topic = int(args[4])
            send_time = args[5]
            intervals_str = args[6]

            h, m = map(int, send_time.split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Неверное время")

            intervals = parse_intervals_list(intervals_str)
            intervals_json = json.dumps(intervals)
            jitter_seconds = int(args[7]) if len(args) == 8 else 0

            source_ct_id = add_chat_topic(
                src_chat, src_topic,
                name=f"Source {src_chat}:{src_topic}"
            )
            target_ct_id = add_chat_topic(
                tgt_chat, tgt_topic,
                name=f"Target {tgt_chat}:{tgt_topic}",
                sendable=True
            )

            route_name = f"Маршрут {src_topic} -> {tgt_chat}"

            # Временно хардкодим route_mode и max_rounds
            # TODO: добавить выбор route_mode ('bulk'/'singular') и max_rounds
            route_id = add_route(
                source_ct_id=source_ct_id,
                route_name=route_name,
                route_mode='bulk',       # хардкод
                send_time=send_time,
                intervals_json=intervals_json,
                jitter_seconds=jitter_seconds,
                max_rounds=-1,           # хардкод: бесконечные круги
            )
            add_route_target(route_id, target_ct_id)

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
        await message.answer(f"Ошибка: {error_text}\n\n{help_text}", parse_mode="HTML")


@commands_router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")


# ---- FSM-шаги пошагового добавления ----

@commands_router.message(AddRouteStates.waiting_for_name, F.from_user.id == ADMIN_ID)
async def route_step_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Название нужно прислать текстом.")
        return
    await state.update_data(route_name=message.text)
    await state.set_state(AddRouteStates.waiting_for_time)
    await message.answer(f"Название сохранено: {message.text}\nТеперь время первой отправки (ЧЧ:ММ)")


@commands_router.message(AddRouteStates.waiting_for_time, F.from_user.id == ADMIN_ID)
async def route_step_time(message: types.Message, state: FSMContext):
    send_time = message.text
    try:
        h, m = map(int, send_time.split(':'))  # type: ignore
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        await message.answer("Неверный формат. Используй ЧЧ:ММ, например 15:30")
        return
    await state.update_data(send_time=send_time)
    await state.set_state(AddRouteStates.waiting_for_intervals)
    await message.answer("Время сохранено. Теперь интервалы (ДД:ЧЧ:ММ:СС через запятую).")


@commands_router.message(AddRouteStates.waiting_for_intervals, F.from_user.id == ADMIN_ID)
async def route_step_intervals(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Интервалы нужно прислать текстом.")
        return
    try:
        intervals = parse_intervals_list(message.text.strip())
        if not intervals:
            raise ValueError("Список пуст")
    except ValueError as e:
        await message.answer(f"Ошибка: {e}\nФормат: ДД:ЧЧ:ММ:СС через запятую.")
        return
    await state.update_data(intervals=intervals)
    await state.set_state(AddRouteStates.waiting_for_jitter)
    await message.answer(
        f"Интервалы: {', '.join(format_interval(i) for i in intervals)}\n"
        "Теперь разброс в секундах (или 0)."
    )


@commands_router.message(AddRouteStates.waiting_for_jitter, F.from_user.id == ADMIN_ID)
async def route_step_jitter(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Разброс нужно прислать числом (секунды) или 0.")
        return

    text = message.text.strip()
    if text in ('0', '00:00:00:00'):
        jitter_seconds = 0
    else:
        try:
            jitter_seconds = parse_interval(text)
        except ValueError as e:
            await message.answer(
                f"Ошибка: {e}\n"
                "Формат: ДД:ЧЧ:ММ:СС или просто число секунд, или 0."
            )
            return

    data = await state.get_data()
    intervals = data['intervals']
    min_interval = min(intervals)

    if jitter_seconds >= min_interval:
        await message.answer(
            f"Разброс ({format_interval(jitter_seconds)}) должен быть меньше "
            f"мин. интервала ({format_interval(min_interval)})."
        )
        return

    # === СОЗДАНИЕ МАРШРУТА В НОВОЙ БД ===
    # Временно хардкодим route_mode и max_rounds
    # TODO: добавить выбор route_mode ('bulk'/'singular') и max_rounds
    route_id = add_route(
        source_ct_id=data['source_ct_id'],
        route_name=data['route_name'],
        route_mode='bulk',       # хардкод
        send_time=data['send_time'],
        intervals_json=json.dumps(intervals),
        jitter_seconds=jitter_seconds,
        max_rounds=-1,           # хардкод: бесконечные круги
    )

    # Привязываем цель (пока одна; TODO: множественные цели)
    add_route_target(route_id, data['target_ct_id'])

    route = get_route_by_id(route_id)
    schedule_route_job(route)

    await state.clear()
    intervals_display = ', '.join(format_interval(i) for i in intervals)
    jitter_text = f", разброс {format_interval(jitter_seconds)}" if jitter_seconds > 0 else ""
    await message.answer(
        f"Маршрут {route_id} успешно создан!\n"
        f"Название: {data['route_name']}\n"
        f"Источник: {data['src_chat']}:{data['src_topic']}\n"
        f"Цель: {data['tgt_chat']}:{data['tgt_topic']}\n"
        f"Первый пост в {data['send_time']}\n"
        f"Интервалы: [{intervals_display}]{jitter_text}"
    )


# ---- Просмотр / управление ----

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

        # Источник
        source_ct = get_chat_topic_by_id(r['source_ct_id'])
        if source_ct:
            src_text = f"чат <code>{source_ct['ct_tg_chat_id']}</code>"
            if source_ct['ct_tg_topic_id']:
                src_text += f", топик <code>{source_ct['ct_tg_topic_id']}</code>"
            else:
                src_text += " (весь чат)"
        else:
            src_text = "неизвестен"

        # Цели (пока одна, но цикл готов к множественным)
        targets = get_route_targets(r['id'])
        tgt_texts = []
        for t in targets:
            t_text = f"чат <code>{t['ct_tg_chat_id']}</code>"
            if t['ct_tg_topic_id']:
                t_text += f", топик <code>{t['ct_tg_topic_id']}</code>"
            tgt_texts.append(t_text)
        tgt_display = "\n    ".join(tgt_texts) if tgt_texts else "нет целей"

        text += (
            f"ID <code>{r['id']}</code>: {route_name}\n"
            f"  Источник: {src_text}\n"
            f"  Цели:\n    {tgt_display}\n"
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
            "<code>/send_now &lt;ID_маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("Прости, но мне нужен числовой идентификатор маршрута.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Я не смогла найти маршрут с ID {route_id}. Проверь /routes")
        return

    await message.answer(f"Пытаюсь отправить пост из маршрута {route_id}...")
    sent = await send_random_post_job(route_id, manual_send=True)
    if sent:
        await message.answer("Готово! Проверь целевой чат :)")
    else:
        await message.answer("Не получилось отправить пост. Подробности в логах.")


@commands_router.message(Command("delete_route"), F.from_user.id == ADMIN_ID)
async def cmd_delete_route(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "Дорогой, используй формат:\n"
            "<code>/delete_route &lt;ID_маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("Прости, но мне нужен числовой идентификатор маршрута.")
        return

    job_id = f"route_{route_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logging.info(f"Планировщик: задача {job_id} удалена")

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Я не смогла найти маршрут с ID {route_id}.")
        return

    if delete_route(route_id):
        route_name = route['route_name'] or f"Маршрут {route_id}"
        await message.answer(f"Ура, я удалила маршрут {route_id} ({route_name})!")
    else:
        await message.answer(f"Не удалось удалить маршрут {route_id} из базы.")


@commands_router.message(Command("import_message"), F.from_user.id == ADMIN_ID)
async def cmd_import_message(message: types.Message):
    """Импортирует конкретное сообщение по ID маршрута и ID сообщения."""
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/import_message &lt;ID_маршрута&gt; &lt;ID_сообщения&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
        msg_id = int(args[2])
    except ValueError:
        await message.answer("ID маршрута и ID сообщения должны быть числами.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут <code>{route_id}</code> не найден.", parse_mode="HTML")
        return

    # Получаем source_chat_id через новую схему
    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    source_chat_id = source_ct['ct_tg_chat_id'] if source_ct else MAIN_SOURCE_CHAT_ID

    # Проверяем доступность сообщения
    sent_msg = await bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=source_chat_id,
        message_id=msg_id,
    )
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=sent_msg.message_id)
    except Exception as e:
        logging.warning(f"Не удалось удалить временное сообщение: {e}")

    if save_post(route_id, [msg_id]):
        await message.answer(
            f"Сообщение <code>{msg_id}</code> импортировано в маршрут <code>{route_id}</code>!",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"Сообщение <code>{msg_id}</code> уже было импортировано.",
            parse_mode="HTML"
        )


@commands_router.message(Command("import_range"), F.from_user.id == ADMIN_ID)
async def cmd_import_range(message: types.Message):
    """Импортирует диапазон сообщений по ID маршрута."""
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 4:
        await message.answer(
            "Формат: <code>/import_range &lt;ID_маршрута&gt; &lt;начальный_ID&gt; &lt;конечный_ID&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
        start_id = int(args[2])
        end_id = int(args[3])
    except ValueError:
        await message.answer("Все параметры должны быть числами.")
        return

    if start_id > end_id:
        await message.answer("Начальный ID должен быть меньше конечного.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут <code>{route_id}</code> не найден.", parse_mode="HTML")
        return

    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    source_chat_id = source_ct['ct_tg_chat_id'] if source_ct else MAIN_SOURCE_CHAT_ID

    await message.answer(
        f"Начинаю импорт сообщений с {start_id} по {end_id} для маршрута {route_id}..."
    )

    imported_count = 0
    failed_count = 0
    skipped_count = 0

    for msg_id in range(start_id, end_id + 1):
        try:
            sent_msg = await bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=source_chat_id,
                message_id=msg_id,
            )
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=sent_msg.message_id)
            except Exception:
                pass

            if save_post(route_id, [msg_id]):
                imported_count += 1
            else:
                skipped_count += 1
        except Exception:
            failed_count += 1
        await asyncio.sleep(0.15)

    await message.answer(
        f"Импорт для маршрута {route_id} завершён!\n"
        f"Успешно: {imported_count}\n"
        f"Пропущено: {skipped_count}\n"
        f"Не удалось: {failed_count}"
    )


@commands_router.message(Command("skip_next"), F.from_user.id == ADMIN_ID)
async def cmd_skip_next(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/skip_next &lt;ID маршрута&gt;</code>", parse_mode="HTML")
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

    next_run = skip_next_publication(route_id)
    if next_run:
        job_id = f"route_{route_id}"
        jitter = route['jitter_seconds'] or 0
        if jitter > 0:
            next_run = next_run + timedelta(seconds=random.randint(0, jitter))

        scheduler.add_job(
            send_random_post_job,
            trigger='date',
            run_date=next_run,
            args=[route_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
            coalesce=True,
        )

        time_str = next_run.strftime('%Y-%m-%d %H:%M:%S %Z')
        now = datetime.now(ZoneInfo(TIMEZONE))
        delta = next_run - now
        total_seconds = int(delta.total_seconds())
        if total_seconds > 86400:
            delta_str = f"{total_seconds // 86400} дн. {(total_seconds % 86400) // 3600} ч."
        elif total_seconds > 3600:
            delta_str = f"{total_seconds // 3600} ч. {(total_seconds % 3600) // 60} мин."
        elif total_seconds > 60:
            delta_str = f"{total_seconds // 60} мин. {total_seconds % 60} сек."
        else:
            delta_str = f"{total_seconds} сек."

        await message.answer(
            f"Ближайшая публикация маршрута <code>{route_id}</code> пропущена.\n"
            f"Следующая: <code>{time_str}</code>\n"
            f"(через {delta_str})",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"Не удалось пропустить публикацию для маршрута {route_id}.")


# ==========================================
# ЛОГИКА ОТПРАВКИ И СБОРА
# ==========================================

SOURCE_MESSAGE_MISSING_ERRORS = (
    'message to copy not found', 'messages to copy not found', 'message not found',
    'message to forward not found', 'there are no messages to forward',
    'message_id_invalid', 'wrong message id', 'invalid message id',
    "message can't be copied", 'was not forwarded', 'failed to send message',
)


def is_missing_source_message_error(error: Exception) -> bool:
    error_str = str(error).lower()
    return any(marker in error_str for marker in SOURCE_MESSAGE_MISSING_ERRORS)


async def send_random_post_job(route_id: int, _attempt: int = 0, manual_send: bool = False) -> bool:
    MAX_ATTEMPTS = 5
    if _attempt >= MAX_ATTEMPTS:
        logging.warning(f"Маршрут {route_id}: лимит попыток ({MAX_ATTEMPTS}).")
        return False

    route = get_route_by_id(route_id)
    if not route:
        logging.warning(f"Маршрут {route_id} не найден.")
        return False

    # Получаем source_chat_id через новую схему
    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    if not source_ct:
        logging.error(f"Маршрут {route_id}: source_ct_id={route['source_ct_id']} не найден в chats_topics!")
        return False
    source_chat_id = source_ct['ct_tg_chat_id']

    # Получаем цели (пока одна, но цикл готов к множественным)
    # TODO: подписи к постам (get_note_for_target) — пока не используем
    targets = get_route_targets(route_id)
    if not targets:
        logging.error(f"У маршрута {route_id} нет активных целей!")
        return False

    post = get_random_unsent_post(route_id)
    if not post:
        logging.info(f"Все посты отправлены. Сбрасываем флаги для маршрута {route_id}.")
        reset_posts_for_route(route_id)
        # TODO: здесь можно вызвать increment_completed_rounds() и проверить max_rounds
        post = get_random_unsent_post(route_id)
    if not post:
        logging.warning(f"В маршруте {route_id} вообще нет постов!")
        return False

    message_ids = sorted(post['message_ids'])
    if not message_ids:
        logging.warning(f"Пост {post['id']} пустой. Удаляю.")
        delete_post(post['id'])
        if manual_send:
            return False
        return await send_random_post_job(route_id, _attempt + 1, manual_send)

    try:
        for target in targets:
            target_chat_id = target['ct_tg_chat_id']
            target_topic_id = target['ct_tg_topic_id']

            send_kwargs = {}
            if target_topic_id:
                send_kwargs['message_thread_id'] = target_topic_id

            # TODO: добавить подпись через get_note_for_target(route_id, target['ct_id'])

            if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
                await bot.copy_messages(
                    chat_id=target_chat_id,
                    from_chat_id=source_chat_id,
                    message_ids=message_ids,
                    **send_kwargs,
                )
            else:
                for msg_id in message_ids:
                    await bot.copy_message(
                        chat_id=target_chat_id,
                        from_chat_id=source_chat_id,
                        message_id=msg_id,
                        **send_kwargs,
                    )

        mark_post_sent(post['id'])
        if not manual_send:
            _schedule_next_publication(route_id, route)
        logging.info(f"Пост отправлен для маршрута {route_id}.")
        return True

    except Exception as e:
        if is_missing_source_message_error(e):
            logging.warning(
                f"Пост {post['id']} недоступен. Удаляю и пробую следующий "
                f"(попытка {_attempt + 1}/{MAX_ATTEMPTS})."
            )
            delete_post(post['id'])
            if manual_send:
                return False
            return await send_random_post_job(route_id, _attempt + 1, manual_send)
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
        next_run += timedelta(seconds=random.randint(0, jitter))

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
        coalesce=True,
    )
    logging.info(
        f"Маршрут {route_id}: следующий пост через {format_interval(interval_seconds)}, в {next_run}"
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
        start_date += timedelta(seconds=random.randint(0, jitter))

    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=start_date,
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )


def _calculate_initial_start(send_time: str) -> datetime:
    h, m = map(int, send_time.split(':'))
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_date = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start_date <= now:
        start_date += timedelta(days=1)
    return start_date


# ==========================================
# СБОР ПОСТОВ ИЗ ИСХОДНЫХ ЧАТОВ
# ==========================================

@collector_router.message()
async def collect_post(message: types.Message):
    """Слушаем сообщения из чатов и сохраняем их, если есть подходящий маршрут."""
    if message.chat.type == 'private':
        return
    if message.text and message.text.startswith('/'):
        return
    if not (message.text or message.photo or message.video or message.document or message.animation):
        return

    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0

    # Ищем маршруты через новую функцию из db_funcs
    routes = get_routes_for_source(chat_id, topic_id)
    if not routes:
        return

    media_group_id = message.media_group_id
    if media_group_id:
        for route in routes:
            key = (route['id'], media_group_id)
            media_groups[key].append(message.message_id)
            if key in media_group_timers:
                media_group_timers[key].cancel()

            async def save_media_group(key=key, route_id=route['id']):
                await asyncio.sleep(5)
                message_ids = sorted(media_groups.pop(key, []))
                media_group_timers.pop(key, None)
                if message_ids:
                    save_post(route_id, message_ids)
                    logging.info(
                        f"Галерея из {len(message_ids)} сообщений сохранена для маршрута {route_id}"
                    )

            task = asyncio.create_task(save_media_group())
            media_group_timers[key] = task
    else:
        for route in routes:
            save_post(route['id'], [message.message_id])
            logging.info(f"Пост {message.message_id} сохранён для маршрута {route['id']}")


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
        types.BotCommand(command="import_message", description="Импортировать одно сообщение"),
        types.BotCommand(command="import_range", description="Импортировать диапазон"),
        types.BotCommand(command="skip_next", description="Пропустить публикацию"),
    ])


async def main():
    init_db()
    await set_bot_commands()
    routes = get_all_routes()
    for r in routes:
        schedule_route_job(r)
    scheduler.start()
    logging.info("Бот запущен и слушает топики...")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен!")