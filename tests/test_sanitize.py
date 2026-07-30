"""Група 1: санітарна сітка для ШІ-фільтра (та сама, що й у словника).

Гасить тихі хибні числа на ШІ-шляху:
  * інвертований діапазон «DR від 40 до 20» → нижній поріг, не нуль;
  * заперечена зона «не .com» → не позитивний фільтр.
Країни при цьому НЕ чіпаємо (легітимний позитив).
"""

from __future__ import annotations

from app.analytics.query import DonorQuery
from app.dictionary.countries import country_by_code
from app.text.sanitize import sanitize_query


class TestІнвертованийДіапазон:
    def test_dr_40_20_лишає_нижній_поріг(self):
        q = DonorQuery(section_key="magic", dr_min=40, dr_max=20)
        s = sanitize_query(q, "меджик британія DR від 40 до 20")
        assert s.dr_min == 40
        assert s.dr_max is None  # не 0-результат

    def test_трафік_інверсія(self):
        q = DonorQuery(section_key="magic", traffic_min=100, traffic_max=5)
        s = sanitize_query(q, "трафік від 100 до 5")
        assert (s.traffic_min, s.traffic_max) == (100, None)

    def test_спам_інверсія(self):
        q = DonorQuery(section_key="mordy", spam_min=50, spam_max=10)
        s = sanitize_query(q, "заспамленість від 50 до 10")
        assert (s.spam_min, s.spam_max) == (50, None)

    def test_коректний_діапазон_не_чіпаємо(self):
        q = DonorQuery(section_key="magic", dr_min=10, dr_max=50)
        s = sanitize_query(q, "dr від 10 до 50")
        assert (s.dr_min, s.dr_max) == (10, 50)

    def test_none_query(self):
        assert sanitize_query(None, "будь-що") is None


class TestЗапереченняЗони:
    def test_знімає_заперечену_зону(self):
        q = DonorQuery(section_key="magic", zones=(".com",))
        s = sanitize_query(q, "меджик у зоні не .com")
        assert ".com" not in s.zones

    def test_позитивну_зону_лишає(self):
        q = DonorQuery(section_key="magic", zones=(".com",))
        s = sanitize_query(q, "меджик у зоні .com")
        assert s.zones == (".com",)

    def test_країну_не_чіпаємо_при_запереченні_зони(self):
        # «морди Франція але зона не .fr»: країна Франція — легітимний позитив,
        # знімаємо лише зони (а їх тут і немає).
        q = DonorQuery(section_key="mordy", country=country_by_code("fr"))
        s = sanitize_query(q, "морди франція але зона не .fr")
        assert s.country == country_by_code("fr")
