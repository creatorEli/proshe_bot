import logging
import random
import json
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from aiogram import types
from instance import scheduler, bot
from config import TIMEZONE
import asyncio
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO)


from db_funcs import (
    get_route_by_id, 
    get_chat_topic_by_id,
    get_next_singular_target,
    increment_completed_rounds,
    reset_posts_for_route,
    deactivate_route,
    get_random_unsent_post,
    mark_post_sent,
    mark_singular_target_sent,
    check_singular_round_complete,
    delete_post,
    get_route_targets,
    get_random_sendable_targets,
)

from utils import format_interval, is_missing_source_message_error

from db_funcs import update_route_schedule

# ==========================================
# ЛОГИКА ОТПРАВКИ И СБОРА
# ==========================================


async def send_random_post_job(route_id: int, _attempt: int = 0, manual_send: bool = False) -> bool:
    """
    Главная функция-оркестратор отправки поста.
    Выносит общую логику (проверки, получение поста, обработка ошибок) 
    и делегирует режим-specific логику в отдельные функции.
    """
    MAX_ATTEMPTS = 5
    if _attempt >= MAX_ATTEMPTS:
        logging.warning(f"Маршрут {route_id}: лимит попыток ({MAX_ATTEMPTS}) исчерпан.")
        return False

    route = get_route_by_id(route_id)
    if not route:
        logging.warning(f"Маршрут {route_id} не найден.")
        return False

    if not route['is_active'] and not manual_send:
        logging.info(f"Маршрут {route_id} заморожен. Пропускаю отправку.")
        return False

    source_ct = get_chat_topic_by_id(route['source_ct_id'])
    if not source_ct:
        logging.error(f"Маршрут {route_id}: source_ct_id={route['source_ct_id']} не найден!")
        return False
    source_chat_id = source_ct['ct_tg_chat_id']

    # 1. Получаем и валидируем пост (общая логика для обоих режимов)
    post = get_random_unsent_post(route_id)
    if not post:
        reset_posts_for_route(route_id)
        post = get_random_unsent_post(route_id)
        
    if not post:
        logging.warning(f"Маршрут {route_id}: нет постов для отправки.")
        return False

    message_ids = sorted(post['message_ids'])
    if not message_ids:
        logging.warning(f"Пост {post['id']} пустой. Удаляю.")
        delete_post(post['id'])
        if not manual_send:
            return await send_random_post_job(route_id, _attempt + 1, manual_send)
        return False

    # 2. Выполняем режим-специфичную отправку
    try:
        if route['route_mode'] == 'singular':
            success = await _execute_singular_send(route, source_chat_id, post, message_ids, manual_send)
        else:
            success = await _execute_bulk_send(route, source_chat_id, post, message_ids, manual_send)

        # 3. Если отправка успешна и это не ручной запуск, планируем следующий
        if success and not manual_send:
            _schedule_next_publication(route_id, route)
            
        return success

    except Exception as e:
        # 4. Единый блок обработки ошибок для обоих режимов
        if is_missing_source_message_error(e):
            logging.warning(f"Пост {post['id']} недоступен. Удаляю и пробую следующий (попытка {_attempt + 1}).")
            delete_post(post['id'])
            if not manual_send:
                return await send_random_post_job(route_id, _attempt + 1, manual_send)
            return False
            
        logging.error(f"Ошибка отправки маршрута {route_id}: {e}")
        return False


# Константа задержки между целями в секундах (5 минут = 300 сек.)
BULK_TARGET_DELAY = 300 

async def _execute_singular_send(route, source_chat_id: int, post: dict, message_ids: list, manual_send: bool) -> bool:
    """Логика отправки для singular-маршрутов (по одной цели за тик)."""
    route_id = route['id']
    current_round = route['completed_rounds']
    
    # 1. Находим цель, которая ещё не получала пост в ТЕКУЩЕМ круге
    target = get_next_singular_target(route_id, current_round)
    
    if not target:
        # Если цель не найдена, значит активных целей нет
        logging.warning(f"Singular {route_id}: нет доступных целей для круга {current_round}.")
        return False

    target_chat_id = target['ct_tg_chat_id']
    target_topic_id = target['ct_tg_topic_id']
    send_kwargs = {'message_thread_id': target_topic_id} if target_topic_id else {}

    # 2. Парсим кнопки (ТОЛЬКО для одиночных сообщений, для альбомов Telegram API не поддерживает кнопки)
    markup = None
    is_album = len(message_ids) > 1
    
    if not is_album and post.get('buttons_json'):
        try:
            buttons_data = json.loads(post['buttons_json'])
            keyboard = [
                [types.InlineKeyboardButton(text=btn['text'], url=btn['url'])]
                 for btn in buttons_data
            ]
            markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
        except Exception as e:
            logging.warning(f"Ошибка парсинга кнопок для поста {post['id']}: {e}")

    # 3. Отправляем сообщения
    try:
        if is_album and hasattr(bot, 'copy_messages'):
            # Для альбомов кнопки не добавляем (ограничение Telegram API)
            await bot.copy_messages(
                chat_id=target_chat_id, 
                from_chat_id=source_chat_id, 
                message_ids=message_ids, 
                **send_kwargs
            )
        else:
            # Одиночное сообщение (с кнопками или без)
            await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=message_ids[0],
                reply_markup=markup,
                **send_kwargs
            )
    except Exception as e:
        logging.error(f"Ошибка при копировании сообщения {post['id']}: {e}")
        raise # Пробрасываем ошибку выше, чтобы сработал retry в send_random_post_job

    # 4. Помечаем пост и цель как отправленные
    mark_post_sent(post['id'])
    mark_singular_target_sent(route_id, target['ct_id'], current_round)
    logging.info(f"Singular: пост {post['id']} отправлен в {target['ct_name'] or target_chat_id} (круг {current_round})")

    # 5. Проверяем, завершён ли текущий круг (все ли цели получили пост)
    if check_singular_round_complete(route_id, current_round):
        logging.info(f"Singular {route_id}: круг {current_round} полностью завершён.")
        
        # Инкрементируем счётчик ЗАВЕРШЁННЫХ кругов
        new_rounds = increment_completed_rounds(route_id)
        
        # Проверяем лимит
        if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
            logging.info(f"Singular {route_id}: достигнут лимит {new_rounds} кругов. Деактивация.")
            deactivate_route(route_id)
            job_id = f"route_{route_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            return True # Пост уже ушёл, возвращаем True
            
        # Лимит не достигнут — сбрасываем посты для НОВОГО круга
        reset_posts_for_route(route_id)
        logging.info(f"Singular {route_id}: посты сброшены, следующий тик начнёт круг {new_rounds}.")

    return True


# async def _execute_singular_send(route, source_chat_id: int, post: dict, message_ids: list, manual_send: bool) -> bool:
#     """Логика отправки для singular-маршрутов (по одной цели за тик)."""
#     route_id = route['id']
#     current_round = route['completed_rounds']
    
#     # 1. Находим цель, которая ещё не получала пост в ТЕКУЩЕМ круге
#     target = get_next_singular_target(route_id, current_round)
    
#     if not target:
#         # Круг завершён! Сбрасываем посты и переходим к следующему кругу
#         logging.info(f"Singular {route_id}: круг {current_round} завершён. Сброс постов.")
#         reset_posts_for_route(route_id)
        
#         new_rounds = increment_completed_rounds(route_id)
        
#         # Проверка лимита кругов
#         if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
#             logging.info(f"Singular {route_id}: достигнут лимит {new_rounds} кругов. Деактивация.")
#             deactivate_route(route_id)
#             job_id = f"route_{route_id}"
#             if scheduler.get_job(job_id):
#                 scheduler.remove_job(job_id)
#             return False
            
#         # Ищем цель уже для нового круга
#         target = get_next_singular_target(route_id, new_rounds)
#         if not target:
#             logging.warning(f"Singular {route_id}: нет активных целей для нового круга {new_rounds}.")
#             return False
#         # Обновляем current_round для корректной пометки цели
#         current_round = new_rounds

#     # Отправляем пост в ОДНУ выбранную цель
#     target_chat_id = target['ct_tg_chat_id']
#     target_topic_id = target['ct_tg_topic_id']
    
#     send_kwargs = {'message_thread_id': target_topic_id} if target_topic_id else {}

#     # 1. Заранее парсим кнопки, но НЕ добавляем их в send_kwargs
#     markup = None
#     if post.get('buttons_json'):
        
#         try:
#             buttons_data = json.loads(post['buttons_json'])
#             keyboard = [[types.InlineKeyboardButton(text=btn['text'], url=btn['url'])] for btn in buttons_data]
#             markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
#         except Exception as e:
#             logging.warning(f"Ошибка парсинга кнопок для поста {post['id']}: {e}")

#     # 2. Копируем сообщения БЕЗ reply_markup
#     if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
#         # copy_messages возвращает список объектов MessageId
#         copied_msgs = await bot.copy_messages(
#             chat_id=target_chat_id, 
#             from_chat_id=source_chat_id, 
#             message_ids=message_ids, 
#             **send_kwargs
#         )
        
#         # Если есть кнопки, добавляем их к ПОСЛЕДНЕМУ сообщению в альбоме
#         if markup and copied_msgs:
#             if len(message_ids) > 1:
#                 last_msg_id = copied_msgs[-1].message_id
                
#                 try:    
#                     await bot.edit_message_reply_markup(
#                         chat_id=target_chat_id,
#                         message_id=last_msg_id,
#                         reply_markup=markup
#                     )

#                 except TelegramBadRequest as e:
#                     if "message is not modified" in str(e):
#                         # Кнопки уже на месте, игнорируем ошибку
#                         pass 
#                     else:
#                         logging.warning(f"Не удалось добавить кнопки: {e}")

#                 except Exception as e:
#                     logging.warning(f"Не удалось добавить кнопки к скопированному альбому {post['id']}: {e}")

#             elif len(message_ids) == 1:
#                 await bot.copy_message(
#                     chat_id=target_chat_id,
#                     from_chat_id=source_chat_id,
#                     message_id=message_ids[0],
#                     reply_markup=markup, # Кнопки применятся сразу при копировании
#                     **send_kwargs
#                 )
                
#     else:
#         for msg_id in message_ids:
#             # copy_message возвращает объект MessageId
#             copied_msg = await bot.copy_message(
#                 chat_id=target_chat_id, 
#                 from_chat_id=source_chat_id, 
#                 message_id=msg_id, 
#                 **send_kwargs
#             )
            
#             # Если есть кнопки, редактируем скопированное сообщение
#             if markup and copied_msg:
#                 try:
#                     await bot.edit_message_reply_markup(
#                         chat_id=target_chat_id,
#                         message_id=copied_msg.message_id,
#                         reply_markup=markup
#                     )
#                 except Exception as e:
#                     logging.warning(f"Не удалось добавить кнопки к сообщению {msg_id}: {e}")


#     # if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
#     #     await bot.copy_messages(chat_id=target_chat_id, from_chat_id=source_chat_id, message_ids=message_ids, **send_kwargs)
#     # else:
#     #     for msg_id in message_ids:
#     #         await bot.copy_message(chat_id=target_chat_id, from_chat_id=source_chat_id, message_id=msg_id, **send_kwargs)

#     # 3. Помечаем пост и цель как отправленные
#     mark_post_sent(post['id'])
#     mark_singular_target_sent(route_id, target['ct_id'], current_round)
#     logging.info(f"Singular: пост {post['id']} отправлен в {target['ct_name'] or target_chat_id} (круг {current_round})")

#     # 4. Проверяем, не стал ли этот чат последним в круге (опционально, для мгновенной реакции)
#     if check_singular_round_complete(route_id, current_round):
#         logging.info(f"Singular {route_id}: круг {current_round} полностью завершён после этой отправки.")
#         # Сброс и инкремент произойдут при следующем тике, когда get_next_singular_target вернёт None.
#         # Это предотвращает двойной инкремент в одном тике.

#     return True


async def _execute_bulk_send(route, source_chat_id: int, post: dict, message_ids: list, manual_send: bool) -> bool:
    """Логика отправки для bulk-маршрутов (во все цели сразу)."""
    route_id = route['id']
    
    # 1. Получаем все активные цели
    targets = get_route_targets(route_id, active_only=True)
    if not targets:
        logging.error(f"Bulk {route_id}: нет активных целей!")
        return False

    guaranteed_ct_ids = []

    # === ПОДГОТОВКА КНОПОК (одинаковые для всех целей в bulk) ===
    # send_kwargs_base = {}
    # if post.get('buttons_json'):
    #     try:
    #         buttons_data = json.loads(post['buttons_json'])
    #         keyboard = [[types.InlineKeyboardButton(text=btn['text'], url=btn['url'])] for btn in buttons_data]
    #         send_kwargs_base['reply_markup'] = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    #     except Exception as e:
    #         logging.warning(f"Ошибка парсинга кнопок для поста {post['id']}: {e}")

    
    # 2. Отправляем во все гарантированные цели С ЗАДЕРЖКОЙ
    for i, target in targets:
        target_chat_id = target['ct_tg_chat_id']
        target_topic_id = target['ct_tg_topic_id']
        guaranteed_ct_ids.append(target['ct_id'])
        
        send_kwargs = {'message_thread_id': target_topic_id} if target_topic_id else {}

        # TODO: Здесь можно добавить get_note_for_target для подписей        
        
        # Делаем задержку перед каждой целью, КРОМЕ первой (i == 0)
        if i > 0:
            logging.info(f"Bulk {route_id}: ожидание {BULK_TARGET_DELAY} сек. перед отправкой в {target['ct_name'] or target_chat_id}...")
            await asyncio.sleep(BULK_TARGET_DELAY)
        
        
        if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
            await bot.copy_messages(chat_id=target_chat_id, from_chat_id=source_chat_id, message_ids=message_ids, **send_kwargs)
        else:
            for msg_id in message_ids:
                await bot.copy_message(chat_id=target_chat_id, from_chat_id=source_chat_id, message_id=msg_id, **send_kwargs)
        logging.info(f"Bulk: пост {post['id']} отправлен в {target['ct_name'] or target_chat_id}")

    # 3. Обработка случайной рассылки (если включена)
    if route.get('use_random_targets'):
        random_pool = get_random_sendable_targets(exclude_ids=guaranteed_ct_ids)
        if random_pool:
            random_target = random.choice(random_pool)
            target_chat_id = random_target['ct_tg_chat_id']
            target_topic_id = random_target['ct_tg_topic_id']
            
            send_kwargs = {'message_thread_id': target_topic_id} if target_topic_id else {}
            logging.info(f"Bulk {route_id}: доп. случайная отправка в {random_target['ct_name'] or target_chat_id}")

            # Задержка и перед случайной целью тоже
            logging.info(f"Bulk {route_id}: ожидание {BULK_TARGET_DELAY} сек. перед случайной отправкой в {random_target['ct_name'] or target_chat_id}...")
            await asyncio.sleep(BULK_TARGET_DELAY)
            
            
            try:
                if len(message_ids) > 1 and hasattr(bot, 'copy_messages'):
                    await bot.copy_messages(chat_id=target_chat_id, from_chat_id=source_chat_id, message_ids=message_ids, **send_kwargs)
                else:
                    for msg_id in message_ids:
                        await bot.copy_message(chat_id=target_chat_id, from_chat_id=source_chat_id, message_id=msg_id, **send_kwargs)
                logging.info(f"Bulk: пост {post['id']} отправлен в случайный чат {random_target['ct_name'] or target_chat_id}")

            except Exception as e:
                logging.warning(f"Не удалось отправить в случайный чат {target_chat_id}: {e}")

    # 4. Помечаем пост как отправленный
    mark_post_sent(post['id'])
    return True


def _schedule_next_publication(route_id: int, route):
    intervals = json.loads(route['intervals_json'] or '[]')
    if not intervals:
        return

    current_index = route['interval_index'] or 0
    interval_seconds = intervals[current_index]
    next_index = (current_index + 1) % len(intervals)

    tz = ZoneInfo(TIMEZONE)


    # Берём ПЛАНОВОЕ время из БД (в нём нет джиттера)
    saved_next_run = route['next_run_time']
    if saved_next_run:
        try:
            base_planned = datetime.fromisoformat(saved_next_run)
        except ValueError:
            base_planned = datetime.now(tz)
    else:
        base_planned = datetime.now(tz)

    # Плановое время следующего запуска = базовое + интервал (СТРОГО, без джиттера)
    next_planned = base_planned + timedelta(seconds=interval_seconds)

    # Сохраняем в БД именно ПЛАНОВОЕ время (без джиттера)
    update_route_schedule(route_id, next_index, next_planned.isoformat())

    # Фактическое время запуска = плановое + джиттер
    actual_run = next_planned

    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        actual_run += timedelta(seconds=random.randint(0, jitter))


    job_id = f"route_{route_id}"
    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=actual_run,  # <-- с джиттером
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    logging.info(
        f"Маршрут {route_id}: плановое время {next_planned}, "
        f"фактический запуск {actual_run} "
        f"(через {format_interval(interval_seconds)})"
    )


def _calculate_initial_start(send_time: str) -> datetime:
    h, m = map(int, send_time.split(':'))
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_date = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start_date <= now:
        start_date += timedelta(days=1)
    return start_date



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
            planned_start = datetime.fromisoformat(saved_next_run)
            if planned_start <= now:
                logging.warning(
                    f"Маршрут {route_id}: плановое время {planned_start} в прошлом. "
                    f"Пересчитываю от текущего момента."
                )
                current_index = route['interval_index'] or 0
                interval_seconds = intervals[current_index]
                planned_start = now + timedelta(seconds=interval_seconds)
            else:
                logging.info(f"Маршрут {route_id}: восстановлено плановое время = {planned_start}")
        except ValueError:
            planned_start = _calculate_initial_start(send_time)
    else:
        planned_start = _calculate_initial_start(send_time)
        logging.info(f"Маршрут {route_id}: первый запуск, плановое время = {planned_start}")

    # Сохраняем в БД ПЛАНОВОЕ время (БЕЗ джиттера)
    update_route_schedule(route_id, route['interval_index'] or 0, planned_start.isoformat())

    # Фактический запуск = плановое + джиттер
    actual_start = planned_start
    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        actual_start += timedelta(seconds=random.randint(0, jitter))

    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=actual_start,  # <-- с джиттером
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    intervals_display = ', '.join(format_interval(i) for i in intervals)
    logging.info(
        f"Загружен маршрут {route_id}: плановое {planned_start}, "
        f"фактический старт {actual_start}, интервалы [{intervals_display}]"
    )

def recalculate_and_reschedule(route_id: int, route):
    """Пересчитывает next_run_time и обновляет задачу в планировщике."""
    intervals = json.loads(route['intervals_json'] or '[]')
    send_time = route['send_time']
    if not intervals or not send_time:
        return
    
    # Плановое время запуска (БЕЗ джиттера)
    planned_start = _calculate_initial_start(send_time)

    # Сохраняем в БД ПЛАНОВОЕ время
    update_route_schedule(route_id, 0, planned_start.isoformat())

    # Фактический запуск = плановое + джиттер
    actual_start = planned_start
    jitter = route['jitter_seconds'] or 0
    if jitter > 0:
        actual_start += timedelta(seconds=random.randint(0, jitter))

    job_id = f"route_{route_id}"
    scheduler.add_job(
        send_random_post_job,
        trigger='date',
        run_date=actual_start,  # <-- с джиттером
        args=[route_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )
    logging.info(
        f"Маршрут {route_id}: расписание обновлено, "
        f"плановое {planned_start}, фактический запуск {actual_start}"
    )

