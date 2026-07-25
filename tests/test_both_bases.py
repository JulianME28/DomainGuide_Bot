"""Зведений показ по ОБОХ базах, коли базу в запиті не назвали.

Раніше запит без назви бази виконувався в одній (останній) базі. Тепер новий
вільний запит без явної бази перевіряє і «Меджик», і «Морди» одним компактним
повідомленням, а повну картку кожної бази відкриває кнопка «Детально по …».
"""

from __future__ import annotations

import pytest

from app.analytics.query import DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import show_both_bases
from app.data.repository import DonorRepository
from app.dictionary.countries import country_by_code
from app.settings import Settings
from app.text.freeform import parse_free_text
from tests.fixtures.fake_data import FakeReader, empty_rows, magic_rows, mordy_rows


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
    def __init__(self) -> None:
        self.edits: list[tuple] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


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


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage | None = None) -> None:
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = FakeUser()
        self.answers: list[tuple] = []
        self.sents: list[FakeSent] = []

    async def answer(self, text=None, show_alert: bool = False, reply_markup=None):
        # show_result бачить не справжній CallbackQuery, тож працює з цим об'єктом
        # як із повідомленням — тому answer має повертати «надіслане».
        self.answers.append((text, show_alert))
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


def _codes(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _shown(message: FakeMessage) -> tuple[str, object]:
    """Фінальний текст і клавіатура (status.edit_text)."""
    return message.sents[-1].edits[-1]


def _services(columns_config, *, mordy):
    repo = DonorRepository(
        FakeReader({"magic": magic_rows(), "mordy": mordy}), columns_config, ttl_seconds=1800
    )
    return BotServices(
        settings=make_settings(), columns=columns_config, repository=repo, action_log=ActionLog()
    )


@pytest.fixture
def both_services(columns_config):
    """Обидві бази з даними."""
    return _services(columns_config, mordy=mordy_rows())


@pytest.fixture
def empty_mordy_services(columns_config):
    """Меджик із даними, Морди порожні."""
    return _services(columns_config, mordy=empty_rows())


class TestТригер:
    def test_база_не_названа(self):
        assert parse_free_text("Нова Зеландія").section_named is False

    def test_база_названа(self):
        assert parse_free_text("Морди Нова Зеландія").section_named is True


class TestЗведенийПоказ:
    async def test_два_блоки_і_кнопки_детально(self, both_services):
        message = FakeMessage()
        await show_both_bases(message, both_services, DonorQuery(section_key="magic"), 1)
        text, markup = _shown(message)

        assert "Меджик" in text and "Морди" in text
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)
        assert "res:detail:mordy" in _codes(markup)

    async def test_нульова_база_все_одно_показана(self, empty_mordy_services):
        message = FakeMessage()
        await show_both_bases(message, empty_mordy_services, DonorQuery(section_key="magic"), 1)
        text, _markup = _shown(message)
        # Блок «Морд» присутній навіть із нулем — щоб було видно, що перевірено.
        assert "Морди" in text
        assert text.count("Знайдено донорів") == 2
        assert "донорів не знайдено" in text  # блок Морд

    async def test_попередження_лише_в_блоці_своєї_бази(self, both_services):
        """Заспамленість: у «Меджику» відкинуто (попередження), у «Мордах» — ні."""
        message = FakeMessage()
        await show_both_bases(
            message, both_services, DonorQuery(section_key="magic", spam_max=0), 1
        )
        text, _markup = _shown(message)
        assert text.count("не застосовано") == 1  # лише в блоці Меджика
        assert "не застосовано" in text.split("Морди")[0]  # до блоку Морд

    async def test_компактно_без_великих_блоків(self, both_services):
        message = FakeMessage()
        await show_both_bases(
            message,
            both_services,
            DonorQuery(section_key="magic", country=country_by_code("de")),
            1,
        )
        text, _markup = _shown(message)
        assert "Суміжні країни" not in text
        assert "Ядро" not in text
        assert "на нейтральних зонах" not in text  # мовні пропозиції — не у зведенні

    async def test_у_зведенні_немає_доменів(self, both_services, magic):
        message = FakeMessage()
        await show_both_bases(message, both_services, DonorQuery(section_key="magic"), 1)
        text, _markup = _shown(message)
        for donor in magic.donors:
            assert donor.domain not in text


class TestХендлер:
    async def test_запит_без_бази_дає_два_блоки(self, both_services):
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Нова Зеландія")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)

    async def test_запит_із_базою_дає_один_блок(self, both_services):
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Морди Нова Зеландія")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 1
        assert "res:filter" in _codes(markup)  # повна картка, а не зведення
        assert not any("res:detail" in c for c in _codes(markup))

    async def test_список_країн_не_роздвоюється(self, both_services):
        """Мультизапит без бази лишається своїм виглядом, а не двома блоками."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Німеччина Франція")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert "Розклад по країнах" in text
        assert text.count("Знайдено донорів") == 0  # це мультикартка, не зведення

    async def test_прохання_без_бази_теж_два_блоки(self, both_services):
        """Маркер-прохання без бази НЕ робить винятку — те саме зведення."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Нова Зеландія; якщо мало — англомовні альтернативи")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)
        assert "res:detail:mordy" in _codes(markup)
        # Пояснення про прохання — рівно ОДИН раз (у шапці, не в кожному блоці).
        assert text.count("зрозумів як прохання показати схожі варіанти") == 1
        assert "у детальній картці" in text  # підказка про суміжні

    async def test_прохання_з_явною_базою_повна_картка(self, both_services):
        """Явна база + маркер → одна повна картка з поясненням, як раніше."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Морди Нова Зеландія; якщо мало — альтернативи")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 1  # одна повна картка
        assert "res:filter" in _codes(markup)
        assert not any("res:detail" in c for c in _codes(markup))
        assert "зрозумів як прохання показати схожі варіанти" in text

    async def test_детально_відкриває_повну_картку(self, both_services):
        """Кнопка «Детально по Морди» дає повну картку Морд зі збереженим фільтром."""
        from app.bot.handlers.sections import show_base_detail

        state = FakeState({"section_key": "magic", "country_code": "de"})
        callback = FakeCallback("res:detail:mordy")
        await show_base_detail(callback, both_services, state)

        text, markup = _shown(callback)
        assert "Морди" in text
        assert "res:filter" in _codes(markup)  # повна картка з додатковими кнопками
        assert state._data["section_key"] == "mordy"

    async def test_продовження_не_перемикається_на_дві(self, both_services):
        """Продовження (детально по одній базі) лишається однією базою, не зведенням."""
        from app.bot.handlers.sections import show_base_detail

        callback = FakeCallback("res:detail:magic")
        state = FakeState({"section_key": "magic", "dr_min": 30})
        await show_base_detail(callback, both_services, state)

        text, markup = _shown(callback)
        assert text.count("Знайдено донорів") == 1  # одна база
        assert not any("res:detail" in c for c in _codes(markup))
        assert state._data["dr_min"] == 30  # фільтр збережено


class TestПерелікБазІПідсумок:
    """Перелік баз («(Меджик + Морди)», «в обох базах») і підсумковий рядок."""

    async def test_перелік_через_плюс_дає_зведення_з_підсумком(self, both_services):
        """«(Меджик + Морди)» → два блоки + рядок про унікальних донорів разом."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)
        assert "унікальних донорів" in text  # підсумковий рядок
        assert "📦 <b>Разом:" in text

    async def test_в_обох_базах_дає_зведення_без_підсумку(self, both_services):
        """«в обох базах» без слова-підсумку → зведення, але рядка «Разом» немає."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Німеччина в обох базах")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "унікальних донорів" not in text  # підсумку не просили

    async def test_скільки_всього_додає_підсумок(self, both_services):
        """«Скільки всього … по Німеччині» (без бази) → зведення + підсумок."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів по Німеччині?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "унікальних донорів" in text

    async def test_підсумок_рахує_унікальні_а_не_суму(self, both_services):
        """Разом ≤ проста сума блоків: спільний домен рахується один раз."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        # У фікстурі uk1.co.uk є і в «Меджику», і в «Мордах» — отже перетин є.
        assert "є в обох базах" in text

    async def test_рядок_перетину_відсутній_коли_перетину_немає(self, empty_mordy_services):
        """Морди порожні → спільних доменів нема → рядка про перетин немає."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, empty_mordy_services, FakeState({}))
        text, _markup = _shown(message)
        assert "унікальних донорів" in text
        assert "є в обох базах" not in text

    async def test_у_підсумку_немає_доменів(self, both_services, magic):
        """Навіть із підсумком жоден домен не потрапляє у відповідь."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        for donor in magic.donors:
            if donor.domain:
                assert donor.domain not in text

    async def test_явна_одна_база_без_підсумку(self, both_services):
        """«Морди Німеччина» — одна картка, підсумку по базах немає."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Морди Німеччина")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 1
        assert "res:filter" in _codes(markup)
        assert "унікальних донорів" not in text
