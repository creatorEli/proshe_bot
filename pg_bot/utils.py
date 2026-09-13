"""
Утилиты бота: парсинг интервалов, проверка ошибок, преобразование параметров.
"""

from config import MAIN_SOURCE_CHAT_ID

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
