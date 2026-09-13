# ==========================================
# КОМАНДЫ СИНГУЛЯРНЫХ МАШРУТОВ 
# ==========================================

import json

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import ADMIN_ID
from db_funcs import (
    add_route,
    add_route_target,
    get_all_routes,
    get_chat_topic_by_id, 
    get_chat_topic_by_name, 
    get_route_by_id,
    get_route_targets,
    update_route
)

from utils import (
    format_interval,
    parse_intervals_list,
)

from state_store import (
    AddRouteStates
)

from pg_bot.scheduler_utils import (
    recalculate_and_reschedule,
    schedule_route_job
)

commands_router = Router(name="singular")

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
            f"{status} ID {r['id']}: {route_name}\n"
            f"   Ист: {src_name}\n"
            f"   Цел: {tgt_display}\n"
            f"   Круги: {rounds_text}\n"
            f"   Старт: {r['send_time']} | Интервалы: [{intervals_display}]\n\n"
        )
    
    await message.answer(text, parse_mode="HTML")

