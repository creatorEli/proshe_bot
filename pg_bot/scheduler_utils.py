import logging
import random
import json
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

from instance import scheduler, bot
from config import TIMEZONE

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

    
    # ==========================================
    # ЛОГИКА ДЛЯ SINGULAR (по одному целевику за раз)
    # ==========================================
    if route['route_mode'] == 'singular':
        current_round = route['completed_rounds']
        
        # 1. Ищем целевой чат, который ещё не получал пост в этом круге
        target = get_next_singular_target(route_id, current_round)
        
        if not target:
            # Круг завершён! Все цели получили пост.
            logging.info(f"Singular маршрут {route_id}: круг {current_round} завершён.")
            
            # Увеличиваем счётчик кругов
            new_rounds = increment_completed_rounds(route_id)
            
            # Сбрасываем посты, чтобы они снова стали доступны для нового круга
            reset_posts_for_route(route_id)
            
            # Проверяем лимит кругов
            if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
                logging.info(f"Singular маршрут {route_id}: достигнут лимит {new_rounds} кругов. Деактивация.")
                deactivate_route(route_id)
                job_id = f"route_{route_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                return False
            
            # Пробуем получить цель уже для нового круга
            target = get_next_singular_target(route_id, new_rounds)
            if not target:
                logging.warning(f"Singular маршрут {route_id}: нет активных целей для нового круга.")
                return False

        # 2. Берём пост (если нет несентых, сбрасываем и берём снова)
        post = get_random_unsent_post(route_id)
        if not post:
            reset_posts_for_route(route_id)
            post = get_random_unsent_post(route_id)
            
        if not post:
            logging.warning(f"Singular маршрут {route_id}: нет постов для отправки.")
            return False

        # 3. Отправляем пост в ОДИН выбранный целевой чат
        target_chat_id = target['ct_tg_chat_id']
        target_topic_id = target['ct_tg_topic_id']
        message_ids = sorted(post['message_ids'])
        
        send_kwargs = {}
        if target_topic_id:
            send_kwargs['message_thread_id'] = target_topic_id

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
            
            # 4. Успешно отправили: помечаем пост и цель
            mark_post_sent(post['id'])
            mark_singular_target_sent(route_id, target['ct_id'], current_round)
            
            logging.info(f"Singular: пост отправлен в {target['ct_name'] or target_chat_id} (круг {current_round})")
            
            # 5. Проверяем, не стал ли этот чат последним в круге
            if check_singular_round_complete(route_id, current_round):
                new_rounds = increment_completed_rounds(route_id)
                reset_posts_for_route(route_id)
                logging.info(f"Singular маршрут {route_id}: круг {current_round} полностью завершён после этой отправки.")
                
                if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
                    deactivate_route(route_id)
                    job_id = f"route_{route_id}"
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
                    logging.info(f"Singular маршрут {route_id}: достигнут максимум кругов ({new_rounds}).")

            if not manual_send:
                _schedule_next_publication(route_id, route)
            return True

        except Exception as e:
            if is_missing_source_message_error(e):
                logging.warning(f"Пост {post['id']} недоступен. Удаляю.")
                delete_post(post['id'])
                if not manual_send:
                    return await send_random_post_job(route_id, _attempt + 1, manual_send)
                return False
            logging.error(f"Ошибка отправки singular маршрута {route_id}: {e}")
            return False



    # ==========================================
    # ЛОГИКА ДЛЯ BULK (всем целям сразу, как было)
    # ==========================================

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

        # # Для singular увеличиваем счётчик кругов
        # if route['route_mode'] == 'singular':
        #     new_rounds = increment_completed_rounds(route_id)
            
        #     # Проверяем лимит
        #     if route['max_rounds'] != -1 and new_rounds >= route['max_rounds']:
        #         logging.info(
        #             f"Singular-маршрут {route_id}: лимит кругов достигнут "
        #             f"({new_rounds}/{route['max_rounds']})"
        #         )
        #         deactivate_route(route_id)
        #         # Удаляем задачу из планировщика
        #         job_id = f"route_{route_id}"
        #         if scheduler.get_job(job_id):
        #             scheduler.remove_job(job_id)
        #             logging.info(f"Планировщик: задача {job_id} удалена (лимит достигнут)")


        

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

