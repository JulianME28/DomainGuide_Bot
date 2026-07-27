"""Тести підключення ШІ. Усе на МОКАНОМУ HTTP — без реальної мережі й ключа.

Перевіряємо головне:
  * адаптер робить правильний запит і розбирає відповідь;
  * інтерпретатор перетворює JSON на валідний фільтр, а сміття/поля поза
    whitelist — відкидає;
  * ліміт, помилка, таймаут → тихий фолбек (None), бот не падає;
  * ключ не потрапляє в repr і в текст винятку.
"""

from __future__ import annotations

import pytest

from app.analytics.query import DonorQuery
from app.dictionary.countries import country_by_code
from app.llm.interpreter import LLMInterpreter, interpret_json
from app.llm.provider import AnthropicProvider, LLMError, OpenAIProvider
from app.llm.service import build_ai_service
from app.settings import Settings


def anthropic_response(text: str) -> dict:
    """Відповідь Messages API у мінімальному вигляді."""
    return {"content": [{"type": "text", "text": text}]}


def openai_response(text: str) -> dict:
    """Відповідь OpenAI Chat Completions у мінімальному вигляді."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def fake_post(response: dict | None = None, *, raises: Exception | None = None):
    """Підміна HTTP: віддає готову відповідь або кидає помилку. Рахує виклики."""
    calls: list[dict] = []

    def _post(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if raises is not None:
            raise raises
        return response

    _post.calls = calls  # type: ignore[attr-defined]
    return _post


def ai_settings(**overrides) -> Settings:
    base = {
        "bot_token": "t",
        "data_backend": "sheets",
        "spreadsheet_id": "s",
        "credentials_file": "credentials.json",
        "allowed_user_ids": frozenset({1}),
        "admin_user_ids": frozenset(),
        "llm_provider": "anthropic",
        "cache_ttl_seconds": 60,
        "rate_limit_requests": 5,
        "rate_limit_window_seconds": 60,
        "log_level": "INFO",
        "llm_api_key": "test-key",
        "llm_model": "claude-haiku-4-5-20251001",
        "llm_timeout_seconds": 5,
        "llm_calls_limit": 2,
        "llm_window_seconds": 3600,
    }
    base.update(overrides)
    return Settings(**base)


class TestАдаптер:
    async def test_повертає_текст_і_правильний_запит(self):
        post = fake_post(anthropic_response('{"country":"de"}'))
        provider = AnthropicProvider("secret-key", "claude-haiku-4-5-20251001", 20, http_post=post)

        out = await provider.complete("system", "user text")

        assert out == '{"country":"de"}'
        call = post.calls[0]
        assert call["headers"]["x-api-key"] == "secret-key"
        assert call["headers"]["anthropic-version"]
        assert call["body"]["model"] == "claude-haiku-4-5-20251001"
        assert call["body"]["messages"][0]["content"] == "user text"

    async def test_помилка_http_стає_LLMError(self):
        provider = AnthropicProvider("k", "m", 1, http_post=fake_post(raises=OSError("down")))
        with pytest.raises(LLMError):
            await provider.complete("s", "u")

    async def test_відповідь_без_тексту_це_помилка(self):
        provider = AnthropicProvider("k", "m", 1, http_post=fake_post({"content": []}))
        with pytest.raises(LLMError):
            await provider.complete("s", "u")

    def test_ключ_не_в_repr(self):
        provider = AnthropicProvider("super-secret-key", "m", 1)
        assert "super-secret-key" not in repr(provider)

    async def test_ключ_не_в_тексті_винятку(self):
        provider = AnthropicProvider(
            "super-secret-key", "m", 1, http_post=fake_post(raises=OSError("boom"))
        )
        try:
            await provider.complete("s", "u")
        except LLMError as exc:
            assert "super-secret-key" not in str(exc)

    async def test_мережева_помилка_логує_повну_причину(self, caplog):
        """У лог (ERROR) іде справжня причина-обгортка, але НЕ ключ."""
        import logging
        import urllib.error

        cause = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        provider = AnthropicProvider(
            "super-secret-key", "m", 1, http_post=fake_post(raises=urllib.error.URLError(cause))
        )
        with caplog.at_level(logging.ERROR), pytest.raises(LLMError):
            await provider.complete("s", "u")

        assert "URLError" in caplog.text
        assert "CERTIFICATE_VERIFY_FAILED" in caplog.text  # справжню причину видно
        assert "super-secret-key" not in caplog.text  # ключ не витік у лог


class TestВалідаціяФільтра:
    def test_валідний_фільтр(self):
        query = interpret_json({"section": "mordy", "country": "de", "dr_min": 30})
        assert query.section_key == "mordy"
        assert query.country is country_by_code("de")
        assert query.dr_min == 30

    def test_список_країн(self):
        query = interpret_json({"countries": ["de", "at", "ch"]})
        assert query.is_multi_country
        assert {c.code for c in query.countries} == {"de", "at", "ch"}

    def test_невідома_країна_відкидається(self):
        query = interpret_json({"country": "xx", "dr_min": 10})
        assert query.country is None
        assert query.dr_min == 10

    def test_поле_поза_whitelist_ігнорується(self):
        """Поля, яких немає в дозволеному переліку, просто не читаються."""
        query = interpret_json({"country": "de", "evil": "; DROP", "raw_sql": "select *"})
        assert query.country is country_by_code("de")
        assert not hasattr(query, "evil")

    def test_невалідна_секція_стає_magic(self):
        query = interpret_json({"section": "submits", "country": "de"})
        assert query.section_key == "magic"

    def test_відємне_число_відкидається(self):
        query = interpret_json({"country": "de", "dr_min": -5})
        assert query.dr_min is None

    def test_порожній_або_сміття_дає_None(self):
        assert interpret_json({}) is None
        assert interpret_json({"foo": "bar"}) is None


class TestІнтерпретатор:
    async def test_мокана_відповідь_стає_фільтром(self):
        post = fake_post(anthropic_response('Ось фільтр: {"country":"de","dr_min":40}. Готово.'))
        interpreter = LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))

        query = await interpreter.interpret("німецькі донори з пристойним DR")

        assert query is not None
        assert query.country is country_by_code("de")
        assert query.dr_min == 40

    async def test_відповідь_сміттям_дає_None(self):
        post = fake_post(anthropic_response("вибачте, не зрозумів запит"))
        interpreter = LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))
        assert await interpreter.interpret("...") is None


class TestСервіс:
    def test_вимкнено_без_ключа(self):
        settings = ai_settings(llm_api_key="")
        assert not settings.llm_enabled
        assert build_ai_service(settings) is None

    def test_ключ_не_в_repr_налаштувань(self):
        settings = ai_settings(llm_api_key="super-secret-key")
        assert "super-secret-key" not in repr(settings)

    async def test_ліміт_викликів_ші(self):
        post = fake_post(anthropic_response('{"country":"de"}'))
        service = build_ai_service(ai_settings(llm_calls_limit=2), http_post=post)

        assert await service.try_interpret(1, "x") is not None
        assert await service.try_interpret(1, "x") is not None
        # Третій виклик — за лімітом: ШІ навіть не викликається.
        assert await service.try_interpret(1, "x") is None
        assert len(post.calls) == 2

    async def test_лічильник_за_день(self):
        post = fake_post(anthropic_response('{"country":"de"}'))
        service = build_ai_service(ai_settings(), http_post=post)
        await service.try_interpret(1, "x")
        assert service.calls_today == 1

    async def test_помилка_api_дає_тихий_фолбек(self):
        post = fake_post(raises=OSError("network down"))
        service = build_ai_service(ai_settings(), http_post=post)
        assert await service.try_interpret(1, "x") is None  # None, без краху

    async def test_валідний_результат_це_DonorQuery(self):
        post = fake_post(anthropic_response('{"countries":["de","fr"],"traffic_min":100}'))
        service = build_ai_service(ai_settings(), http_post=post)
        query = await service.try_interpret(1, "щось під Німеччину і Францію з трафіком")
        assert isinstance(query, DonorQuery)
        assert query.is_multi_country
        assert query.traffic_min == 100


class TestOpenAIПровайдер:
    """OpenAI Chat Completions — той самий контракт, що й Anthropic."""

    async def test_повертає_текст_і_правильний_запит(self):
        post = fake_post(openai_response('{"country":"de"}'))
        provider = OpenAIProvider("sk-proj-secret", "gpt-4o-mini", 20, http_post=post)

        out = await provider.complete("system", "user text")

        assert out == '{"country":"de"}'
        call = post.calls[0]
        assert call["url"].endswith("/chat/completions")
        assert call["headers"]["Authorization"] == "Bearer sk-proj-secret"
        assert call["body"]["model"] == "gpt-4o-mini"
        roles = [m["role"] for m in call["body"]["messages"]]
        assert roles == ["system", "user"]

    async def test_відповідь_без_тексту_це_помилка(self):
        provider = OpenAIProvider("k", "m", 1, http_post=fake_post({"choices": []}))
        with pytest.raises(LLMError):
            await provider.complete("s", "u")

    async def test_помилка_http_стає_LLMError(self):
        provider = OpenAIProvider("k", "m", 1, http_post=fake_post(raises=OSError("down")))
        with pytest.raises(LLMError):
            await provider.complete("s", "u")

    def test_ключ_не_в_repr(self):
        assert "sk-proj-secret" not in repr(OpenAIProvider("sk-proj-secret", "m", 1))

    async def test_ключ_не_в_тексті_винятку(self):
        provider = OpenAIProvider(
            "sk-proj-secret", "m", 1, http_post=fake_post(raises=OSError("boom"))
        )
        try:
            await provider.complete("s", "u")
        except LLMError as exc:
            assert "sk-proj-secret" not in str(exc)


class TestOpenAIСервіс:
    """Вибір провайдера через .env і той самий фолбек/ліміт/логування."""

    def test_build_обирає_openai_провайдера(self):
        service = build_ai_service(
            ai_settings(llm_provider="openai", llm_model="gpt-4o-mini"),
            http_post=fake_post(openai_response("{}")),
        )
        assert service is not None
        assert isinstance(service._interpreter._provider, OpenAIProvider)

    def test_build_обирає_anthropic_за_замовчуванням(self):
        service = build_ai_service(ai_settings(), http_post=fake_post(anthropic_response("{}")))
        assert isinstance(service._interpreter._provider, AnthropicProvider)

    async def test_валідний_фільтр_через_openai(self):
        post = fake_post(openai_response('{"countries":["de","fr"],"traffic_min":100}'))
        service = build_ai_service(ai_settings(llm_provider="openai"), http_post=post)

        query = await service.try_interpret(1, "німецькі й французькі з трафіком")

        assert isinstance(query, DonorQuery)
        assert query.is_multi_country
        assert query.traffic_min == 100

    async def test_сміття_від_openai_дає_None(self):
        post = fake_post(openai_response("вибачте, не зрозумів"))
        service = build_ai_service(ai_settings(llm_provider="openai"), http_post=post)
        assert await service.try_interpret(1, "...") is None

    async def test_401_openai_тихий_фолбек(self):
        import urllib.error

        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        service = build_ai_service(
            ai_settings(llm_provider="openai"), http_post=fake_post(raises=err)
        )
        assert await service.try_interpret(1, "x") is None  # None, без краху

    async def test_мережа_openai_тихий_фолбек(self):
        service = build_ai_service(
            ai_settings(llm_provider="openai"), http_post=fake_post(raises=OSError("network down"))
        )
        assert await service.try_interpret(1, "x") is None

    async def test_ключ_openai_не_тече_в_лог(self, caplog):
        import logging
        import urllib.error

        service = build_ai_service(
            ai_settings(llm_provider="openai", llm_api_key="sk-proj-super-secret"),
            http_post=fake_post(raises=urllib.error.URLError(OSError("boom"))),
        )
        with caplog.at_level(logging.ERROR):
            await service.try_interpret(1, "x")
        assert "sk-proj-super-secret" not in caplog.text
