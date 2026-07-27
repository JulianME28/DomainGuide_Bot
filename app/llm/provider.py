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

from app.logging_setup import get_logger

logger = get_logger(__name__)

# Офіційний ендпойнт і версія Messages API (стабільні значення заголовків).
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


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
    """


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


class AnthropicProvider:
    """Один виклик моделі Anthropic: system + текст користувача → текст."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        *,
        http_post: HttpPost | None = None,
        max_tokens: int = 512,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout_seconds
        self._http_post = http_post or _default_http_post
        self._max_tokens = max_tokens

    async def complete(self, system: str, user_text: str) -> str:
        """Робить запит і повертає текст відповіді. Кидає LLMError на будь-якій
        проблемі — виклик вище її ловить і тихо повертається до словника."""
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }

        try:
            # Синхронний HTTP — в окремому потоці, щоб не блокувати цикл бота.
            data = await asyncio.to_thread(
                self._http_post, ANTHROPIC_URL, headers, body, self._timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Деталі йдуть у ЛОГ (з причиною-обгорткою), а не в текст винятку —
            # LLMError лишається без подробиць, щоб напевно не витік ключ/заголовки.
            _log_network_error(exc)
            raise LLMError(f"мережева помилка виклику ШІ: {type(exc).__name__}") from None
        except Exception as exc:  # будь-що інше — теж деталі в лог, не в текст
            _log_network_error(exc)
            raise LLMError(f"помилка виклику ШІ: {type(exc).__name__}") from None

        return _extract_text(data)

    def __repr__(self) -> str:
        # Ключ у repr не потрапляє — лише модель.
        return f"AnthropicProvider(model={self.model!r})"


def _extract_text(data: dict) -> str:
    """Дістає текст із відповіді Messages API: content[].text."""
    content = data.get("content") if isinstance(data, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    raise LLMError("у відповіді ШІ немає тексту")
