# handlers/collector.py 

import asyncio
import logging
from aiogram import Router, types, F

from db_funcs import get_routes_for_source, save_post
from state_store import (
    media_groups, media_group_timers,
    singular_media_groups, singular_media_group_timers,
    singular_import_active,
)

collector_router = Router(name="collector")

from instance import bot

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
            # Проверяем, не истёк ли таймаут (WARN было get_event_loop())
            if asyncio.get_running_loop().time() > data['expires_at']:
                del singular_import_active[r_id]
                continue
            active_import_route = r_id
            import_data = data
            break

    if active_import_route is not None and import_data is not None:
        # Мы в режиме импорта! Сохраняем пост именно в этот маршрут.
        media_group_id = message.media_group_id
        admin_id = import_data['admin_id']

        if media_group_id:
            key = (active_import_route, media_group_id)
            singular_media_groups[key].append(message.message_id)
            if key in singular_media_group_timers:
                singular_media_group_timers[key].cancel()


            async def save_singular_import(key=key, r_id=active_import_route, admin_id=admin_id):
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
                    except Exception as e:
                        logging.warning(f"Не удалось уведомить админа {admin_id} об импорте: {e}")
                    # Отключаем режим после успешного импорта
                    if r_id in singular_import_active:
                        del singular_import_active[r_id]

                        try:
                            await bot.send_message(
                                admin_id,
                                f"Импорт завершён!",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logging.warning(f"Не удалось уведомить админа {admin_id} о завершении импорта: {e}")
                                            


            task = asyncio.create_task(save_singular_import())
            singular_media_group_timers[key] = task
            # Уведомляем админа в личку, а не в исходный чат
            try:
                await bot.send_message(
                    import_data['admin_id'],
                    "⏳ Принимаю галерею для импорта... (подожди 5 сек)"
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить админа {admin_id} о статусе импорта: {e}")
        else:
            # Одиночное сообщение
            if save_post(active_import_route, [message.message_id]):
                try:
                    await bot.send_message(
                        import_data['admin_id'],
                        f"✅ <b>Сообщение <code>{message.message_id}</code> успешно импортировано в маршрут {active_import_route}!</b>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logging.warning(f"Не удалось уведомить админа об импорте: {e}")

                # Отключаем режим
                if active_import_route in singular_import_active:
                    del singular_import_active[active_import_route]
                    try:
                        await bot.send_message(
                            admin_id,
                            f"Импорт завершён!",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logging.warning(f"Не удалось уведомить админа {admin_id} о завершении импорта: {e}")
                                                    

            #await message.answer("✅ Сообщение сохранено для импорта.")
        
        # ВАЖНО: прерываем выполнение, чтобы не сработала логика bulk
        return

    # ==========================================
    # 2. ОБЫЧНАЯ ЛОГИКА BULK
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

