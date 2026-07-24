"""Тести: фільтр по виміру, якого база не має, не мовчить, а попереджає.

Проблема з реальної роботи: «незаспамлені Меджик» повертало всю базу без
пояснення, бо в «Меджику» немає колонок заспамленості — фільтр мовчки
відкидався. Тепер картка про це чесно каже й пропонує перейти в «Морди».
"""

from __future__ import annotations

from app.analytics.engine import run_query, unsupported_dimensions
from app.analytics.query import Dimension, DonorQuery
from app.text.cards import render_result, render_unsupported_note
from app.text.freeform import parse_free_text


class TestРозборПрикметника:
    def test_незаспамлені_це_заспамленість_нуль(self):
        query = parse_free_text("незаспамлені Меджик").query
        assert query.spam_max == 0
        assert query.spam_min is None

    def test_заспамлені_це_більше_нуля(self):
        query = parse_free_text("заспамлені донори").query
        assert query.spam_min == 1
        assert query.spam_max is None

    def test_іменник_заспамленість_лишається_числовим(self):
        """«заспамленість від 40» — числовий вимір, прикметник його не чіпає."""
        query = parse_free_text("заспамленість від 40").query
        assert query.spam_min == 40

    def test_прикметник_комбінується(self):
        query = parse_free_text("незаспамлені, DR від 30").query
        assert query.spam_max == 0
        assert query.dr_min == 30


class TestВідкинутіВиміри:
    async def test_перелік_відкинутих(self, magic):
        """У «Меджику» немає спаму/вихідних — обидва в списку відкинутих."""
        query = DonorQuery(section_key="magic", spam_max=0, outlinks_max=20)
        dropped = unsupported_dimensions(magic, query)
        assert dropped == {Dimension.SPAM, Dimension.OUTLINKS}

    async def test_результат_несе_відкинуті(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic", spam_max=0))
        assert Dimension.SPAM in result.dropped_dimensions
        # Фільтр справді знято: повернулася вся база, а не нуль.
        assert result.core.count == magic.count

    async def test_база_з_виміром_нічого_не_відкидає(self, mordy):
        """У «Мордах» заспамленість Є — нічого не відкидається."""
        result = run_query(mordy, DonorQuery(section_key="mordy", spam_max=0))
        assert result.dropped_dimensions == frozenset()

    async def test_geo_немає_колонки_відкидається(self):
        """Синтетична база без GEO: фільтр гео відкидається."""
        from app.data.models import Dataset, Donor
        from app.dictionary.countries import country_by_code

        dataset = Dataset(
            "magic",
            "Тест",
            "Тест",
            (Donor("a.com", ".com", "english", None, None),),
            0.0,
            tracks_geo=False,
        )
        dropped = unsupported_dimensions(
            dataset, DonorQuery(section_key="magic", geo=country_by_code("pl"))
        )
        assert dropped == {Dimension.GEO}


class TestПопередженняВКартці:
    async def test_попереджувальний_рядок_є(self, magic):
        card = render_result(
            run_query(magic, DonorQuery(section_key="magic", spam_max=0)),
            dropped_alt_base="Морди",
        )
        assert "не застосовано" in card
        assert "заспамленості" in card
        assert "у базі Меджик немає таких даних" in card
        assert "лише в базі Морди" in card

    async def test_кілька_відкинутих_разом(self, magic):
        card = render_result(
            run_query(magic, DonorQuery(section_key="magic", spam_max=0, outlinks_max=20)),
            dropped_alt_base="Морди",
        )
        # Один рядок перелічує обидва виміри.
        assert "вихідних лінках й заспамленості" in card
        assert card.count("не застосовано") == 1

    async def test_у_базі_з_виміром_попередження_немає(self, mordy):
        card = render_result(run_query(mordy, DonorQuery(section_key="mordy", spam_max=0)))
        assert "не застосовано" not in card

    def test_без_альтернативної_бази_без_другого_речення(self):
        note = render_unsupported_note(frozenset({Dimension.SPAM}), "Меджик", alt_base=None)
        assert "не застосовано" in note
        assert "лише в базі" not in note

    def test_немає_відкинутих_немає_рядка(self):
        assert render_unsupported_note(frozenset(), "Меджик", "Морди") is None


class TestКнопкаПереходу:
    def test_кнопка_є_коли_вимір_є_в_іншій_базі(self):
        from app.bot.keyboards import result_menu

        def codes(markup):
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        with_alt = result_menu("magic", run_in=("mordy", "Морди"))
        assert "res:runin:mordy" in codes(with_alt)
        assert "res:runin:mordy" not in codes(result_menu("magic"))

    async def test_alt_base_знаходить_морди(self, magic, columns_config):
        """Для фільтра заспамленості в «Меджику» альтернатива — «Морди»."""
        from app.bot.context import ActionLog, BotServices
        from app.bot.execution import _alt_base_for
        from app.settings import Settings

        settings = Settings(
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
        services = BotServices(
            settings=settings, columns=columns_config, repository=None, action_log=ActionLog()
        )
        result = run_query(magic, DonorQuery(section_key="magic", spam_max=0))
        assert _alt_base_for(services, result) == ("mordy", "Морди")

    async def test_немає_кнопки_коли_нічого_не_відкинуто(self, mordy, columns_config):
        from app.bot.context import ActionLog, BotServices
        from app.bot.execution import _alt_base_for
        from app.settings import Settings

        settings = Settings(
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
        services = BotServices(
            settings=settings, columns=columns_config, repository=None, action_log=ActionLog()
        )
        result = run_query(mordy, DonorQuery(section_key="mordy", spam_max=0))
        assert _alt_base_for(services, result) is None
