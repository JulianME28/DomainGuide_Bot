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

from app.analytics.query import ComparisonQuery, DonorQuery
from app.dictionary.countries import country_by_code
from app.llm.interpreter import (
    SYSTEM_PROMPT,
    LLMInterpreter,
    _comparison_from_inverted_ranges,
    _parse_json,
    interpret_json,
    read_operation,
)
from app.llm.provider import AnthropicProvider, LLMError, OpenAIProvider
from app.llm.service import build_ai_service
from app.settings import Settings
from app.text.freeform import parse_free_text


class TestОпераціяПорівняння:
    def test_інвертований_dr_стає_двома_незалежними_зрізами(self):
        operation = _comparison_from_inverted_ranges(
            {"section": "magic", "country": "de", "dr_min": 50, "dr_max": 20}
        )

        assert isinstance(operation, ComparisonQuery)
        assert len(operation.variants) == 2
        assert operation.variants[0].dr_min == 50
        assert operation.variants[0].dr_max is None
        assert operation.variants[1].dr_min is None
        assert operation.variants[1].dr_max == 20
        assert all(query.country.code == "de" for query in operation.variants)

    def test_валідний_діапазон_не_розбивається(self):
        assert (
            _comparison_from_inverted_ranges(
                {"section": "magic", "country": "de", "dr_min": 20, "dr_max": 50}
            )
            is None
        )

    async def test_інтерпретатор_рятує_злиті_межі(self):
        response = anthropic_response(
            '{"section":"magic","country":"de","dr_min":50,"dr_max":20}'
        )
        interpreter = LLMInterpreter(
            AnthropicProvider("secret", "model", 10, http_post=fake_post(response))
        )

        interpretation = await interpreter.interpret_full("DR від 50 і до 20 по Німеччині")

        assert interpretation.query is None
        assert isinstance(interpretation.operation, ComparisonQuery)
        assert len(interpretation.operation.variants) == 2

    def test_три_окремі_критерії_стають_трьома_запитами(self):
        operation = read_operation(
            {
                "op": "compare",
                "section": "magic",
                "country": "de",
                "criteria": [{"traffic_min": 2}, {"traffic_min": 30}, {"dr_min": 4}],
            }
        )
        assert isinstance(operation, ComparisonQuery)
        assert len(operation.variants) == 3
        assert all(query.country.code == "de" for query in operation.variants)
        assert operation.variants[0].traffic_min == 2
        assert operation.variants[1].traffic_min == 30
        assert operation.variants[2].dr_min == 4

    def test_невідомі_і_дубльовані_критерії_не_створюють_операцію(self):
        assert (
            read_operation(
                {
                    "op": "compare",
                    "section": "magic",
                    "country": "de",
                    "criteria": [{"traffic_min": 2}, {"traffic_min": 2}, {"nonsense": 3}],
                }
            )
            is None
        )

    def test_один_критерій_дубльований_моделлю_по_базах_не_стає_compare(self):
        """Регресія зі скриншота: «Меджик + Морди, DR 30» — один критерій,
        section усередині criteria не повинен маскувати його дублювання."""
        assert (
            read_operation(
                {
                    "op": "compare",
                    "section": "magic",
                    "country": "gb",
                    "criteria": [
                        {"section": "magic", "dr_min": 30},
                        {"section": "mordy", "dr_min": 30},
                    ],
                }
            )
            is None
        )

    def test_промпт_явно_описує_compare(self):
        assert '"op":"compare"' in SYSTEM_PROMPT
        assert "окремо" in SYSTEM_PROMPT.lower()
        assert "DR від 50 і до 20" in SYSTEM_PROMPT

    def test_ші_може_повернути_виключену_країну(self):
        query = interpret_json(
            {"section": "magic", "excluded_countries": ["fr"], "traffic_min": 50}
        )

        assert query is not None
        assert [country.code for country in query.excluded_countries] == ["fr"]
        assert query.traffic_min == 50
        assert "excluded_countries" in SYSTEM_PROMPT


class TestCoverageНеВигадуєКраїну:
    async def test_у_нас_не_перетворюється_на_usa(self):
        response = anthropic_response(
            '{"op":"coverage","section":"mordy","needs":{"us":100},'
            '"traffic_thresholds":[100]}'
        )
        interpreter = LLMInterpreter(
            AnthropicProvider("secret", "model", 10, http_post=fake_post(response))
        )

        interpretation = await interpreter.interpret_full(
            "чи є у нас 100 донорів в мордах з трафіком 100+?"
        )

        assert interpretation.operation is None
        assert interpretation.query is None

    def test_промпт_пояснює_що_у_нас_не_us(self):
        assert "у нас" in SYSTEM_PROMPT
        assert "USA/us" in SYSTEM_PROMPT

    def test_вкладений_needs_не_рахується_другим_json(self):
        payload = _parse_json(
            '{"op":"coverage","section":"mordy","needs":{"de":100},'
            '"traffic_thresholds":[100]}'
        )
        assert payload == {
            "op": "coverage",
            "section": "mordy",
            "needs": {"de": 100},
            "traffic_thresholds": [100],
        }

    async def test_список_країн_без_потреби_не_отримує_need_один(self):
        response = anthropic_response(
            '{"op":"coverage","section":"magic","needs":'
            '{"de":1,"ca":1,"fr":1,"bg":1},"traffic_thresholds":[]}'
        )
        interpreter = LLMInterpreter(
            AnthropicProvider("secret", "model", 10, http_post=fake_post(response))
        )

        interpretation = await interpreter.interpret_full(
            "німеччина канада франція болгарія морди і меджик"
        )

        assert interpretation.operation is None
        assert interpretation.query is not None
        assert {country.code for country in interpretation.query.countries} == {
            "de",
            "ca",
            "fr",
            "bg",
        }

    async def test_слово_треба_залишає_справжній_coverage(self):
        response = anthropic_response(
            '{"op":"coverage","section":"magic","needs":{"de":2},'
            '"traffic_thresholds":[50]}'
        )
        interpreter = LLMInterpreter(
            AnthropicProvider("secret", "model", 10, http_post=fake_post(response))
        )
        interpretation = await interpreter.interpret_full(
            "Німеччина: треба 2 донори, чи вистачає з трафіком 50+?"
        )
        assert interpretation.operation is not None


def anthropic_response(text: str, *, stop_reason: str | None = None) -> dict:
    """Відповідь Messages API у мінімальному вигляді (з опційним stop_reason)."""
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason}


def openai_response(text: str, *, finish_reason: str | None = None) -> dict:
    """Відповідь OpenAI Chat Completions (з опційним finish_reason)."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
        ]
    }


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

    def test_старе_поле_мови_підтримується(self):
        query = interpret_json({"language": "en"})

        assert [language.code for language in query.languages] == ["en"]

    def test_новий_список_мов_підтримується_і_дедуплікується(self):
        query = interpret_json({"languages": ["en", "de", "xx", "en", "fr"]})

        assert [language.code for language in query.languages] == ["en", "de", "fr"]

    def test_новий_список_мов_має_пріоритет_над_старим_полем(self):
        query = interpret_json({"language": "fr", "languages": ["de", "en"]})

        assert [language.code for language in query.languages] == ["de", "en"]

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

    async def test_відповідь_сміттям_це_помилка_розбору(self):
        """Текст без JSON — це помилка стадії розбору (unparsable), не тихе None."""
        post = fake_post(anthropic_response("вибачте, не зрозумів запит"))
        interpreter = LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))
        with pytest.raises(LLMError) as exc:
            await interpreter.interpret("...")
        assert exc.value.stage == "unparsable"

    async def test_валідний_json_без_фільтрів_дає_None(self):
        """Валідний, але порожній JSON ({}) — не помилка, а «нічого не впізнано»."""
        post = fake_post(anthropic_response("{}"))
        interpreter = LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))
        assert await interpreter.interpret("...") is None

    async def test_json_у_markdown_блоці_розбирається(self):
        """Модель обгорнула JSON у ```json — усе одно розбираємо."""
        raw = '```json\n{"country":"de","dr_min":30}\n```'
        interpreter = LLMInterpreter(
            AnthropicProvider("k", "m", 1, http_post=fake_post(anthropic_response(raw)))
        )
        query = await interpreter.interpret("...")
        assert query.country is country_by_code("de")
        assert query.dr_min == 30

    async def test_json_серед_пояснень_із_зайвими_дужками(self):
        """Пояснення зі сторонніми {дужками} навколо JSON не збиває розбір."""
        raw = 'Ось {результат} для вас: {"country":"fr"}. Дякую!'
        interpreter = LLMInterpreter(
            AnthropicProvider("k", "m", 1, http_post=fake_post(anthropic_response(raw)))
        )
        query = await interpreter.interpret("...")
        assert query.country is country_by_code("fr")


class TestСтадіїЗбою:
    """Кожна невдача має конкретну стадію в LLMError і зрозумілий лог/повідомлення."""

    async def test_обрізана_відповідь_openai_це_truncated(self):
        """finish_reason=length + порожній текст → стадія truncated (не «недоступний»)."""
        post = fake_post(openai_response("", finish_reason="length"))
        provider = OpenAIProvider("k", "m", 1, http_post=post)
        with pytest.raises(LLMError) as exc:
            await provider.complete("s", "u")
        assert exc.value.stage == "truncated"
        assert "LLM_MAX_TOKENS" in str(exc.value)  # підказка, що робити

    async def test_обрізана_відповідь_anthropic_це_truncated(self):
        post = fake_post(anthropic_response("", stop_reason="max_tokens"))
        provider = AnthropicProvider("k", "m", 1, http_post=post)
        with pytest.raises(LLMError) as exc:
            await provider.complete("s", "u")
        assert exc.value.stage == "truncated"

    async def test_порожня_відповідь_це_empty(self):
        """Порожній текст без ознаки обрізання → стадія empty."""
        post = fake_post(openai_response("", finish_reason="stop"))
        provider = OpenAIProvider("k", "m", 1, http_post=post)
        with pytest.raises(LLMError) as exc:
            await provider.complete("s", "u")
        assert exc.value.stage == "empty"

    async def test_обрізана_відповідь_логує_стадію_не_недоступний(self, caplog):
        """Сервіс на обрізаній відповіді логує ERROR зі стадією truncated."""
        import logging

        post = fake_post(openai_response("", finish_reason="length"))
        service = build_ai_service(ai_settings(llm_provider="openai"), http_post=post)
        with caplog.at_level(logging.ERROR):
            outcome = await service.interpret_with_reason(1, "Морди до 20 по Британії")
        assert outcome.query is None
        assert outcome.reason == "unparsable"  # для користувача — «не розібрав», не «недоступний»
        assert "truncated" in caplog.text

    async def test_нерозбірний_json_логує_сиру_відповідь(self, caplog):
        """При невалідному JSON у лог іде перші символи того, що повернула модель."""
        import logging

        post = fake_post(anthropic_response("вибачте, поясню без JSON..."))
        service = build_ai_service(ai_settings(), http_post=post)
        with caplog.at_level(logging.ERROR):
            outcome = await service.interpret_with_reason(1, "щось")
        assert outcome.reason == "unparsable"
        assert "вибачте, поясню" in caplog.text  # видно, що саме повернула модель

    async def test_бойовий_max_tokens_із_запасом(self):
        """У бойовому виклику max_tokens беремо з налаштувань (запас, не 512/16)."""
        post = fake_post(openai_response('{"country":"de"}'))
        service = build_ai_service(ai_settings(llm_provider="openai"), http_post=post)
        await service.try_interpret(1, "німецькі")
        assert post.calls[0]["body"]["max_completion_tokens"] >= 500


class TestУзгодженняЗПарсером:
    """ШІ має розбирати запит так само, як словниковий парсер: «вихідні» = фільтр
    ЗАСПАМЛЕНОСТІ (стовпець G), окремого числового фільтра вихідних немає.

    LLM тут моканий: у відповідь підкладаємо JSON, який модель має віддати за
    ОНОВЛЕНОЮ інструкцією. Перевіряємо, що інтерпретатор зводить це до того самого
    фільтра, що й парсер, а сам промт більше не дозволяє поле про вихідні."""

    def _interpreter(self, json_text: str) -> LLMInterpreter:
        post = fake_post(anthropic_response(json_text))
        return LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))

    def _fields(self, query: DonorQuery) -> tuple:
        return (query.section_key, query.country, query.spam_min, query.spam_max)

    async def test_до_20_вихідних_це_заспамленість_без_окремого_фільтра(self):
        """«Морди до 20 вихідних по Британії» → spam_max=20, без поля вихідних."""
        interpreter = self._interpreter('{"section":"mordy","country":"gb","spam_max":20}')
        query = await interpreter.interpret("Морди до 20 вихідних по Британії")
        assert query is not None
        assert query.section_key == "mordy"
        assert query.country is country_by_code("gb")
        assert (query.spam_min, query.spam_max) == (None, 20)
        assert not hasattr(query, "outlinks_min")
        assert not hasattr(query, "outlinks_max")

    async def test_збіг_зі_словниковим_парсером(self):
        """Той самий запит через ШІ і через парсер дає той самий фільтр."""
        text = "Морди до 20 вихідних лінків по Британії"
        dict_query = parse_free_text(text).query
        ai_query = await self._interpreter(
            '{"section":"mordy","country":"gb","spam_max":20}'
        ).interpret(text)
        assert self._fields(ai_query) == self._fields(dict_query)
        assert self._fields(dict_query) == ("mordy", country_by_code("gb"), None, 20)

    async def test_заспамленість_до_20_те_саме(self):
        query = await self._interpreter('{"section":"mordy","spam_max":20}').interpret(
            "заспамленість до 20"
        )
        assert (query.spam_min, query.spam_max) == (None, 20)

    async def test_dr_від_50_лишається_мінімумом(self):
        """Напрямок DR не плутається: «від 50» — це dr_min, а не dr_max."""
        query = await self._interpreter('{"dr_min":50}').interpret("DR від 50")
        assert query.dr_min == 50
        assert query.dr_max is None

    async def test_незаспамлені_без_числа_без_порога(self):
        """«незаспамлені» БЕЗ конкретного числа — ШІ не вигадує поріг: лише країна.

        Модель за оновленою інструкцією не повертає spam_* — «незаспамлений» для
        кожного означає різне, поріг ставимо тільки на конкретне число. Розподіл
        заспамленості бот покаже сам під результатом.
        """
        interpreter = self._interpreter('{"section":"mordy","country":"gb"}')
        query = await interpreter.interpret("незаспамлені Морди по Британії")
        assert query is not None
        assert query.section_key == "mordy"
        assert query.country is country_by_code("gb")
        assert query.spam_min is None
        assert query.spam_max is None

    async def test_незаспамлені_з_числом_ставить_поріг(self):
        """«незаспамлені до 20» — тут число Є, тож поріг ставимо (spam_max=20)."""
        query = await self._interpreter('{"section":"mordy","spam_max":20}').interpret(
            "незаспамлені Морди до 20"
        )
        assert (query.spam_min, query.spam_max) == (None, 20)

    def test_легасі_вихідні_від_моделі_зводяться_до_заспамленості(self):
        """Захисна сітка: навіть якщо модель поверне старе outlinks_max — це spam."""
        query = interpret_json({"section": "mordy", "outlinks_max": 20})
        assert query.spam_max == 20
        assert not hasattr(query, "outlinks_max")


class TestПромтБезПоляВихідних:
    """Схема/інструкція для моделі не має окремого фільтра вихідних (стовпець F)."""

    def test_у_схемі_немає_поля_вихідних(self):
        assert '"spam_min","spam_max"' in SYSTEM_PROMPT
        assert "outlinks" not in SYSTEM_PROMPT.lower()

    def test_схема_описує_новий_і_старий_формати_мов(self):
        assert '"languages"' in SYSTEM_PROMPT
        assert '"language"' in SYSTEM_PROMPT

    def test_промт_явно_забороняє_окремий_фільтр_вихідних(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "заспамлен" in lowered
        assert "вихідн" in lowered
        assert "не існує" in lowered  # «Окремого фільтра „вихідні лінки" НЕ існує»

    def test_промт_задає_напрямки(self):
        """DR/трафік — мінімум за замовчуванням; заспамленість — максимум."""
        assert "spam_max" in SYSTEM_PROMPT
        assert "traffic_min" in SYSTEM_PROMPT
        assert "dr_min" in SYSTEM_PROMPT

    def test_промт_не_ставить_поріг_на_голе_незаспамлені(self):
        """Промт більше не каже ставити spam_max=0 на «незаспамлені» без числа."""
        assert '{"spam_max": 0}' not in SYSTEM_PROMPT
        lowered = SYSTEM_PROMPT.lower()
        assert "незаспамлені" in lowered
        # Явно сказано не ставити поріг без конкретного числа.
        assert "конкретне число" in lowered
        assert "не став" in lowered

    def test_промт_велить_виправляти_одруки(self):
        """Промт має просити виправляти одруки в назвах країн/мов (не відкидати)."""
        lowered = SYSTEM_PROMPT.lower()
        assert "одрук" in lowered
        assert "англьійською" in lowered  # конкретний приклад одруку → мова en


class TestПитальніФормулювання:
    """Фаза 2B: питальні запити («Скільки донорів по X?») — теж фільтр, не {}.

    LLM моканий: підкладаємо JSON, який модель має віддати за ПІДСИЛЕНОЮ
    інструкцією. Перевіряємо, що інтерпретатор зводить це до непорожнього
    фільтра (сам факт, що gpt-4o-mini на це формулювання без інструкції віддавав
    `{}`, і був причиною глухого кута — див. tests/test_ai_fallback.py)."""

    async def test_питальний_запит_дає_непорожній_фільтр(self):
        post = fake_post(anthropic_response('{"section":"mordy","country":"us"}'))
        interpreter = LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))
        query = await interpreter.interpret("Скільки донорів у базі Морди по США?")
        assert query is not None
        assert query.section_key == "mordy"
        assert query.country is country_by_code("us")

    def test_промт_велить_трактувати_питання_як_фільтр(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "питальн" in lowered  # інструкція про питальні формулювання є
        assert "скільки донорів" in lowered  # few-shot приклад із питанням


class TestМультиОбєктнаВідповідь:
    """Фікс (II): модель віддала КІЛЬКА JSON-об'єктів — нічого не губимо мовчки.

    Раніше брався лише перший об'єкт, і решта (бази й країни) зникали навіть в
    одно-базових multi-country запитах. Тепер об'єкти зливаються без втрат."""

    def test_parse_json_зливає_обєкти_без_втрати_країн(self):
        from app.llm.interpreter import _parse_json

        merged = _parse_json(
            '{"section":"magic","country":"gb","traffic_min":100},'
            '{"section":"mordy","country":"de","traffic_min":100}'
        )
        assert merged["countries"] == ["gb", "de"]  # обидві країни збережено
        assert "country" not in merged
        assert merged["section"] == "magic"  # перша секція (контракт одно-базовий)
        assert merged["traffic_min"] == 100

    def test_parse_json_один_обєкт_без_змін(self):
        from app.llm.interpreter import _parse_json

        assert _parse_json('{"country":"de"}') == {"country": "de"}

    async def test_інтерпретатор_не_губить_другу_країну(self):
        raw = '{"section":"magic","country":"gb"},{"section":"mordy","country":"de"}'
        interpreter = LLMInterpreter(
            AnthropicProvider("k", "m", 1, http_post=fake_post(anthropic_response(raw)))
        )
        query = await interpreter.interpret("британія і німеччина")
        assert query is not None
        assert {c.code for c in query.countries} == {"gb", "de"}
        assert query.is_multi_country


class TestСанітарнаСіткаШІ:
    """Група 1: фільтр від ШІ проходить ТУ САМУ сітку, що й словниковий."""

    def _interp(self, json_text: str) -> LLMInterpreter:
        post = fake_post(anthropic_response(json_text))
        return LLMInterpreter(AnthropicProvider("k", "m", 1, http_post=post))

    async def test_інвертований_dr_без_і_стає_інтервалом(self):
        interpretation = await self._interp(
            '{"section":"magic","country":"gb","dr_min":40,"dr_max":20}'
        ).interpret_full("меджик британія DR від 40 до 20")
        assert interpretation.operation is None
        assert interpretation.query is not None
        assert interpretation.query.dr_min == 20
        assert interpretation.query.dr_max == 40

    async def test_заперечена_зона_через_ші_знімається(self):
        q = await self._interp('{"section":"magic","country":"de","zones":[".com"]}').interpret(
            "меджик німеччина у зоні не .com"
        )
        assert q is not None
        assert ".com" not in q.zones  # заперечену зону знято
        assert q.country is country_by_code("de")  # країна лишилась


class TestПромтГрупа1:
    def test_регіони_не_країни(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "регіон" in lowered
        assert "каталон" in lowered  # приклад: Каталонія ≠ Канада

    def test_сторонні_числа_не_в_метрики(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "ціна" in lowered
        assert "топ" in lowered

    def test_заперечення_не_позитивне(self):
        assert "запереченн" in SYSTEM_PROMPT.lower()


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
