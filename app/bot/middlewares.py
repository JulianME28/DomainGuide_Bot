"""Захисні шари: доступ, обмеження частоти, журнал, обробка помилок.

Middleware — це «прошарок», через який проходить кожне повідомлення, перш
ніж потрапити до обробника. Зручно тим, що перевірку доступу достатньо
написати один раз, і вона автоматично діє на всі команди й кнопки, зокрема
й на ті, які додадуть у майбутньому.

Порядок важливий:

    1. Доступ      — стороннього далі не пускаємо взагалі
    2. Ліміт       — свої теж не мають завалювати бота запитами
    3. Помилки     — що б не сталося, бот відповідає, а не мовчить
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.bot.context import BotServices
from app.logging_setup import get_logger

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class AccessMiddleware(BaseMiddleware):
    """Пускає лише тих, чий Telegram ID є у списку ALLOWED_USER_IDS.

    Стороннім бот не відповідає взагалі (вимога ТЗ, розділ 6): мовчання
    краще за відповідь «вам не можна», бо не підтверджує, що бот існує.
    Спроба доступу пишеться в лог.
    """

    def __init__(self, services: BotServices) -> None:
        self._services = services
        self._warned: set[int] = set()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")

        if user is None:
            return None

        if not self._services.settings.is_allowed(user.id):
            # Логуємо кожного чужого лише раз, щоб не засмічувати журнал.
            if user.id not in self._warned:
                self._warned.add(user.id)
                logger.warning("Відхилено доступ: Telegram ID %s", user.id)
            return None

        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Обмежує кількість запитів від одного користувача.

    Захищає і бота, і квоту Google API. Налаштовується в .env:
    RATE_LIMIT_REQUESTS запитів за RATE_LIMIT_WINDOW_SECONDS секунд.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        hits = self._hits[user.id]

        # Викидаємо звернення, які вже вийшли за часове вікно.
        while hits and now - hits[0] > self._window:
            hits.popleft()

        if len(hits) >= self._limit:
            logger.info("Ліміт запитів вичерпано: користувач %s", user.id)
            await _notify_rate_limited(event, self._window)
            return None

        hits.append(now)
        return await handler(event, data)


class ErrorMiddleware(BaseMiddleware):
    """Ловить будь-яку несподівану помилку.

    Головна вимога ТЗ до надійності: бот не «падає». Навіть якщо в коді
    трапиться помилка, користувач отримає зрозуміле повідомлення, а
    подробиці підуть у лог — але не в чат.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            user: User | None = data.get("event_from_user")
            logger.exception(
                "Помилка під час обробки запиту (користувач %s)", getattr(user, "id", "?")
            )
            await _notify_error(event)
            return None


class ActionLogMiddleware(BaseMiddleware):
    """Записує звернення в журнал для адмін-меню.

    Пишемо лише команду або назву натиснутої кнопки. Тексту вільних запитів
    у журналі немає навмисно: він може містити що завгодно, а журнал бачить
    адмін.
    """

    def __init__(self, services: BotServices) -> None:
        self._services = services

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")

        if user is not None:
            if isinstance(event, CallbackQuery) and event.data:
                self._services.action_log.add(user.id, f"кнопка: {event.data}")
            elif isinstance(event, Message) and event.text:
                label = event.text if event.text.startswith("/") else "вільний запит"
                self._services.action_log.add(user.id, label)

        return await handler(event, data)


# ---------------------------------------------------------------------------
# Допоміжні відповіді
# ---------------------------------------------------------------------------


async def _notify_rate_limited(event: TelegramObject, window: int) -> None:
    text = f"⏳ Забагато запитів поспіль. Зачекайте {window} секунд і спробуйте ще раз."
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:
        logger.debug("Не вдалося попередити про ліміт запитів", exc_info=True)


async def _notify_error(event: TelegramObject) -> None:
    text = (
        "⚠️ Щось пішло не так під час обробки запиту.\nСпробуйте ще раз або почніть спочатку: /start"
    )
    try:
        if isinstance(event, CallbackQuery):
            await event.answer("Сталася помилка", show_alert=False)
            if event.message:
                await event.message.answer(text)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception:
        logger.debug("Не вдалося повідомити користувача про помилку", exc_info=True)


def setup_middlewares(dispatcher, services: BotServices) -> None:
    """Вмикає всі захисні шари в правильному порядку."""
    settings = services.settings

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(ErrorMiddleware())
        observer.middleware(AccessMiddleware(services))
        observer.middleware(
            RateLimitMiddleware(settings.rate_limit_requests, settings.rate_limit_window_seconds)
        )
        observer.middleware(ActionLogMiddleware(services))
