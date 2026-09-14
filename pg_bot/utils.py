"""
Утилиты бота: парсинг интервалов, проверка ошибок, преобразование параметров.
"""

from config import MAIN_SOURCE_CHAT_ID
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import logging
from typing import Callable, Tuple
from aiogram import types
from aiogram.exceptions import TelegramBadRequest

# ==========================================
# ТЕКСТЫ И УТИЛИТЫ
# ==========================================



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


def resolve_source_chat(raw_chat_id: int) -> int:
    """Заменяет 0 на MAIN_SOURCE_CHAT_ID"""
    return MAIN_SOURCE_CHAT_ID if raw_chat_id == 0 else raw_chat_id



SOURCE_MESSAGE_MISSING_ERRORS = (
    'message to copy not found', 'messages to copy not found', 'message not found',
    'message to forward not found', 'there are no messages to forward',
    'message_id_invalid', 'wrong message id', 'invalid message id',
    "message can't be copied", 'was not forwarded', 'failed to send message',
)


def is_missing_source_message_error(error: Exception) -> bool:
    """
    Проверяет, что ошибка связана с отсутствием/недоступностью
    исходного сообщения или галереи в чате-источнике.
    """
    error_str = str(error).lower()
    return any(marker in error_str for marker in SOURCE_MESSAGE_MISSING_ERRORS)



def paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    """
    Разбивает список на страницы.
    Возвращает: (элементы текущей страницы, номер страницы (0-based), всего страниц)
    """
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = start + page_size
    return items[start:end], page, total_pages


def build_pagination_keyboard(prefix: str, current_page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру с кнопками пагинации."""
    buttons = []
    
    if total_pages > 1:
        row = []
        if current_page > 0:
            row.append(InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"{prefix}:{current_page - 1}"
            ))
        if current_page < total_pages - 1:
            row.append(InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=f"{prefix}:{current_page + 1}"
            ))
        if row:
            buttons.append(row)
    
    # Индикатор текущей страницы (callback_data="noop" — ничего не делает)
    buttons.append([
        InlineKeyboardButton(
            text=f"📄 {current_page + 1} / {total_pages}",
            callback_data="noop"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def apply_pagination_callback(
    callback: types.CallbackQuery,
    build_fn: Callable[[int], Tuple[str, InlineKeyboardMarkup]],
) -> None:
    """
    Универсальный обработчик callback-кнопок пагинации.
    build_fn(page) должна возвращать (text, keyboard) для нужной страницы.
    """
    # Guard 1: callback.data по типам может быть None
    data = callback.data
    if data is None:
        return

    await callback.answer()  # гасим «спиннер» на кнопке

    try:
        _, page_str = data.split(":", 1)
        page = int(page_str)
    except (ValueError, IndexError):
        return

    text, keyboard = build_fn(page)

    # Guard 2: callback.message может быть InaccessibleMessage без edit_text
    message = callback.message
    if not isinstance(message, types.Message):
        return

    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest as e:
        # Контент не изменился (например, двойное нажатие) — не спамим в логи
        if "message is not modified" not in str(e):
            logging.warning(f"Не удалось обновить страницу: {e}")
    except Exception as e:
        logging.warning(f"Не удалось обновить страницу: {e}")