"""Адаптер Anthropic API (Messages API).

Робить один запит до моделі й повертає ТЕКСТ відповіді. Мережева частина
винесена в окрему функцію `http_post`, яку можна підмінити — тому тести
працюють на моканому HTTP, без реальної мережі й без ключа.

Ключ ніде не логується й не потрапляє в repr чи текст винятку — так само
дбайливо, як із токеном бота.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

from app.logging_setup import get_logger

logger = get_logger(__name__)

# Офіційні ендпойнти й версія Messages API (стабільні значення заголовків).
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Скільки токенів дозволяємо моделі на відповідь. JSON-фільтр короткий, але
# reasoning-моделі витрачають частину ліміту на «міркування» — тому запас,
# інакше відповідь обірветься (finish_reason=length) з порожнім текстом.
DEFAULT_MAX_TOKENS = 800


def _log_network_error(exc: BaseException) -> None:
    """Пише в лог ПОВНУ причину мережевої помилки виклику ШІ. Рівень ERROR.

    Ключа тут немає: він лише в заголовках ЗАПИТУ, а вони у виняток не
    потрапляють. Друкуємо тип, repr і причину-обгортку, щоб було видно
    справжнє коріння — SSLError / ConnectTimeout / ConnectionError / ProxyError:
      * urllib кладе причину в `.reason` (напр. URLError(SSLError(...)));
      * httpx та інші обгортки — у `__cause__`.
    Трасування додаємо через exc_info, щоб було видно й ланцюг обгорток."""
    logger.error(
        "Помилка виклику ШІ (мережа): тип=%s | repr=%r | reason=%r | cause=%r",
        type(exc).__name__,
        exc,
        getattr(exc, "reason", None),
        exc.__cause__,
        exc_info=exc,
    )


# Тип функції-транспорту: (url, headers, body) -> розібраний JSON-відповіді.
HttpPost = Callable[[str, dict[str, str], dict, float], dict]


class LLMError(RuntimeError):
    """Будь-яка проблема з викликом ШІ: мережа, таймаут, поганий формат.

    Текст цього винятку НІКОЛИ не містить ключа — його можна безпечно логувати.

    `stage` — на якій саме стадії стався збій, щоб лог і повідомлення користувачу
    були конкретні (а не одне загальне «недоступний»):
      * "network"    — HTTP/таймаут/мережа;
      * "no_text"    — у відповіді немає поля з текстом (несподіваний формат);
      * "empty"      — модель повернула порожній текст;
      * "truncated"  — відповідь обірвана на ліміті токенів (finish_reason=length);
      * "unparsable" — текст є, але валідного JSON у ньому немає.
    """

    def __init__(self, message: str, *, stage: str = "network") -> None:
        super().__init__(message)
        self.stage = stage


class LLMProvider(Protocol):
    """Спільний контракт провайдера: текст → текст. Хто саме кличе API (Anthropic
    чи OpenAI) — байдуже інтерпретатору й сервісу."""

    model: str

    async def complete(self, system: str, user_text: str) -> str: ...


def _default_http_post(url: str, headers: dict[str, str], body: dict, timeout: float) -> dict:
    """Реальний HTTP POST через стандартну бібліотеку (без зайвих залежностей).

    Синхронний — у провайдері викликається в окремому потоці, щоб не блокувати
    бота. Виняток тут — звичайна справа (немає мережі, 4xx/5xx, таймаут); його
    ловить провайдер і перетворює на LLMError.
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# --- Побудова запиту для кожного провайдера (єдине джерело формату) -----------
# Ці ж функції використовує діагностичний скрипт scripts/check_llm.py, щоб
# формат запиту не розповзався по коду.


def anthropic_request(
    api_key: str, model: str, system: str, user_text: str, max_tokens: int
) -> tuple[str, dict[str, str], dict]:
    """(url, headers, body) для Anthropic Messages API."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    return ANTHROPIC_URL, headers, body


def openai_request(
    api_key: str, model: str, system: str, user_text: str, max_tokens: int
) -> tuple[str, dict[str, str], dict]:
    """(url, headers, body) для OpenAI Chat Completions API.

    `system` іде окремим повідомленням role=system, `max_completion_tokens` —
    новіша (сумісна з новими моделями) назва обмеження довжини відповіді."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }
    return OPENAI_URL, headers, body


async def _post_json(
    http_post: HttpPost, url: str, headers: dict[str, str], body: dict, timeout: float
) -> dict:
    """Спільний мережевий шар: POST у окремому потоці + однакова обробка помилок.

    Деталі помилки йдуть у ЛОГ (з причиною-обгорткою), а не в текст LLMError —
    щоб напевно не витік ключ/заголовки. Однаково для Anthropic і OpenAI."""
    try:
        return await asyncio.to_thread(http_post, url, headers, body, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log_network_error(exc)
        raise LLMError(
            f"мережева помилка виклику ШІ: {type(exc).__name__}", stage="network"
        ) from None
    except Exception as exc:  # будь-що інше — теж деталі в лог, не в текст
        _log_network_error(exc)
        raise LLMError(f"помилка виклику ШІ: {type(exc).__name__}", stage="network") from None


class AnthropicProvider:
    """Один виклик моделі Anthropic: system + текст користувача → текст."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        *,
        http_post: HttpPost | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout_seconds
        self._http_post = http_post or _default_http_post
        self._max_tokens = max_tokens

    async def complete(self, system: str, user_text: str) -> str:
        """Робить запит і повертає текст відповіді. Кидає LLMError на будь-якій
        проблемі — виклик вище її ловить і тихо повертається до словника."""
        url, headers, body = anthropic_request(
            self._api_key, self.model, system, user_text, self._max_tokens
        )
        data = await _post_json(self._http_post, url, headers, body, self._timeout)
        return _extract_anthropic_text(data, self._max_tokens)

    def __repr__(self) -> str:
        # Ключ у repr не потрапляє — лише модель.
        return f"AnthropicProvider(model={self.model!r})"


class OpenAIProvider:
    """Один виклик моделі OpenAI (Chat Completions): system + текст → текст.

    Той самий контракт, що й у AnthropicProvider (метод complete, прихований
    ключ, спільна обробка помилок) — відрізняється лише формат запиту й місце
    тексту у відповіді."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        *,
        http_post: HttpPost | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout_seconds
        self._http_post = http_post or _default_http_post
        self._max_tokens = max_tokens

    async def complete(self, system: str, user_text: str) -> str:
        url, headers, body = openai_request(
            self._api_key, self.model, system, user_text, self._max_tokens
        )
        data = await _post_json(self._http_post, url, headers, body, self._timeout)
        return _extract_openai_text(data, self._max_tokens)

    def __repr__(self) -> str:
        return f"OpenAIProvider(model={self.model!r})"


def _raise_empty_or_truncated(finish_reason: object, truncated_value: str, max_tokens: int) -> None:
    """Порожній текст — це або обрізання на ліміті токенів, або просто порожньо.

    Розрізняємо за причиною зупинки (finish_reason у OpenAI, stop_reason у
    Anthropic). Обрізання — найчастіша прихована причина: reasoning-модель
    з'їдає ліміт на «міркування» й не встигає видати JSON."""
    if finish_reason == truncated_value:
        raise LLMError(
            f"відповідь ШІ обрізана на ліміті токенів (причина={finish_reason}, "
            f"max_tokens={max_tokens}); збільшіть LLM_MAX_TOKENS",
            stage="truncated",
        )
    raise LLMError(f"порожня відповідь ШІ (причина={finish_reason})", stage="empty")


def _extract_anthropic_text(data: dict, max_tokens: int) -> str:
    """Дістає текст із відповіді Anthropic Messages API: content[].text.

    Порожній текст розрізняємо на «обрізано» / «порожньо» за stop_reason."""
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise LLMError("у відповіді ШІ немає тексту (немає content)", stage="no_text")
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", ""))
            break
    if text.strip():
        return text
    _raise_empty_or_truncated(data.get("stop_reason"), "max_tokens", max_tokens)
    raise AssertionError("недосяжно")  # _raise_* завжди кидає — для типізатора


def _extract_openai_text(data: dict, max_tokens: int) -> str:
    """Дістає текст із відповіді OpenAI Chat Completions: choices[0].message.content.

    Порожній текст розрізняємо на «обрізано» / «порожньо» за finish_reason."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        raise LLMError("у відповіді ШІ немає тексту (немає choices)", stage="no_text")
    first = choices[0]
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    if text.strip():
        return text
    _raise_empty_or_truncated(first.get("finish_reason"), "length", max_tokens)
    raise AssertionError("недосяжно")  # _raise_* завжди кидає — для типізатора
