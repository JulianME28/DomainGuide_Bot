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


class TestПерелікБаз:
    """Перелік баз («(Меджик + Морди)», «в обох базах») → зведення БЕЗ «Загалом»."""

    async def test_перелік_через_плюс_дає_два_блоки(self, both_services):
        """«(Меджик + Морди)» → два блоки й кнопки «Детально», без рядка «Загалом»."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 2
        assert "res:detail:magic" in _codes(markup)
        assert "Загалом" not in text
        assert "по двох базах" not in text

    async def test_рядок_загалом_прибрано(self, both_services):
        """Питання унікальні-vs-сума відкрите → рядка «Загалом» немає взагалі."""
        from app.bot.handlers.freeform import handle_free_text

        for query in ("Німеччина в обох базах", "Нова Зеландія"):
            message = FakeMessage(text=query)
            await handle_free_text(message, both_services, FakeState({}))
            text, _markup = _shown(message)
            assert text.count("Знайдено донорів") == 2  # блоки на місці
            assert "Загалом" not in text
            assert "є в обох базах" not in text

    async def test_у_зведенні_немає_доменів(self, both_services, magic):
        """Жоден домен не потрапляє у відповідь (безпековий інваріант)."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Скільки всього донорів (Меджик + Морди)?")
        await handle_free_text(message, both_services, FakeState({}))
        text, _markup = _shown(message)
        for donor in magic.donors:
            if donor.domain:
                assert donor.domain not in text

    async def test_явна_одна_база_це_картка(self, both_services):
        """«Морди Німеччина» — одна картка, не зведення."""
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text="Морди Німеччина")
        await handle_free_text(message, both_services, FakeState({}))
        text, markup = _shown(message)
        assert text.count("Знайдено донорів") == 1
        assert "res:filter" in _codes(markup)


class TestНеЗрозумівЗапит:
    """Значущі, але нерозпізнані слова НЕ відкидаються тихо й не вивалюють базу."""

    async def _run(self, services, text):
        from app.bot.handlers.freeform import handle_free_text

        message = FakeMessage(text=text)
        await handle_free_text(message, services, FakeState({}))
        return message

    async def test_одрук_мови_дає_нуль_і_не_зрозумів(self, both_services):
        """«англьійською» → 0 + «не зрозумів по: англьійською», НЕ вся база."""
        message = await self._run(both_services, "Скільки донорів Меджик з англьійською мовою?")
        answer = message.answers[-1][0]
        assert "Знайдено донорів:</b> 0" in answer
        assert "англьійською" in answer
        assert all(sent.edits == [] for sent in message.sents)  # базу не порахували

    async def test_неіснуюча_країна_дає_нуль_і_не_зрозумів(self, both_services):
        """«Атлантида» → 0 + «не зрозумів по: Атлантида», НЕ вся база."""
        message = await self._run(both_services, "Скільки донорів Меджик по Атлантиді?")
        answer = message.answers[-1][0]
        assert "Знайдено донорів:</b> 0" in answer
        assert "атлантид" in answer.lower()
        assert all(sent.edits == [] for sent in message.sents)

    async def test_частковий_запит_виконується_з_попередженням(self, both_services):
        """Німеччина розпізнана → картка по ній, але з рядком про відкинуте."""
        message = await self._run(both_services, "Меджик Німеччина англьійською")
        text, _markup = _shown(message)
        assert "Німеччина" in text
        assert "Не зрозумів запит по" in text and "англьійською" in text

    async def test_чистий_донори_це_підказка_а_не_не_зрозумів(self, both_services):
        """Порожній «донори» → уточнення, а не «0 + не зрозумів»."""
        message = await self._run(both_services, "донори")
        answer = message.answers[-1][0]
        assert "Не зрозумів запит по" not in answer
        assert all(sent.edits == [] for sent in message.sents)

    async def test_службові_слова_не_в_не_зрозумів(self, both_services):
        """«по/та/разом» не потрапляють у перелік нерозпізнаного."""
        message = await self._run(both_services, "Меджик по та разом Атлантида")
        answer = message.answers[-1][0]
        assert "атлантида" in answer.lower()
        for junk in ("«по»", "«та»", "«разом»"):
            assert junk not in answer

    async def test_валідний_запит_без_зайвих_попереджень(self, both_services):
        """Повністю зрозумілий запит працює як раніше, без «не зрозумів»."""
        message = await self._run(both_services, "Меджик Британія трафік від 50")
        text, _markup = _shown(message)
        assert "Знайдено донорів" in text
        assert "Не зрозумів запит по" not in text


class TestДвіБазиМультикраїни:
    """Пункт III: «2 бази × кілька країн» — обидві бази, у кожної розклад по країнах."""

    async def test_2x2_обидві_бази_з_розкладом_по_країнах(self, both_services):
        message = FakeMessage()
        query = parse_free_text("меджик і морди британія і німеччина трафік від 100").query
        assert query.is_multi_country  # передумова: це мультикраїнний запит
        await show_both_bases(message, both_services, query, 1, explicit_both=True)

        text, _markup = _shown(message)
        # Обидві бази присутні.
        assert "Меджик" in text and "Морди" in text
        # Під кожною — розклад по країнах з обома країнами.
        assert text.count("Розклад по країнах") == 2
        assert "Британія" in text and "Німеччина" in text
        assert "Разом донорів" in text
        # Пояснення про ексклюзивність — РАЗ у шапці, не в кожному блоці.
        assert text.count("лише в одній країні") == 1

    async def test_2x2_попередження_про_незастосовну_заспамленість(self, both_services):
        """Спам-фільтр у 2×2: Меджик чесно каже, що заспамленість незастосовна."""
        message = FakeMessage()
        query = parse_free_text("меджик і морди британія і німеччина до 20 вихідних").query
        assert query.spam_max == 20 and query.is_multi_country
        await show_both_bases(message, both_services, query, 1, explicit_both=True)

        text, _markup = _shown(message)
        # Меджик попереджає (колонки немає), а не мовчки ігнорує — і вказує на Морди.
        assert "не застосовано" in text and "заспамленості" in text
        assert "лише в базі Морди" in text

    async def test_регресія_2_бази_одна_країна_компактний_блок(self, both_services):
        """Комбо 3 (Варіант C): дві бази + ОДНА країна — звичайний компактний блок,
        без мультикраїнного «Розкладу по країнах»."""
        message = FakeMessage()
        query = parse_free_text("меджик і морди британія трафік від 100").query
        assert not query.is_multi_country
        await show_both_bases(message, both_services, query, 1, explicit_both=True)

        text, _markup = _shown(message)
        assert "Меджик" in text and "Морди" in text
        assert "Розклад по країнах" not in text  # одна країна — без розкладу
        assert "Знайдено донорів" in text


class TestМаршрутизаціяРішенняA:
    """Рішення A: база не названа → обидві бази (і для однієї країни, і для списку)."""

    def test_названа_база_мультикраїни_це_одна_база(self):
        p = parse_free_text("меджик британія і німеччина")
        assert p.section_named and not p.both_bases  # → одна база (show_multi_country)
        assert p.query.is_multi_country

    def test_без_бази_мультикраїни_це_обидві_бази(self):
        p = parse_free_text("британія і німеччина")
        assert not p.section_named  # → обидві бази (нова інтенція A)
        assert p.query.is_multi_country

    def test_без_бази_одна_країна_це_обидві_бази(self):
        p = parse_free_text("британія")
        assert not p.section_named  # симетрично — теж обидві бази
