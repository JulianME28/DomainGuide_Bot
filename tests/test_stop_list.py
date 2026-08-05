"""Добровільна перевірка результатів Мордів за окремим стоп-листом."""

from app.analytics.engine import audit_stop_list, run_query
from app.analytics.query import DonorQuery
from app.bot.keyboards import both_bases_menu, result_menu
from app.bot.states import query_from_state, query_to_state
from app.data.repository import DonorRepository
from app.dictionary.countries import country_by_code
from tests.fixtures.fake_data import FakeReader, mordy_rows


def _codes(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class TestStopAudit:
    def test_вилучає_лише_збіги_з_результату(self, mordy):
        query = DonorQuery(section_key="mordy", traffic_min=300)
        before = run_query(mordy, query).core.count
        report = audit_stop_list(mordy, query, frozenset({"https://www.m1.de/path", "m6.pl"}))

        # Стоп-лист у бойовому сховищі нормалізується до аудиту; тут передаємо
        # вже канонічні значення, як їх і отримує аналітика.
        canonical = audit_stop_list(mordy, query, frozenset({"m1.de", "m6.pl"}))
        assert report.before == before
        assert report.stopped == 0
        assert canonical.before == before
        assert canonical.stopped == 1
        assert canonical.allowed == before - 1

    def test_мультикраїнний_запит_не_перетворюється_на_всю_базу(self, mordy):
        query = DonorQuery(
            section_key="mordy",
            countries=(country_by_code("de"), country_by_code("gb")),
        )
        report = audit_stop_list(mordy, query, frozenset({"m1.de", "glob.com", "m6.pl"}))
        assert report.before == 5
        assert report.stopped == 1
        assert report.allowed == 4


class TestStopStorage:
    async def test_читає_нормалізує_і_кешує(self, columns_config):
        reader = FakeReader(
            {"mordy": mordy_rows()},
            domain_lists={"Стоп Морди": ["https://WWW.M1.DE/path", "m1.de.", "", "M6.PL"]},
        )
        repository = DonorRepository(reader, columns_config, ttl_seconds=1800)

        first = await repository.get_stop_domains()
        second = await repository.get_stop_domains()

        assert first.available
        assert first.domains == frozenset({"m1.de", "m6.pl"})
        assert second is first
        assert reader.calls.count("Стоп Морди") == 1


class TestStopButtons:
    def test_кнопка_є_лише_для_мордів(self):
        assert "res:stop" in _codes(result_menu("mordy"))
        assert "res:stop" not in _codes(result_menu("magic"))

    def test_кнопка_є_у_зведенні_з_мордами(self):
        markup = both_bases_menu([("magic", "Меджик"), ("mordy", "Морди")])
        assert "res:stop" in _codes(markup)

    def test_стан_зберігає_список_країн_для_перевірки(self):
        query = DonorQuery(
            section_key="mordy",
            countries=(country_by_code("de"), country_by_code("ca")),
        )
        restored = query_from_state(query_to_state(query))
        assert [country.code for country in restored.countries] == ["de", "ca"]
