import os
from pathlib import Path
from dotenv import load_dotenv

# Путь к папке проекта
BASE_DIR = Path(__file__).resolve().parent

# Подгружаем .env из папки проекта
load_dotenv(BASE_DIR / ".env")


def get_env(name: str, default: str = None) -> str:
    value = os.getenv(name, default)

    if value is None:
        raise RuntimeError(
            f"Переменная окружения {name} не задана. "
            f"Проверь файл .env или переменные окружения."
        )

    return value


def get_int_env(name: str, default: int = None) -> int:
    value = os.getenv(name)

    if value is None:
        if default is not None:
            return default

        raise RuntimeError(
            f"Переменная окружения {name} не задана. "
            f"Проверь файл .env или переменные окружения."
        )

    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"Переменная окружения {name} должна быть числом, "
            f"а получено: {value}"
        )


ADMIN_ID = get_int_env("ADMIN_ID")
API_TOKEN = get_env("API_TOKEN")
MAIN_SOURCE_CHAT_ID = get_int_env("MAIN_SOURCE_CHAT_ID")

TIMEZONE = get_env("TIMEZONE", default="Europe/Moscow")
DB_NAME = get_env("DB_NAME", default="bot.db")