import os
import sqlite3
import random

# Получаем абсолютный путь к папке, где лежит бот bot.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Формируем полный путь к файлу бд

from config import DB_NAME
DB_PATH = os.path.join(BASE_DIR, DB_NAME)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # таблица маршрутов (из какого топика в какой чат)
    c.execute('''CREATE TABLE IF NOT EXISTS routes
              (id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_topic_id INTEGER,
              target_chat_id INTEGER,
              send_time TEXT)''')
    # таблица постов
    c.execute('''CREATE TABLE IF NOT EXISTS posts
              (id INTEGER PRIMARY KEY AUTOINCREMENT,
              route_id INTEGER,
              message_id INTEGER,
              is_sent INTEGER DEFAULT 0,
              FOREIGN KEY(route_id) REFERENCES routes(id))''')
    
    conn.commit()
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

def save_post_db(route_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO posts (route_id, message_id, is_sent) VALUES (?, ?, 0)',
               (route_id, message_id,))
    conn.commit()
    conn.close()

def get_random_unsent_post(route_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, message_id FROM posts WHERE route_id = ? AND is_sent = 0', (route_id,))
    posts = c.fetchall()
    conn.close()
    if not posts:
        return None
    return random.choice(posts)

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