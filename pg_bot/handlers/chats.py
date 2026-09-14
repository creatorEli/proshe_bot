# ==========================================
# КОМАНДЫ УПРАВЛЕНИЯ ЧАТАМИ
# ==========================================


import logging

from aiogram import Router, types, F
from aiogram.filters import Command

from config import ADMIN_ID
from utils import (
    apply_pagination_callback,
    resolve_source_chat,
    paginate, 
    build_pagination_keyboard
)

from aiogram.types import InlineKeyboardMarkup

CHATS_PAGE_SIZE = 10


from db_funcs import (
    add_chat_topic,
    check_chat_topic_in_use,
    delete_chat_topic,
    get_all_chat_topics,
    get_chat_topic_by_id,
    get_chat_topic_by_name,
    get_chat_topic_by_tg_ids,
    update_chat_topic
)

# Каждый модуль создаёт СВОЙ роутер
commands_router = Router(name="chats")

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
        tg_chat_id = resolve_source_chat(int(args[2]))
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




def _build_chats_page(chats: list, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Генерирует текст и клавиатуру для одной страницы списка чатов."""
    page_items, current_page, total_pages = paginate(chats, page, CHATS_PAGE_SIZE)
    
    text = "<b>📚 Зарегистрированные чаты и топики:</b>\n\n"
    if not page_items:
        text += "<i>Чатов пока нет.</i>"
    else:
        for c in page_items:
            status = "✅" if c['is_active'] else "❄️"
            sendable = "▶️" if c['ct_sendable'] else "⬇️"
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
                f"{status}{sendable} ID <code>{c['id']}</code>: {name}\n"
                f"   TG: <code>{c['ct_tg_chat_id']}{topic_text}</code>\n"
                f"   {usage_text}\n\n"
            )
    
    text += (
        "<b>Легенда:</b>\n"
        "✅ активен / ❄️ заморожен\n"
        "▶️ sendable / ⬇️ только источник"
    )
    
    keyboard = build_pagination_keyboard("chats", current_page, total_pages)
    return text, keyboard


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
    text, keyboard = _build_chats_page(chats, 0)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@commands_router.callback_query(F.data.startswith("chats:"))
async def chats_pagination(callback: types.CallbackQuery):
    await apply_pagination_callback(
        callback,
        lambda page: _build_chats_page(get_all_chat_topics(active_only=False), page),
    )





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

