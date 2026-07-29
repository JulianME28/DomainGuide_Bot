"""Надійність режиму «Індивідуальний запит»: фолбек у словник і тексти невдач.

Фаза 2. Раніше режим був LLM-only: щойно ШІ повертав порожній фільтр (а
gpt-4o-mini на питальні формулювання віддає рівно `{}`), користувач упирався
в глухий кут. Тепер, коли ШІ не дав фільтра, той самий текст пробує СЛОВНИК —
рівно як звичайний вільний текст. Глухий кут лишається, лише коли НІ ШІ, НІ
словник не зрозуміли запит.

Усе на фейкових даних і фейковому ШІ — без мережі й без ключа.
"""

from __future__ import annotations

from app.bot.context import ActionLog, BotServices
from app.bot.execution import (
    AI_EMPTY_TEXT,
    AI_LIMIT_TEXT,
    AI_UNAVAILABLE_TEXT,
    run_ai_query,
)
from app.data.repository import DonorRepository
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
