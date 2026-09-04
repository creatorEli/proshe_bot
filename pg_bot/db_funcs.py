import os
import sqlite3
import random
import json
from typing import Optional
from config import DB_NAME, MAIN_SOURCE_CHAT_ID

# Получаем абсолютный путь к папке, где лежит бот (bot.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Формируем полный путь к файлу БД
DB_PATH = os.path.join(BASE_DIR, "..", "data", DB_NAME)

# ============================================================
# Инициализация
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ct_name TEXT,
        ct_link TEXT DEFAULT NULL,
        ct_tg_chat_id INTEGER NOT NULL,
        ct_tg_topic_id INTEGER DEFAULT 0,
        ct_note TEXT DEFAULT NULL,
        ct_sendable INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        UNIQUE(ct_tg_chat_id, ct_tg_topic_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_name TEXT DEFAULT NULL,
        route_mode TEXT DEFAULT 'bulk',
        source_ct_id INTEGER NOT NULL,
        send_time TEXT,
        intervals_json TEXT DEFAULT '[]',
        interval_index INTEGER DEFAULT 0,
        jitter_seconds INTEGER DEFAULT 0,
        next_run_time TEXT DEFAULT NULL,
        max_rounds INTEGER DEFAULT -1,
        completed_rounds INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY(source_ct_id) REFERENCES chats_topics(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS route_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER NOT NULL,
        ct_id INTEGER NOT NULL,
        note_override TEXT DEFAULT NULL,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY(route_id) REFERENCES routes(id),
        FOREIGN KEY(ct_id) REFERENCES chats_topics(id),
        UNIQUE(route_id, ct_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER,
        message_ids TEXT,
        is_sent INTEGER DEFAULT 0,
        FOREIGN KEY(route_id) REFERENCES routes(id)
    )''')
    conn.commit()
    conn.close()

def _get_conn() -> sqlite3.Connection:
    """Возвращает соединение с включёнными foreign keys и row_factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

# ============================================================
# chats_topics
# ============================================================

def add_chat_topic(tg_chat_id: int, tg_topic_id: int = 0,
                   name: str = None, link: str = None,  # type: ignore
                   note: str = None, sendable: bool = False) -> int:  # type: ignore
    """Добавляет чат/топик. Возвращает id. Если уже существует — возвращает существующий."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        '''INSERT OR IGNORE INTO chats_topics
        (ct_tg_chat_id, ct_tg_topic_id, ct_name, ct_link, ct_note, ct_sendable)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (tg_chat_id, tg_topic_id, name, link, note, int(sendable))
    )
    conn.commit()
    c.execute(
        'SELECT id FROM chats_topics WHERE ct_tg_chat_id = ? AND ct_tg_topic_id = ?',
        (tg_chat_id, tg_topic_id)
    )
    row = c.fetchone()
    conn.close()
    return row['id']

def get_chat_topic_by_id(ct_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM chats_topics WHERE id = ?', (ct_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_chat_topic_by_tg_ids(tg_chat_id: int, tg_topic_id: int = 0) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT * FROM chats_topics WHERE ct_tg_chat_id = ? AND ct_tg_topic_id = ?',
        (tg_chat_id, tg_topic_id)
    )
    row = c.fetchone()
    conn.close()
    return row

def get_all_chat_topics(active_only: bool = True) -> list[sqlite3.Row]:
    conn = _get_conn()
    c = conn.cursor()
    query = 'SELECT * FROM chats_topics'
    if active_only:
        query += ' WHERE is_active = 1'
    c.execute(query)
    rows = c.fetchall()
    conn.close()
    return rows

def get_sendable_chat_topics() -> list[sqlite3.Row]:
    """Все активные чаты/топики, куда можно отправлять."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM chats_topics WHERE is_active = 1 AND ct_sendable = 1')
    rows = c.fetchall()
    conn.close()
    return rows

def update_chat_topic(ct_id: int, **kwargs) -> bool:
    """Обновляет поля чата/топика. Пример: update_chat_topic(1, ct_name='Новое имя')."""
    allowed = {'ct_name', 'ct_link', 'ct_note', 'ct_sendable', 'is_active'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    for key in ('ct_sendable', 'is_active'):
        if key in fields:
            fields[key] = int(fields[key])
    set_clause = ', '.join(f'{k} = ?' for k in fields)
    values = list(fields.values()) + [ct_id]
    conn = _get_conn()
    c = conn.cursor()
    c.execute(f'UPDATE chats_topics SET {set_clause} WHERE id = ?', values)
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    return updated

# ============================================================
# routes
# ============================================================

def add_route(source_ct_id: int, route_name: str = None,  # type: ignore
              route_mode: str = 'bulk', send_time: str = None,  # type: ignore
              intervals_json: str = '[]', jitter_seconds: int = 0,
              max_rounds: int = -1) -> int:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO routes
        (route_name, route_mode, source_ct_id, send_time, intervals_json,
         jitter_seconds, max_rounds)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (route_name, route_mode, source_ct_id, send_time,
         intervals_json, int(jitter_seconds), int(max_rounds))
    )
    conn.commit()
    route_id = c.lastrowid
    conn.close()
    return route_id

def get_route_by_id(route_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM routes WHERE id = ?', (route_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_routes(active_only: bool = True) -> list[sqlite3.Row]:
    conn = _get_conn()
    c = conn.cursor()
    query = 'SELECT * FROM routes'
    if active_only:
        query += ' WHERE is_active = 1'
    c.execute(query)
    rows = c.fetchall()
    conn.close()
    return rows

def get_routes_by_mode(route_mode: str) -> list[sqlite3.Row]:
    """'bulk' или 'singular'"""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM routes WHERE route_mode = ? AND is_active = 1', (route_mode,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_route(route_id: int, **kwargs) -> bool:
    """Обновляет поля маршрута. Пример: update_route(1, route_name='Новое')."""
    allowed = {
        'route_name', 'route_mode', 'source_ct_id', 'send_time',
        'intervals_json', 'interval_index', 'jitter_seconds',
        'next_run_time', 'max_rounds', 'completed_rounds', 'is_active'
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    if 'is_active' in fields:
        fields['is_active'] = int(fields['is_active'])
    set_clause = ', '.join(f'{k} = ?' for k in fields)
    values = list(fields.values()) + [route_id]
    conn = _get_conn()
    c = conn.cursor()
    c.execute(f'UPDATE routes SET {set_clause} WHERE id = ?', values)
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    return updated

def update_route_schedule(route_id: int, interval_index: int, next_run_time_iso: str):
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'UPDATE routes SET interval_index = ?, next_run_time = ? WHERE id = ?',
        (interval_index, next_run_time_iso, route_id)
    )
    conn.commit()
    conn.close()

def increment_completed_rounds(route_id: int) -> int:
    """Увеличивает счётчик завершённых кругов. Возвращает новое значение."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'UPDATE routes SET completed_rounds = completed_rounds + 1 WHERE id = ?',
        (route_id,)
    )
    conn.commit()
    c.execute('SELECT completed_rounds FROM routes WHERE id = ?', (route_id,))
    row = c.fetchone()
    conn.close()
    return row['completed_rounds'] if row else 0

def deactivate_route(route_id: int):
    conn = _get_conn()
    c = conn.cursor()
    c.execute('UPDATE routes SET is_active = 0 WHERE id = ?', (route_id,))
    conn.commit()
    conn.close()

def delete_route(route_id: int) -> bool:
    """Удаляет маршрут вместе со всеми целями и постами."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM route_targets WHERE route_id = ?', (route_id,))
        c.execute('DELETE FROM posts WHERE route_id = ?', (route_id,))
        c.execute('DELETE FROM routes WHERE id = ?', (route_id,))
        deleted = c.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()

def skip_next_publication(route_id: int):
    """
    Пропускает ближайшую публикацию: сдвигает interval_index и пересчитывает
    next_run_time от времени пропущенной публикации.
    Возвращает новый next_run_time (datetime) или None.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from config import TIMEZONE

    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM routes WHERE id = ?', (route_id,))
    route = c.fetchone()
    conn.close()

    if not route:
        return None

    intervals = json.loads(route['intervals_json'] or '[]')
    if not intervals:
        return None

    current_index = route['interval_index'] or 0
    next_index = (current_index + 1) % len(intervals)

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    saved_next_run = route['next_run_time']
    if saved_next_run:
        try:
            base_time = datetime.fromisoformat(saved_next_run)
            if base_time <= now:
                base_time = now
        except ValueError:
            base_time = now
    else:
        send_time = route['send_time']
        h, m = map(int, send_time.split(':'))
        base_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if base_time <= now:
            base_time += timedelta(days=1)

    next_run = base_time + timedelta(seconds=intervals[current_index])

    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'UPDATE routes SET interval_index = ?, next_run_time = ? WHERE id = ?',
        (next_index, next_run.isoformat(), route_id)
    )
    conn.commit()
    conn.close()
    return next_run

# ============================================================
# route_targets
# ============================================================

def add_route_target(route_id: int, ct_id: int, note_override: str = None) -> bool:  # type: ignore
    """Добавляет цель маршруту. Возвращает False, если связь уже есть."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute(
            'INSERT OR IGNORE INTO route_targets (route_id, ct_id, note_override) VALUES (?, ?, ?)',
            (route_id, ct_id, note_override)
        )
        conn.commit()
        added = c.rowcount > 0
        return added
    finally:
        conn.close()

def get_route_targets(route_id: int, active_only: bool = True) -> list[sqlite3.Row]:
    """
    Возвращает цели маршрута с JOIN на chats_topics
    для получения полного описания каждого чата/топика.
    """
    conn = _get_conn()
    c = conn.cursor()
    query = '''
        SELECT rt.id AS rt_id, rt.route_id, rt.note_override, rt.is_active,
               ct.id AS ct_id, ct.ct_name, ct.ct_tg_chat_id, ct.ct_tg_topic_id,
               ct.ct_note, ct.ct_sendable
        FROM route_targets rt
        JOIN chats_topics ct ON rt.ct_id = ct.id
        WHERE rt.route_id = ?
    '''
    if active_only:
        query += ' AND rt.is_active = 1 AND ct.is_active = 1'
    c.execute(query, (route_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def remove_route_target(route_id: int, ct_id: int) -> bool:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'DELETE FROM route_targets WHERE route_id = ? AND ct_id = ?',
        (route_id, ct_id)
    )
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    return deleted

def get_note_for_target(route_id: int, ct_id: int) -> Optional[str]:
    """
    Возвращает подпись для поста в указанную цель.
    Приоритет: note_override из route_targets > ct_note из chats_topics.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        '''SELECT COALESCE(rt.note_override, ct.ct_note) AS note
           FROM route_targets rt
           JOIN chats_topics ct ON rt.ct_id = ct.id
           WHERE rt.route_id = ? AND rt.ct_id = ?''',
        (route_id, ct_id)
    )
    row = c.fetchone()
    conn.close()
    return row['note'] if row else None

# ============================================================
# posts
# ============================================================

def save_post(route_id: int, message_ids_list: list[int]) -> bool:
    """Сохраняет пост (список message_id). Возвращает False если дубликат."""
    if not message_ids_list:
        return False
    message_ids_list = sorted(set(message_ids_list))
    message_ids_json = json.dumps(message_ids_list)
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT id FROM posts WHERE route_id = ? AND message_ids = ?',
        (route_id, message_ids_json)
    )
    if c.fetchone():
        conn.close()
        return False
    c.execute(
        'INSERT INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
        (route_id, message_ids_json)
    )
    conn.commit()
    conn.close()
    return True

def get_random_unsent_post(route_id: int) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT id, message_ids FROM posts WHERE route_id = ? AND is_sent = 0',
        (route_id,)
    )
    posts = c.fetchall()
    conn.close()
    if not posts:
        return None
    post = random.choice(posts)
    try:
        message_ids = json.loads(post['message_ids'])
    except Exception:
        message_ids = []
    return {'id': post['id'], 'message_ids': message_ids}

def mark_post_sent(post_id: int):
    conn = _get_conn()
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 1 WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()

def reset_posts_for_route(route_id: int):
    conn = _get_conn()
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 0 WHERE route_id = ?', (route_id,))
    conn.commit()
    conn.close()

def delete_post(post_id: int):
    conn = _get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()

def get_posts_count(route_id: int) -> dict:
    """Возвращает количество постов: {'total': N, 'unsent': M, 'sent': K}."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT COUNT(*) AS total, SUM(is_sent) AS sent FROM posts WHERE route_id = ?',
        (route_id,)
    )
    row = c.fetchone()
    conn.close()
    total = row['total'] or 0
    sent = row['sent'] or 0
    return {'total': total, 'sent': sent, 'unsent': total - sent}

# ============================================================
# Поиск маршрутов по источнику (для collector_router)
# ============================================================

def get_routes_for_source(tg_chat_id: int, tg_topic_id: int = 0) -> list[sqlite3.Row]:
    """
    Ищет активные маршруты для указанного TG чата и топика.
    
    Логика:
    - Если tg_topic_id != 0: ищем маршруты, привязанные к точному топику
      ИЛИ к «всему чату» (ct_tg_topic_id = 0).
    - Если tg_topic_id == 0 (сообщение без топика): ищем только
      маршруты с ct_tg_topic_id = 0.
    """
    conn = _get_conn()
    c = conn.cursor()

    if tg_topic_id:
        c.execute('''
            SELECT r.* FROM routes r
            JOIN chats_topics ct ON r.source_ct_id = ct.id
            WHERE ct.ct_tg_chat_id = ?
              AND (ct.ct_tg_topic_id = ? OR ct.ct_tg_topic_id = 0)
              AND r.is_active = 1
        ''', (tg_chat_id, tg_topic_id))
    else:
        c.execute('''
            SELECT r.* FROM routes r
            JOIN chats_topics ct ON r.source_ct_id = ct.id
            WHERE ct.ct_tg_chat_id = ?
              AND ct.ct_tg_topic_id = 0
              AND r.is_active = 1
        ''', (tg_chat_id,))

    rows = c.fetchall()
    conn.close()
    return rows