"""Фіча: розбивка по країнах на запит без переліку країн (топ-N + «показати всі»).

Перевіряємо: детекцію слова-сигналу, повний підрахунок рушієм (числа, не домени),
рендер топ-N + «Разом» + «…та ще N», чанкер повного списку і кнопки.
"""

from __future__ import annotations

from app.analytics.engine import CountryDistribution, country_distribution, run_query
from app.analytics.query import DonorQuery
from app.bot.keyboards import both_bases_menu, result_menu
from app.text.cards import render_country_breakdown, render_country_list_full
from app.text.freeform import parse_free_text


def _codes(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestСигналРозбивки:
    def test_які_країни_вмикає_розбивку(self):
        assert parse_free_text("Морди які країни є з трафіком 100+").wants_country_breakdown

    def test_по_країнах_вмикає(self):
        assert parse_free_text("Меджик по країнах трафік від 50").wants_country_breakdown

    def test_розбивка_вмикає(self):
        assert parse_free_text("Морди розбивка, трафік 100").wants_country_breakdown

    def test_звичайний_запит_не_вмикає(self):
        assert not parse_free_text("Морди трафік від 100").wants_country_breakdown

    def test_конкретна_країна_не_сигнал(self):
        # «по Британії» — це країна, а не «по країнах».
        assert not parse_free_text("Морди по Британії трафік 100").wants_country_breakdown


class TestРушійРахуєПовну:
    async def test_розподіл_рахує_всі_країни(self, mordy):
        query = DonorQuery(section_key="mordy", traffic_min=100)
        dist = country_distribution(mordy, query)
        total = run_query(mordy, query).core.count

        # «Разом» збігається зі звичайним «Знайдено донорів».
        assert dist.total == total
        # Кожен донор — або країна, або глобальна, або невідома зона (без втрат).
        assert (
            sum(c for _label, c in dist.countries) + dist.global_count + dist.unknown_count == total
        )
        # Відсортовано за спаданням.
        counts = [c for _label, c in dist.countries]
        assert counts == sorted(counts, reverse=True)

    async def test_картка_показує_топ_і_разом(self, mordy):
        query = DonorQuery(section_key="mordy", traffic_min=100)
        result = run_query(mordy, query, with_breakdowns=False)
        dist = country_distribution(mordy, query)

        text = render_country_breakdown(result, dist, top_n=8)
        assert "Розбивка по країнах" in text
        assert "Разом донорів" in text
        if dist.country_count > 8:
            assert "та ще" in text  # хвіст згорнуто


class TestПовнийСписокІЧанкер:
    def _dist(self, n: int) -> CountryDistribution:
        countries = tuple((f"🏳 Країна{i}", n - i) for i in range(n))
        return CountryDistribution(
            countries=countries,
            global_count=5,
            unknown_count=2,
            total=sum(n - i for i in range(n)) + 7,
        )

    def test_короткий_список_одне_повідомлення(self):
        chunks = render_country_list_full(self._dist(10), title="Морди")
        assert len(chunks) == 1
        assert "Усі країни (10)" in chunks[0]

    def test_довгий_список_ділиться_на_кілька(self):
        chunks = render_country_list_full(self._dist(100), title="Меджик", char_budget=300)
        assert len(chunks) > 1  # чанкер спрацював
        # Жоден шматок не перевищує бюджет (з невеликим допуском на останній рядок).
        assert all(len(c) <= 400 for c in chunks)


class TestКнопки:
    def test_кнопка_показати_всі_під_розбивкою(self):
        markup = result_menu("mordy", has_recommendations=False, all_countries=True)
        assert "res:allcountries" in _codes(markup)

    def test_без_прапорця_кнопки_немає(self):
        markup = result_menu("mordy", has_recommendations=False)
        assert "res:allcountries" not in _codes(markup)

    def test_обидві_бази_кнопки_на_базу(self):
        markup = both_bases_menu([("magic", "Меджик"), ("mordy", "Морди")], country_breakdown=True)
        codes = _codes(markup)
        assert "res:allcountries:magic" in codes
        assert "res:allcountries:mordy" in codes
