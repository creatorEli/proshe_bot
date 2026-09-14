# handlers/imports.py

import asyncio
import json
import logging

from aiogram import Router, types, F
from aiogram.filters import Command

from config import ADMIN_ID, MAIN_SOURCE_CHAT_ID
from db_funcs import (
    get_chat_topic_by_id,
    get_last_post_id,
    get_route_by_id,
    save_post,
    update_post_buttons, 
    get_posts_for_route, 
    clear_post_buttons, 
    remove_post_button, 
    _get_conn

)

from state_store import (
    singular_import_active,
)

from instance import bot, scheduler 

# Один роутер, без дублей
commands_router = Router(name="imports")


@commands_router.message(Command("import_singular"), F.from_user.id == ADMIN_ID)
async def cmd_import_singular(message: types.Message):
    """Активирует режим прослушивания исходного чата для импорта singular-поста."""
    if not message.text:
        return

    if message.from_user is None:   # ← гасит ошибку Pyright
        return
    
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
        'expires_at': asyncio.get_running_loop().time() + 60
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

# @commands_router.message(
#     F.forward_from_chat, 
#     SingularImportStates.waiting_for_forward, 
#     F.from_user.id == ADMIN_ID
# )


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

    # Проверяем доступность сообщения (OR bot.copyMessage)
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


@commands_router.message(Command("add_buttons"), F.from_user.id == ADMIN_ID)
async def cmd_add_buttons(message: types.Message):
    """
    Добавляет inline-кнопки к посту.
    Формат: /add_buttons <ID маршрута ИЛИ ID поста> <Текст1|URL1, Текст2|URL2>
    """
    if not message.text:
        return
    
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "<b>Формат:</b>\n"
            "<code>/add_buttons &lt;ID маршрута или ID поста&gt; &lt;Текст1|URL1, Текст2|URL2&gt;</code>\n\n"
            "<b>Примеры:</b>\n"
            "<code>/add_buttons 5 Канал|https://t.me/channel</code> (к последнему посту маршрута 5)\n"
            "<code>/add_buttons 123 Сайт|https://example.com</code> (к конкретному посту 123)\n\n"
            "ID постов можно посмотреть через <code>/posts &lt;ID маршрута&gt;</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        target_id = int(args[1])
        buttons_str = args[2].strip()
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    
    # Определяем, что именно нам передали: ID поста или ID маршрута
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT id, route_id FROM posts WHERE id = ?', (target_id,))
    post_row = c.fetchone()
    conn.close()
    
    if post_row:
        # Передали ID конкретного поста
        post_id = post_row['id']
        route_id = post_row['route_id']
    else:
        # Передали ID маршрута, ищем последний пост
        route_id = target_id
        route = get_route_by_id(route_id)
        if not route:
            await message.answer(f"Маршрут {route_id} не найден (и поста с таким ID тоже нет).")
            return
        post_id = get_last_post_id(route_id)
        if not post_id:
            await message.answer(f"В маршруте {route_id} нет постов. Сначала импортируй пост.")
            return

    # Парсим кнопки
    buttons_list = []
    try:
        pairs = [p.strip() for p in buttons_str.split(',')]
        for pair in pairs:
            if '|' in pair:
                text, url = pair.split('|', 1)
                if text.strip() and url.strip():
                    buttons_list.append({"text": text.strip(), "url": url.strip()})
        
        if not buttons_list:
            raise ValueError("Неверный формат кнопок")
    except Exception as e:
        await message.answer(f"Ошибка в формате кнопок: {e}")
        return
    
    # Сохраняем кнопки
    buttons_json = json.dumps(buttons_list)
    if update_post_buttons(post_id, buttons_json):
        await message.answer(
            f"✅ Добавлено {len(buttons_list)} кнопок к посту <code>{post_id}</code> (маршрут {route_id}):\n"
            + "\n".join([f"  • {btn['text']}: {btn['url']}" for btn in buttons_list]),
            parse_mode="HTML"
        )
    else:
        await message.answer("Не удалось обновить кнопки.")


@commands_router.message(Command("clear_buttons"), F.from_user.id == ADMIN_ID)
async def cmd_clear_buttons(message: types.Message):
    """Полностью очищает кнопки у поста."""
    if not message.text: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Формат: <code>/clear_buttons &lt;ID поста&gt;</code>", parse_mode="HTML")
        return
    try:
        post_id = int(args[1])
    except ValueError:
        await message.answer("ID поста должен быть числом.")
        return
        
    if clear_post_buttons(post_id):
        await message.answer(f"✅ Все кнопки успешно удалены у поста <code>{post_id}</code>.", parse_mode="HTML")
    else:
        await message.answer(f"❌ Пост <code>{post_id}</code> не найден.", parse_mode="HTML")


@commands_router.message(Command("del_button"), F.from_user.id == ADMIN_ID)
async def cmd_del_button(message: types.Message):
    """Удаляет конкретную кнопку по её номеру из списка."""
    if not message.text: return
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/del_button &lt;ID поста&gt; &lt;номер&gt;</code>\n"
            "Номер кнопки можно посмотреть в <code>/posts &lt;ID маршрута&gt;</code>", 
            parse_mode="HTML"
        )
        return
    try:
        post_id = int(args[1])
        index = int(args[2])
    except ValueError:
        await message.answer("ID поста и номер кнопки должны быть числами.")
        return
        
    success, msg = remove_post_button(post_id, index)
    if success:
        await message.answer(f"✅ {msg}", parse_mode="HTML")
    else:
        await message.answer(f"❌ {msg}", parse_mode="HTML")