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
from dataclasses import dataclass

from app.analytics.query import DonorQuery
from app.llm.chat import ConversationResponder
from app.llm.interpreter import LLMInterpreter
from app.llm.provider import AnthropicProvider, HttpPost, LLMError, LLMProvider, OpenAIProvider
from app.logging_setup import get_logger
from app.settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AIOutcome:
    """Результат спроби ШІ + ЧОМУ (щоб показати користувачу точне повідомлення).

    reason:
      * "ok"          — розпізнано, query заповнено;
      * "limit"       — вичерпано ліміт викликів ШІ;
      * "unavailable" — ШІ недоступний: мережа, порожня відповідь, дивний формат;
      * "unparsable"  — ШІ відповів, але не валідним JSON / відповідь обрізана;
      * "empty"       — валідний JSON, але жодного дозволеного фільтра не впізнано.

    intent — намір, який повернула модель у ТОМУ САМОМУ виклику ("filter" за
    замовчуванням, "question" — пояснювальне питання). Керує маршрутизацією ЛИШЕ
    коли query is None: питання → розмовна смуга, інакше → словниковий фолбек.
    Валідний фільтр (query != None) завжди донор-запит, хай там який intent.
    """

    query: DonorQuery | None
    reason: str
    intent: str = "filter"


# Стадія помилки виклику (LLMError.stage) → причина для користувача/логіки.
_STAGE_TO_REASON = {
    "truncated": "unparsable",
    "unparsable": "unparsable",
    "empty": "unavailable",
    "no_text": "unavailable",
    "network": "unavailable",
}


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
        responder: ConversationResponder | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._interpreter = interpreter
        self._responder = responder
        self._limiter = AICallLimiter(limit, window_seconds)
        self._counter = AIDailyCounter()
        self.model = model
        self._clock = clock

    @property
    def calls_today(self) -> int:
        return self._counter.today

    async def try_interpret(self, user_id: int, text: str) -> DonorQuery | None:
        """Пробує розібрати запит через ШІ. None — не вийшло (і це нормально).

        Тонка обгортка над interpret_with_reason для викликів, яким причина не
        потрібна (автоматичний фолбек словника не показує окремих повідомлень)."""
        return (await self.interpret_with_reason(user_id, text)).query

    async def interpret_with_reason(self, user_id: int, text: str) -> AIOutcome:
        """Як try_interpret, але повертає ще й ПРИЧИНУ невдачі — щоб бот показав
        конкретне повідомлення, а лог мав стадію збою (не одне «недоступний»)."""
        now = self._clock()
        if not self._limiter.allow(user_id, now):
            logger.info("Ліміт викликів ШІ вичерпано: користувач %s", user_id)
            return AIOutcome(None, "limit")

        self._counter.bump(now)
        try:
            interpretation = await self._interpreter.interpret_full(text)
        except LLMError as exc:
            # Текст LLMError не містить ключа — логувати безпечно. Стадію пишемо
            # окремо, щоб у логу було видно, ЩО саме зламалось.
            stage = getattr(exc, "stage", "network")
            reason = _STAGE_TO_REASON.get(stage, "unavailable")
            logger.error(
                "Виклик ШІ не вдався (користувач %s): стадія=%s, причина=%s: %s",
                user_id,
                stage,
                reason,
                exc,
            )
            return AIOutcome(None, reason)
        except Exception as exc:  # несподіване — теж не валимо бота
            logger.error(
                "Виклик ШІ впав несподівано (користувач %s): %s", user_id, exc, exc_info=exc
            )
            return AIOutcome(None, "unavailable")

        query, intent = interpretation.query, interpretation.intent
        # Рішення маршрутизатора в лог: намір моделі + чи витягнувся фільтр. Куди
        # запит пішов урешті (донор / чат / словник) логує вже викликач.
        logger.info(
            "Виклик ШІ (користувач %s): intent=%s, фільтр=%s",
            user_id,
            intent,
            "є" if query is not None else "нема",
        )
        if query is None:
            return AIOutcome(None, "empty", intent=intent)
        return AIOutcome(query, "ok", intent=intent)

    async def answer_question(self, user_id: int, text: str) -> str | None:
        """Розмовна відповідь на пояснювальне питання — через ту саму квоту ШІ
        (той самий ліміт і лічильник витрат, ТЗ §11).

        None — коли розмовна смуга недоступна, ліміт вичерпано або стався збій;
        тоді викликач показує ввічливе повідомлення. Донорів і даних ця смуга не
        бачить (див. app/llm/chat.py)."""
        if self._responder is None:
            return None
        now = self._clock()
        if not self._limiter.allow(user_id, now):
            logger.info("Ліміт викликів ШІ вичерпано (розмова): користувач %s", user_id)
            return None
        self._counter.bump(now)
        try:
            answer = await self._responder.answer(text)
        except Exception as exc:  # мережа/формат/таймаут — не валимо бота
            logger.error("Розмовна відповідь ШІ не вдалася (користувач %s): %s", user_id, exc)
            return None
        logger.info("Виклик ШІ (користувач %s): розмовна відповідь надана", user_id)
        return answer


def _build_provider(
    settings: Settings, http_post: HttpPost | None, *, model: str | None = None
) -> LLMProvider:
    """Обирає провайдера ШІ за LLM_PROVIDER (перемикання лише через .env).

    `model` дозволяє задати іншу модель, ніж дефолтна фільтрова (для розмовної
    смуги — LLM_CHAT_MODEL). Контракт однаковий, тож решта коду не залежить від
    того, який саме API за цим викликом."""
    common = {
        "api_key": settings.llm_api_key,
        "model": model or settings.llm_model,
        "timeout_seconds": settings.llm_timeout_seconds,
        "http_post": http_post,
        "max_tokens": settings.llm_max_tokens,
    }
    if settings.llm_provider == "openai":
        return OpenAIProvider(**common)
    return AnthropicProvider(**common)


def build_ai_service(settings: Settings, *, http_post: HttpPost | None = None) -> AIService | None:
    """Збирає сервіс ШІ з налаштувань. None — коли ШІ вимкнено (немає ключа).

    Будує ДВІ смуги на одному ключі й під однією квотою:
      * інтерпретатор фільтрів — на LLM_MODEL (дешева фільтрова модель);
      * розмовна смуга — на LLM_CHAT_MODEL (за замовчуванням = LLM_MODEL; сильнішу
        вмикають свідомо через .env). Розмовна смуга даних не бачить (chat.py).

    http_post дозволяє підмінити мережу в тестах; у бою лишається стандартним.
    """
    if not settings.llm_enabled:
        return None

    provider = _build_provider(settings, http_post)
    chat_provider = _build_provider(settings, http_post, model=settings.llm_chat_model)
    return AIService(
        LLMInterpreter(provider),
        limit=settings.llm_calls_limit,
        window_seconds=settings.llm_window_seconds,
        model=settings.llm_model,
        responder=ConversationResponder(chat_provider),
    )
