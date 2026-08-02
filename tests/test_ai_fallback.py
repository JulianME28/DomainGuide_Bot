"""Надійність режиму «Індивідуальний запит»: фолбек у словник і тексти невдач.

Фаза 2. Раніше режим був LLM-only: щойно ШІ повертав порожній фільтр (а
gpt-4o-mini на питальні формулювання віддає рівно `{}`), користувач упирався
в глухий кут. Тепер, коли ШІ не дав фільтра, той самий текст пробує СЛОВНИК —
рівно як звичайний вільний текст. Глухий кут лишається, лише коли НІ ШІ, НІ
словник не зрозуміли запит.

Усе на фейкових даних і фейковому ШІ — без мережі й без ключа.
"""

from __future__ import annotations

from app.analytics.query import CoverageQuery, DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import (
    AI_EMPTY_TEXT,
    AI_LIMIT_TEXT,
    AI_UNAVAILABLE_TEXT,
    run_ai_query,
)
from app.data.repository import DonorRepository
from app.dictionary.countries import country_by_code
from app.llm.service import AIOutcome
from app.settings import Settings
from tests.fixtures.fake_data import FakeReader, empty_rows, magic_rows


def make_settings() -> Settings:
    return Settings(
        bot_token="t",
        data_backend="sheets",
        spreadsheet_id="s",
        credentials_file="c",
        allowed_user_ids=frozenset({1}),
        admin_user_ids=frozenset(),
        llm_provider="none",
        cache_ttl_seconds=1800,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
        log_level="INFO",
    )


class FakeUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


class FakeSent:
    """Повідомлення, яке повернув answer(): його потім редагують або видаляють."""

    def __init__(self) -> None:
        self.edits: list[tuple] = []
        self.deleted = False

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = FakeUser()
        self.answers: list[tuple] = []
        self.sents: list[FakeSent] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))
        sent = FakeSent()
        self.sents.append(sent)
        return sent


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})
        self.current_state = "unset"

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)
        return dict(self._data)

    async def set_state(self, state=None):
        self.current_state = state


class FakeAI:
    """Фейковий сервіс ШІ: повертає заздалегідь задану невдачу (query=None)."""

    def __init__(self, outcome: AIOutcome) -> None:
        self._outcome = outcome
        self.calls: list[tuple[int, str]] = []

    async def interpret_with_reason(self, user_id: int, text: str) -> AIOutcome:
        self.calls.append((user_id, text))
        return self._outcome

    async def try_interpret(self, user_id: int, text: str):
        return self._outcome.query


def make_services(columns_config, *, ai: FakeAI) -> BotServices:
    repo = DonorRepository(
        FakeReader({"magic": magic_rows(), "mordy": empty_rows()}),
        columns_config,
        ttl_seconds=1800,
    )
    return BotServices(
        settings=make_settings(),
        columns=columns_config,
        repository=repo,
        action_log=ActionLog(),
        ai=ai,
    )


def _all_texts(message: FakeMessage) -> list[str]:
    """Усі тексти, які бачив користувач: і прямі answer, і фінальні edit_text."""
    texts = [text for text, _markup in message.answers]
    for sent in message.sents:
        texts.extend(text for text, _markup in sent.edits)
    return texts


class TestФолбекУСловник:
    async def test_питальний_запит_падає_в_словник_і_дає_картку(self, columns_config):
        """ШІ повернув порожньо (як gpt-4o-mini на «Скільки…?») → словник рятує."""
        ai = FakeAI(AIOutcome(None, "empty"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "Скільки донорів у базі Морди по США?")

        texts = _all_texts(message)
        # Показано картку результату по «Мордах», а не глухий кут.
        assert any("Морди" in t and "Знайдено донорів" in t for t in texts)
        assert AI_EMPTY_TEXT not in texts
        # ШІ таки викликали (спершу він, потім резерв).
        assert ai.calls

    async def test_empty_і_словник_не_зрозумів_показує_ai_empty(self, columns_config):
        """Явний глухий кут: НІ ШІ, НІ словник → чесний AI_EMPTY_TEXT."""
        ai = FakeAI(AIOutcome(None, "empty"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "привіт як справи")

        assert AI_EMPTY_TEXT in _all_texts(message)

    async def test_ліміт_показує_власний_текст(self, columns_config):
        """reason=limit більше не губиться в загальному AI_FAILED — свій текст."""
        ai = FakeAI(AIOutcome(None, "limit"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "привіт як справи")

        assert AI_LIMIT_TEXT in _all_texts(message)

    async def test_недоступність_показує_власний_текст(self, columns_config):
        """reason=unavailable → окремий зрозумілий текст, коли словник теж не зміг."""
        ai = FakeAI(AIOutcome(None, "unavailable"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "привіт як справи")

        assert AI_UNAVAILABLE_TEXT in _all_texts(message)


class TestДвіБазиЧерезШІ:
    """Варіант C: «меджик і морди …» через ШІ → обидві бази (show_both_bases).

    Контракт ШІ одно-базовий, тож фільтр приходить від ШІ, а рішення «обидві
    бази» — від детермінованої детекції parse_free_text на тому самому тексті."""

    async def test_обидві_бази_коли_текст_каже_меджик_і_морди(self, columns_config):
        gb = country_by_code("gb")
        # ШІ віддав ОДНОбазовий фільтр (як і мусить за контрактом) — Британія.
        ai = FakeAI(AIOutcome(DonorQuery(section_key="mordy", country=gb), "ok"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "меджик і морди британія трафік 100")

        combined = " ".join(_all_texts(message))
        # У відповіді присутні ОБИДВІ бази.
        assert "Меджик" in combined and "Морди" in combined

    async def test_одна_база_лишається_однією(self, columns_config):
        """Регресія: без «обидві бази» в тексті — звичайна одно-базова картка."""
        de = country_by_code("de")
        ai = FakeAI(AIOutcome(DonorQuery(section_key="magic", country=de), "ok"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "німецькі донори")

        combined = " ".join(_all_texts(message))
        assert "ШІ зрозумів як" in combined  # одно-базова картка з підписом ШІ
        assert "Морди" not in combined  # другу базу не приплітаємо

    async def test_2x2_через_ші_обидві_бази_з_країнами(self, columns_config):
        """Пункт III: ШІ віддав мультикраїнний фільтр + «меджик і морди» в тексті →
        обидві бази, у кожної розклад по країнах."""
        gb, de = country_by_code("gb"), country_by_code("de")
        ai = FakeAI(AIOutcome(DonorQuery(section_key="magic", countries=(gb, de)), "ok"))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(message, services, state, 1, "меджик і морди британія і німеччина")

        combined = " ".join(_all_texts(message))
        assert "Меджик" in combined and "Морди" in combined
        assert "Розклад по країнах" in combined
        assert "Британія" in combined and "Німеччина" in combined


def _coverage_op(section: str, needs: dict[str, int], thresholds=(0,)) -> CoverageQuery:
    return CoverageQuery(
        section_key=section,
        needs=tuple((country_by_code(c), n) for c, n in needs.items()),
        thresholds=thresholds,
    )


class TestОпераціяCoverage:
    """Крок 2, Фаза A: операція coverage маршрутизується й рахується рушієм."""

    async def test_операція_дає_картку_покриття(self, columns_config):
        # ШІ РОЗКЛАВ запит на операцію; рушій рахує по реальних (фейкових) даних.
        # magic: de=7, gb=3 → de(потреба 2) ✅, gb(потреба 5) ❌.
        op = _coverage_op("magic", {"de": 2, "gb": 5})
        ai = FakeAI(AIOutcome(None, "operation", operation=op))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState()

        await run_ai_query(
            message, services, state, 1, "меджик треба 2 німеччини 5 британії, чого бракує"
        )

        combined = " ".join(_all_texts(message))
        assert "Зрозумів як" in combined and "покриття по країнах" in combined
        assert "покриття за потребою" in combined
        assert "✅" in combined  # Німеччина закрита
        assert "❌" in combined  # Британії бракує

    async def test_операція_скидає_стан(self, columns_config):
        op = _coverage_op("magic", {"de": 2})
        ai = FakeAI(AIOutcome(None, "operation", operation=op))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage()
        state = FakeState({"chat_history": [{"role": "user", "content": "привіт"}]})

        await run_ai_query(message, services, state, 1, "меджик треба 2 німеччини, чи закриваємо")

        assert state.current_state is None
        assert (await state.get_data()).get("chat_history") == []


class TestДівертПокриттяЗВільногоТексту:
    """Покривний запит із вільного тексту НЕ рахується хибно словником, а йде ШІ."""

    async def test_вільний_текст_diverts_у_операцію(self, columns_config):
        from app.bot.handlers.freeform import handle_free_text

        op = _coverage_op("magic", {"de": 2, "gb": 5})
        ai = FakeAI(AIOutcome(None, "operation", operation=op))
        services = make_services(columns_config, ai=ai)
        message = FakeMessage("меджик треба 2 німеччини 5 британії, скільки всього, чого бракує")
        state = FakeState()

        await handle_free_text(message, services, state)

        combined = " ".join(_all_texts(message))
        # Показано картку покриття, а НЕ хибний мультикраїнний «Розклад по країнах».
        assert "покриття за потребою" in combined
        assert "Розклад по країнах" not in combined
        assert ai.calls  # ШІ таки залучили
