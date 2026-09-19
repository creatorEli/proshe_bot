# ==========================================
# КОМАНДЫ МАРШРУТОВ 
# ==========================================

from datetime import datetime, timedelta
import json
import logging
import random
from zoneinfo import ZoneInfo

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import InlineKeyboardMarkup

from config import ADMIN_ID, TIMEZONE
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

from db_funcs import (
    add_route,
    add_route_target,
    delete_route,
    get_all_chat_topics,
    get_all_routes,
    get_chat_topic_by_id, 
    get_chat_topic_by_name,
    get_posts_for_route, 
    get_route_by_id,
    get_route_targets,
    get_sendable_chat_topics,
    normalize_tag,
    remove_route_target,
    skip_next_publication,
    update_route,
    get_chats_by_tags
)

from utils import (
    apply_pagination_callback,
    format_interval,
    parse_intervals_list,
    paginate, 
    build_pagination_keyboard,
    parse_interval
)

from state_store import (
    AddRouteStates
)

from scheduler_utils import (
    recalculate_and_reschedule,
    schedule_route_job,
    send_random_post_job
)

ROUTES_PAGE_SIZE = 2

commands_router = Router(name="routes")

@commands_router.message(Command("add_route"), F.from_user.id == ADMIN_ID)
async def cmd_add_route(message: types.Message, state: FSMContext):
    if not message.text:
        return
    args = message.text.split(maxsplit=6)
    help_text = (
        "<b>Однострочный режим:</b>\n"
        "<code>/add_route &lt;имя_исх&gt; &lt;цели&gt; &lt;ЧЧ:ММ&gt; &lt;интервалы&gt; [jitter] [название]</code>\n\n"
        "<b>Цели</b> — имена, <code>tag:тег</code> (AND), <code>all</code>, <code>sendable</code> через пробел:\n"
        "<code>/add_route ads tag:asia 18:00 01:00:00:00</code>\n"
        "<code>/add_route ads tag:asia tag:old 18:00 01:00:00:00 300 Реклама Азия</code>\n\n"
        "<b>Пошаговый режим:</b>\n"
        "<code>/add_route &lt;имя_исх&gt; &lt;имя_цели&gt;</code>\n"
        "Затем я спрошу название, время, интервалы и разброс.\n\n"
        "<b>💡 Имена чатов</b> — те, что ты зарегистрировал через /add_chat\n"
        "Посмотри список: /chats, теги: /tags"
    )
    if len(args) not in (3, 5, 6, 7):
        await message.answer(help_text, parse_mode="HTML")
        return
    try:
        source_name = args[1]
        targets_str = args[2]
        source_ct = get_chat_topic_by_name(source_name)
        if not source_ct:
            await message.answer(
                f"❌ Исходный чат <code>{source_name}</code> не найден.\n"
                f"Зарегистрируй его через /add_chat или проверь список: /chats",
                parse_mode="HTML"
            )
            return

        if len(args) == 3:
            # ---- Пошаговый режим (без изменений, одна цель по имени) ----
            target_ct = get_chat_topic_by_name(targets_str)
            if not target_ct:
                await message.answer(
                    f"❌ Целевой чат <code>{targets_str}</code> не найден.\n"
                    f"Зарегистрируй его через /add_chat или проверь список: /chats",
                    parse_mode="HTML"
                )
                return
            await state.update_data(
                source_ct_id=source_ct['id'],
                target_ct_id=target_ct['id'],
                source_name=source_name,
                target_name=targets_str,
            )
            await state.set_state(AddRouteStates.waiting_for_name)
            await message.answer(
                f"✅ Найдены чаты:\n"
                f"  Источник: <code>{source_name}</code> (ID {source_ct['id']})\n"
                f"  Цель: <code>{targets_str}</code> (ID {target_ct['id']})\n\n"
                f"Теперь отправь название для этого маршрута.",
                parse_mode="HTML"
            )
            return

        # ---- Однострочный режим ----
        send_time = args[3]
        intervals_str = args[4]
        jitter_seconds = 0
        route_name = None

        if len(args) >= 6:
            try:
                jitter_seconds = int(args[5])
            except ValueError:
                jitter_seconds = parse_interval(args[5])
            if jitter_seconds < 0:
                raise ValueError("Разброс не может быть отрицательным")
        if len(args) == 7:
            route_name = args[6].strip() or None

        h, m = map(int, send_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("Неверное время")
        intervals = parse_intervals_list(intervals_str)
        if not intervals:
            raise ValueError("Список интервалов пуст")
        if jitter_seconds >= min(intervals):
            raise ValueError(
                f"Разброс ({jitter_seconds} сек.) должен быть меньше "
                f"минимального интервала ({format_interval(min(intervals))})"
            )

        # Разрешаем цели (имена + tag: + all/sendable)
        ids, warnings = _select_chats(targets_str, mode='add')
        if not ids:
            await message.answer(
                ("❌ Не найдено ни одной цели.\n" if not warnings else "")
                + "\n".join(warnings),
                parse_mode="HTML"
            )
            return

        intervals_json = json.dumps(intervals)
        if route_name is None:
            if len(ids) == 1:
                one = get_chat_topic_by_id(ids[0])
                one_name = one['ct_name'] if one and one['ct_name'] else f"ID {ids[0]}"
                route_name = f"Маршрут {source_name} → {one_name}"
            else:
                route_name = f"Маршрут {source_name} → целей: {len(ids)}"

        route_id = add_route(
            source_ct_id=source_ct['id'],
            route_name=route_name,
            route_mode='bulk',
            send_time=send_time,
            intervals_json=intervals_json,
            jitter_seconds=jitter_seconds,
            max_rounds=-1,
        )
        if route_id is None:
            await message.answer("❌ Ошибка: не удалось создать маршрут в базе данных.")
            return
        for cid in ids:
            add_route_target(route_id, cid)
        route = get_route_by_id(route_id)
        schedule_route_job(route)

        intervals_display = ', '.join(format_interval(i) for i in intervals)
        jitter_text = f", разброс {jitter_seconds} сек." if jitter_seconds > 0 else ""
        await message.answer(
            f"✅ Маршрут {route_id} создан!\n"
            f"Название: {route_name}\n"
            f"Целей добавлено: {len(ids)}\n"
            f"Первый пост в {send_time}\n"
            f"Интервалы: [{intervals_display}]{jitter_text}"
            + (("\n" + "\n".join(warnings)) if warnings else ""),
            parse_mode="HTML"
        )
    except ValueError as e:
        await message.answer(f"Ошибка: {e}\n\n{help_text}", parse_mode="HTML")
    except Exception as e:
        error_text = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        await message.answer(f"Ошибка: {error_text}\n\n{help_text}", parse_mode="HTML")


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
            recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        
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

# ---- Просмотр / управление ----


def _build_routes_page(routes: list, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Генерирует текст и клавиатуру для одной страницы списка маршрутов."""
    page_items, current_page, total_pages = paginate(routes, page, ROUTES_PAGE_SIZE)
    
    text = "<b>🛣️ Маршруты:</b>\n\n"
    if not page_items:
        text += "<i>Маршрутов пока нет.</i>"
    else:
        for r in page_items:
            route_name = r['route_name'] or f"Маршрут {r['id']}"
            intervals = json.loads(r['intervals_json'] or '[]')
            intervals_display = ', '.join(format_interval(i) for i in intervals) if intervals else "нет"
            status = "✅" if r['is_active'] else "❄️"
            source_ct = get_chat_topic_by_id(r['source_ct_id'])
            src_name = source_ct['ct_name'] if source_ct and source_ct['ct_name'] else f"ID {r['source_ct_id']}"
            targets = get_route_targets(r['id'])
            tgt_names = [t['ct_name'] or f"ID {t['ct_id']}" for t in targets]
            tgt_display = ", ".join(tgt_names) if tgt_names else "нет целей"
            random_status = "🎲 вкл" if r['use_random_targets'] else "🎲 выкл"
            pool_tags = json.loads(r['random_pool_tags'] or '[]')
            pool_line = f"   🎯 Фильтр пула: {', '.join(pool_tags)}\n" if pool_tags else ""
            text += (
                f"{status} ID <code>{r['id']}</code>: {route_name}\n"
                f"   Ист: {src_name}\n"
                f"   Цел: {tgt_display}\n"
                f"   Случ.расс. {random_status}\n"
                f"   Старт: {r['send_time']} | Инт-ы: [{intervals_display}]\n"
                f"   Jit: {r['jitter_seconds']} сек.\n\n"
            )
    
    keyboard = build_pagination_keyboard("routes", current_page, total_pages)
    return text, keyboard


@commands_router.message(Command("routes"), F.from_user.id == ADMIN_ID)
async def cmd_list_routes(message: types.Message):
    routes = get_all_routes(active_only=False)
    if not routes:
        await message.answer("Маршрутов пока нет. Ты всегда можешь их добавить)")
        return
    text, keyboard = _build_routes_page(routes, 0)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@commands_router.callback_query(F.data.startswith("routes:"))
async def routes_pagination(callback: types.CallbackQuery):
    await apply_pagination_callback(
        callback,
        lambda page: _build_routes_page(get_all_routes(active_only=False), page),
    )



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



def _select_chats(targets_str: str, mode: str, pool: list | None = None) -> tuple[list[int], list[str]]:
    """
    Универсальный разбор строки целей.
    Поддерживает: all, sendable, tag:<тег> (несколько = AND), имена чатов (смесь).
    mode='add':    pool=None — выбор среди всех чатов.
    mode='remove': pool=текущие цели (rows из get_route_targets) — выбор только среди них.
    Возвращает (список ct_id, список предупреждений).
    """
    tokens = [t.strip() for t in targets_str.replace(',', ' ').split() if t.strip()]
    pool_ids = {t['ct_id'] for t in pool} if pool is not None else None
    ids: list[int] = []
    seen: set[int] = set()
    warnings: list[str] = []

    def push(cid: int):
        if pool_ids is not None and cid not in pool_ids:
            return
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)

    tags = [t[4:] for t in tokens if t.lower().startswith('tag:') and len(t) > 4]
    others = [t for t in tokens if not t.lower().startswith('tag:')]

    for tok in others:
        low = tok.lower()
        if low == 'all':
            if pool is not None:
                for t in pool:
                    push(t['ct_id'])
            else:
                for row in get_all_chat_topics(active_only=True):
                    push(row['id'])
        elif low == 'sendable':
            if pool is not None:
                for t in pool:
                    if t['ct_sendable']:
                        push(t['ct_id'])
            else:
                for row in get_sendable_chat_topics():
                    push(row['id'])
        else:
            ct = get_chat_topic_by_name(tok)
            if ct:
                push(ct['id'])
            else:
                warnings.append(f"⚠️ Чат с именем <code>{tok}</code> не найден.")

    if tags:
        rows = get_chats_by_tags(tags, active_only=(pool is None))
        if rows:
            for row in rows:
                push(row['id'])
        else:
            warnings.append(f"⚠️ Нет чатов со ВСЕМИ тегами (AND): {', '.join(tags)}.")

    return ids, warnings



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
            "<code>/add_target 1 sendable</code> — добавить все чаты с флагом sendable\n"
            "<code>/add_target 1 tag:asia tag:old</code> — чаты со ВСЕМИ указанными тегами (AND)\n"
            "<code>/add_target 1 tag:asia my_channel</code> — можно смешивать теги и имена\n",
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


    ids, warnings = _select_chats(targets_str, mode='add')
    if not ids and not warnings:
        await message.answer("Не найдено чатов для добавления.")
        return

    added_count = 0
    skipped_count = 0
    source_warning = False
    for cid in ids:
        if cid in current_target_ct_ids:
            skipped_count += 1
            continue
        if cid == source_ct_id:
            source_warning = True
        if add_route_target(route_id, cid):
            added_count += 1
        else:
            skipped_count += 1

    report = list(warnings)
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


    ids, warnings = _select_chats(targets_str, mode='remove', pool=current_targets)
    if not ids and not warnings:
        await message.answer("Не найдено целей для удаления.")
        return

    removed_count = 0
    skipped_count = 0
    for cid in ids:
        if remove_route_target(route_id, cid):
            removed_count += 1
        else:
            skipped_count += 1

    report = list(warnings)
    if removed_count > 0:
        report.append(f"✅ <b>Убрано целей:</b> {removed_count}")
    if skipped_count > 0:
        report.append(f"⏭️ <b>Пропущено:</b> {skipped_count}")
    if not report:
        report.append("Ни одна цель не была удалена.")

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
        recalculate_and_reschedule(route_id, route)
        
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
            recalculate_and_reschedule(route_id, get_route_by_id(route_id))
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
            recalculate_and_reschedule(route_id, get_route_by_id(route_id))
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
            recalculate_and_reschedule(route_id, get_route_by_id(route_id))
        await message.answer(
            f"✅ Разброс маршрута {route_id} изменён на {jitter_seconds} сек."
        )
    else:
        await message.answer(f"Не удалось обновить разброс для маршрута {route_id}.")

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

@commands_router.message(Command("rename_route"), F.from_user.id == ADMIN_ID)
async def cmd_rename_route(message: types.Message):
    """Переименовывает маршрут (работает и для bulk, и для singular)."""
    if not message.text:
        return
    
    # maxsplit=2, чтобы название с пробелами попало целиком в args[2]
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/rename_route &lt;ID маршрута&gt; &lt;новое название&gt;</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/rename_route 5 Реклама партнёров</code>\n"
            "<code>/rename_route 3 Основной постинг</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        route_id = int(args[1])
        new_name = args[2].strip()
    except ValueError:
        await message.answer("ID маршрута должен быть числом.")
        return
    
    if not new_name:
        await message.answer("Название не может быть пустым.")
        return
    
    route = get_route_by_id(route_id)
    if not route:
        await message.answer(f"Маршрут {route_id} не найден.")
        return
    
    old_name = route['route_name'] or f"Маршрут {route_id}"
    
    if update_route(route_id, route_name=new_name):
        await message.answer(
            f"✅ Маршрут <code>{route_id}</code> переименован!\n\n"
            f"🔹 Было: <code>{old_name}</code>\n"
            f"🔹 Стало: <code>{new_name}</code>",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"Не удалось переименовать маршрут {route_id}.")


@commands_router.message(Command("posts"), F.from_user.id == ADMIN_ID)
async def cmd_list_posts(message: types.Message):
    """Показывает список постов в маршруте с их внутренними ID и кнопками."""
    if not message.text: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/posts &lt;ID маршрута&gt;</code>", parse_mode="HTML")
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
        
    posts = get_posts_for_route(route_id)
    if not posts:
        await message.answer(f"В маршруте {route_id} нет постов.")
        return
        
    text = f"<b>📚 Посты в маршруте {route_id}:</b>\n\n"
    for p in posts[:20]: # Показываем последние 20, чтобы не спамить
        msg_ids = json.loads(p['message_ids']) if p['message_ids'] else []
        sent = "✅" if p['is_sent'] else "⏳"
        btn_count = 0
        btn_preview = ""
        if p['buttons_json']:
            try:
                btns = json.loads(p['buttons_json'])
                btn_count = len(btns)
                preview_parts = [f"{i+1}. {b['text']}" for i, b in enumerate(btns[:3])]
                btn_preview = ", ".join(preview_parts)
                if len(btns) > 3:
                    btn_preview += f"... (+{len(btns)-3})"
            except:
                pass
                
        text += (
            f"<b>ID поста:</b> <code>{p['id']}</code> {sent}\n"
            f"TG Msg IDs: <code>{msg_ids}</code>\n"
            f"Кнопок: <b>{btn_count}</b>"
        )
        if btn_preview:
            text += f"\n<i>{btn_preview}</i>"
        text += "\n\n"
        
    if len(posts) > 20:
        text += f"<i>...и еще {len(posts) - 20} постов (показаны последние 20).</i>\n\n"
        
    text += "<b>🛠 Управление кнопками:</b>\n"
    text += "<code>/add_buttons &lt;ID поста&gt; Текст|URL</code> — добавить еще\n"
    text += "<code>/del_button &lt;ID поста&gt; &lt;номер&gt;</code> — удалить одну\n"
    text += "<code>/clear_buttons &lt;ID поста&gt;</code> — удалить все"
    
    await message.answer(text, parse_mode="HTML")\


@commands_router.message(Command("set_pool_tags"), F.from_user.id == ADMIN_ID)
async def cmd_set_pool_tags(message: types.Message):
    """Задаёт фильтр тегов для случайного пула маршрута (пусто = без фильтра)."""
    if not message.text:
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/set_pool_tags &lt;ID маршрута&gt; [теги...]</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/set_pool_tags 5 asia -old</code> — рандом только из чатов с тегом asia и БЕЗ тега old\n"
            "<code>/set_pool_tags 5</code> — снять фильтр (весь sendable-пул)\n\n"
            "Токены: <code>tag</code> — обязан иметь (AND), <code>-tag</code> — обязан не иметь.",
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

    tokens = []
    if len(args) == 3:
        for tok in args[2].replace(',', ' ').split():
            raw = tok[1:] if tok.startswith('-') else tok
            norm = normalize_tag(raw)
            if not norm:
                await message.answer(f"⚠️ Пустой тег в токене <code>{tok}</code>.", parse_mode="HTML")
                return
            tokens.append(('-' + norm) if tok.startswith('-') else norm)

    if update_route(route_id, random_pool_tags=json.dumps(tokens)):
        if tokens:
            await message.answer(
                f"✅ Фильтр случайного пула маршрута {route_id}: <code>{', '.join(tokens)}</code>",
                parse_mode="HTML"
            )
        else:
            await message.answer(f"✅ Фильтр случайного пула маршрута {route_id} снят (весь sendable-пул).")
    else:
        await message.answer("Не удалось обновить фильтр.")