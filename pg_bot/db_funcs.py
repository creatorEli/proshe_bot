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
        ct_promotable INTEGER DEFAULT 0,
        ct_tags TEXT DEFAULT '[]',
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
        use_random_targets INTEGER DEFAULT 0,
        random_pool_tags TEXT DEFAULT '[]'
        FOREIGN KEY(source_ct_id) REFERENCES chats_topics(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS route_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER NOT NULL,
        ct_id INTEGER NOT NULL,
        note_override TEXT DEFAULT NULL,
        is_active INTEGER DEFAULT 1,
        last_sent_round INTEGER DEFAULT -1,
        FOREIGN KEY(route_id) REFERENCES routes(id),
        FOREIGN KEY(ct_id) REFERENCES chats_topics(id),
        UNIQUE(route_id, ct_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER,
        message_ids TEXT,
        is_sent INTEGER DEFAULT 0,
        buttons_json TEXT DEFAULT NULL,
        FOREIGN KEY(route_id) REFERENCES routes(id)
    )''')

    # === МИГРАЦИЯ: теги чатов ===
    try:
        c.execute("ALTER TABLE chats_topics ADD COLUMN ct_tags TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

    # === МИГРАЦИЯ: фильтр случайного пула у маршрутов ===
    try:
        c.execute("ALTER TABLE routes ADD COLUMN random_pool_tags TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

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

def add_route(source_ct_id: int, route_name: Optional[str] = None,
              route_mode: str = 'bulk', send_time: Optional[str] = None,
              intervals_json: str = '[]', jitter_seconds: int = 0,
              max_rounds: int = -1, use_random_targets: bool = False) -> int | None:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO routes
        (route_name, route_mode, source_ct_id, send_time, intervals_json,
         jitter_seconds, max_rounds, use_random_targets)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (route_name, route_mode, source_ct_id, send_time,
         intervals_json, int(jitter_seconds), int(max_rounds),
         int(use_random_targets))
    )
    conn.commit()
    route_id = c.lastrowid
    conn.close()

    # Явно обрабатываем случай, если lastrowid вдруг вернет None
    if route_id is None:
        raise ValueError("Не удалось получить ID новой записи (lastrowid is None)")
       
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
        'next_run_time', 'max_rounds', 'completed_rounds', 'is_active',
        'use_random_targets','random_pool_tags',
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    for key in ('is_active', 'use_random_targets'):   # <-- добавлено
        if key in fields:
            fields[key] = int(fields[key])
    
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

def add_route_target(route_id: int, ct_id: int, note_override: str | None = None) -> bool:
    """Добавляет цель маршруту. Возвращает False, если связь уже есть."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute(
            'INSERT OR IGNORE INTO route_targets (route_id, ct_id, note_override, last_sent_round) VALUES (?, ?, ?, -1)',
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

def save_post(route_id: int, message_ids_list: list[int], buttons_json: Optional[str] = None) -> bool:
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
        'INSERT INTO posts (route_id, message_ids, is_sent, buttons_json) VALUES (?, ?, 0, ?)',
        (route_id, message_ids_json, buttons_json)
    )
    conn.commit()
    conn.close()
    return True

def get_random_unsent_post(route_id: int) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT id, message_ids, buttons_json FROM posts WHERE route_id = ? AND is_sent = 0',
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
    return {
        'id': post['id'], 
        'message_ids': message_ids,
        'buttons_json': post['buttons_json']
    }

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
# Поиск по имени и удаление
# ============================================================

def get_chat_topic_by_name(name: str) -> Optional[sqlite3.Row]:
    """
    Ищет чат/топик по имени (ct_name).
    Возвращает запись или None, если не найдено.
    Если имён несколько — возвращает первое (активное приоритетнее).
    """
    conn = _get_conn()
    c = conn.cursor()
    # Сначала ищем среди активных
    c.execute(
        'SELECT * FROM chats_topics WHERE ct_name = ? AND is_active = 1 LIMIT 1',
        (name,)
    )
    row = c.fetchone()
    if row:
        conn.close()
        return row
    # Если нет активных — ищем среди всех
    c.execute(
        'SELECT * FROM chats_topics WHERE ct_name = ? LIMIT 1',
        (name,)
    )
    row = c.fetchone()
    conn.close()
    return row


def check_chat_topic_in_use(ct_id: int) -> dict:
    """
    Проверяет, используется ли чат/топик в маршрутах.
    Возвращает {'as_source': [...ids], 'as_target': [...ids]}
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT id FROM routes WHERE source_ct_id = ?', (ct_id,))
    as_source = [r['id'] for r in c.fetchall()]
    c.execute(
        'SELECT route_id FROM route_targets WHERE ct_id = ?',
        (ct_id,)
    )
    as_target = [r['route_id'] for r in c.fetchall()]
    conn.close()
    return {'as_source': as_source, 'as_target': as_target}


def delete_chat_topic(ct_id: int) -> tuple[bool, str]:
    """
    Удаляет чат/топик. Возвращает (успех, сообщение).
    Отказывается удалять, если чат используется в маршрутах.
    """
    usage = check_chat_topic_in_use(ct_id)
    if usage['as_source'] or usage['as_target']:
        all_routes = sorted(set(usage['as_source'] + usage['as_target']))
        return False, (
            f"Чат используется в маршрутах: {all_routes}. "
            f"Сначала удалите эти маршруты."
        )

    conn = _get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM chats_topics WHERE id = ?', (ct_id,))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()
    if deleted:
        return True, "Чат удалён."
    return False, "Чат не найден."


def get_next_singular_target(route_id: int, current_round: int) -> Optional[sqlite3.Row]:
    """
    Возвращает один случайный целевой чат, который ещё не получал пост в текущем круге.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        SELECT rt.ct_id, ct.ct_tg_chat_id, ct.ct_tg_topic_id, ct.ct_name
        FROM route_targets rt
        JOIN chats_topics ct ON rt.ct_id = ct.id
        WHERE rt.route_id = ? AND rt.is_active = 1 AND ct.is_active = 1
          AND (rt.last_sent_round < ? OR rt.last_sent_round IS NULL)
        ORDER BY RANDOM() LIMIT 1
    ''', (route_id, current_round))
    row = c.fetchone()
    conn.close()
    return row

def mark_singular_target_sent(route_id: int, ct_id: int, current_round: int):
    """
    Помечает, что целевой чат получил пост в текущем круге.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        UPDATE route_targets 
        SET last_sent_round = ? 
        WHERE route_id = ? AND ct_id = ?
    ''', (current_round, route_id, ct_id))
    conn.commit()
    conn.close()

def check_singular_round_complete(route_id: int, current_round: int) -> bool:
    """
    Возвращает True, если все активные цели уже получили пост в текущем круге.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        SELECT 1 FROM route_targets rt
        JOIN chats_topics ct ON rt.ct_id = ct.id
        WHERE rt.route_id = ? AND rt.is_active = 1 AND ct.is_active = 1
          AND (rt.last_sent_round < ? OR rt.last_sent_round IS NULL)
        LIMIT 1
    ''', (route_id, current_round))
    row = c.fetchone()
    conn.close()
    return row is None  # True, если строк не найдено (все получили)


# ============================================================
# Поиск маршрутов по источнику (для collector_router)
# ============================================================

def get_random_sendable_targets(exclude_ids: list[int], pool_tags: Optional[list[str]] = None) -> list[sqlite3.Row]:
    """
    Активные sendable-чаты для случайной рассылки, КРОМЕ exclude_ids.
    pool_tags — фильтр маршрута: 'tag' = обязан иметь (AND), '-tag' = обязан не иметь.
    """
    conn = _get_conn()
    c = conn.cursor()
    if exclude_ids:
        placeholders = ','.join('?' * len(exclude_ids))
        c.execute(
            f'''SELECT * FROM chats_topics
                WHERE is_active = 1 AND ct_sendable = 1
                  AND id NOT IN ({placeholders})''',
            exclude_ids
        )
    else:
        c.execute(
            '''SELECT * FROM chats_topics
               WHERE is_active = 1 AND ct_sendable = 1'''
        )
    
    rows = c.fetchall()
    conn.close()

    if not pool_tags:
        return rows

    include = {normalize_tag(t) for t in pool_tags if not t.startswith('-') and normalize_tag(t)}
    exclude = {normalize_tag(t[1:]) for t in pool_tags if t.startswith('-') and normalize_tag(t[1:])}
    if not include and not exclude:
        return rows

    result = []
    for row in rows:
        try:
            row_tags = set(json.loads(row['ct_tags'] or '[]'))
        except Exception:
            row_tags = set()
        if include and not include.issubset(row_tags):
            continue
        if exclude and (exclude & row_tags):
            continue
        result.append(row)
    return result


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


# работа с кнопками под постом

def update_post_buttons(post_id: int, new_buttons_json: str) -> bool:
    """Обновляет кнопки у существующего поста."""
    conn = _get_conn()
    c = conn.cursor()
    # 1. Читаем текущие кнопки
    c.execute('SELECT buttons_json FROM posts WHERE id = ?', (post_id,))
    row = c.fetchone()
    
    current_buttons = []
    if row and row['buttons_json']:
        try:
            current_buttons = json.loads(row['buttons_json'])
        except Exception:
            current_buttons = []
            
    # 2. Парсим новые и объединяем списки
    try:
        new_buttons = json.loads(new_buttons_json)
        current_buttons.extend(new_buttons)
    except Exception:
        conn.close()
        return False
        
    # 3. Сохраняем обновленный список
    final_json = json.dumps(current_buttons)
    c.execute('UPDATE posts SET buttons_json = ? WHERE id = ?', (final_json, post_id))
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    return updated

def get_last_post_id(route_id: int) -> Optional[int]:
    """Возвращает ID последнего добавленного поста в маршруте."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT id FROM posts WHERE route_id = ? ORDER BY id DESC LIMIT 1',
        (route_id,)
    )
    row = c.fetchone()
    conn.close()
    return row['id'] if row else None


def get_posts_for_route(route_id: int) -> list[sqlite3.Row]:
    """Возвращает все посты маршрута с их кнопками для просмотра."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        'SELECT id, message_ids, is_sent, buttons_json FROM posts WHERE route_id = ? ORDER BY id DESC',
        (route_id,)
    )
    rows = c.fetchall()
    conn.close()
    return rows

def clear_post_buttons(post_id: int) -> bool:
    """Полностью удаляет все кнопки у поста."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('UPDATE posts SET buttons_json = NULL WHERE id = ?', (post_id,))
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    return updated

def remove_post_button(post_id: int, index: int) -> tuple[bool, str]:
    """Удаляет одну кнопку по её номеру (начиная с 1)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT buttons_json FROM posts WHERE id = ?', (post_id,))
    row = c.fetchone()
    if not row or not row['buttons_json']:
        conn.close()
        return False, "У поста нет кнопок или пост не найден."
    
    try:
        buttons = json.loads(row['buttons_json'])
    except Exception:
        conn.close()
        return False, "Ошибка чтения кнопок."
        
    if index < 1 or index > len(buttons):
        conn.close()
        return False, f"Неверный номер. Доступно от 1 до {len(buttons)}."
        
    removed = buttons.pop(index - 1)
    
    new_json = json.dumps(buttons) if buttons else None
    c.execute('UPDATE posts SET buttons_json = ? WHERE id = ?', (new_json, post_id))
    conn.commit()
    conn.close()
    return True, f"Кнопка «{removed.get('text', '?')}» удалена."


# ============================================================
# Теги чатов
# ============================================================
def normalize_tag(tag: str) -> str:
    """Нормализует тег: lower-case, trim, пробелы -> _."""
    return tag.strip().lower().replace(' ', '_')


def update_chat_tags(ct_id: int, tags: list[str], mode: str = 'set') -> bool:
    """
    Управляет тегами чата. mode: 'set' (заменить), 'add' (добавить), 'remove' (убрать).
    Теги нормализуются и дедуплицируются.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT ct_tags FROM chats_topics WHERE id = ?', (ct_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False

    try:
        current = json.loads(row['ct_tags'] or '[]')
    except Exception:
        current = []

    new_tags = [normalize_tag(t) for t in tags if normalize_tag(t)]

    if mode == 'set':
        result = list(dict.fromkeys(new_tags))          # дедуп с сохранением порядка
    elif mode == 'add':
        result = current + [t for t in new_tags if t not in current]
    elif mode == 'remove':
        drop = set(new_tags)
        result = [t for t in current if t not in drop]
    else:
        conn.close()
        return False

    c.execute('UPDATE chats_topics SET ct_tags = ? WHERE id = ?', (json.dumps(result), ct_id))
    conn.commit()
    conn.close()
    return True


def get_chats_by_tags(tags: list[str], active_only: bool = True, sendable_only: bool = True) -> list[sqlite3.Row]:
    """Возвращает чаты, у которых есть ВСЕ перечисленные теги (семантика AND)."""
    conn = _get_conn()
    c = conn.cursor()
    query = 'SELECT * FROM chats_topics'
    if active_only:
        query += ' WHERE is_active = 1'
    if sendable_only:
        query += ' AND ct_sendable = 1'
    c.execute(query)
    rows = c.fetchall()
    conn.close()

    needed = {normalize_tag(t) for t in tags if normalize_tag(t)}
    if not needed:
        return []

    result = []
    for row in rows:
        try:
            row_tags = set(json.loads(row['ct_tags'] or '[]'))
        except Exception:
            row_tags = set()
        if needed.issubset(row_tags):
            result.append(row)
    return result


def get_all_tags() -> dict[str, list[int]]:
    """Возвращает словарь {тег: [id чатов]} для команды /tags."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('SELECT id, ct_tags FROM chats_topics')
    rows = c.fetchall()
    conn.close()

    tags_map: dict[str, list[int]] = {}
    for row in rows:
        try:
            row_tags = json.loads(row['ct_tags'] or '[]')
        except Exception:
            continue
        for t in row_tags:
            tags_map.setdefault(t, []).append(row['id'])
    return tags_map