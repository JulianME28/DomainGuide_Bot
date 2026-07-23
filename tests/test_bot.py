"""Тести телеграм-частини: доступ, ліміт, клавіатури, стани, запуск."""

from __future__ import annotations

import pytest

from app.analytics.query import DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import execute
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
    wizard_outlinks,
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

    async def answer(self, text=None, show_alert: bool = False, reply_markup=None):
        # reply_markup приймаємо, бо wizard-хелпери можуть показувати крок
        # через цей самий об'єкт (у бою це робить окремий шлях для Message).
        self.answers.append((text, reply_markup if reply_markup is not None else show_alert))


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

        assert executed.result.core.count == 9  # трикроковий підсумок Німеччини
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


class TestМайстерВихідніІСпам:
    """Нові кроки майстра «Вихідні лінки» й «Заспамленість» — лише для баз,
    які мають ці колонки («Морди»), і з тією ж механікою, що трафік/DR."""

    async def test_кроки_зявляються_для_мордів(self, services):
        """Після DR «Морди» йдуть на крок вихідних лінків."""
        from app.bot.handlers.wizard import _after_dr

        state = FakeState({"section_key": "mordy"})
        await _after_dr(FakeMessage(), services, state)
        assert state.current_state == Wizard.outlinks

    async def test_кроків_немає_для_меджика(self, services):
        """У «Меджика» цих колонок немає — після DR одразу резюме."""
        from app.bot.handlers.wizard import _after_dr

        state = FakeState({"section_key": "magic"})
        await _after_dr(FakeMessage(), services, state)
        assert state.current_state == Wizard.confirm

    async def test_після_вихідних_іде_заспамленість(self, services):
        from app.bot.handlers.wizard import _after_outlinks

        state = FakeState({"section_key": "mordy"})
        await _after_outlinks(FakeMessage(), services, state)
        assert state.current_state == Wizard.spam

    async def test_обране_значення_потрапляє_у_фільтр(self, services):
        """Кнопка «Від 25» задає outlinks_min і веде далі на спам."""
        from app.bot.handlers.wizard import pick_outlinks, pick_spam

        state = FakeState({"section_key": "mordy"})
        await pick_outlinks(FakeCallback("wizard:outlinks:25"), services, state)
        assert state._data["outlinks_min"] == 25
        assert state.current_state == Wizard.spam

        await pick_spam(FakeCallback("wizard:spam:5"), services, state)
        assert state._data["spam_min"] == 5
        assert state.current_state == Wizard.confirm

    async def test_не_важливо_знімає_фільтр(self, services):
        from app.bot.handlers.wizard import pick_outlinks

        state = FakeState({"section_key": "mordy", "outlinks_min": 50})
        await pick_outlinks(FakeCallback("wizard:outlinks:any"), services, state)
        assert state._data["outlinks_min"] is None
        assert state._data["outlinks_max"] is None

    async def test_текстом_теж_можна(self, services):
        """Число текстом на кроці працює так само, як кнопка."""
        from app.bot.handlers.wizard import type_spam

        state = FakeState({"section_key": "mordy"})
        await type_spam(FakeMessage(text="20"), services, state)
        assert state._data["spam_min"] == 20
        assert state.current_state == Wizard.confirm

    def test_обране_значення_в_резюме_морд(self):
        query = DonorQuery(section_key="mordy", outlinks_min=25, spam_min=5)
        text = summary_lines(query, "Морди", tracks_spam=True)
        assert "Вихідні лінки" in text and "від 25" in text
        assert "Заспамленість" in text and "від 5" in text

    def test_резюме_меджика_без_нових_рядків(self):
        query = DonorQuery(section_key="magic", dr_min=30)
        text = summary_lines(query, "Меджик", tracks_spam=False)
        assert "Вихідні лінки" not in text
        assert "Заспамленість" not in text

    async def test_прибрати_вихідні_працює(self, services):
        """Кнопка «❌ Прибрати вихідні лінки» знімає лише цей фільтр."""
        from app.bot.handlers.wizard import drop_dimension

        state = FakeState(
            {"section_key": "mordy", "outlinks_min": 25, "spam_min": 5, FRESH_KEY: []}
        )
        await drop_dimension(FakeCallback("wizard:drop:outlinks"), services, state)
        assert state._data["outlinks_min"] is None
        assert state._data["spam_min"] == 5, "заспамленість чіпати не мали"

    async def test_навігація_назад_не_ламає_стан(self, services):
        from app.bot.handlers.wizard import go_back

        state = FakeState({"section_key": "mordy"})
        await go_back(FakeCallback("wizard:back:outlinks"), services, state)
        assert state.current_state == Wizard.outlinks

    async def test_резюме_морд_назад_веде_на_спам(self, services):
        """Останній крок перед резюме для «Морд» — заспамленість."""
        from app.bot.handlers.wizard import _goto_confirm

        message = FakeMessage()
        state = FakeState({"section_key": "mordy", FRESH_KEY: []})
        await _goto_confirm(message, services, state)
        _text, markup = message.answers[-1]
        assert "wizard:back:spam" in _callback_data(markup)

    def test_нові_клавіатури_мають_навігацію_і_ліміт(self):
        for keyboard in (wizard_outlinks(), wizard_spam()):
            data = str(keyboard)
            assert "wizard:reset" in data, "має бути «Скинути»"
            assert "wizard:back" in data, "має бути «Назад»"
            for code in _callback_data(keyboard):
                assert len(code.encode("utf-8")) <= 64
