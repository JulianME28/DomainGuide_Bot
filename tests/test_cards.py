"""Тести карток результату.

Найважливіше тут — порядок блоків. Мовний рядок має бути ОСТАННІМ, окремо
від головного числа. Якщо він опиниться поруч із зоновим числом, їх легко
сплутати й пообіцяти клієнту неіснуючих донорів.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import run_query
from app.analytics.query import DonorQuery
from app.analytics.recommendations import build_recommendations
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.text.cards import (
    LANGUAGE_MARK,
    number,
    plural_donors,
    render_breakdown,
    render_result,
    render_unavailable,
)


def query_for(code: str, **filters) -> DonorQuery:
    return DonorQuery(section_key="magic", country=country_by_code(code), **filters)


class TestМножина:
    @pytest.mark.parametrize(
        ("count", "word"),
        [
            (1, "донор"), (2, "донори"), (3, "донори"), (4, "донори"),
            (5, "донорів"), (11, "донорів"), (12, "донорів"), (14, "донорів"),
            (21, "донор"), (22, "донори"), (25, "донорів"), (101, "донор"),
            (111, "донорів"), (0, "донорів"),
        ],
    )  # fmt: skip
    def test_форми_слова(self, count, word):
        assert plural_donors(count) == word


class TestЧисла:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(32.0, "32"), (3133.3, "3 133"), (10000, "10 000"), (0, "0"), (None, "—")],
    )
    def test_форматування(self, value, expected):
        assert number(value) == expected


class TestПорядокБлоків:
    async def test_мовний_рядок_останній(self, magic):
        """Головна вимога до картки."""
        card = render_result(run_query(magic, query_for("de")))

        assert LANGUAGE_MARK in card
        language_position = card.index(LANGUAGE_MARK)

        for earlier in ("Знайдено донорів", "Середній DR", "Середній трафік", "похибка"):
            assert card.index(earlier) < language_position, f"«{earlier}» має бути ДО мовного рядка"

    async def test_мовний_рядок_після_рекомендацій(self, magic):
        result = run_query(magic, query_for("de"))
        recommendations = build_recommendations(magic, result.query)
        card = render_result(result, recommendations=recommendations)

        assert card.index("Суміжні країни") < card.index(LANGUAGE_MARK)

    async def test_мовний_блок_завершує_повідомлення(self, magic):
        """Після мовного блоку не має бути нічого, крім його ж попередження."""
        card = render_result(run_query(magic, query_for("gb")))
        tail = card[card.index(LANGUAGE_MARK) :]

        assert "Знайдено" not in tail
        assert "Середній" not in tail


class TestРозкладСкладових:
    async def test_підсумок_з_розкладом(self, magic):
        card = render_result(run_query(magic, query_for("de")))
        # Німецька — СПІЛЬНА (de/at/ch), тож мовний крок не в підсумку:
        # Німеччина: 7 (.de 6 | GEO 1), без складової «мова».
        assert "Знайдено донорів:</b> 7 (.de 6 | GEO 1)" in card
        assert "мова" not in card.split("Знайдено донорів")[1].split("\n")[0]

    async def test_додаток_окремим_рядком(self, magic):
        card = render_result(run_query(magic, query_for("de")))
        assert "на зонах інших країн — 2" in card

    async def test_додаток_не_входить_у_підсумок(self, magic):
        """Підсумок 7 і додаток 2 не зливаються в 9 у рядку розкладу."""
        card = render_result(run_query(magic, query_for("de")))
        found = next(line for line in card.split("\n") if "Знайдено донорів" in line)
        assert "9" not in found

    async def test_формулювання_каже_на_зонах_інших_країн(self, magic):
        card = render_result(run_query(magic, query_for("de")))
        assert "на зонах інших країн" in card

    async def test_морди_з_geo_складовою(self, mordy):
        """У «Морд» є GEO. Німецька — спільна, тож складової «мова» нема:
        Німеччина: 4 (.de 3 | GEO 1) — m1,m4,m7 у зоні, m2 через GEO(de).
        """
        card = render_result(
            run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))
        )
        found = next(line for line in card.split("\n") if "Знайдено донорів" in line)
        assert "4 (.de 3 | GEO 1)" in found
        assert "GEO" in found
        assert "мова" not in found

    async def test_якщо_додатка_немає_рядка_теж_немає(self, magic):
        card = render_result(run_query(magic, query_for("de", dr_min=100)))
        assert LANGUAGE_MARK not in card


class TestПопередженняПроСпільніМови:
    async def test_англійська_попереджає(self, magic):
        card = render_result(run_query(magic, query_for("gb")))
        # Спільна мова: до мовних рядків додається «(це не лише Британія)».
        assert "це не лише Британія" in card

    async def test_французька_не_попереджає(self, magic):
        card = render_result(run_query(magic, query_for("fr")))
        assert LANGUAGE_MARK in card, "мовний рядок має бути"
        assert "це не лише" not in card, "а застереження — ні"

    async def test_британія_має_обидва_мовні_рядки(self, magic):
        """Для спільної мови — і нейтральний рядок, і рядок інших зон."""
        card = render_result(run_query(magic, query_for("gb")))
        assert "на нейтральних зонах" in card
        assert "на зонах інших країн" in card


class TestПопередженняПроПоказники:
    async def test_мала_вибірка(self, magic):
        """Туреччина в базі має одного донора — середні ненадійні."""
        card = render_result(run_query(magic, query_for("tr")))
        assert "менш ніж на трьох донорах" in card

    async def test_похибка_завжди_згадується(self, magic):
        card = render_result(run_query(magic, query_for("de")))
        # Німеччина (німецька — спільна): підсумок 7 → нижня межа 5.
        assert "орієнтовна кількість з урахуванням похибки 5–7 (допустима похибка 30%)" in card
        # Старого окремого рядка «Орієнтовна кількість...» більше немає.
        assert "Орієнтовна кількість з урахуванням похибки:" not in card

    async def test_порожній_результат(self, magic):
        card = render_result(run_query(magic, query_for("jp")))
        assert "не знайдено" in card
        assert "допустима похибка" not in card, "для нуля похибка не має сенсу"


class TestБезпекаКартки:
    async def test_домен_не_витікає_в_картку(self, magic):
        """Найважливіша перевірка безпеки на рівні тексту."""
        for code in ("de", "fr", "gb", "tr"):
            result = run_query(magic, query_for(code))
            card = render_result(result, recommendations=build_recommendations(magic, result.query))

            for donor in magic.donors:
                assert donor.domain not in card, f"домен {donor.domain} потрапив у відповідь"

    async def test_у_картці_немає_списку_сайтів(self, magic):
        card = render_result(run_query(magic, DonorQuery(section_key="magic")))
        # Зони бути можуть, а от конкретні домени — ні.
        assert "de1" not in card
        assert "glob" not in card

    async def test_розподіл_показує_лише_групи(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        text = render_breakdown("Розподіл по зонах", result.zone_breakdown)

        for donor in magic.donors:
            assert donor.domain not in text


class TestЕкранування:
    async def test_апострофи_лишаються_апострофами(self, magic):
        """«м'якшими» не має перетворитися на «м&#x27;якшими»."""
        result = run_query(magic, query_for("gb"))
        card = render_result(result, recommendations=build_recommendations(magic, result.query))

        assert "&#x27;" not in card
        assert "&quot;" not in card

    def test_небезпечні_символи_екрануються(self):
        """А от «<» і «&» Telegram сприйняв би як розмітку — їх екрануємо."""
        from app.text.cards import escape

        assert escape("<script>") == "&lt;script&gt;"
        assert escape("Tom & Jerry") == "Tom &amp; Jerry"
        assert escape('м\'які лапки "тут"') == 'м\'які лапки "тут"'


class TestНедоступнаБаза:
    def test_повідомлення_пояснює_причину(self):
        from app.data.models import Dataset

        broken = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=(),
            loaded_at=0.0,
            available=False,
            error="Google не дав доступ до таблиці",
        )
        card = render_unavailable(run_query(broken, DonorQuery(section_key="magic")))

        assert "тимчасово недоступна" in card
        assert "Google не дав доступ" in card

    async def test_render_result_сам_обирає_потрібний_вигляд(self, repository):
        from app.data.models import Dataset

        broken = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=(),
            loaded_at=0.0,
            available=False,
            error="збій",
        )
        card = render_result(run_query(broken, DonorQuery(section_key="magic")))
        assert "недоступна" in card


class TestРозподіли:
    async def test_розподіл_по_країнах(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        text = render_breakdown("Розподіл по країнах", result.country_breakdown)

        assert "Німеччина" in text
        assert "Глобальні зони" in text

    def test_порожній_розподіл(self):
        assert "немає" in render_breakdown("Розподіл", ())


class TestКомбінованийЗапит:
    async def test_мова_і_країна_в_описі(self, magic):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("de"),
        )
        card = render_result(run_query(magic, query))

        assert "Німеччина" in card
        assert "мова німецька" in card
        assert LANGUAGE_MARK not in card, "користувач сам звузив — додаток зайвий"


class TestФразаПрохання:
    """Фразу-прохання бот пояснює й показує суміжні, а не робить фільтром."""

    async def test_пояснювальний_рядок_у_картці(self, magic):
        query = query_for("de", request_hint="англомовні альтернативи")
        card = render_result(run_query(magic, query))
        assert "англомовні альтернативи" in card
        assert "прохання показати схожі варіанти" in card
        assert "не як фільтр" in card

    async def test_без_прохання_рядка_немає(self, magic):
        card = render_result(run_query(magic, query_for("de")))
        assert "прохання показати схожі варіанти" not in card

    async def test_суміжні_країни_показані(self, magic):
        """При проханні блок суміжних країн присутній (Німеччина → Австрія/Швейцарія)."""
        query = query_for("de", request_hint="англомовні альтернативи")
        recommendations = build_recommendations(magic, query)
        card = render_result(run_query(magic, query), recommendations=recommendations)
        assert "Суміжні країни" in card
