# handlers/fsm_steps.py

# ==========================================
# ПОШАГОВЫЕ ДЕЙСТВИЯ 
# ==========================================


import json

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import ADMIN_ID
from utils import (
    format_interval,
    parse_interval,
    parse_intervals_list,
)

from db_funcs import (
    add_route,
    add_route_target,
    get_route_by_id,
)

from pg_bot.scheduler_utils import schedule_route_job

from state_store import (
    AddRouteStates,
    singular_media_groups, singular_media_group_timers,
    singular_import_active,
)



# Каждый модуль создаёт СВОЙ роутер
commands_router = Router(name="fsm_steps")

@commands_router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user is None:
        await message.answer("Действие отменено.")
        return
    # Очищаем режим импорта, если он был активен для этого админа
    routes_to_delete = [
        r_id for r_id, data in singular_import_active.items() 
        if data['admin_id'] == message.from_user.id
    ]
    for r_id in routes_to_delete:
        singular_import_active.pop(r_id)
    
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
