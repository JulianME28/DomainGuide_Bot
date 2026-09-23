"""Стоп-фільтр Морд: рядки зі «Стоп» у стовпці «Стоп» виключаються з УСЬОГО.

Виключення відбувається ще при завантаженні бази — тож «стоп»-донори не
потрапляють у пам'ять узагалі: ні в кількість, ні в розбивку, ні в coverage,
ні в середні. Стосується лише «Морд»; «Меджик» стовпця «Стоп» не має.
"""

from __future__ import annotations

import logging

import pytest

from app.analytics.coverage import run_coverage
from app.analytics.engine import country_distribution, run_query
from app.analytics.query import CoverageQuery, DonorQuery
from app.data.repository import DonorRepository, _is_stop
from app.dictionary.countries import country_by_code
from tests.fixtures.fake_data import FakeReader, magic_rows


def _mordy_row(domain: str, dr: str, stop: str = "") -> dict[str, str]:
    return {
        "domain": domain,
        "language": "English",
        "dr": dr,
        "traffic": "1000",
        "outlinks": "5",
        "spam": "1",
        "geo": "",
        "stop": stop,
    }


# 5 рядків: 3 зі «стоп» (різний регістр/пробіли), 2 живі.
#   au1 живий, au2 «Стоп», gb1 «СТОП», de1 « стоп », de2 живий.
MORDY_MIX = [
    _mordy_row("au1.com.au", "10"),
    _mordy_row("au2.com.au", "90", stop="Стоп"),
    _mordy_row("gb1.co.uk", "90", stop="СТОП"),
    _mordy_row("de1.de", "90", stop=" стоп "),
    _mordy_row("de2.de", "20"),
]
# Ті самі рядки, але БЕЗ стовпця «Стоп» узагалі (скрипт ще не проставив).
MORDY_NO_STOP_COL = [{k: v for k, v in row.items() if k != "stop"} for row in MORDY_MIX]


def _repo(rows) -> DonorRepository:
    from app.data.columns import load_columns_config

    return DonorRepository(
        FakeReader({"magic": magic_rows(), "mordy": rows}), load_columns_config()
    )


class TestТолерантність:
    @pytest.mark.parametrize("value", ["Стоп", " стоп ", "СТОП", "  СТОП  ", "стоп"])
    def test_це_стоп(self, value):
        assert _is_stop(value) is True

    @pytest.mark.parametrize("value", ["", "   ", None, "ні", "stop-ish", "0"])
    def test_не_стоп(self, value):
        assert _is_stop(value) is False


class TestВиключенняПриЗавантаженні:
    async def test_стоп_донори_не_в_памʼяті(self):
        dataset = await _repo(MORDY_MIX).get("mordy")
        assert dataset.count == 2  # лишились au1, de2
        assert dataset.rows_stopped == 3
        assert dataset.rows_read == 5
        domains = {d.domain for d in dataset.donors}
        assert domains == {"au1.com.au", "de2.de"}
        # Жоден зі «стоп»-доменів не просочився.
        assert "au2.com.au" not in domains
        assert "gb1.co.uk" not in domains

    async def test_не_в_загальній_кількості(self):
        dataset = await _repo(MORDY_MIX).get("mordy")
        assert run_query(dataset, DonorQuery(section_key="mordy")).core.count == 2

    async def test_не_в_розбивці_по_країнах(self):
        dataset = await _repo(MORDY_MIX).get("mordy")
        dist = country_distribution(dataset, DonorQuery(section_key="mordy"))
        # gb (єдиний рядок був «стоп») зникає повністю; au лишається 1 (не 2).
        assert dist.total == 2
        assert [n for label, n in dist.countries if "Австралія" in label] == [1]
        assert not any("Британія" in label for label, _ in dist.countries)

    async def test_не_в_coverage(self):
        dataset = await _repo(MORDY_MIX).get("mordy")
        cq = CoverageQuery(
            section_key="mordy",
            needs=((country_by_code("au"), 5), (country_by_code("gb"), 5)),
            thresholds=(0,),
        )
        result = run_coverage(dataset, cq)
        rows = {r.country.code: r.total for r in result.rows}
        assert rows["au"] == 1  # au2 (стоп) не рахується
        assert rows["gb"] == 0  # gb1 (стоп) — країни взагалі немає

    async def test_не_в_середніх(self):
        # Живі au1(DR10) і de2(DR20) → середнє 15. «Стоп»-донори мали DR 90 —
        # якби рахувались, середнє було б значно вищим.
        dataset = await _repo(MORDY_MIX).get("mordy")
        assert run_query(dataset, DonorQuery(section_key="mordy")).core.avg_dr == 15

    async def test_донор_без_стоп_рахується(self):
        dataset = await _repo([_mordy_row("live.de", "40")]).get("mordy")
        assert dataset.count == 1
        assert dataset.rows_stopped == 0


class TestСумісністьБезСтовпця:
    async def test_немає_стовпця_усі_живі(self):
        dataset = await _repo(MORDY_NO_STOP_COL).get("mordy")
        assert dataset.count == 5  # нікого не виключено
        assert dataset.rows_stopped == 0

    async def test_попередження_гучне(self, caplog):
        with caplog.at_level(logging.WARNING):
            await _repo(MORDY_NO_STOP_COL).get("mordy")
        text = "\n".join(caplog.messages)
        assert "стовпця «Стоп» немає" in text
        assert "НЕ застосовано" in text

    async def test_порожня_база_без_попередження(self, caplog):
        # Порожній аркуш — не привід кричати про відсутній стовпець.
        with caplog.at_level(logging.WARNING):
            await _repo([]).get("mordy")
        assert "стовпця «Стоп»" not in "\n".join(caplog.messages)


class TestЛогКількості:
    async def test_кількість_виключених_у_лог(self, caplog):
        with caplog.at_level(logging.INFO):
            await _repo(MORDY_MIX).get("mordy")
        assert any("виключено 3 донорів за стовпцем «Стоп»" in m for m in caplog.messages)


class TestМеджикНеЗачеплено:
    async def test_меджик_не_має_стоп_ролі(self):
        from app.data.columns import load_columns_config

        assert not load_columns_config().section("magic").filters_stop

    async def test_меджик_вантажиться_без_падіння(self):
        # У magic_rows() немає ключа «stop»; Меджик не має ролі stop → ні фільтра,
        # ні попередження, ні падіння.
        dataset = await _repo(MORDY_MIX).get("magic")
        assert dataset.available
        assert dataset.rows_stopped == 0
        assert dataset.count > 0
