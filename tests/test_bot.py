"""Тести телеграм-частини: доступ, ліміт, клавіатури, стани, запуск."""

from __future__ import annotations

import pytest

from app.analytics.query import DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import STATUS_TEXT, execute
from app.bot.handlers import build_router
from app.bot.keyboards import (
    admin_menu,
    country_picker,
    main_menu,
    result_menu,
    section_menu,
    wizard_confirm,
    wizard_countries,
    wizard_dr,
    wizard_spam,
    wizard_traffic,
)
from app.bot.middlewares import AccessMiddleware, RateLimitMiddleware
from app.bot.states import FRESH_KEY, Wizard, query_from_state, query_to_state, summary_lines
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.settings import Settings


def make_settings(allowed=(1, 2), admins=(1,)) -> Settings:
    return Settings(
        bot_token="test",
        data_backend="sheets",
        spreadsheet_id="test",
        credentials_file="credentials.json",
        allowed_user_ids=frozenset(allowed),
        admin_user_ids=frozenset(admins),
        llm_provider="none",
        cache_ttl_seconds=1800,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
        log_level="INFO",
    )


@pytest.fixture
def services(repository, columns_config) -> BotServices:
    return BotServices(
        settings=make_settings(),
        columns=columns_config,
        repository=repository,
        action_log=ActionLog(),
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeSent:
    """Повідомлення, яке повернув answer(): show_result потім його редагує."""

    def __init__(self) -> None:
        self.edits: list[tuple] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = 1) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple] = []
        self.sents: list[FakeSent] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))
        sent = FakeSent()
        self.sents.append(sent)
        return sent


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage | None = None, user_id: int = 1) -> None:
        self.data = data
        self.message = message if message is not None else FakeMessage(user_id=user_id)
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple] = []
        self.sents: list[FakeSent] = []

    async def answer(self, text=None, show_alert: bool = False, reply_markup=None):
        # reply_markup приймаємо, бо wizard-хелпери можуть показувати крок
        # через цей самий об'єкт (у бою це робить окремий шлях для Message).
        # Повертаємо «надіслане»: show_result бачить не справжній CallbackQuery,
        # тож працює з цим об'єктом як із повідомленням (isinstance не спрацює).
        self.answers.append((text, reply_markup if reply_markup is not None else show_alert))
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


def _callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class TestЗбіркаБота:
    def test_маршрутизатор_збирається_з_правильним_порядком(self):
        """Збираємо рівно один раз: aiogram не дозволяє підключити ті самі
        обробники до другого маршрутизатора, та й у бою це робиться однократно.

        Порядок критичний: «freeform» приймає будь-який текст, тому має бути
        останнім — інакше він перехоплював би відповіді на кроках майстра.
        """
        router = build_router()

        assert router.name == "root"
        names = [child.name for child in router.sub_routers]
        assert names[-1] == "freeform"
        assert "wizard" in names
        assert names.index("wizard") < names.index("freeform")


class TestКлавіатури:
    @pytest.mark.parametrize(
        "keyboard",
        [
            main_menu(),
            main_menu(is_admin=True),
            section_menu("magic"),
            country_picker("magic"),
            result_menu("magic"),
            wizard_countries(),
            wizard_traffic(),
            wizard_dr(),
            wizard_confirm(),
            admin_menu(),
        ],
    )
    def test_callback_data_вкладається_в_ліміт(self, keyboard):
        """Telegram дозволяє не більше 64 байтів на кнопку."""
        for row in keyboard.inline_keyboard:
            for button in row:
                assert button.callback_data
                assert len(button.callback_data.encode("utf-8")) <= 64

    def test_адмін_бачить_додаткову_кнопку(self):
        plain = str(main_menu())
        with_admin = str(main_menu(is_admin=True))
        assert "admin:menu" not in plain
        assert "admin:menu" in with_admin

    def test_на_кожному_кроці_майстра_є_назад_і_скинути(self):
        """Вимога ТЗ, розділ 29."""
        for keyboard in (wizard_traffic(), wizard_dr(), wizard_confirm()):
            data = str(keyboard)
            assert "wizard:reset" in data, "має бути кнопка «Скинути»"
            assert "back" in data or "wizard:back" in data, "має бути кнопка «Назад»"

    def test_крок_називається_країна_а_не_гео(self):
        """У даних немає гео — обіцяти його в інтерфейсі не можна."""
        from app.bot.handlers.wizard import STEP_COUNTRY

        assert "КРАЇНУ" in STEP_COUNTRY
        assert "гео" not in STEP_COUNTRY.lower().replace("гео визначається", "")


class TestДоступ:
    async def test_чужого_не_пускають(self, services):
        middleware = AccessMiddleware(services)
        called = False

        async def handler(event, data):
            nonlocal called
            called = True

        await middleware(handler, object(), {"event_from_user": FakeUser(999)})
        assert not called, "стороннього не має бути пропущено далі"

    async def test_свого_пускають(self, services):
        middleware = AccessMiddleware(services)
        called = False

        async def handler(event, data):
            nonlocal called
            called = True

        await middleware(handler, object(), {"event_from_user": FakeUser(1)})
        assert called

    def test_адмін_має_і_звичайний_доступ(self):
        """Інакше вийшло б: адмін є, а користуватися ботом не може."""
        settings = make_settings(allowed=(2,), admins=(7,))
        assert settings.is_allowed(7)
        assert settings.is_admin(7)
        assert not settings.is_admin(2)

    def test_список_доступу_не_в_коді(self):
        """ID мають братися з .env, а не бути зашитими в код."""
        import inspect

        import app.bot.middlewares as middlewares

        source = inspect.getsource(middlewares)
        assert "850410806" not in source


class TestЛімітЗапитів:
    async def test_після_ліміту_запити_не_проходять(self):
        middleware = RateLimitMiddleware(limit=3, window_seconds=60)
        passed = 0

        async def handler(event, data):
            nonlocal passed
            passed += 1

        for _ in range(5):
            await middleware(handler, object(), {"event_from_user": FakeUser(1)})

        assert passed == 3, "перші три пройшли, решта — ні"

    async def test_ліміт_окремий_для_кожного(self):
        middleware = RateLimitMiddleware(limit=2, window_seconds=60)
        passed = []

        async def handler(event, data):
            passed.append(data["event_from_user"].id)

        for _ in range(3):
            await middleware(handler, object(), {"event_from_user": FakeUser(1)})
        await middleware(handler, object(), {"event_from_user": FakeUser(2)})

        assert passed.count(1) == 2
        assert passed.count(2) == 1


class TestЗберіганняЗапиту:
    def test_запит_зберігається_і_відновлюється(self):
        original = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
            dr_min=30,
            traffic_min=100,
        )
        restored = query_from_state(query_to_state(original))

        assert restored.section_key == "magic"
        assert restored.country is country_by_code("de")
        assert restored.language is language_by_code("en")
        assert restored.dr_min == 30
        assert restored.traffic_min == 100

    def test_кілька_мов_зберігаються_і_відновлюються(self):
        original = DonorQuery(
            section_key="magic",
            languages=(
                language_by_code("en"),
                language_by_code("de"),
                language_by_code("fr"),
            ),
        )

        data = query_to_state(original)
        restored = query_from_state(data)

        assert data["language_codes"] == ["en", "de", "fr"]
        assert [language.code for language in restored.languages] == ["en", "de", "fr"]

    def test_порожній_запит_теж_відновлюється(self):
        restored = query_from_state({})
        assert restored.section_key == "magic"
        assert restored.country is None

    def test_у_памʼяті_лише_прості_значення(self):
        """Стан зберігається як прості типи — інакше він не переживе перезапуск."""
        data = query_to_state(DonorQuery(section_key="magic", country=country_by_code("de")))
        for value in data.values():
            assert value is None or isinstance(value, str | int | float | list)

    def test_резюме_фільтрів(self):
        query = DonorQuery(section_key="magic", country=country_by_code("gb"), traffic_min=1)
        text = summary_lines(query, "Меджик")

        assert "Меджик" in text
        assert "Британія" in text
        assert "від 1" in text
        assert "Країна" in text, "крок називається «Країна», а не «Гео»"


class TestЖурналДій:
    def test_записує_і_віддає_найновіші_зверху(self):
        log = ActionLog(capacity=10)
        log.add(1, "перша")
        log.add(1, "друга")

        records = log.recent()
        assert records[0].action == "друга"

    def test_старі_записи_витісняються(self):
        log = ActionLog(capacity=3)
        for i in range(10):
            log.add(1, f"дія {i}")
        assert len(log) == 3


class TestВиконанняЗапиту:
    async def test_запит_виконується_наскрізно(self, services):
        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )

        assert executed.result.core.count == 7  # зона 6 + GEO 1 (німецька — спільна)
        assert executed.result.addendum.count == 2
        assert "Знайдено донорів" in executed.text

    async def test_у_тексті_немає_доменів(self, services, magic):
        """Наскрізна перевірка безпеки: від бази до готового повідомлення."""
        for code in ("de", "fr", "gb"):
            executed = await execute(
                services, DonorQuery(section_key="magic", country=country_by_code(code))
            )
            for donor in magic.donors:
                assert donor.domain not in executed.text

    async def test_у_журнал_не_потрапляють_домени(self, services, magic):
        from app.text.cards import render_summary

        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )
        services.action_log.add(1, render_summary(executed.result))

        for record in services.action_log.recent():
            for donor in magic.donors:
                assert donor.domain not in record.action

    async def test_порожня_база_не_валить_виконання(self, services):
        executed = await execute(services, DonorQuery(section_key="mordy"))
        assert executed.result.core.count == 0


class TestНаскрізнийЗапитКраїни:
    """Бот має рахувати країну трикроковою моделлю від конфігу до картки."""

    async def test_морди_без_geo_теж_працюють(self, services):
        """«Морди» колонки GEO не мають — водоспад працює на двох кроках."""
        executed = await execute(
            services, DonorQuery(section_key="mordy", country=country_by_code("de"))
        )
        # У базовій фікстурі «Морди» порожні — 0, але без падіння.
        assert executed.result.available

    async def test_наскрізно_меджик(self, services):
        """Конфіг → дані → запит → картка: країна визначається трьома сигналами."""
        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )

        assert executed.result.available
        assert executed.result.core.count > 0
        assert "Німеччина" in executed.text
        # Розклад складових видно в картці.
        assert ".de" in executed.text and "GEO" in executed.text

    def test_сервіси_збираються(self, services):
        assert services.section_title("magic") == "Меджик"
        assert services.section_title("невідомий") == "невідомий"

    async def test_понад_30_країн_дає_зрозуміле_повідомлення(self, services):
        """Список > 30 країн не рахуємо — чесно кажемо про це, без падіння."""
        from app.analytics.query import DonorQuery
        from app.bot.execution import show_multi_country
        from app.dictionary.countries import COUNTRIES

        many = tuple(list(COUNTRIES.values())[:31])
        message = FakeMessage()
        await show_multi_country(
            message, services, DonorQuery(section_key="magic", countries=many), 1
        )

        text, _markup = message.answers[-1]
        assert "забагато країн" in text.lower()
        assert "31" in text


class TestПідказкаПереплутаногоРежиму:
    """Ввели «.ua» в мовному режимі (або мову в країновому) — бот пояснює
    і пропонує вибір, а не віддає порожній результат."""

    async def test_зона_в_мовному_режимі_не_дає_порожнечі(self, services):
        from app.bot.handlers.sections import receive_language

        message = FakeMessage(text=".ua")
        state = FakeState({"section_key": "magic"})
        await receive_language(message, services, state)

        # Рівно одна відповідь — підказка. Ані «Рахую...», ані порожній нуль.
        assert len(message.answers) == 1
        text, markup = message.answers[0]
        assert "доменна зона" in text and "не мова" in text
        assert "Україна" in text and "українська" in text
        codes = _callback_data(markup)
        assert "q:country:magic:ua" in codes
        assert "q:lang:magic:uk" in codes

    async def test_мова_в_країновому_режимі_дає_дзеркальну_підказку(self, services):
        from app.bot.handlers.sections import receive_country

        message = FakeMessage(text="Ukrainian")
        state = FakeState({"section_key": "magic"})
        await receive_country(message, services, state)

        assert len(message.answers) == 1
        text, markup = message.answers[0]
        assert "це мова" in text and "не країна" in text
        codes = _callback_data(markup)
        assert "q:lang:magic:uk" in codes
        assert "q:country:magic:ua" in codes

    async def test_кнопка_мови_виконує_мовний_запит(self, services, monkeypatch):
        """Кнопка «українська (мова)» справді запускає мовний запит."""
        import app.bot.handlers.sections as sections

        captured = {}

        async def fake_show_result(target, svc, query, user_id):
            captured["query"] = query

        monkeypatch.setattr(sections, "show_result", fake_show_result)

        callback = FakeCallback("q:lang:magic:uk")
        state = FakeState({"section_key": "magic"})
        await sections.query_language(callback, services, state)

        assert captured["query"].language is language_by_code("uk")
        assert captured["query"].country is None
        assert captured["query"].section_key == "magic"
        assert state.current_state is None, "крок введення знято"

    async def test_кнопка_країни_виконує_країновий_запит(self, services, monkeypatch):
        """Кнопка «🇺🇦 Україна (країна)» запускає країновий запит."""
        import app.bot.handlers.sections as sections

        captured = {}

        async def fake_show_result(target, svc, query, user_id):
            captured["query"] = query

        monkeypatch.setattr(sections, "show_result", fake_show_result)

        callback = FakeCallback("q:country:magic:ua")
        state = FakeState({"section_key": "magic"})
        await sections.query_country(callback, services, state)

        assert captured["query"].country is country_by_code("ua")
        assert captured["query"].language is None
        assert state.current_state is None

    async def test_нерозпізнане_поводиться_як_раніше(self, services):
        """Незрозуміле введення в мовному режимі — старе повідомлення, без підказки."""
        from app.bot.handlers.sections import receive_language

        message = FakeMessage(text="абракадабра")
        state = FakeState({"section_key": "magic"})
        await receive_language(message, services, state)

        text, _markup = message.answers[0]
        assert "Не впізнав мову" in text

    def test_кнопки_підказки_вкладаються_в_ліміт(self):
        """Callback-дані підказки теж мають лишатися в межах 64 байтів."""
        from app.bot.keyboards import cross_mode_keyboard
        from app.dictionary.resolver import hint_for_country_mode, hint_for_language_mode

        markups = (
            cross_mode_keyboard("magic", hint_for_language_mode(".ua"), mode="language"),
            cross_mode_keyboard("mordy", hint_for_country_mode("німецькою"), mode="country"),
        )
        for markup in markups:
            for code in _callback_data(markup):
                assert len(code.encode("utf-8")) <= 64


class TestМайстерЗаспамленість:
    """Крок майстра «Заспамленість» (стовпець G) — лише для «Морд». Окремого
    кроку «вихідні» немає: стовпець F числом не фільтрується."""

    async def test_крок_заспамленості_для_мордів(self, services):
        """Після DR «Морди» йдуть одразу на крок заспамленості."""
        from app.bot.handlers.wizard import _after_dr

        state = FakeState({"section_key": "mordy"})
        await _after_dr(FakeMessage(), services, state)
        assert state.current_state == Wizard.spam

    async def test_кроку_немає_для_меджика(self, services):
        """У «Меджика» цієї колонки немає — після DR одразу резюме."""
        from app.bot.handlers.wizard import _after_dr

        state = FakeState({"section_key": "magic"})
        await _after_dr(FakeMessage(), services, state)
        assert state.current_state == Wizard.confirm


class TestМайстерGEO:
    """Крок «ГЕО (країна трафіку)» — для баз із колонкою GEO (зараз обидві)."""

    async def test_крок_гео_для_обох_баз(self, services):
        """Після країни — крок гео і для «Меджика», і для «Морд»."""
        from app.bot.handlers.wizard import _after_country

        for section_key in ("magic", "mordy"):
            state = FakeState({"section_key": section_key})
            await _after_country(FakeMessage(), services, state)
            assert state.current_state == Wizard.geo, section_key

    async def test_гео_текстом_потрапляє_у_фільтр(self, services):
        """Назву країни на кроці гео записуємо як GEO-фільтр і йдемо до трафіку."""
        from app.bot.handlers.wizard import type_geo

        state = FakeState({"section_key": "magic"})
        await type_geo(FakeMessage(text="Польща"), state)
        assert state._data["geo_code"] == "pl"
        assert state.current_state == Wizard.traffic

    async def test_не_важливо_знімає_гео(self, services):
        from app.bot.handlers.wizard import skip_geo

        state = FakeState({"section_key": "magic", "geo_code": "pl"})
        await skip_geo(FakeCallback("wizard:geo:any"), state)
        assert state._data["geo_code"] is None
        assert state.current_state == Wizard.traffic

    async def test_гео_у_резюме_з_кнопкою_прибрати(self, services):
        """GEO-фільтр видно в резюме, і його можна прибрати кнопкою."""
        from app.bot.handlers.wizard import _goto_confirm

        message = FakeMessage()
        state = FakeState({"section_key": "magic", "geo_code": "pl", FRESH_KEY: []})
        await _goto_confirm(message, services, state)
        text, markup = message.answers[-1]
        assert "Гео (країна трафіку)" in text and "Польща" in text
        assert "wizard:drop:geo" in _callback_data(markup)

    async def test_прибрати_гео_знімає_лише_гео(self, services):
        from app.bot.handlers.wizard import drop_dimension

        state = FakeState({"section_key": "magic", "geo_code": "pl", "dr_min": 30, FRESH_KEY: []})
        await drop_dimension(FakeCallback("wizard:drop:geo"), services, state)
        assert state._data["geo_code"] is None
        assert state._data["dr_min"] == 30, "DR чіпати не мали"

    async def test_назад_із_трафіку_веде_на_гео(self, services):
        """Для баз із GEO «Назад» із трафіку веде на крок гео, не на країну."""
        from app.bot.handlers.wizard import _goto_traffic

        message = FakeMessage()
        state = FakeState({"section_key": "magic"})
        await _goto_traffic(message, state, back="geo")
        _text, markup = message.answers[-1]
        assert "wizard:back:geo" in _callback_data(markup)

    async def test_обране_значення_потрапляє_у_фільтр(self, services):
        """Кнопка «До 5» задає spam_max (менше = краще) і веде до резюме."""
        from app.bot.handlers.wizard import pick_spam

        state = FakeState({"section_key": "mordy"})
        await pick_spam(FakeCallback("wizard:spam:5"), services, state)
        assert state._data["spam_max"] == 5
        assert state._data["spam_min"] is None
        assert state.current_state == Wizard.confirm

    async def test_не_важливо_знімає_фільтр(self, services):
        from app.bot.handlers.wizard import pick_spam

        state = FakeState({"section_key": "mordy", "spam_max": 50})
        await pick_spam(FakeCallback("wizard:spam:any"), services, state)
        assert state._data["spam_min"] is None
        assert state._data["spam_max"] is None

    async def test_текстом_теж_можна(self, services):
        """Число текстом на кроці працює так само, як кнопка — це «до N»."""
        from app.bot.handlers.wizard import type_spam

        state = FakeState({"section_key": "mordy"})
        await type_spam(FakeMessage(text="20"), services, state)
        assert state._data["spam_max"] == 20
        assert state._data["spam_min"] is None
        assert state.current_state == Wizard.confirm

    def test_заспамленість_у_резюме_морд(self):
        query = DonorQuery(section_key="mordy", spam_max=5)
        text = summary_lines(query, "Морди", tracks_spam=True)
        assert "Заспамленість" in text and "≤ 5" in text
        # Окремого рядка про «вихідні» немає — F не фільтрується.
        assert "Вихідні лінки" not in text

    def test_резюме_меджика_без_рядка_заспамленості(self):
        query = DonorQuery(section_key="magic", dr_min=30)
        text = summary_lines(query, "Меджик", tracks_spam=False)
        assert "Заспамленість" not in text

    async def test_прибрати_заспамленість_працює(self, services):
        """Кнопка «❌ Прибрати заспамленість» знімає лише цей фільтр."""
        from app.bot.handlers.wizard import drop_dimension

        state = FakeState({"section_key": "mordy", "spam_max": 5, "dr_min": 30, FRESH_KEY: []})
        await drop_dimension(FakeCallback("wizard:drop:spam"), services, state)
        assert state._data["spam_max"] is None
        assert state._data["dr_min"] == 30, "DR чіпати не мали"

    async def test_навігація_назад_не_ламає_стан(self, services):
        from app.bot.handlers.wizard import go_back

        state = FakeState({"section_key": "mordy"})
        await go_back(FakeCallback("wizard:back:spam"), services, state)
        assert state.current_state == Wizard.spam

    async def test_резюме_морд_назад_веде_на_спам(self, services):
        """Останній крок перед резюме для «Морд» — заспамленість."""
        from app.bot.handlers.wizard import _goto_confirm

        message = FakeMessage()
        state = FakeState({"section_key": "mordy", FRESH_KEY: []})
        await _goto_confirm(message, services, state)
        _text, markup = message.answers[-1]
        assert "wizard:back:spam" in _callback_data(markup)

    def test_нові_клавіатури_мають_навігацію_і_ліміт(self):
        for keyboard in (wizard_spam(),):
            data = str(keyboard)
            assert "wizard:reset" in data, "має бути «Скинути»"
            assert "wizard:back" in data, "має бути «Назад»"
            for code in _callback_data(keyboard):
                assert len(code.encode("utf-8")) <= 64


class SpyAI:
    """Підміна сервісу ШІ: рахує виклики й віддає заготовлений результат.

    reason дозволяє в тесті задати причину невдачі (нерозбірне / порожнє /
    недоступне), щоб перевірити відповідні повідомлення бота."""

    def __init__(self, result=None, *, reason=None, answer="Уточніть, будь ласка, країну і мету.") -> None:
        self.result = result
        self.reason = reason or ("ok" if result is not None else "unavailable")
        self.answer = answer
        self.calls: list[tuple] = []
        self.answer_calls: list[tuple] = []

    async def try_interpret(self, user_id, text):
        return (await self.interpret_with_reason(user_id, text)).query

    async def interpret_with_reason(self, user_id, text):
        from app.llm.service import AIOutcome

        self.calls.append((user_id, text))
        return AIOutcome(self.result, self.reason)

    async def answer_question(self, user_id, text, history=()):
        self.answer_calls.append((user_id, text, list(history)))
        return self.answer


class TestШІФолбек:
    """ШІ — резерв: працює лише коли словник не зрозумів і ШІ ввімкнено."""

    def _services(self, repository, columns_config, ai):
        return BotServices(
            settings=make_settings(),
            columns=columns_config,
            repository=repository,
            action_log=ActionLog(),
            ai=ai,
        )

    async def test_зрозумілий_запит_не_кличе_ші(self, repository, columns_config):
        """Усе, що словник розібрав, лишається миттєвим — ШІ не турбуємо."""
        from app.bot.handlers.freeform import handle_free_text

        spy = SpyAI()
        services = self._services(repository, columns_config, spy)
        await handle_free_text(FakeMessage(text="Німеччина"), services, FakeState({}))

        assert spy.calls == [], "зрозумілий запит не має викликати ШІ"

    async def test_незрозумілий_без_ші_дає_підказку(self, repository, columns_config):
        from app.bot.handlers.freeform import handle_free_text

        services = self._services(repository, columns_config, None)  # ШІ вимкнено
        message = FakeMessage(text="qweasd zxcvbn")
        await handle_free_text(message, services, FakeState({}))

        # Незрозумілі слова названо прямо — не глухе мовчання й не вся база.
        answer = message.answers[-1][0]
        assert "Не зрозумів запит по" in answer
        assert "qweasd" in answer and "zxcvbn" in answer

    async def test_незрозумілий_з_ші_виконує_запит(self, repository, columns_config):
        from app.bot.handlers.freeform import handle_free_text

        spy = SpyAI(result=DonorQuery(section_key="magic", country=country_by_code("de")))
        services = self._services(repository, columns_config, spy)
        # Навмисно беззмістовний для словника текст — має піти в ШІ.
        message = FakeMessage(text="підбери щось пристойне тільки не сміття qwerty")
        await handle_free_text(message, services, FakeState({}))

        assert len(spy.calls) == 1, "незрозумілий запит має піти в ШІ"
        # show_result відпрацював: «Рахую...» → картка.
        assert message.answers and message.answers[0][0] == STATUS_TEXT


class TestІндивідуальнийЗапит:
    """Кнопка «Індивідуальний запит»: текст ЗАВЖДИ через ШІ (не словником)."""

    def _services(self, repository, columns_config, ai):
        return BotServices(
            settings=make_settings(),
            columns=columns_config,
            repository=repository,
            action_log=ActionLog(),
            ai=ai,
        )

    def test_кнопка_названа_індивідуальний_запит(self):
        from app.bot.keyboards import main_menu

        markup = main_menu()
        assert "ai:start" in _callback_data(markup)
        texts = [b.text for row in markup.inline_keyboard for b in row]
        assert any("Індивідуальний запит" in t for t in texts)
        assert not any("Майстер-запит" in t for t in texts)

    async def test_кличе_ші_навіть_коли_словник_зрозумів_би(self, repository, columns_config):
        """Головна відмінність режиму: ШІ викликається навіть на «Німеччина»."""
        from app.bot.handlers.ai import receive_ai_query

        spy = SpyAI(DonorQuery(section_key="magic", country=country_by_code("de")))
        services = self._services(repository, columns_config, spy)
        message = FakeMessage(text="Німеччина")  # словник зрозумів би й сам
        await receive_ai_query(message, services, FakeState({}))

        assert spy.calls == [(1, "Німеччина")]  # ШІ таки викликано
        card = message.sents[-1].edits[-1][0]
        assert "ШІ зрозумів як" in card

    async def test_ші_вимкнено_повідомлення_без_краху(self, repository, columns_config):
        from app.bot.handlers.ai import receive_ai_query

        services = self._services(repository, columns_config, None)  # ШІ вимкнено
        message = FakeMessage(text="німецькі мало заспамлені")
        await receive_ai_query(message, services, FakeState({}))

        assert any("вимкнено" in a[0] for a in message.answers)
        assert all(sent.edits == [] for sent in message.sents)  # картки немає

    async def test_ші_не_впорався_повідомлення(self, repository, columns_config):
        from app.bot.handlers.ai import receive_ai_query

        services = self._services(repository, columns_config, SpyAI(None))
        message = FakeMessage(text="абракадабра")  # не розбирає й словник
        await receive_ai_query(message, services, FakeState({}))

        shown = [e[0] for sent in message.sents for e in sent.edits] + [
            a[0] for a in message.answers
        ]
        # SpyAI(None) → reason=unavailable → тепер власний текст «тимчасово недоступний».
        assert any("недоступний" in t for t in shown)

    async def test_нерозбірна_відповідь_окреме_повідомлення(self, repository, columns_config):
        """Нерозбірний/обрізаний JSON → «не вдалося розібрати», не «недоступний»."""
        from app.bot.handlers.ai import receive_ai_query

        services = self._services(repository, columns_config, SpyAI(None, reason="unparsable"))
        # Текст, який НЕ розбирає й словник, — щоб дійти до повідомлення про причину
        # (розбірний словником запит тепер коректно падає в словник, а не в текст).
        message = FakeMessage(text="абракадабра")
        await receive_ai_query(message, services, FakeState({}))

        shown = [e[0] for sent in message.sents for e in sent.edits] + [
            a[0] for a in message.answers
        ]
        assert any("не вдалося розібрати" in t.lower() for t in shown)

    async def test_порожній_фільтр_просить_уточнення(self, repository, columns_config):
        """Валідний JSON без фільтра → живе уточнення, а не глухий кут."""
        from app.bot.handlers.ai import receive_ai_query

        spy = SpyAI(None, reason="empty")
        services = self._services(repository, columns_config, spy)
        message = FakeMessage(text="абракадабра")  # не розбирає й словник
        await receive_ai_query(message, services, FakeState({}))

        shown = [e[0] for sent in message.sents for e in sent.edits] + [
            a[0] for a in message.answers
        ]
        assert any("Уточніть" in t for t in shown)
        assert spy.answer_calls

    async def test_ручний_виклик_рахується_лічильником(self, repository, columns_config):
        """Ліміт/лічильник (наявні) застосовуються й до ручних викликів ШІ."""
        from app.bot.handlers.ai import receive_ai_query
        from app.llm.service import build_ai_service
        from tests.test_llm import ai_settings, anthropic_response, fake_post

        service = build_ai_service(
            ai_settings(), http_post=fake_post(anthropic_response('{"country":"de"}'))
        )
        services = self._services(repository, columns_config, service)
        assert service.calls_today == 0
        await receive_ai_query(FakeMessage(text="німецькі донори"), services, FakeState({}))
        assert service.calls_today == 1  # ручний виклик враховано


class TestУточнитиЧерезШІ:
    """Кнопка «🧠 Уточнити через ШІ» під карткою «не зрозумів»."""

    def _services(self, repository, columns_config, ai):
        return BotServices(
            settings=make_settings(),
            columns=columns_config,
            repository=repository,
            action_log=ActionLog(),
            ai=ai,
        )

    async def test_кнопка_зʼявляється_на_не_зрозумів(self, repository, columns_config):
        """«Меджик по Атлантиді» → картка «не зрозумів» із кнопкою ai:retry."""
        from app.bot.handlers.freeform import handle_free_text

        services = self._services(repository, columns_config, SpyAI(None))
        message = FakeMessage(text="Меджик по Атлантиді")
        await handle_free_text(message, services, FakeState({}))
        text, markup = message.answers[-1]
        assert "Не зрозумів запит по" in text
        assert "ai:retry" in _callback_data(markup)

    async def test_кнопки_немає_без_ші(self, repository, columns_config):
        from app.bot.handlers.freeform import handle_free_text

        services = self._services(repository, columns_config, None)  # ШІ вимкнено
        message = FakeMessage(text="Меджик по Атлантиді")
        await handle_free_text(message, services, FakeState({}))
        _text, markup = message.answers[-1]
        assert "ai:retry" not in _callback_data(markup)

    async def test_кнопки_немає_на_зрозумілому_запиті(self, repository, columns_config):
        """Повністю зрозумілий запит → у меню картки немає «уточнити через ШІ»."""
        from app.bot.handlers.freeform import handle_free_text

        services = self._services(repository, columns_config, SpyAI(None))
        message = FakeMessage(text="Меджик Німеччина")
        await handle_free_text(message, services, FakeState({}))
        _text, markup = message.sents[-1].edits[-1]
        assert "ai:retry" not in _callback_data(markup)

    async def test_ретрай_кличе_ші_на_тому_самому_тексті(self, repository, columns_config):
        from app.bot.handlers.ai import retry_via_ai

        spy = SpyAI(DonorQuery(section_key="magic", country=country_by_code("de")))
        services = self._services(repository, columns_config, spy)
        state = FakeState({"last_text": "Меджик по Атлантиді"})
        callback = FakeCallback("ai:retry")
        await retry_via_ai(callback, services, state)

        assert spy.calls == [(1, "Меджик по Атлантиді")]  # той самий текст
        # show_result бачить не справжній CallbackQuery, тож картка — на callback.
        card = callback.sents[-1].edits[-1][0]
        assert "ШІ зрозумів як" in card

    async def test_ретрай_без_збереженого_тексту(self, repository, columns_config):
        from app.bot.handlers.ai import retry_via_ai

        services = self._services(repository, columns_config, SpyAI(None))
        callback = FakeCallback("ai:retry", message=FakeMessage())
        await retry_via_ai(callback, services, FakeState({}))  # немає last_text
        assert any("Немає запиту" in (a[0] or "") for a in callback.message.answers)

    # -- Повний оригінальний текст у кнопці «Уточнити через ШІ» ----------------

    _TYPO_TEXT = "Скільки донорів Меджик з англьійською мовою"

    async def _free_text_then_state(self, repository, columns_config, ai, text):
        """Проганяє вільний текст (з'явиться картка «не зрозумів») і повертає стан
        із збереженим last_text — як перед натисканням кнопки в реальному боті."""
        from app.bot.handlers.freeform import handle_free_text

        services = self._services(repository, columns_config, ai)
        state = FakeState({})
        await handle_free_text(FakeMessage(text=text), services, state)
        return services, state

    async def test_ретрай_передає_повний_оригінальний_текст(self, repository, columns_config):
        """Кнопка передає в ШІ ПОВНИЙ оригінал, а не залишок після словника."""
        from app.bot.handlers.ai import retry_via_ai

        spy = SpyAI(None)  # ШІ «не зрозумів» → зупиняємось до show_result
        services, state = await self._free_text_then_state(
            repository, columns_config, spy, self._TYPO_TEXT
        )
        spy.calls.clear()
        await retry_via_ai(FakeCallback("ai:retry"), services, state)
        assert spy.calls == [(1, self._TYPO_TEXT)]  # повний текст, не урізаний

    async def test_ретрай_і_індивідуальний_дають_той_самий_вхід(self, repository, columns_config):
        """Обидва шляхи викликають ШІ ІДЕНТИЧНО — той самий текст на тому самому запиті."""
        from app.bot.handlers.ai import receive_ai_query, retry_via_ai

        # Шлях кнопки: вільний текст → картка → retry.
        spy_btn = SpyAI(None)
        services_btn, state = await self._free_text_then_state(
            repository, columns_config, spy_btn, self._TYPO_TEXT
        )
        spy_btn.calls.clear()
        await retry_via_ai(FakeCallback("ai:retry"), services_btn, state)

        # Шлях «Індивідуальний запит»: той самий текст напряму.
        spy_ind = SpyAI(None)
        services_ind = self._services(repository, columns_config, spy_ind)
        await receive_ai_query(FakeMessage(text=self._TYPO_TEXT), services_ind, FakeState({}))

        assert spy_btn.calls[-1] == spy_ind.calls[-1] == (1, self._TYPO_TEXT)

    async def test_ретрай_виправляє_одрук_англійською(self, repository, columns_config):
        """Одрук «англьійською» через кнопку → ШІ (мок) розбирає як мову en, і бот
        показує картку результату, а не «не зрозумів»."""
        from app.bot.handlers.ai import retry_via_ai

        # Модель виправляє одрук і повертає мову en (як має робити за промтом).
        spy = SpyAI(DonorQuery(section_key="magic", language=language_by_code("en")))
        services, state = await self._free_text_then_state(
            repository, columns_config, SpyAI(None), self._TYPO_TEXT
        )
        # Підмінюємо ШІ на «розумний» перед натисканням кнопки (текст уже в стані).
        services = self._services(repository, columns_config, spy)
        callback = FakeCallback("ai:retry")
        await retry_via_ai(callback, services, state)

        assert spy.calls == [(1, self._TYPO_TEXT)]  # ШІ отримав повний текст
        card = callback.sents[-1].edits[-1][0]
        assert "ШІ зрозумів як" in card  # це картка результату, не «не зрозумів»
        assert "англійськ" in card.lower()  # мову впізнано
