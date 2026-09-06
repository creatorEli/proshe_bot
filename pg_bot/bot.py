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
    get_sendable_chat_topics,
    init_db,
    add_chat_topic, get_chat_topic_by_id, get_chat_topic_by_tg_ids,
    get_chat_topic_by_name, get_all_chat_topics,
    add_route, get_route_by_id, get_all_routes, update_route_schedule,
    delete_route, skip_next_publication,
    add_route_target, get_route_targets,remove_route_target,
    save_post, mark_post_sent, reset_posts_for_route,
    get_random_unsent_post, delete_post,
    get_routes_for_source,
    update_route, update_chat_topic, delete_chat_topic, check_chat_topic_in_use,
    get_random_sendable_targets,
    increment_completed_rounds, deactivate_route,
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

class SingularImportStates(StatesGroup):
    waiting_for_forward = State()

commands_router = Router()
collector_router = Router()
dp.include_router(commands_router)
dp.include_router(collector_router)

logging.basicConfig(level=logging.INFO)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Словарь для агрегации галерей
media_groups = defaultdict(list)
media_group_timers = {}
singular_media_groups = defaultdict(list)
singular_media_group_timers = {}

# Режим импорта singular-постов: {route_id: {'source_chat_id': int, 'admin_id': int, 'expires_at': float}}
singular_import_active = {}

# ==========================================
# ТЕКСТЫ И УТИЛИТЫ
# ==========================================

HELP_TEXT = (
    "Привет! Я бот для автоматической отправки постов по маршрутам.\n"
    "Используй команды ниже.\n\n"
    "Все команды управления доступны только администратору.\n\n"
    "<b>📚 Управление чатами/топиками:</b>\n"
    "<code>/add_chat &lt;имя&gt; &lt;tg_chat_id&gt; [tg_topic_id]</code> — зарегистрировать чат/топик\n"
    "<code>/chats</code> — список всех зарегистрированных чатов\n"
    "<code>/rename_chat &lt;id&gt; &lt;новое_имя&gt;</code> — переименовать\n"
    "<code>/freeze_chat &lt;id&gt;</code> — заморозить чат (отключить)\n"
    "<code>/unfreeze_chat &lt;id&gt;</code> — разморозить чат\n"
    "<code>/toggle_sendable &lt;id&gt;</code> — переключить флаг «можно отправлять» (для целей)\n"
    "<code>/delete_chat &lt;id&gt;</code> — удалить чат (если не используется)\n\n"
    "<b>🛣️ Управление маршрутами:</b>\n"
    "<code>/routes</code> — список маршрутов\n"
    "<code>/add_route &lt;имя_исх&gt; &lt;имя_цели&gt;</code> — пошаговое добавление\n"
    "<code>/add_route &lt;имя_исх&gt; &lt;имя_цели&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt;</code> — однострочное\n"
    "<code>/send_now &lt;ID&gt;</code> — отправить пост сейчас\n"
    "<code>/delete_route &lt;ID&gt;</code> — удалить маршрут\n"
    "<code>/freeze_route &lt;ID&gt;</code> — заморозить маршрут\n"
    "<code>/unfreeze_route &lt;ID&gt;</code> — разморозить маршрут\n"
    "<code>/edit_time &lt;ID&gt; &lt;ЧЧ:ММ&gt;</code> — изменить время\n"
    "<code>/edit_intervals &lt;ID&gt; &lt;интервалы&gt;</code> — изменить интервалы\n"
    "<code>/edit_jitter &lt;ID&gt; &lt;секунды&gt;</code> — изменить разброс\n"
    "<code>/skip_next &lt;ID&gt;</code> — пропустить ближайшую публикацию\n\n"
    "<code>/toggle_random &lt;ID&gt</code> — вкл/выкл случайную рассылку по sendable-чатaм (для источника)\n"

    "<code>/add_target &lt;ID&gt; &lt;имена_чатов&gt;</code> — добавить цели (через запятую)\n"
    "<i>Спец. значения:</i> <code>all</code> (все активные чаты) или <code>sendable</code> (только sendable)\n\n"
    "<code>/remove_target &lt;ID маршрута&gt; &lt;имя_чата&gt;</code> — убрать цель из маршрута\n"
    "<i>Спец. значения:</i> <code>all</code> (все цели) или <code>sendable</code> (все sendable-чаты из целей)\n\n"

    "<b>📥 Импорт сообщений:</b>\n"
    "<code>/import_message &lt;ID маршрута&gt; &lt;ID сообщения&gt;</code>\n"
    "<code>/import_range &lt;ID маршрута&gt; &lt;начальный ID&gt; &lt;конечный ID&gt;</code>\n\n"
    "<b>Отмена:</b> /cancel\n\n"
    "<b>💡 Подсказки:</b>\n"
    "Имена чатов должны быть уникальными\n"
    "<code>0</code> вместо tg_topic_id — общий поток (без топика)\n"
    "Флаг «sendable» отмечает чаты, куда можно отправлять (для массовой рассылки)\n\n"
    "<b>📢 Singular-маршруты (реклама):</b>\n"
    "<code>/add_singular &lt;имя_источника&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt; [rounds]</code> — создать singular-маршрут\n"
    "<code>/list_singular</code> — список singular-маршрутов\n"
    "<code>/set_rounds &lt;ID&gt; &lt;N&gt;</code> — установить количество кругов (-1 = бесконечно)\n"
    "<code>/extend_route &lt;ID&gt; &lt;N&gt;</code> — продлить маршрут на N кругов\n"
    "<code>/get_rounds &lt;ID&gt;</code> — показать статус кругов\n\n"
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
    """Заменяет 0 на MAIN_SOURCE_CHAT_ID"""
    return MAIN_SOURCE_CHAT_ID if raw_chat_id == 0 else raw_chat_id


def _recalculate_and_reschedule(route_id: int, route):
    """Пересчитывает next_run_time и обновляет задачу в планировщике."""
    intervals = json.loads(route['intervals_json'] or '[]')
    send_time = route['send_time']
    if not intervals or not send_time:
        return
    
    # Вычисляем новое время запуска
    start_date = _calculate_initial_start(send_time)

    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        start_date += timedelta(seconds=random.randint(0, jitter))

    # Обновляем расписание в БД
    update_route_schedule(route_id, 0, start_date.isoformat())

    # Обновляем задачу в планировщике
    job_id = f"route_{route_id}"
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
    logging.info(f"Маршрут {route_id}: расписание обновлено, следующий запуск {start_date}")


# ==========================================
# КОМАНДЫ УПРАВЛЕНИЯ ЧАТАМИ
# ==========================================

@commands_router.message(Command("add_chat"), F.from_user.id == ADMIN_ID)
async def cmd_add_chat(message: types.Message):
    if not message.text:
        return
    args = message.text.split()

    if len(args) < 3 or len(args) > 4:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/add_chat &lt;имя&gt; &lt;tg_chat_id&gt; [tg_topic_id]</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/add_chat my_channel -1001234567890</code>\n"
            "<code>/add_chat my_topic -1001234567890 42</code>\n\n"
            "Имя — одно слово (без пробелов). Используйте <code>_</code> для разделения.\n"
            "<code>0</code> вместо tg_topic_id — общий поток.",
            parse_mode="HTML"
        )
        return

    try:
        name = args[1]
        tg_chat_id = _resolve_source_chat(int(args[2]))
        tg_topic_id = int(args[3]) if len(args) == 4 else 0

        # Проверяем уникальность имени
        existing = get_chat_topic_by_name(name)
        if existing:
            await message.answer(
                f"❌ Имя <code>{name}</code> уже занято чатом ID {existing['id']} "
                f"({existing['ct_tg_chat_id']}:{existing['ct_tg_topic_id']}).\n"
                f"Выбери другое имя или используй /rename_chat.",
                parse_mode="HTML"
            )
            return

        # Проверяем, нет ли уже такого чата/топика
        existing_ct = get_chat_topic_by_tg_ids(tg_chat_id, tg_topic_id)
        if existing_ct and existing_ct['ct_name']:
            await message.answer(
                f"⚠️ Чат <code>{tg_chat_id}:{tg_topic_id}</code> уже зарегистрирован "
                f"под именем <code>{existing_ct['ct_name']}</code> (ID {existing_ct['id']}).\n"
                f"Используй /rename_chat для переименования.",
                parse_mode="HTML"
            )
            return

        ct_id = add_chat_topic(
            tg_chat_id, tg_topic_id,
            name=name,
            sendable=False,  # По умолчанию — не sendable
        )

        await message.answer(
            f"✅ Чат зарегистрирован!\n"
            f"ID: <code>{ct_id}</code>\n"
            f"Имя: <code>{name}</code>\n"
            f"TG: <code>{tg_chat_id}:{tg_topic_id}</code>\n"
            f"Sendable: нет (переключи через /toggle_sendable, если нужно)",
            parse_mode="HTML"
        )
    except ValueError as e:
        await message.answer(f"Ошибка в числовых параметрах: {e}")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@commands_router.message(Command("chats"), F.from_user.id == ADMIN_ID)
async def cmd_list_chats(message: types.Message):
    chats = get_all_chat_topics(active_only=False)
    if not chats:
        await message.answer(
            "Зарегистрированных чатов пока нет.\n"
            "Добавь первый: <code>/add_chat &lt;имя&gt; &lt;chat_id&gt; [topic_id]</code>",
            parse_mode="HTML"
        )
        return

    text = "<b>📚 Зарегистрированные чаты и топики:</b>\n\n"
    for c in chats:
        status = "✅" if c['is_active'] else "❄️"
        sendable = "📤" if c['ct_sendable'] else "📥"
        name = c['ct_name'] or "<i>без имени</i>"

        usage = check_chat_topic_in_use(c['id'])
        usage_parts = []
        if usage['as_source']:
            usage_parts.append(f"источник для {usage['as_source']}")
        if usage['as_target']:
            usage_parts.append(f"цель для {usage['as_target']}")
        usage_text = ", ".join(usage_parts) if usage_parts else "не используется"

        topic_text = f":{c['ct_tg_topic_id']}" if c['ct_tg_topic_id'] else ""

        text += (
            f"{status}{sendable} ID <code>{c['id']}</code>: <code>{name}</code>\n"
            f"   TG: <code>{c['ct_tg_chat_id']}{topic_text}</code>\n"
            f"   {usage_text}\n\n"
        )

    text += (
        "<b>Легенда:</b>\n"
        "✅ активен / ❄️ заморожен\n"
        "📤 sendable (можно отправлять) / 📥 только источник"
    )
    await message.answer(text, parse_mode="HTML")


@commands_router.message(Command("rename_chat"), F.from_user.id == ADMIN_ID)
async def cmd_rename_chat(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/rename_chat &lt;id&gt; &lt;новое_имя&gt;</code>",
            parse_mode="HTML"
        )
        return

    try:
        ct_id = int(args[1])
        new_name = args[2]
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    # Проверяем, что чат существует
    ct = get_chat_topic_by_id(ct_id)
    if not ct:
        await message.answer(f"Чат с ID {ct_id} не найден.")
        return

    # Проверяем уникальность нового имени
    existing = get_chat_topic_by_name(new_name)
    if existing and existing['id'] != ct_id:
        await message.answer(
            f"Имя <code>{new_name}</code> уже занято чатом ID {existing['id']}.",
            parse_mode="HTML"
        )
        return

    if update_chat_topic(ct_id, ct_name=new_name):
        await message.answer(
            f"✅ Чат {ct_id} переименован: <code>{new_name}</code>",
            parse_mode="HTML"
        )
    else:
        await message.answer("Не удалось переименовать.")


@commands_router.message(Command("freeze_chat"), F.from_user.id == ADMIN_ID)
async def cmd_freeze_chat(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/freeze_chat &lt;id&gt;</code>", parse_mode="HTML")
        return
    try:
        ct_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    ct = get_chat_topic_by_id(ct_id)
    if not ct:
        await message.answer(f"Чат {ct_id} не найден.")
        return
    if not ct['is_active']:
        await message.answer(f"Чат {ct_id} уже заморожен.")
        return

    if update_chat_topic(ct_id, is_active=False):
        name = ct['ct_name'] or f"чат {ct_id}"
        await message.answer(f"❄️ Чат <code>{name}</code> (ID {ct_id}) заморожен.")
    else:
        await message.answer("Не удалось заморозить.")


@commands_router.message(Command("unfreeze_chat"), F.from_user.id == ADMIN_ID)
async def cmd_unfreeze_chat(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/unfreeze_chat &lt;id&gt;</code>", parse_mode="HTML")
        return
    try:
        ct_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    ct = get_chat_topic_by_id(ct_id)
    if not ct:
        await message.answer(f"Чат {ct_id} не найден.")
        return
    if ct['is_active']:
        await message.answer(f"Чат {ct_id} уже активен.")
        return

    if update_chat_topic(ct_id, is_active=True):
        name = ct['ct_name'] or f"чат {ct_id}"
        await message.answer(f"✅ Чат <code>{name}</code> (ID {ct_id}) разморожен.")
    else:
        await message.answer("Не удалось разморозить.")


@commands_router.message(Command("toggle_sendable"), F.from_user.id == ADMIN_ID)
async def cmd_toggle_sendable(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/toggle_sendable &lt;id&gt;</code>", parse_mode="HTML")
        return
    try:
        ct_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    ct = get_chat_topic_by_id(ct_id)
    if not ct:
        await message.answer(f"Чат {ct_id} не найден.")
        return

    new_sendable = not bool(ct['ct_sendable'])
    if update_chat_topic(ct_id, ct_sendable=new_sendable):
        name = ct['ct_name'] or f"чат {ct_id}"
        status = "📤 МОЖНО отправлять" if new_sendable else "📥 только источник"
        await message.answer(
            f"✅ Чат <code>{name}</code> (ID {ct_id}): {status}",
            parse_mode="HTML"
        )
    else:
        await message.answer("Не удалось переключить.")


@commands_router.message(Command("delete_chat"), F.from_user.id == ADMIN_ID)
async def cmd_delete_chat(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/delete_chat &lt;id&gt;</code>", parse_mode="HTML")
        return
    try:
        ct_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    success, msg = delete_chat_topic(ct_id)
    if success:
        await message.answer(f"✅ {msg}")
    else:
        await message.answer(f"❌ {msg}")


# ==========================================
# КОМАНДЫ МАРШРУТОВ 
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
        "<code>/add_route &lt;имя_исх&gt; &lt;имя_цели&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt;</code>\n\n"
        "<b>Пошаговый режим:</b>\n"
        "<code>/add_route &lt;имя_исх&gt; &lt;имя_цели&gt;</code>\n"
        "Затем я спрошу название, время, интервалы и разброс.\n\n"
        "<b>💡 Имена чатов</b> — те, что ты зарегистрировал через /add_chat\n"
        "Посмотри список: /chats"
    )

    if len(args) not in (3, 5):
        await message.answer(help_text, parse_mode="HTML")
        return

    try:
        source_name = args[1]
        target_name = args[2]

        # Ищем чаты по именам
        source_ct = get_chat_topic_by_name(source_name)
        if not source_ct:
            await message.answer(
                f"❌ Исходный чат <code>{source_name}</code> не найден.\n"
                f"Зарегистрируй его через /add_chat или проверь список: /chats",
                parse_mode="HTML"
            )
            return

        target_ct = get_chat_topic_by_name(target_name)
        if not target_ct:
            await message.answer(
                f"❌ Целевой чат <code>{target_name}</code> не найден.\n"
                f"Зарегистрируй его через /add_chat или проверь список: /chats",
                parse_mode="HTML"
            )
            return

        if len(args) == 3:
            # ---- Пошаговый режим ----
            await state.update_data(
                source_ct_id=source_ct['id'],
                target_ct_id=target_ct['id'],
                source_name=source_name,
                target_name=target_name,
            )
            await state.set_state(AddRouteStates.waiting_for_name)
            await message.answer(
                f"✅ Найдены чаты:\n"
                f"  Источник: <code>{source_name}</code> (ID {source_ct['id']})\n"
                f"  Цель: <code>{target_name}</code> (ID {target_ct['id']})\n\n"
                f"Теперь отправь название для этого маршрута.",
                parse_mode="HTML"
            )
        else:
            # ---- Однострочный режим ----
            send_time = args[3]
            intervals_str = args[4]

            h, m = map(int, send_time.split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Неверное время")

            intervals = parse_intervals_list(intervals_str)
            intervals_json = json.dumps(intervals)

            route_name = f"Маршрут {source_name} → {target_name}"

            route_id = add_route(
                source_ct_id=source_ct['id'],
                route_name=route_name,
                route_mode='bulk',
                send_time=send_time,
                intervals_json=intervals_json,
                jitter_seconds=0,
                max_rounds=-1,
            )
            add_route_target(route_id, target_ct['id'])

            route = get_route_by_id(route_id)
            schedule_route_job(route)

            intervals_display = ', '.join(format_interval(i) for i in intervals)
            await message.answer(
                f"✅ Маршрут {route_id} создан!\n"
                f"Название: {route_name}\n"
                f"Первый пост в {send_time}\n"
                f"Интервалы: [{intervals_display}]",
                parse_mode="HTML"
            )

    except ValueError as e:
        await message.answer(f"Ошибка: {e}\n\n{help_text}", parse_mode="HTML")
    except Exception as e:
        error_text = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        await message.answer(f"Ошибка: {error_text}\n\n{help_text}", parse_mode="HTML")


@commands_router.message(Command("add_singular"), F.from_user.id == ADMIN_ID)
async def cmd_add_singular(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    
    if len(args) < 4 or len(args) > 5:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/add_singular &lt;имя_источника&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt; [rounds]</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/add_singular ads_archive 18:00 01:00:00:00</code> — бесконечно\n"
            "<code>/add_singular ads_archive 18:00 01:00:00:00 10</code> — 10 кругов\n\n"
            "После создания добавь цели через /add_target",
            parse_mode="HTML"
        )
        return
    
    try:
        source_name = args[1]
        send_time = args[2]
        intervals_str = args[3]
        max_rounds = int(args[4]) if len(args) == 5 else -1
        
        # Валидация времени
        h, m = map(int, send_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Неверное время")
        
        # Парсим интервалы
        intervals = parse_intervals_list(intervals_str)
        intervals_json = json.dumps(intervals)
        
        # Ищем источник
        source_ct = get_chat_topic_by_name(source_name)
        if not source_ct:
            await message.answer(
                f"❌ Исходный чат <code>{source_name}</code> не найден.\n"
                f"Зарегистрируй его через /add_chat",
                parse_mode="HTML"
            )
            return
        
        # Создаём singular-маршрут
        route_id = add_route(
            source_ct_id=source_ct['id'],
            route_name=f"Singular: {source_name}",
            route_mode='singular',
            send_time=send_time,
            intervals_json=intervals_json,
            jitter_seconds=0,
            max_rounds=max_rounds,
        )
        
        route = get_route_by_id(route_id)
        schedule_route_job(route)
        
        rounds_text = "бесконечно" if max_rounds == -1 else f"{max_rounds} кругов"
        intervals_display = ', '.join(format_interval(i) for i in intervals)
        
        await message.answer(
            f"✅ Singular-маршрут {route_id} создан!\n"
            f"Источник: <code>{source_name}</code>\n"
            f"Первый пост в {send_time}\n"
            f"Интервалы: [{intervals_display}]\n"
            f"Кругов: {rounds_text}\n\n"
            f"Теперь добавь цели через /add_target {route_id} &lt;имя_чата&gt;",
            parse_mode="HTML"
        )
        
    except ValueError as e:
        await message.answer(f"Ошибка: {e}")


@commands_router.message(Command("list_singular"), F.from_user.id == ADMIN_ID)
async def cmd_list_singular(message: types.Message):
    routes = get_all_routes(active_only=False)
    singular_routes = [r for r in routes if r['route_mode'] == 'singular']
    
    if not singular_routes:
        await message.answer("Singular-маршрутов пока нет.")
        return
    
    text = "<b>📢 Singular-маршруты:</b>\n\n"
    for r in singular_routes:
        route_name = r['route_name'] or f"Маршрут {r['id']}"
        status = "✅" if r['is_active'] else "❄️"
        
        source_ct = get_chat_topic_by_id(r['source_ct_id'])
        src_name = source_ct['ct_name'] if source_ct and source_ct['ct_name'] else f"ID {r['source_ct_id']}"
        
        targets = get_route_targets(r['id'], active_only=False)
        tgt_names = [t['ct_name'] or f"ID {t['ct_id']}" for t in targets]
        tgt_display = ", ".join(tgt_names) if tgt_names else "нет целей"

        intervals = json.loads(r['intervals_json'] or '[]')
        intervals_display = ', '.join(format_interval(i) for i in intervals) if intervals else "нет"
                
        
        completed = r['completed_rounds']
        max_r = r['max_rounds']
        if max_r == -1:
            rounds_text = f"{completed} (бесконечно)"
        else:
            rounds_text = f"{completed}/{max_r}"
        
        text += (
            f"{status} ID <code>{r['id']}</code>: {route_name}\n"
            f"   Ист: <code>{src_name}</code>\n"
            f"   Цел: {tgt_display}\n"
            f"   Круги: {rounds_text}\n"
            f"   Старт: {r['send_time']} | Интервалы: [{intervals_display}]\n\n"
        )
    
    await message.answer(text, parse_mode="HTML")


@commands_router.message(Command("set_rounds"), F.from_user.id == ADMIN_ID)
async def cmd_set_rounds(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/set_rounds &lt;ID&gt; &lt;N&gt;</code>\n"
            "Пример: <code>/set_rounds 5 10</code> — 10 кругов\n"
            "Пример: <code>/set_rounds 5 -1</code> — бесконечно",
            parse_mode="HTML"
        )
        return
    
    try:
        route_id = int(args[1])
        max_rounds = int(args[2])
    except ValueError:
        await message.answer("ID и количество должны быть числами.")
        return
    
    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return
    
    if route['route_mode'] != 'singular':
        await message.answer("Эта команда только для singular-маршрутов.")
        return
    
    if update_route(route_id, max_rounds=max_rounds):
        rounds_text = "бесконечно" if max_rounds == -1 else f"{max_rounds} кругов"
        await message.answer(f"✅ Для маршрута {route_id} установлено: {rounds_text}")
    else:
        await message.answer("Не удалось обновить.")


@commands_router.message(Command("extend_route"), F.from_user.id == ADMIN_ID)
async def cmd_extend_route(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/extend_route &lt;ID&gt; &lt;N&gt;</code>\n"
            "Пример: <code>/extend_route 5 5</code> — добавить ещё 5 кругов",
            parse_mode="HTML"
        )
        return
    
    try:
        route_id = int(args[1])
        add_rounds = int(args[2])
    except ValueError:
        await message.answer("ID и количество должны быть числами.")
        return
    
    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return
    
    if route['route_mode'] != 'singular':
        await message.answer("Эта команда только для singular-маршрутов.")
        return
    
    current_max = route['max_rounds']
    if current_max == -1:
        await message.answer("Маршрут уже бесконечный.")
        return
    
    new_max = current_max + add_rounds
    if update_route(route_id, max_rounds=new_max):
        # Если маршрут был деактивирован — активируем его
        if not route['is_active']:
            update_route(route_id, is_active=True)
            _recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        
        await message.answer(
            f"✅ Маршрут {route_id} продлён.\n"
            f"Было: {current_max} кругов\n"
            f"Стало: {new_max} кругов"
        )
    else:
        await message.answer("Не удалось продлить маршрут.")


@commands_router.message(Command("get_rounds"), F.from_user.id == ADMIN_ID)
async def cmd_get_rounds(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/get_rounds &lt;ID&gt;</code>", parse_mode="HTML")
        return
    
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    
    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return
    
    if route['route_mode'] != 'singular':
        await message.answer("Эта команда только для singular-маршрутов.")
        return
    
    completed = route['completed_rounds']
    max_r = route['max_rounds']
    status = "✅ активен" if route['is_active'] else "❄️ завершён"
    
    if max_r == -1:
        rounds_text = f"{completed} (бесконечно)"
    else:
        remaining = max(0, max_r - completed)
        rounds_text = f"{completed}/{max_r} (осталось {remaining})"
    
    await message.answer(
        f"Singular-маршрут {route_id}:\n"
        f"Статус: {status}\n"
        f"Круги: {rounds_text}",
        parse_mode="HTML"
    )



@commands_router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Очищаем режим импорта, если он был активен для этого админа
    routes_to_delete = [
        r_id for r_id, data in singular_import_active.items() 
        if data['admin_id'] == message.from_user.id
    ]
    for r_id in routes_to_delete:
        del singular_import_active[r_id]
    
    # Очищаем таймеры галерей
    for key in list(singular_media_group_timers.keys()):
        singular_media_group_timers[key].cancel()
        singular_media_group_timers.pop(key, None)
        singular_media_groups.pop(key, None)
        
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
        f"✅ Маршрут {route_id} создан!\n"
        f"Название: {data['route_name']}\n"
        f"Источник: <code>{data['source_name']}</code>\n"
        f"Цель: <code>{data['target_name']}</code>\n"
        f"Первый пост в {data['send_time']}\n"
        f"Интервалы: [{intervals_display}]{jitter_text}",
        parse_mode="HTML"
    )


# ---- Просмотр / управление ----

@commands_router.message(Command("routes"), F.from_user.id == ADMIN_ID)
async def cmd_list_routes(message: types.Message):
    routes = get_all_routes(active_only=False)  # Показываем все, включая замороженные
    if not routes:
        await message.answer("Маршрутов пока нет. Ты всегда можешь их добавить)")
        return

    text = "Маршруты, которые я сохранила для тебя:\n\n"
    for r in routes:
        route_name = r['route_name'] or f"Маршрут {r['id']}"
        intervals = json.loads(r['intervals_json'] or '[]')
        intervals_display = ', '.join(format_interval(i) for i in intervals) if intervals else "нет"
        status = "✅" if r['is_active'] else "❄️"

        source_ct = get_chat_topic_by_id(r['source_ct_id'])
        src_name = source_ct['ct_name'] if source_ct and source_ct['ct_name'] else f"ID {r['source_ct_id']}"

        targets = get_route_targets(r['id'])
        tgt_names = []
        for t in targets:
            t_name = t['ct_name'] or f"ID {t['ct_id']}"
            tgt_names.append(f"<code>{t_name}</code>")
        tgt_display = ", ".join(tgt_names) if tgt_names else "нет целей"

        random_status = "🎲 вкл" if r['use_random_targets'] else "🎲 выкл"

        text += (
            f"{status} ID <code>{r['id']}</code>: {route_name}\n"
            f"   Ист: <code>{src_name}</code>\n"
            f"   Цел: {tgt_display}\n"
            f"   Случ.расс.{random_status}\n"
            f"   Старт: {r['send_time']} | Инт-ы: [{intervals_display}]\n"
            f"   Jit: {r['jitter_seconds']} сек.\n\n"
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

    await message.answer(f"Отправляю пост из маршрута {route_id}...")
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


@commands_router.message(Command("add_target"), F.from_user.id == ADMIN_ID)
async def cmd_add_target(message: types.Message):
    if not message.text:
        return
    
    # Используем maxsplit=2, чтобы всё после ID маршрута попало в одну строку
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/add_target &lt;ID маршрута&gt; &lt;имена_чатов&gt;</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/add_target 1 channel_a, channel_b</code>\n"
            "<code>/add_target 1 all</code> — добавить все активные чаты (даже источники)\n"
            "<code>/add_target 1 sendable</code> — добавить все чаты с флагом sendable",
            parse_mode="HTML"
        )
        return

    try:
        route_id = int(args[1])
        targets_str = args[2].strip()
    except ValueError:
        await message.answer("ID маршрута должен быть числом.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    source_ct_id = route['source_ct_id']

    # Получаем текущие цели, чтобы не добавлять дубликаты
    current_targets = get_route_targets(route_id, active_only=False)
    current_target_ct_ids = {t['ct_id'] for t in current_targets}

    chats_to_add = []

    # 1. Обработка спец. команд
    if targets_str.lower() == 'all':
        chats_to_add = get_all_chat_topics(active_only=True)
    elif targets_str.lower() == 'sendable':
        chats_to_add = get_sendable_chat_topics()
    else:
        # 2. Обработка перечисления имён (поддерживаем и запятые, и пробелы)
        names = [name.strip() for name in targets_str.replace(',', ' ').split() if name.strip()]
        for name in names:
            ct = get_chat_topic_by_name(name)
            if ct:
                chats_to_add.append(ct)
            else:
                await message.answer(f"⚠️ Чат с именем <code>{name}</code> не найден.", parse_mode="HTML")

    if not chats_to_add:
        await message.answer("Не найдено чатов для добавления.")
        return

    added_count = 0
    skipped_count = 0
    source_warning = False

    for ct in chats_to_add:
        # Пропускаем, если уже является целью
        if ct['id'] in current_target_ct_ids:
            skipped_count += 1
            continue

        # Предупреждаем, если добавляем источник в качестве цели (но не блокируем, как просили)
        if ct['id'] == source_ct_id:
            source_warning = True

        if add_route_target(route_id, ct['id']):
            added_count += 1
        else:
            skipped_count += 1

    # Формируем красивый отчёт для админа
    report = []
    if added_count > 0:
        report.append(f"✅ <b>Добавлено целей:</b> {added_count}")
    if skipped_count > 0:
        report.append(f"⏭️ <b>Пропущено</b> (уже были целями или не найдены): {skipped_count}")
    
    if source_warning:
        report.append("⚠️ <i>Внимание: среди добавленных есть исходный чат этого маршрута.</i>")

    if not report:
        report.append("Все указанные чаты уже являются целями этого маршрута.")

    await message.answer("\n".join(report), parse_mode="HTML")


@commands_router.message(Command("remove_target"), F.from_user.id == ADMIN_ID)
async def cmd_remove_target(message: types.Message):
    if not message.text:
        return
    
    # Используем maxsplit=2, чтобы всё после ID маршрута попало в одну строку
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/remove_target &lt;ID маршрута&gt; &lt;имена_чатов&gt;</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/remove_target 1 channel_a, channel_b</code>\n"
            "<code>/remove_target 1 all</code> — убрать все цели\n"
            "<code>/remove_target 1 sendable</code> — убрать все sendable-чаты из целей",
            parse_mode="HTML"
        )
        return

    try:
        route_id = int(args[1])
        targets_str = args[2].strip()
    except ValueError:
        await message.answer("ID маршрута должен быть числом.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    # Получаем текущие цели
    current_targets = get_route_targets(route_id, active_only=False)
    if not current_targets:
        await message.answer(f"У маршрута {route_id} нет целей.")
        return

    chats_to_remove = []

    # 1. Обработка спец. команд
    if targets_str.lower() == 'all':
        chats_to_remove = list(current_targets)
    elif targets_str.lower() == 'sendable':
        chats_to_remove = [t for t in current_targets if t['ct_sendable']]
    else:
        # 2. Обработка перечисления имён (поддерживаем и запятые, и пробелы)
        names = [name.strip() for name in targets_str.replace(',', ' ').split() if name.strip()]
        for name in names:
            ct = get_chat_topic_by_name(name)
            if ct:
                # Проверяем, что этот чат действительно является целью маршрута
                if any(t['ct_id'] == ct['id'] for t in current_targets):
                    chats_to_remove.append(ct)
                else:
                    await message.answer(
                        f"⚠️ Чат <code>{name}</code> не является целью маршрута {route_id}.",
                        parse_mode="HTML"
                    )
            else:
                await message.answer(
                    f"⚠️ Чат с именем <code>{name}</code> не найден.",
                    parse_mode="HTML"
                )

    if not chats_to_remove:
        await message.answer("Не найдено целей для удаления.")
        return

    removed_count = 0
    skipped_count = 0

    for ct in chats_to_remove:
        if remove_route_target(route_id, ct['id']):
            removed_count += 1
        else:
            skipped_count += 1

    # Формируем отчёт
    report = []
    if removed_count > 0:
        report.append(f"✅ <b>Убрано целей:</b> {removed_count}")
    if skipped_count > 0:
        report.append(f"⏭️ <b>Пропущено:</b> {skipped_count}")

    if not report:
        report.append("Ни одна цель не была удалена.")

    # Предупреждение, если целей не осталось
    remaining_targets = get_route_targets(route_id, active_only=False)
    if not remaining_targets:
        report.append("⚠️ <i>Внимание: у маршрута больше нет целей. Добавьте хотя бы одну через /add_target.</i>")

    await message.answer("\n".join(report), parse_mode="HTML")



@commands_router.message(Command("toggle_random"), F.from_user.id == ADMIN_ID)
async def cmd_toggle_random(message: types.Message):
    """Переключает флаг для массовой рассылки из указанного ID источника"""
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/toggle_random &lt;ID маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    new_value = not bool(route['use_random_targets'])
    if update_route(route_id, use_random_targets=new_value):
        status = "✅ ВКЛЮЧЕНА" if new_value else "❌ ВЫКЛЮЧЕНА"
        await message.answer(
            f"Случайная рассылка для маршрута {route_id} {status}.\n"
            f"Теперь посты будут уходить в гарантированные цели "
            f"{'+' if new_value else ''}{'один случайный sendable-чат' if new_value else ''}."
        )
    else:
        await message.answer("Не удалось обновить.")

# ---- Заморозка/разморозка маршрутов ----

@commands_router.message(Command("freeze_route"), F.from_user.id == ADMIN_ID)
async def cmd_freeze_route(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/freeze_route &lt;ID маршрута&gt;</code>",
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
    if not route['is_active']:
        await message.answer(f"Маршрут {route_id} уже заморожен.")
        return

    # Удаляем задачу из планировщика
    job_id = f"route_{route_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logging.info(f"Планировщик: задача {job_id} удалена при заморозке")

    # Замораживаем маршрут
    if update_route(route_id, is_active=False):
        route_name = route['route_name'] or f"Маршрут {route_id}"
        await message.answer(
            f"❄️ Маршрут {route_id} ({route_name}) заморожен.\n"
            f"Публикации остановлены. Используй /unfreeze_route для возобновления."
        )
    else:
        await message.answer(f"Не удалось заморозить маршрут {route_id}.")


@commands_router.message(Command("unfreeze_route"), F.from_user.id == ADMIN_ID)
async def cmd_unfreeze_route(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/unfreeze_route &lt;ID маршрута&gt;</code>",
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

    if route['is_active']:
        await message.answer(f"Маршрут {route_id} уже активен.")
        return

    # Размораживаем маршрут
    if update_route(route_id, is_active=True):
        # Пересчитываем расписание и добавляем задачу в планировщик
        _recalculate_and_reschedule(route_id, route)
        
        route_name = route['route_name'] or f"Маршрут {route_id}"
        await message.answer(
            f"✅ Маршрут {route_id} ({route_name}) разморожен.\n"
            f"Публикации возобновлены."
        )
    else:
        await message.answer(f"Не удалось разморозить маршрут {route_id}.")


# ---- Редактирование графика ----

@commands_router.message(Command("edit_time"), F.from_user.id == ADMIN_ID)
async def cmd_edit_time(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/edit_time &lt;ID маршрута&gt; &lt;ЧЧ:ММ&gt;</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
        send_time = args[2]
        h, m = map(int, send_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Неверное время")
    except ValueError as e:
        await message.answer(f"Ошибка: {e}\nФормат времени: ЧЧ:ММ")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    if update_route(route_id, send_time=send_time):
        if route['is_active']:
            _recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        await message.answer(
            f"✅ Время отправки маршрута {route_id} изменено на {send_time}."
        )
    else:
        await message.answer(f"Не удалось обновить время для маршрута {route_id}.")


@commands_router.message(Command("edit_intervals"), F.from_user.id == ADMIN_ID)
async def cmd_edit_intervals(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/edit_intervals &lt;ID маршрута&gt; &lt;интервалы&gt;</code>\n"
            "Пример: <code>/edit_intervals 1 00:09:00:00,00:15:00:00</code>",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
        intervals_str = args[2]
        intervals = parse_intervals_list(intervals_str)
        if not intervals:
            raise ValueError("Список интервалов пуст")
    except ValueError as e:
        await message.answer(f"Ошибка: {e}\nФормат: ДД:ЧЧ:ММ:СС через запятую")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    intervals_json = json.dumps(intervals)
    if update_route(route_id, intervals_json=intervals_json, interval_index=0):
        if route['is_active']:
            _recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        intervals_display = ', '.join(format_interval(i) for i in intervals)
        await message.answer(
            f"✅ Интервалы маршрута {route_id} изменены.\n"
            f"Новые интервалы: [{intervals_display}]"
        )
    else:
        await message.answer(f"Не удалось обновить интервалы для маршрута {route_id}.")


@commands_router.message(Command("edit_jitter"), F.from_user.id == ADMIN_ID)
async def cmd_edit_jitter(message: types.Message):
    if not message.text:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/edit_jitter &lt;ID маршрута&gt; &lt;секунды&gt;</code>\n"
            "Пример: <code>/edit_jitter 1 300</code> (разброс до 5 минут)",
            parse_mode="HTML"
        )
        return
    try:
        route_id = int(args[1])
        jitter_seconds = int(args[2])
        if jitter_seconds < 0:
            raise ValueError("Разброс не может быть отрицательным")
    except ValueError as e:
        await message.answer(f"Ошибка: {e}\nРазброс должен быть неотрицательным числом секунд")
        return

    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return

    # Проверяем, что разброс меньше минимального интервала
    intervals = json.loads(route['intervals_json'] or '[]')
    if intervals and jitter_seconds >= min(intervals):
        await message.answer(
            f"Разброс ({jitter_seconds} сек.) должен быть меньше "
            f"минимального интервала ({format_interval(min(intervals))})."
        )
        return

    if update_route(route_id, jitter_seconds=jitter_seconds):
        if route['is_active']:
            _recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        await message.answer(
            f"✅ Разброс маршрута {route_id} изменён на {jitter_seconds} сек."
        )
    else:
        await message.answer(f"Не удалось обновить разброс для маршрута {route_id}.")


# ---- Импорт сообщений ----

@commands_router.message(Command("import_singular"), F.from_user.id == ADMIN_ID)
async def cmd_import_singular(message: types.Message):
    """Активирует режим прослушивания исходного чата для импорта singular-поста."""
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/import_singular &lt;ID маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        route_id = int(args[1])
    except ValueError:
        await message.answer("ID маршрута должен быть числом.")
        return

    route = get_route_by_id(route_id)
    if not route or route['route_mode'] != 'singular':
        await message.answer(f"Маршрут {route_id} не найден или не является singular.")
        return

    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    if not source_ct:
        await message.answer("Ошибка: не найден исходный чат маршрута.")
        return

    # Активируем режим прослушивания на 60 секунд
    singular_import_active[route_id] = {
        'source_chat_id': source_ct['ct_tg_chat_id'],
        'admin_id': message.from_user.id,
        'expires_at': asyncio.get_event_loop().time() + 60
    }

    await message.answer(
        f"✅ <b>Режим импорта для маршрута {route_id} активирован на 60 секунд!</b>\n\n"
        f"📌 <b>Что делать:</b>\n"
        f"1. Зайди в исходный чат: <code>{source_ct['ct_tg_chat_id']}</code>\n"
        f"2. Выбери нужное сообщение или <b>всю галерею</b>.\n"
        f"3. <b>Перешли (Forward)</b> его прямо в этот же чат.\n\n"
        f"Бот автоматически перехватит его, сохранит точные ID и уведомит тебя здесь.\n"
        f"<i>Для отмены напиши /cancel</i>",
        parse_mode="HTML"
    )

@commands_router.message(
    F.forward_from_chat, 
    SingularImportStates.waiting_for_forward, 
    F.from_user.id == ADMIN_ID
)


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

    # Проверяем, активен ли маршрут
    if not route['is_active'] and not manual_send:
        logging.info(f"Маршрут {route_id} заморожен. Пропускаю отправку.")
        return False

    # Получаем source_chat_id через новую схему
    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    if not source_ct:
        logging.error(f"Маршрут {route_id}: source_ct_id={route['source_ct_id']} не найден в chats_topics!")
        return False
    source_chat_id = source_ct['ct_tg_chat_id']

    # Получаем цели 
    # TODO: подписи к постам (get_note_for_target) — пока не используем

    # Получаем цели
        # Для singular получаем цели БЕЗ фильтрации по is_active
        # Для bulk — только активные
    targets = get_route_targets(route_id, active_only=(route['route_mode'] != 'singular'))

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
        # 1. Отправляем во все ГАРАНТИРОВАННЫЕ цели
        guaranteed_ct_ids = []
        for target in targets:
            target_chat_id = target['ct_tg_chat_id']
            target_topic_id = target['ct_tg_topic_id']
            guaranteed_ct_ids.append(target['ct_id'])

            send_kwargs = {}
            if target_topic_id:
                send_kwargs['message_thread_id'] = target_topic_id

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

        # 2. Если включена случайная рассылка — выбираем один случайный sendable-чат
        if route['use_random_targets']:
            random_pool = get_random_sendable_targets(exclude_ids=guaranteed_ct_ids)
            if random_pool:
                random_target = random.choice(random_pool)
                target_chat_id = random_target['ct_tg_chat_id']
                target_topic_id = random_target['ct_tg_topic_id']

                send_kwargs = {}
                if target_topic_id:
                    send_kwargs['message_thread_id'] = target_topic_id

                logging.info(
                    f"Маршрут {route_id}: случайная отправка в "
                    f"{random_target['ct_name'] or target_chat_id}"
                )

                try:
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
                except Exception as e:
                    # Если случайный чат недоступен — логируем, но не падаем
                    logging.warning(
                        f"Не удалось отправить в случайный чат "
                        f"{target_chat_id}: {e}"
                    )
            else:
                logging.info(
                    f"Маршрут {route_id}: случайная рассылка включена, "
                    f"но пул sendable-чатов пуст (после исключения гарантированных)."
                )

        mark_post_sent(post['id'])

        # Для singular увеличиваем счётчик кругов
        if route['route_mode'] == 'singular':
            new_rounds = increment_completed_rounds(route_id)
            
            # Проверяем лимит
            if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
                logging.info(
                    f"Singular-маршрут {route_id}: лимит кругов достигнут "
                    f"({new_rounds}/{route['max_rounds']})"
                )
                deactivate_route(route_id)
                # Удаляем задачу из планировщика
                job_id = f"route_{route_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                    logging.info(f"Планировщик: задача {job_id} удалена (лимит достигнут)")


        

        if not manual_send:
            _schedule_next_publication(route_id, route)
        logging.info(f"Пост отправлен для маршрута {route_id}.")
        return True

    except Exception as e:
        if is_missing_source_message_error(e):
            logging.warning(
                f"Пост {post['id']} недоступен. Удаляю и пробую следующий\n"
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

    # Если маршрут заморожен, не добавляем в планировщик
    if not route['is_active']:
        logging.info(f"Маршрут {route_id} заморожен, пропускаю планирование")
        return

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    saved_next_run = route['next_run_time']
    if saved_next_run:
        try:
            start_date = datetime.fromisoformat(saved_next_run)
            if start_date <= now:
                logging.warning(
                    f"Маршрут {route_id}: next_run_time {start_date} в прошлом "
                    f"(сейчас {now}). Пересчитываю расписание от текущего момента."
                )
                # Берём текущий интервал и откладываем от "сейчас"
                current_index = route['interval_index'] or 0
                interval_seconds = intervals[current_index]
                start_date = now + timedelta(seconds=interval_seconds)
            else:
                logging.info(f"Маршрут {route_id}: восстановлен next_run_time = {start_date}")
        except ValueError:
            start_date = _calculate_initial_start(send_time)
    else:
        start_date = _calculate_initial_start(send_time)
        logging.info(f"Маршрут {route_id}: первый запуск, start_date = {start_date}")

    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        start_date += timedelta(seconds=random.randint(0, jitter))
    
    # Обновляем next_run_time в БД, чтобы после рестарта не было рассинхрона
    update_route_schedule(route_id, route['interval_index'] or 0, start_date.isoformat())

    
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

    intervals_display = ', '.join(format_interval(i) for i in intervals)
    logging.info(
        f"Загружен маршрут {route_id}: старт {start_date}, "
        f"интервалы [{intervals_display}]"
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
    """Слушаем сообщения из чатов и сохраняем их."""
    if message.chat.type == 'private':
        return
    if message.text and message.text.startswith('/'):
        return
    if not (message.text or message.photo or message.video or message.document or message.animation):
        return

    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0

    # ==========================================
    # 1. ПРОВЕРКА РЕЖИМА ИМПОРТА SINGULAR
    # ==========================================
    active_import_route = None
    import_data = None
    
    for r_id, data in list(singular_import_active.items()):
        if data['source_chat_id'] == chat_id:
            # Проверяем, не истёк ли таймаут
            if asyncio.get_event_loop().time() > data['expires_at']:
                del singular_import_active[r_id]
                continue
            active_import_route = r_id
            import_data = data
            break

    if active_import_route:
        # Мы в режиме импорта! Сохраняем пост именно в этот маршрут.
        media_group_id = message.media_group_id
        
        if media_group_id:
            key = (active_import_route, media_group_id)
            singular_media_groups[key].append(message.message_id)
            if key in singular_media_group_timers:
                singular_media_group_timers[key].cancel()
            
            async def save_singular_import(key=key, r_id=active_import_route, admin_id=import_data['admin_id']):
                await asyncio.sleep(5)
                msg_ids = sorted(list(set(singular_media_groups.pop(key, []))))
                singular_media_group_timers.pop(key, None)
                
                if msg_ids:
                    save_post(r_id, msg_ids)
                    # Уведомляем админа в личные сообщения
                    try:
                        await bot.send_message(
                            admin_id,
                            f"✅ <b>Галерея успешно импортирована в маршрут {r_id}!</b>\n"
                            f"Количество сообщений: {len(msg_ids)}\n"
                            f"ID: <code>{msg_ids}</code>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    # Отключаем режим после успешного импорта
                    if r_id in singular_import_active:
                        del singular_import_active[r_id]

            task = asyncio.create_task(save_singular_import())
            singular_media_group_timers[key] = task
            await message.answer("⏳ Принимаю галерею для импорта... (подожди 5 сек)")
        else:
            # Одиночное сообщение
            if save_post(active_import_route, [message.message_id]):
                try:
                    await bot.send_message(
                        import_data['admin_id'],
                        f"✅ <b>Сообщение <code>{message.message_id}</code> успешно импортировано в маршрут {active_import_route}!</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                # Отключаем режим
                if active_import_route in singular_import_active:
                    del singular_import_active[active_import_route]
            await message.answer("✅ Сообщение сохранено для импорта.")
        
        # ВАЖНО: прерываем выполнение, чтобы не сработала логика bulk
        return

    # ==========================================
    # 2. ОБЫЧНАЯ ЛОГИКА BULK (без изменений)
    # ==========================================
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
                    logging.info(f"Галерея из {len(message_ids)} сообщений сохранена для маршрута {route_id}")
            
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
    await set_bot_commands()
    routes = get_all_routes(active_only=False)  # Загружаем все маршруты
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