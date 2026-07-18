"""Налаштування логів.

Лог — це щоденник роботи бота: хто що запитував і чи були помилки.

ГОЛОВНЕ ПРАВИЛО БЕЗПЕКИ: у лог ніколи не потрапляють домени донорів.
Логуються тільки службові події та агреговані числа («знайдено 120»).
Це забезпечено ще й тим, що шар аналітики фізично не віддає список донорів
назовні — логувати просто нічого.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Вмикає вивід логів у консоль."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Ці бібліотеки дуже балакучі — приглушуємо, щоб не топити корисні
    # повідомлення в потоці технічних деталей.
    for noisy in ("aiogram.event", "aiohttp.access", "urllib3", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Повертає логер для конкретного модуля."""
    return logging.getLogger(name)
