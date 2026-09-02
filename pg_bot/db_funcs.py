import os
import sqlite3
import random
import json
from config import DB_NAME, MAIN_SOURCE_CHAT_ID

# Получаем абсолютный путь к папке, где лежит бот bot.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # тут именно __file__ с нижними подчёркиваниями по краям
# Формируем полный путь к файлу бд
# DB_PATH = os.path.join(BASE_DIR, DB_NAME)
DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "data", DB_NAME))


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_name TEXT DEFAULT NULL,
        source_chat_id INTEGER DEFAULT 0,
        source_topic_id INTEGER DEFAULT 0,
        target_chat_id INTEGER,
        target_topic_id INTEGER DEFAULT 0,
        send_time TEXT,
        intervals_json TEXT DEFAULT '[]',
        interval_index INTEGER DEFAULT 0,
        jitter_seconds INTEGER DEFAULT 0,
        next_run_time TEXT DEFAULT NULL
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


def add_route_db(source_chat_id, source_topic_id, target_chat_id, target_topic_id,
                 send_time=None, intervals_json='[]', route_name=None, jitter_seconds=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        '''INSERT INTO routes
           (source_chat_id, source_topic_id, target_chat_id, target_topic_id,
            send_time, intervals_json, route_name, jitter_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            source_chat_id or 0,
            source_topic_id or 0,
            target_chat_id,
            target_topic_id or 0,
            send_time,
            intervals_json,
            route_name,
            int(jitter_seconds or 0)
        )
    )
    conn.commit()
    route_id = c.lastrowid
    conn.close()
    return route_id


def get_all_routes():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM routes')
    routes = c.fetchall()
    conn.close()
    return routes


def get_route_by_id(route_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM routes WHERE id = ?", (route_id,))
    route = c.fetchone()
    conn.close()
    return route


def get_routes_for_source(chat_id, topic_id):
    topic_id = topic_id or 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        '''SELECT * FROM routes
           WHERE
              (source_chat_id = ? OR (source_chat_id = 0 AND ? = ?))
              AND
              (source_topic_id = 0 OR source_topic_id = ?)
        ''',
        (chat_id, chat_id, MAIN_SOURCE_CHAT_ID, topic_id)
    )
    routes = c.fetchall()
    conn.close()
    return routes


def update_route_schedule(route_id, interval_index, next_run_time_iso):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'UPDATE routes SET interval_index = ?, next_run_time = ? WHERE id = ?',
        (interval_index, next_run_time_iso, route_id)
    )
    conn.commit()
    conn.close()


def skip_next_publication(route_id):
    """
    Пропускает ближайшую публикацию: сдвигает interval_index и пересчитывает next_run_time.
    Отсчёт ведётся от времени пропущенной публикации, а не от текущего момента.
    Возвращает новый next_run_time или None.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from config import TIMEZONE

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    # Определяем базу для отсчёта
    saved_next_run = route['next_run_time']
    if saved_next_run:
        try:
            base_time = datetime.fromisoformat(saved_next_run)
            # Если сохранённое время уже в прошлом (бот был выключен, публикация просрочена),
            # используем текущее время как базу
            if base_time <= now:
                base_time = now
        except ValueError:
            base_time = now
    else:
        # next_run_time не сохранён (маршрут только создан, ни разу не срабатывал)
        # Вычисляем начальное время от send_time
        send_time = route['send_time']
        h, m = map(int, send_time.split(':'))
        base_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if base_time <= now:
            base_time += timedelta(days=1)

    # Следующая публикация = база + текущий интервал
    next_run = base_time + timedelta(seconds=intervals[current_index])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'UPDATE routes SET interval_index = ?, next_run_time = ? WHERE id = ?',
        (next_index, next_run.isoformat(), route_id)
    )
    conn.commit()
    conn.close()

    return next_run


def delete_route_db(route_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute('DELETE FROM posts WHERE route_id = ?', (route_id,))
        c.execute('DELETE FROM routes WHERE id = ?', (route_id,))
        deleted = c.rowcount
        conn.commit()
        return deleted > 0
    finally:
        conn.close()


def save_post_db(route_id, message_ids_list):
    if not message_ids_list:
        return False
    message_ids_list = sorted(set(message_ids_list))
    message_ids_json = json.dumps(message_ids_list)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT id FROM posts WHERE route_id = ? AND message_ids = ?',
        (route_id, message_ids_json)
    )
    if c.fetchone():
        conn.close()
        return False
    c.execute(
        'INSERT OR IGNORE INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
        (route_id, message_ids_json)
    )
    conn.commit()
    conn.close()
    return True


def get_random_unsent_post(route_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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


def mark_post_sent(post_db_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 1 WHERE id = ?', (post_db_id,))
    conn.commit()
    conn.close()


def reset_posts_for_route(route_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 0 WHERE route_id = ?', (route_id,))
    conn.commit()
    conn.close()


def delete_post_db(post_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()