"""
Глобальное состояние бота:
- состояния FSM
- словари для агрегации галерей
- режим импорта singular-постов
"""
from collections import defaultdict
from aiogram.fsm.state import State, StatesGroup

class AddRouteStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_time = State()
    waiting_for_intervals = State()
    waiting_for_jitter = State()

class SingularImportStates(StatesGroup):
    waiting_for_forward = State()


# ==========================================
# Словари для агрегации галерей (для bulk)
# ==========================================
media_groups = defaultdict(list)
media_group_timers = {}

# ==========================================
# Словари для агрегации галерей (для singular import)
# ==========================================
singular_media_groups = defaultdict(list)
singular_media_group_timers = {}


# ==========================================
# Режим импорта singular-постов
# {route_id: {'source_chat_id': int, 'admin_id': int, 'expires_at': float}}
# ==========================================\
singular_import_active = {}