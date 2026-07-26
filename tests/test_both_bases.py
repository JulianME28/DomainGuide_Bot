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
        # До блоку Морд (ділимо по заголовку блоку, а не по слову «Морди»,
        # яке тепер трапляється й у підсумковому рядку вгорі).
        assert "не застосовано" in text.split("🗂 <b>Морди</b>")[0]

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


class TestПорожнійЗапит:
    """Порожній запит без бази не вивалює базу, а підказує, що вказати."""

    async def _run(self, services, text):
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text=text)
        await handle_free_text(message, services, FakeState({}))
        return message

    @pytest.mark.parametrize("text", ["разом", "всього", "по обох базах", "обидві бази"])
    async def test_порожній_дає_підказку_а_не_базу(self, both_services, text):
        from app.text.freeform import EMPTY_QUERY_HINT

        message = await self._run(both_services, text)
        # Показано підказку і НЕ пораховано жодної картки: картка з'являється
        # через status.edit_text, тож у жодного «надісланого» немає edits.
        assert any(a[0] == EMPTY_QUERY_HINT for a in message.answers)
        assert all(sent.edits == [] for sent in message.sents)

    async def test_донори_теж_не_вивалює(self, both_services):
        """«донори» без параметрів → підказка (через уточнення), не вся база."""
        message = await self._run(both_services, "донори")
        assert message.answers
        assert all(sent.edits == [] for sent in message.sents)  # картки немає

    async def test_з_фільтром_виконується(self, both_services):
        """Щойно є хоч один фільтр — виконуємо як звичайно (зведення по базах)."""
        message = await self._run(both_services, "трафік від 1")
        text, _markup = _shown(message)
        assert text.count("Знайдено донорів") == 2  # база показана


class TestПерелікБазІПідсумок:
    """Перелік баз («(Меджик + Морди)», «в обох базах») і підсумок угорі."""

    async def test_перелік_через_плюс_дає_зведення_з_підсумком(self, both_services):
        """«(Меджик + Морди)» → два блоки + підсумок «Загалом … по двох базах»."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)
        assert "📦 <b>Загалом:" in text
        assert "по двох базах" in text

    async def test_підсумок_стоїть_вгорі_а_не_внизу(self, both_services):
        """Підсумок — у шапці (перед блоками баз) і НЕ дублюється внизу."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert text.count("Загалом:") == 1  # рівно один раз
        # Підсумок — раніше за перший блок бази.
        assert text.index("Загалом:") < text.index("Знайдено донорів")

    async def test_підсумок_є_в_будь_якому_зведенні(self, both_services):
        """Навіть без слова «всього» зведення показує загальне число."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Німеччина в обох базах")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "по двох базах" in text  # підсумок є, хоч «всього» не казали

    async def test_підсумок_є_і_без_переліку_баз(self, both_services):
        """Звичайний запит без бази («Нова Зеландія») теж дає підсумок угорі."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Нова Зеландія")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "по двох базах" in text

    async def test_підсумок_рахує_унікальні_а_не_суму(self, both_services):
        """Разом ≤ проста сума блоків: спільний домен рахується один раз."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        # У фікстурі uk1.co.uk є і в «Меджику», і в «Мордах» — отже перетин є.
        assert "є в обох базах" in text

    async def test_рядок_перетину_відсутній_коли_перетину_немає(self, empty_mordy_services):
        """Морди порожні → спільних доменів нема → дужки про перетин немає."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, empty_mordy_services, FakeState({}))
        text, _markup = _shown(message)
        assert "по двох базах" in text
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
        assert "по двох базах" not in text
