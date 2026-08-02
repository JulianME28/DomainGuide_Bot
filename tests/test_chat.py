"""Фаза 3 — розмовна смуга: ізоляція, відмова, маршрутизація.

ГОЛОВНА гарантія безпеки — ast-тест ізоляції (модуль розмови фізично не імпортує
шар даних, тож домени недосяжні). Промт-відмова «не віддаю домени» — це лише UX,
НЕ механізм безпеки; так вона й перевіряється (окремим, м'якшим тестом).
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.llm.chat as chat_module
from app.analytics.query import DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import (
    AI_CHAT_FAILED_TEXT,
    AI_EMPTY_TEXT,
    CHAT_HISTORY_MESSAGES,
    chat_reply,
    reset_chat_history,
    run_ai_query,
)
from app.data.repository import DonorRepository
from app.dictionary.countries import country_by_code
from app.llm.chat import CHAT_SYSTEM_PROMPT, ConversationResponder
from app.llm.interpreter import read_intent
from app.llm.service import AIOutcome
from app.settings import Settings
from tests.fixtures.fake_data import FakeReader, empty_rows, magic_rows

# Модулі/імена шару даних, яких розмовна смуга НЕ сміє торкатися.
FORBIDDEN_IMPORTS = ("app.data", "app.analytics", "sheets", "repository", "columns")
FORBIDDEN_NAMES = ("QueryResult", "Dataset", "Donor", "DonorRepository")


# ---------------------------------------------------------------------------
# (а) ГОЛОВНЕ: ast-ізоляція модуля розмови від шару даних
# ---------------------------------------------------------------------------


class TestІзоляціяВідДаних:
    def _imports(self) -> tuple[list[str], list[str]]:
        """(модулі, імена) з усіх import у app/llm/chat.py."""
        source = Path(chat_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules: list[str] = []
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                names += [alias.name for alias in node.names]
        return modules, names

    def test_не_імпортує_шар_даних(self):
        modules, names = self._imports()
        for module in modules:
            for bad in FORBIDDEN_IMPORTS:
                assert bad not in module, f"chat.py імпортує заборонений модуль: {module}"
        for name in names:
            assert name not in FORBIDDEN_NAMES, f"chat.py імпортує заборонене ім'я: {name}"

    def test_імпортує_лише_провайдера_й_логи(self):
        """Позитивна перевірка: лише app.llm.provider і logging_setup."""
        modules, _names = self._imports()
        app_imports = [m for m in modules if m.startswith("app.")]
        assert set(app_imports) <= {"app.llm.provider", "app.logging_setup"}


# ---------------------------------------------------------------------------
# (б) Відмова від доменів — це UX (промт), НЕ механізм безпеки
# ---------------------------------------------------------------------------


class TestВідмоваUX:
    def test_промт_містить_відмову_й_агрегати(self):
        lowered = CHAT_SYSTEM_PROMPT.lower()
        assert "відмов" in lowered  # інструкція відмовляти
        assert "домен" in lowered  # саме про домени
        assert "агрегован" in lowered  # «лише агреговані показники»

    def test_промт_каже_що_даних_не_бачить(self):
        assert "не бачиш" in CHAT_SYSTEM_PROMPT.lower() or "не маєш" in CHAT_SYSTEM_PROMPT.lower()

    async def test_responder_повертає_текст_від_провайдера(self):
        class FakeProvider:
            def __init__(self):
                self.calls = []

            async def complete_chat(self, system, messages):
                self.calls.append((system, messages))
                return "Заспамленість — це кількість заспамлених вихідних лінків."

        provider = FakeProvider()
        responder = ConversationResponder(provider)
        answer = await responder.answer([], "що таке заспамленість?")
        assert "заспамлен" in answer.lower()
        # Провайдер отримав САМЕ розмовний промт (data-free), а не фільтровий.
        assert provider.calls[0][0] is CHAT_SYSTEM_PROMPT

    async def test_responder_несе_історію_діалогу(self):
        """Багатоходовість: попередні репліки йдуть у виклик разом із питанням."""

        class FakeProvider:
            def __init__(self):
                self.messages = None

            async def complete_chat(self, system, messages):
                self.messages = messages
                return "Порада…"

        provider = FakeProvider()
        history = [
            {"role": "user", "content": "порадь гео"},
            {"role": "assistant", "content": "США і Британія популярні"},
        ]
        await ConversationResponder(provider).answer(history, "а Німеччина?")
        # У виклик пішла історія + нове питання останнім.
        assert provider.messages[0]["content"] == "порадь гео"
        assert provider.messages[-1] == {"role": "user", "content": "а Німеччина?"}


# ---------------------------------------------------------------------------
# read_intent — whitelist наміру
# ---------------------------------------------------------------------------


class TestReadIntent:
    def test_question_проходить(self):
        assert read_intent({"intent": "question"}) == "question"

    def test_filter_за_замовчуванням(self):
        assert read_intent({}) == "filter"
        assert read_intent({"intent": "filter"}) == "filter"

    def test_невідоме_значення_зводиться_до_filter(self):
        # Безпечний дефолт: збій класифікації НЕ веде в розмову.
        assert read_intent({"intent": "chat"}) == "filter"
        assert read_intent({"intent": 123}) == "filter"


# ---------------------------------------------------------------------------
# (в)+(г) Маршрутизація: питання → чат; фільтр завжди донор; регресії
# ---------------------------------------------------------------------------


def _settings() -> Settings:
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
    def __init__(self) -> None:
        self.edits: list[tuple] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def delete(self):
        pass


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

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)
        return dict(self._data)

    async def set_state(self, state=None):
        pass


class FakeAI:
    """Фейковий AIService: віддає заготовлений outcome і рахує виклики чату."""

    def __init__(self, outcome: AIOutcome, *, answer: str | None = "Це пояснення поняття.") -> None:
        self._outcome = outcome
        self._answer = answer
        self.answer_calls: list[tuple[int, str]] = []

    async def interpret_with_reason(self, user_id: int, text: str) -> AIOutcome:
        return self._outcome

    async def try_interpret(self, user_id: int, text: str):
        return self._outcome.query

    async def answer_question(self, user_id: int, text: str, history=()) -> str | None:
        self.answer_calls.append((user_id, text, list(history)))
        return self._answer


def _services(columns_config, ai: FakeAI) -> BotServices:
    repo = DonorRepository(
        FakeReader({"magic": magic_rows(), "mordy": empty_rows()}),
        columns_config,
        ttl_seconds=1800,
    )
    return BotServices(
        settings=_settings(),
        columns=columns_config,
        repository=repo,
        action_log=ActionLog(),
        ai=ai,
    )


def _all_texts(message: FakeMessage) -> list[str]:
    texts = [t for t, _m in message.answers]
    for sent in message.sents:
        texts.extend(t for t, _m in sent.edits)
    return texts


class TestМаршрутизаціяЧат:
    async def test_питання_йде_в_розмовну_смугу(self, columns_config):
        ai = FakeAI(AIOutcome(None, "empty", intent="question"), answer="Заспамленість — це …")
        services = _services(columns_config, ai)
        message = FakeMessage()

        await run_ai_query(message, services, FakeState(), 1, "що таке заспамленість?")

        assert ai.answer_calls  # розмовну смугу викликано
        combined = " ".join(_all_texts(message))
        assert "Заспамленість" in combined
        assert AI_EMPTY_TEXT not in combined  # не глухий кут

    async def test_валідний_фільтр_завжди_донор_навіть_при_intent_question(self, columns_config):
        """Безпечний пріоритет: є фільтр → донор-запит, хай intent=question."""
        query = DonorQuery(section_key="magic", country=country_by_code("de"))
        ai = FakeAI(AIOutcome(query, "ok", intent="question"))
        services = _services(columns_config, ai)
        message = FakeMessage()

        await run_ai_query(message, services, FakeState(), 1, "німеччина")

        assert not ai.answer_calls  # у чат НЕ пішло
        combined = " ".join(_all_texts(message))
        assert "ШІ зрозумів як" in combined  # донор-картка

    async def test_порожній_фільтр_не_питання_йде_в_словник(self, columns_config):
        """intent=filter + порожньо → словниковий фолбек (не чат)."""
        ai = FakeAI(AIOutcome(None, "empty", intent="filter"))
        services = _services(columns_config, ai)
        message = FakeMessage()

        # Текст, що словник РОЗБЕРЕ (Морди/США) → фолбек дасть картку, не чат.
        await run_ai_query(message, services, FakeState(), 1, "Морди США")

        assert not ai.answer_calls
        combined = " ".join(_all_texts(message))
        assert "Морди" in combined

    async def test_розмовна_смуга_ліміт_ввічливе_повідомлення(self, columns_config):
        """answer_question вернув None (ліміт/збій) → ввічливий текст, без падіння."""
        ai = FakeAI(AIOutcome(None, "empty", intent="question"), answer=None)
        services = _services(columns_config, ai)
        message = FakeMessage()

        await run_ai_query(message, services, FakeState(), 1, "як користуватись ботом?")

        assert ai.answer_calls
        assert AI_CHAT_FAILED_TEXT in _all_texts(message)


class TestБагатоходовість:
    """Контекст розмови тримається в FSM, скидається на донор-запиті, обрізається."""

    async def test_історія_накопичується_між_ходами(self, columns_config):
        ai = FakeAI(AIOutcome(None, "empty", intent="question"), answer="США і Британія")
        services = _services(columns_config, ai)
        state = FakeState()

        await chat_reply(services, state, 1, "порадь гео")
        await chat_reply(services, state, 1, "а Німеччина?")

        # Другий виклик отримав історію першого обміну.
        _uid, _text, history2 = ai.answer_calls[1]
        contents = [m["content"] for m in history2]
        assert "порадь гео" in contents
        assert "США і Британія" in contents

    async def test_донор_запит_скидає_контекст(self, columns_config):
        ai = FakeAI(AIOutcome(None, "empty", intent="question"), answer="відповідь")
        services = _services(columns_config, ai)
        state = FakeState()

        await chat_reply(services, state, 1, "порадь гео")
        await reset_chat_history(state)  # ← між питаннями був донор-запит
        await chat_reply(services, state, 1, "нове питання")

        _uid, _text, history_last = ai.answer_calls[-1]
        assert history_last == []  # консультація й підрахунок не змішались

    async def test_історія_обрізається_до_ліміту(self, columns_config):
        ai = FakeAI(AIOutcome(None, "empty", intent="question"), answer="ok")
        services = _services(columns_config, ai)
        state = FakeState()

        for i in range(10):
            await chat_reply(services, state, 1, f"питання {i}")

        data = await state.get_data()
        assert len(data["chat_history"]) <= CHAT_HISTORY_MESSAGES


class TestПромтКонсультанта:
    def test_персона_консультанта_і_уточнення(self):
        lowered = CHAT_SYSTEM_PROMPT.lower()
        assert "консультант" in lowered
        assert "радь" in lowered  # радить стратегії
        assert "уточн" in lowered  # ставить уточнюючі питання

    def test_направляє_на_донор_запит_замість_вигаданого_числа(self):
        lowered = CHAT_SYSTEM_PROMPT.lower()
        assert "не називай число" in lowered  # не вигадує кількість
        assert "порахую" in lowered  # приклад перенаправлення на запит
