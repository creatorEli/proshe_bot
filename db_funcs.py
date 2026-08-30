import os
import sqlite3
import random
import json
from config import DB_NAME, MAIN_SOURCE_CHAT_ID

# Получаем абсолютный путь к папке, где лежит бот bot.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Формируем полный путь к файлу бд
DB_PATH = os.path.join(BASE_DIR, DB_NAME)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER DEFAULT 0,
                    source_topic_id INTEGER DEFAULT 0,
                    target_chat_id INTEGER,
                    target_topic_id INTEGER DEFAULT 0,
                    send_time TEXT
                )''')


    # Аккуратная миграция для старой таблицы routes
    c.execute('PRAGMA table_info(routes)')
    route_columns = {row[1] for row in c.fetchall()}

    if 'source_chat_id' not in route_columns:
        c.execute('ALTER TABLE routes ADD COLUMN source_chat_id INTEGER DEFAULT 0')

    if 'target_topic_id' not in route_columns:
        c.execute('ALTER TABLE routes ADD COLUMN target_topic_id INTEGER DEFAULT 0')

    
    # Таблица постов: один пост может содержать несколько message_id,
    # например галерею. Храним список как JSON.
    c.execute('''CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER,
                    message_ids TEXT,
                    is_sent INTEGER DEFAULT 0,
                    FOREIGN KEY(route_id) REFERENCES routes(id)
                )''')
    
    conn.commit()

    # Миграция старой таблицы posts, если там был message_id вместо message_ids
    try:
        c.execute('SELECT message_id FROM posts LIMIT 1')
        c.execute('SELECT id, route_id, message_id FROM posts')
        old_posts = c.fetchall()

        if old_posts:
            c.execute('DROP TABLE posts')

            c.execute('''CREATE TABLE posts
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          route_id INTEGER,
                          message_ids TEXT,
                          is_sent INTEGER DEFAULT 0,
                          FOREIGN KEY(route_id) REFERENCES routes(id))''')
            
            for _, route_id, message_id in old_posts:
                c.execute('INSERT INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
                         (route_id, json.dumps([message_id])))
            conn.commit()

    except sqlite3.OperationalError:
        # Если таблицы постов нет или в ней уже нет колонки message_id,
        # значит миграция не нужна
        pass

    conn.close()


def add_route_db(source_chat_id, source_topic_id, target_chat_id, target_topic_id, send_time):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        '''INSERT INTO routes
           (source_chat_id, source_topic_id, target_chat_id, target_topic_id, send_time)
           VALUES (?, ?, ?, ?, ?)''',
        (
            source_chat_id or 0,
            source_topic_id or 0,
            target_chat_id,
            target_topic_id or 0,
            send_time
        )
    )

    conn.commit()
    route_id = c.lastrowid
    conn.close()

    return route_id

def get_all_routes():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # get columns by Name
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

def get_route_by_topic(topic_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM routes WHERE source_topic_id = ?', (topic_id,))
    route = c.fetchone()
    conn.close()
    return route

def get_routes_for_source(chat_id, topic_id):
    """
    Возвращает маршруты, которые подходят для входящего сообщения.

    Логика:
    - если source_chat_id  = 0, используем MAIN_SOURCE_CHAT_ID;
    - если source_topic_id = 0, маршрут подходит для всего чата;
    - если source_topic_id > 0, маршрут подходит только для этого топика.
    """
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


def save_post_db(route_id, message_ids_list):
    """
    Сохраняет пост.
    Один пост может содержать несколько message_id, например галерею.
    """
    if not message_ids_list:
        return False

    # Сортируем и убираем дубли, чтобы сразу сохранить корректный порядок
    message_ids_list = sorted(set(message_ids_list))
    message_ids_json = json.dumps(message_ids_list)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Проверяем, не сохранена ли уже эта группа сообщений
    c.execute(
        'SELECT id FROM posts WHERE route_id = ? AND message_ids = ?',
        (route_id, message_ids_json,)
    )
    
    existing = c.fetchone()
    if existing:
        conn.close()
        return False

    c.execute(
        'INSERT OR IGNORE INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
        (route_id, message_ids_json,)
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
    # Парсим JSON обратно в список
    try:
        message_ids = json.loads(post['message_ids'])
    except Exception:
        message_ids = []
        
    return {
        'id': post['id'],
        'message_ids': message_ids
    }

def mark_post_sent(post_db_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 1 WHERE id = ?', (post_db_id,))
    conn.commit()
    conn.close()

def reset_posts_for_route(route_id):
    # сбрасывает статус is_sent когда все посты отправены
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE posts SET is_sent = 0 WHERE route_id = ?', (route_id,))
    conn.commit()
    conn.close()

def delete_route_db(route_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()

        # Сначала удаляем посты, связанные с маршрутом
        c.execute('DELETE FROM posts WHERE route_id = ?', (route_id,))

        # Затем удаляем сам маршрут
        c.execute('DELETE FROM routes WHERE id = ?', (route_id,))

        deleted = c.rowcount
        conn.commit()

        return deleted > 0
    finally:
        conn.close()