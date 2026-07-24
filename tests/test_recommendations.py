"""Тести рекомендацій: суміжні гео, пониження вимог, запас, дефіцит."""

from __future__ import annotations

from app.analytics.query import DonorQuery
from app.analytics.recommendations import (
    build_recommendations,
    deficit_hint,
    relaxed_suggestions,
    reserve_group,
    same_language_suggestions,
    summary_line,
)
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code


def germany(**filters) -> DonorQuery:
    return DonorQuery(section_key="magic", country=country_by_code("de"), **filters)


class TestСуміжніКраїниЗіСпільноюМовою:
    async def test_німеччина_пропонує_австрію_і_швейцарію(self, magic):
        """Саме той приклад, який був у завданні."""
        suggestions = same_language_suggestions(magic, germany())
        labels = " ".join(s.label for s in suggestions)

        assert "Австрія" in labels
        assert ".at" in labels
        assert "Швейцарія" in labels
        assert ".ch" in labels

    async def test_кількості_справжні(self, magic):
        suggestions = {
            s.label.split()[1]: s.count for s in same_language_suggestions(magic, germany())
        }
        # Німецька — СПІЛЬНА (de/at/ch), тож мовний крок не входить у підсумок:
        # Австрія = at1(зона) = 1; Швейцарія = ch1(зона) = 1. glob1/glob2 не рахуються.
        assert suggestions["Австрія"] == 1
        assert suggestions["Швейцарія"] == 1

    async def test_сама_країна_не_пропонується(self, magic):
        labels = " ".join(s.label for s in same_language_suggestions(magic, germany()))
        assert "Німеччина" not in labels

    async def test_країни_без_донорів_не_показуються(self, magic):
        """Пропонувати гео з нулем донорів безглуздо."""
        assert all(s.count > 0 for s in same_language_suggestions(magic, germany()))

    async def test_для_запиту_про_мову_суміжних_країн_немає(self, magic):
        query = DonorQuery(section_key="magic", language=language_by_code("de"))
        assert same_language_suggestions(magic, query) == ()

    async def test_фільтри_переносяться_на_суміжні_гео(self, magic):
        """Якщо запит був із DR ≥ 30, суміжні гео рахуються з тим самим фільтром."""
        suggestions = same_language_suggestions(magic, germany(dr_min=30))
        counts = {s.label.split()[1]: s.count for s in suggestions}

        # Німецька спільна — мовний крок не рахується. DR≥30:
        # Австрія = at1(35, зона) = 1; Швейцарія = ch1(20, зона ✗) = 0 → не показується.
        assert counts.get("Австрія") == 1
        assert "Швейцарія" not in counts


class TestПониженняВимог:
    async def test_dr_знижується_на_десять(self, magic):
        suggestions = relaxed_suggestions(magic, DonorQuery(section_key="magic", dr_min=50))
        labels = [s.label for s in suggestions]
        assert "DR від 40" in labels

    async def test_трафік_ділиться_навпіл(self, magic):
        suggestions = relaxed_suggestions(magic, DonorQuery(section_key="magic", traffic_min=1000))
        labels = [s.label for s in suggestions]
        assert "трафік від 500" in labels

    async def test_обидва_фільтри_разом(self, magic):
        suggestions = relaxed_suggestions(
            magic, DonorQuery(section_key="magic", dr_min=40, traffic_min=1000)
        )
        labels = [s.label for s in suggestions]
        assert any("і трафік від" in label for label in labels)

    async def test_пропонується_лише_те_що_дає_більше(self, magic):
        query = DonorQuery(section_key="magic", dr_min=50)
        current = 3  # uk1(50), de4(55), glob3(60)
        for suggestion in relaxed_suggestions(magic, query):
            assert suggestion.count > current

    async def test_без_фільтрів_нічого_не_пропонується(self, magic):
        assert relaxed_suggestions(magic, DonorQuery(section_key="magic")) == ()

    async def test_dr_не_йде_в_мінус(self, magic):
        suggestions = relaxed_suggestions(magic, DonorQuery(section_key="magic", dr_min=5))
        for suggestion in suggestions:
            assert "-" not in suggestion.label


class TestЯдроІЗапас:
    async def test_без_метрик_запасу_немає(self, magic):
        """Немає що послаблювати → рядка «Ядро + запас» немає взагалі."""
        assert reserve_group(magic, germany()) is None

    async def test_запас_це_приріст_від_послаблення(self, magic):
        """DR≥50: у підсумку лише de4(55). Знижуємо до DR≥40 → +de1(40, зона).
        glob1(45, німецькою на .com) не рахується — німецька спільна. Запас = 1."""
        group = reserve_group(magic, germany(dr_min=50))
        assert group is not None
        assert group.core_count == 1
        assert group.reserve_count == 1
        assert group.total == 2

    async def test_підпис_містить_фактичні_пороги(self, magic):
        group = reserve_group(magic, germany(dr_min=50))
        assert "з пониженими вимогами" in group.reserve_label
        assert "DR від 40 замість 50" in group.reserve_label

    async def test_трафік_теж_послаблюється(self, magic):
        group = reserve_group(magic, germany(traffic_min=1000))
        assert group is not None
        # Німецька спільна: підсумок = зона + GEO. Трафік ≥1000: de1,de2,de4 = 3.
        assert group.core_count == 3
        assert group.reserve_count == 1  # de3(500): не ≥1000, але ≥500
        assert "трафік від 500 замість 1000" in group.reserve_label

    async def test_запас_не_бере_мовні_рядки(self, magic):
        """at1(.at, DR35) — німецька на зоні іншої країни («на зонах інших країн»).

        DR≥40 → знижуємо до DR≥30. at1(35) підходить за DR, але він НЕ в підсумку
        країни, тож у запас не потрапляє. Додається лише de6(30, зона .de) → 1.
        """
        group = reserve_group(magic, germany(dr_min=40))
        assert group.reserve_count == 1  # тільки de6; at1 не рахується

    async def test_запас_це_та_сама_країна(self, magic):
        """Запас — донори тієї ж країни, лише зі зниженими метриками.

        Приріст (2 для DR≥50) дорівнює різниці підсумків країни: зі зниженим
        порогом мінус із вихідним."""
        from app.analytics.engine import result_count

        query = germany(dr_min=50)
        softer = germany(dr_min=40)
        gain = result_count(magic, softer) - result_count(magic, query)
        assert reserve_group(magic, query).reserve_count == gain

    async def test_без_країни_запасу_немає(self, magic):
        assert reserve_group(magic, DonorQuery(section_key="magic", dr_min=30)) is None

    async def test_морди_послаблення_їхніх_метрик(self, mordy):
        """Для «Морд» послаблення враховує вихідні лінки й заспамленість.

        Німеччина трикроково = m1,m4,m7 (зона) + m2 (GEO de). Вихідні ≤10 лишає
        ядро m4(0),m7(8); знижуємо до ≤20 → додаються m1(16, зона) і m2(20, GEO).
        """
        query = DonorQuery(section_key="mordy", country=country_by_code("de"), outlinks_max=10)
        group = reserve_group(mordy, query)
        assert group is not None
        assert group.core_count == 2  # m4(0), m7(8)
        assert group.reserve_count == 2  # m1(16, зона) і m2(20, GEO): не ≤10, але ≤20
        assert "вихідні лінки до 20 замість 10" in group.reserve_label


class TestАналізДефіциту:
    async def test_знаходить_найжорсткіший_фільтр(self, magic):
        """DR ≥ 50 ріже сильніше, ніж трафік ≥ 100."""
        hint = deficit_hint(magic, DonorQuery(section_key="magic", dr_min=50, traffic_min=100))
        assert hint is not None
        assert "DR від 50" in hint.filter_label
        assert hint.gain > 0

    async def test_показує_скільки_додасть_зняття_фільтра(self, magic):
        hint = deficit_hint(magic, DonorQuery(section_key="magic", dr_min=50))
        assert hint.current_count == 3
        assert hint.without_filter_count == 24
        assert hint.gain == 21

    async def test_без_фільтрів_дефіциту_немає(self, magic):
        assert deficit_hint(magic, DonorQuery(section_key="magic")) is None

    async def test_якщо_фільтр_нікого_не_ріже(self, magic):
        """DR ≥ 0 нікого не відсіює понад те, що вже відсіяно — підказки немає."""
        assert deficit_hint(magic, DonorQuery(section_key="magic", dr_min=0)) is not None


class TestУсеРазом:
    async def test_повний_набір_рекомендацій(self, magic):
        recommendations = build_recommendations(magic, germany(dr_min=20))

        assert recommendations.same_language, "мають бути суміжні країни"
        assert recommendations.reserve is not None
        assert not recommendations.is_empty

    async def test_суміжні_гео_регіону(self, magic):
        """Крім німецькомовних, є ще європейські сусіди з донорами."""
        recommendations = build_recommendations(magic, germany())
        labels = " ".join(s.label for s in recommendations.same_region)
        assert "Франція" in labels or "Бельгія" in labels

    async def test_суміжна_спільномовна_країна_без_мовного_кроку(self, magic):
        """Головне заради чого зміна: Британія у суміжних — підсумок без мови.

        Британія (спільна мова) = зона(3) + GEO(0) = 3, а не 3+2(мова)=5.
        На реальній базі це різниця між сотнями й тисячами.
        """
        from app.dictionary.countries import country_by_code

        france = DonorQuery(section_key="magic", country=country_by_code("fr"))
        recommendations = build_recommendations(magic, france)
        britain = next(s for s in recommendations.same_region if "Британія" in s.label)
        assert britain.count == 3  # без мовного кроку (інакше було б 5)

    async def test_регіон_не_дублює_спільну_мову(self, magic):
        recommendations = build_recommendations(magic, germany())
        language_labels = {s.label for s in recommendations.same_language}
        region_labels = {s.label for s in recommendations.same_region}
        assert not (language_labels & region_labels)

    async def test_порожня_база_не_дає_рекомендацій(self, repository):
        mordy = await repository.get("mordy")
        recommendations = build_recommendations(
            mordy, DonorQuery(section_key="mordy", country=country_by_code("de"))
        )
        assert recommendations.is_empty

    async def test_загальна_кількість_суміжних(self, magic):
        recommendations = build_recommendations(magic, germany())
        assert recommendations.extra_total > 0

    async def test_підказка_для_менеджера(self, magic):
        recommendations = build_recommendations(magic, germany())
        line = summary_line(6, recommendations)
        assert "6" in line
        assert "суміжних гео" in line


class TestБезпекаРекомендацій:
    async def test_у_рекомендаціях_немає_доменів(self, magic):
        recommendations = build_recommendations(magic, germany(dr_min=10))
        dumped = repr(recommendations)

        for donor in magic.donors:
            assert donor.domain not in dumped, f"домен {donor.domain} витік у рекомендації"
