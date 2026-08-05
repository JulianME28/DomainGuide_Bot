"""Група E: два реальні баги, що давали ХИБНІ числа (виправлено).

  (1) «трафік від 100 + до 5 вихідних» на Меджику → неможливий діапазон 100–5 →
      тихий нуль. «До 5» насправді про ВИХІДНІ (стовпця в Меджику немає), а не
      про трафік. Тепер перевернутий діапазон гаситься: лишається «від 100».

  (2) «зона НЕ .com» → бот брав .com як ПОЗИТИВНИЙ фільтр (протилежне наміру).
      Тепер заперечення зони не стає позитивним фільтром (повне виключення зон —
      окремий, ще не зроблений крок).

Усе на розборі тексту — без мережі, без даних.
"""

from __future__ import annotations

from app.dictionary.countries import country_by_code
from app.text.dimensions import _sane_range
from app.text.freeform import parse_free_text


class TestНеможливийДіапазон:
    """Баг 1: перевернутий діапазон більше не дає тихий нуль."""

    def test_трафік_від_100_до_5_вихідних_не_дає_діапазон_100_5(self):
        q = parse_free_text("Меджик DR від 50 + трафік від 100 + до 5 вихідних").query
        # Лишається явний нижній поріг, хибний верхній відкинуто.
        assert q.traffic_min == 100
        assert q.traffic_max is None  # НЕ 5 — інакше був би неможливий діапазон
        assert q.dr_min == 50

    def test_другий_приклад_10000_до_3(self):
        q = parse_free_text("Меджик: DR від 90 + трафік від 10000 + до 3 вихідних.").query
        assert q.traffic_min == 10000
        assert q.traffic_max is None

    def test_коректний_діапазон_не_чіпаємо(self):
        """Регресія: справжній «від 10 до 100» лишається діапазоном."""
        q = parse_free_text("Меджик трафік від 10 до 100").query
        assert (q.traffic_min, q.traffic_max) == (10, 100)

    def test_вище_нижче_та_заперечення_метрики(self):
        assert parse_free_text("трафік вище 50").query.traffic_min == 50
        assert parse_free_text("DR нижче 30").query.dr_max == 30
        query = parse_free_text("Морди не DR від 50").query
        assert query.dr_min is None and query.dr_max is None

    def test_крім_країни_не_стає_позитивним_фільтром(self):
        query = parse_free_text("Меджик крім Франції, трафік від 50").query
        assert query.country is None
        assert [country.code for country in query.excluded_countries] == ["fr"]

    def test_sane_range_напряму(self):
        assert _sane_range(100, 5) == (100, None)  # перевернутий → нижній поріг
        assert _sane_range(10, 100) == (10, 100)  # коректний — без змін
        assert _sane_range(50, None) == (50, None)
        assert _sane_range(None, 20) == (None, 20)


class TestЗапереченняЗони:
    """Баг 2: «зона НЕ .com» не стає позитивним фільтром .com."""

    def test_зона_не_com_не_дає_позитивний_com(self):
        q = parse_free_text("Меджик: гео США, але зона НЕ .com — скільки?").query
        assert ".com" not in q.zones
        assert q.zones == ()  # зону взагалі не застосовано
        # Решта запиту не постраждала — GEO США лишилось.
        assert q.geo == country_by_code("us")

    def test_заперечення_ccTLD_теж_не_позитивне(self):
        q = parse_free_text("Морди зона не .de").query
        assert q.zones == ()

    def test_крім_зони_не_позитивне(self):
        q = parse_free_text("Меджик крім зони .org").query
        assert ".org" not in q.zones

    def test_позитивна_зона_досі_працює(self):
        """Регресія: звичайна «у зоні .com» лишається позитивним фільтром."""
        q = parse_free_text("Меджик у зоні .com").query
        assert q.zones == (".com",)

    def test_зона_не_важлива_лишається_скасуванням(self):
        """Регресія: «зона не важлива» — це скасування, а не заперечення значення."""
        q = parse_free_text("Меджик у зоні .com, зона не важлива").query
        # Скасування прибирає зону; головне — не падати й не давати сміття.
        assert ".com" not in q.zones or q.zones == ()
