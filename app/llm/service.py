"""Сервіс ШІ: коли можна викликати, облік викликів, тихий фолбек.

Обгортає інтерпретатор трьома речами з ТЗ (розділ 11 — контроль витрат):
  * ліміт викликів ШІ на користувача за вікно (ОКРЕМО від загального
    rate-limit бота);
  * лічильник викликів за сьогодні (для рядка статусу в адмінці);
  * логування кожного виклику (факт і результат) — без ключа.

Будь-яка проблема (ліміт, помилка, таймаут) → повертаємо None, і бот тихо
працює далі словником. ШІ ніколи не «підвішує» бота.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.analytics.query import DonorQuery
from app.llm.interpreter import LLMInterpreter
from app.llm.provider import AnthropicProvider, HttpPost, LLMProvider, OpenAIProvider
from app.logging_setup import get_logger
from app.settings import Settings

logger = get_logger(__name__)


class AICallLimiter:
    """Ковзне вікно викликів ШІ на користувача (як rate-limit, але окремо)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, now: float) -> bool:
        hits = self._hits[user_id]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


class AIDailyCounter:
    """Скільки викликів ШІ було СЬОГОДНІ. Скидається зі зміною дати."""

    def __init__(self) -> None:
        self._day: str | None = None
        self._count = 0

    def bump(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._day:
            self._day = day
            self._count = 0
        self._count += 1

    @property
    def today(self) -> int:
        return self._count


class AIService:
    """ШІ як резерв: пускаємо в межах ліміту, рахуємо, логуємо, не падаємо."""

    def __init__(
        self,
        interpreter: LLMInterpreter,
        *,
        limit: int,
        window_seconds: int,
        model: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._interpreter = interpreter
        self._limiter = AICallLimiter(limit, window_seconds)
        self._counter = AIDailyCounter()
        self.model = model
        self._clock = clock

    @property
    def calls_today(self) -> int:
        return self._counter.today

    async def try_interpret(self, user_id: int, text: str) -> DonorQuery | None:
        """Пробує розібрати запит через ШІ. None — не вийшло (і це нормально)."""
        now = self._clock()
        if not self._limiter.allow(user_id, now):
            logger.info("Ліміт викликів ШІ вичерпано: користувач %s", user_id)
            return None

        self._counter.bump(now)
        try:
            query = await self._interpreter.interpret(text)
        except Exception as exc:
            # exc — це LLMError без ключа всередині, логувати безпечно.
            logger.warning("Виклик ШІ не вдався (користувач %s): %s", user_id, exc)
            return None

        logger.info(
            "Виклик ШІ: користувач %s, результат — %s",
            user_id,
            "розпізнано" if query is not None else "порожньо",
        )
        return query


def _build_provider(settings: Settings, http_post: HttpPost | None) -> LLMProvider:
    """Обирає провайдера ШІ за LLM_PROVIDER (перемикання лише через .env).

    Контракт однаковий, тож решта коду (інтерпретатор, сервіс) не залежить від
    того, який саме API за цим викликом."""
    common = {
        "api_key": settings.llm_api_key,
        "model": settings.llm_model,
        "timeout_seconds": settings.llm_timeout_seconds,
        "http_post": http_post,
    }
    if settings.llm_provider == "openai":
        return OpenAIProvider(**common)
    return AnthropicProvider(**common)


def build_ai_service(settings: Settings, *, http_post: HttpPost | None = None) -> AIService | None:
    """Збирає сервіс ШІ з налаштувань. None — коли ШІ вимкнено (немає ключа).

    http_post дозволяє підмінити мережу в тестах; у бою лишається стандартним.
    """
    if not settings.llm_enabled:
        return None

    provider = _build_provider(settings, http_post)
    return AIService(
        LLMInterpreter(provider),
        limit=settings.llm_calls_limit,
        window_seconds=settings.llm_window_seconds,
        model=settings.llm_model,
    )
