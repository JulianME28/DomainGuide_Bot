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
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.bot.access import AttemptLimiter, verify_code
from app.bot.context import BotServices
from app.bot.keyboards import main_menu
from app.logging_setup import get_logger

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# Тексти шлюзу доступу за кодом. Сам КОД тут не фігурує ніколи.
ACCESS_PROMPT_TEXT = (
    "🔒 <b>Бот приватний.</b>\n\n"
    "Щоб отримати доступ, введіть <b>код доступу</b> одним повідомленням."
)
ACCESS_GRANTED_TEXT = "✅ <b>Доступ відкрито!</b>\n\nВітаю — тепер бот доступний. Ось меню:"
ACCESS_WRONG_TEXT = "❌ Невірний код. Перевірте й спробуйте ще раз."
ACCESS_TOO_MANY_TEXT = (
    "⏳ <b>Забагато спроб.</b>\n\nСпробуйте пізніше — вхід тимчасово заблоковано."
)


class AccessMiddleware(BaseMiddleware):
    """Єдиний шлюз доступу.

    Пускає тих, чий Telegram ID є у СТАТИЧНОМУ списку .env (ALLOWED_USER_IDS +
    адміни) АБО у ДИНАМІЧНОМУ сховищі (хто зайшов за кодом).

    Для НЕавторизованих:
      * якщо код доступу вимкнено (ACCESS_CODE порожній) — мовчимо, як раніше
        (мовчання краще за «вам не можна»: не підтверджує існування бота);
      * якщо код увімкнено — просимо ввести код; правильний → грант назавжди,
        невірний → відмова (з лімітом спроб проти брутфорсу). Сам код у лог і в
        тексти відмов не потрапляє НІКОЛИ.
    """

    def __init__(self, services: BotServices) -> None:
        self._services = services
        self._warned: set[int] = set()
        settings = services.settings
        self._attempts = AttemptLimiter(
            settings.access_code_attempts, settings.access_code_window_seconds
        )

    def _is_allowed(self, user_id: int) -> bool:
        """Статичний список (.env) АБО динамічне сховище (за кодом)."""
        if self._services.settings.is_allowed(user_id):
            return True
        store = self._services.access_store
        return store is not None and store.contains(user_id)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")

        if user is None:
            return None

        if self._is_allowed(user.id):
            return await handler(event, data)

        # Не авторизований. Код вимкнено → давня поведінка: мовчимо, логуємо раз.
        if not self._services.settings.access_code_enabled:
            if user.id not in self._warned:
                self._warned.add(user.id)
                logger.warning("Відхилено доступ: Telegram ID %s", user.id)
            return None

        # Код увімкнено → шлюз уведення коду. Далі обробник НЕ викликаємо.
        await self._handle_code_entry(event, user)
        return None

    def _classify(self, user_id: int, text: str) -> str:
        """Чисте рішення шлюзу: "blocked" | "grant" | "prompt" | "wrong".

        Робить облік спроб (fail/reset), але у СХОВИЩЕ не пише — це асинхронний
        крок, його виконує _process_text. Винесено окремо, щоб логіку можна було
        протестувати без телеграм-обʼєктів. Сам код нікуди не повертаємо."""
        if self._attempts.blocked(user_id):
            return "blocked"
        if verify_code(text, self._services.settings.access_code):
            self._attempts.reset(user_id)
            return "grant"
        # Порожнє / команда / привітання → підказка; спробу НЕ рахуємо (щоб
        # людина не «згоріла» на вітанні).
        if not text or text.startswith("/"):
            return "prompt"
        # Схоже на код, але невірне → рахуємо спробу. Сам код у лог НЕ пишемо.
        self._attempts.register_failure(user_id)
        return "wrong"

    async def _process_text(self, user_id: int, text: str) -> str:
        """Рішення шлюзу + запис у сховище на грант. Повертає мітку рішення."""
        outcome = self._classify(user_id, text.strip())
        if outcome == "grant" and self._services.access_store is not None:
            await self._services.access_store.grant(user_id)
        return outcome

    async def _handle_code_entry(self, event: TelegramObject, user: User) -> None:
        """Обробляє спробу входу за кодом для неавторизованого користувача."""
        # Кнопок у чужого немає — на callback лише тихо квитуємо, без відповіді.
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer()
            return
        if not isinstance(event, Message):
            return

        outcome = await self._process_text(user.id, event.text or "")

        if outcome == "blocked":
            logger.warning("Забагато спроб коду доступу: Telegram ID %s", user.id)
            await _safe_answer(event, ACCESS_TOO_MANY_TEXT)
        elif outcome == "grant":
            await _safe_answer(event, ACCESS_GRANTED_TEXT, main_menu(is_admin=False))
        elif outcome == "prompt":
            await _safe_answer(event, ACCESS_PROMPT_TEXT)
        else:  # "wrong"
            logger.warning("Невірний код доступу: Telegram ID %s", user.id)
            await _safe_answer(event, ACCESS_WRONG_TEXT)


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


async def _safe_answer(event: Message, text: str, markup: Any = None) -> None:
    """Відповідає на повідомлення шлюзу доступу, не падаючи на дрібницях."""
    try:
        await event.answer(text, reply_markup=markup)
    except Exception:
        logger.debug("Не вдалося відповісти на шлюзі доступу", exc_info=True)


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
