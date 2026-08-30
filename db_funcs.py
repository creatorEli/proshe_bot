import os
import sqlite3
import random
import json
from config import DB_NAME

# Получаем абсолютный путь к папке, где лежит бот bot.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Формируем полный путь к файлу бд
DB_PATH = os.path.join(BASE_DIR, DB_NAME)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS routes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_topic_id INTEGER,
                  target_chat_id INTEGER,
                  send_time TEXT)''')
    
    # Новая схема: message_ids вместо message_id
    c.execute('''CREATE TABLE IF NOT EXISTS posts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  route_id INTEGER,
                  message_ids TEXT,
                  is_sent INTEGER DEFAULT 0,
                  FOREIGN KEY(route_id) REFERENCES routes(id))''')
    
    conn.commit()

    # Миграция старой схемы если нужно
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
            
            for post_id, route_id, message_id in old_posts:
                c.execute('INSERT INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
                         (route_id, json.dumps([message_id])))
            conn.commit()

    except sqlite3.OperationalError:
        pass

    conn.close()


def add_route_db(source_topic, target_chat, send_time):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO routes (source_topic_id, target_chat_id, send_time) VALUES (?, ?, ?)',
              (source_topic, target_chat, send_time))
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

def get_route_by_topic(topic_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM routes WHERE source_topic_id = ?', (topic_id,))
    route = c.fetchone()
    conn.close()
    return route




def save_post_db(route_id, message_ids_list):
    """Сохраняет пост с одним или несколькими message_id"""

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Сериализуем список в JSON строку
    message_ids_json = json.dumps(message_ids_list)

    # Проверяем, не сохранена ли уже эта группа сообщений
    c.execute('SELECT id FROM posts WHERE route_id = ? AND message_ids = ?',
             (route_id, message_ids_json,))
    existing = c.fetchone()

    if existing:
        conn.close()
        return False

    c.execute('INSERT OR IGNORE INTO posts (route_id, message_ids, is_sent) VALUES (?, ?, 0)',
               (route_id, message_ids_json,))
    conn.commit()
    conn.close()

    return True


def get_random_unsent_post(route_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('SELECT id, message_ids FROM posts WHERE route_id = ? AND is_sent = 0', (route_id,))
    posts = c.fetchall()
    conn.close()

    if not posts:
        return None
    
    post = random.choice(posts)
    # Парсим JSON обратно в список
    message_ids = json.loads(post['message_ids'])
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

def get_route_by_id(route_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM routes WHERE id = ?", (route_id,))
    route = c.fetchone()
    conn.close()
    return route

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