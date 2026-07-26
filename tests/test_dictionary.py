"""Тести словника: мови, країни, зони, розпізнавання в тексті.

Найважливіша частина — клас TestЗбігиПочатківСлів. Українські назви країн і
мов часто починаються однаково («Англія» / «англійською»), і саме там
найлегше отримати тихо неправильну відповідь.
"""

from __future__ import annotations

import pytest

from app.data.parsing import MULTI_PART_SUFFIXES, extract_zone
from app.dictionary.countries import (
    ALL_COUNTRY_ZONES,
    COUNTRIES,
    WIDESPREAD_LANGUAGE_CODES,
    countries_with_language,
    country_by_code,
    country_by_zone,
    is_widespread_language,
)
from app.dictionary.languages import (
    LANGUAGES,
    display_language,
    language_by_code,
    language_by_data_value,
)
from app.dictionary.resolver import (
    find_all_countries,
    hint_for_country_mode,
    hint_for_language_mode,
    resolve_country,
    resolve_language,
    scan_entities,
)
from app.dictionary.zones import GLOBAL_ZONES, is_global_zone


class TestСловникМов:
    def test_мов_достатньо(self):
        assert len(LANGUAGES) >= 60, "у даних 60+ мов, словник має їх покривати"

    @pytest.mark.parametrize(
        "name",
        [
            "English", "Spanish", "Portuguese", "Turkish", "Italian", "French",
            "Arabic", "German", "Indonesian", "Vietnamese", "Polish", "Dutch",
            "Romanian", "Greek", "Thai", "Hindi", "Chinese", "Japanese",
            "Russian", "Hungarian", "Swedish", "Persian", "Korean", "Czech",
            "Danish", "Croatian", "Bulgarian", "Serbian", "Ukrainian",
            "Norwegian", "Lithuanian", "Slovak", "Hebrew", "Slovenian",
            "Latvian", "Bosnian", "Estonian", "Finnish", "Malay", "Georgian",
            "Catalan",
        ],
    )  # fmt: skip
    def test_мова_з_даних_розпізнається(self, name):
        """Усі мови, які реально трапляються в таблиці."""
        assert language_by_data_value(name) is not None

    def test_брудне_значення_мови_теж_розпізнається(self):
        """У таблиці бувають хвостові пробіли й різний регістр."""
        assert language_by_data_value("English ") is language_by_code("en")
        assert language_by_data_value("  turkish") is language_by_code("tr")
        assert language_by_data_value("GERMAN") is language_by_code("de")

    def test_невідома_мова_не_ламає_бот(self):
        assert language_by_data_value("Клінгонська") is None
        assert display_language("Клінгонська") == "Клінгонська"

    # Спільна мова = основна для 2+ країн. Німецька/нідерландська тепер теж тут.
    @pytest.mark.parametrize("code", ["en", "es", "pt", "ar", "de", "nl"])
    def test_спільні_мови_позначені(self, code):
        """Цими мовами пишуть кілька країн — потрібне попередження."""
        assert language_by_code(code).widespread
        assert code in WIDESPREAD_LANGUAGE_CODES
        assert is_widespread_language(code)

    @pytest.mark.parametrize("code", ["fr", "it", "tr", "ja", "pl", "vi", "uk"])
    def test_однозначні_мови_не_потребують_попередження(self, code):
        """Мова однієї країни — не спільна, мовний крок входить у підсумок."""
        assert not language_by_code(code).widespread
        assert not is_widespread_language(code)

    def test_спільність_обчислюється_зі_словника_а_не_списком(self):
        """WIDESPREAD_LANGUAGE_CODES = рівно ті мови, що основні для 2+ країн."""
        from collections import Counter

        counts = Counter(c.primary_language for c in COUNTRIES.values())
        expected = {code for code, n in counts.items() if n >= 2}
        assert set(WIDESPREAD_LANGUAGE_CODES) == expected
        assert expected == {"en", "es", "de", "ar", "nl", "pt"}


class TestСловникКраїн:
    def test_країн_достатньо(self):
        assert len(COUNTRIES) >= 43

    @pytest.mark.parametrize(
        ("code", "zone"),
        [
            ("de", ".de"), ("fr", ".fr"), ("it", ".it"), ("es", ".es"),
            ("nl", ".nl"), ("pl", ".pl"), ("pt", ".pt"), ("se", ".se"),
            ("cz", ".cz"), ("ro", ".ro"), ("gr", ".gr"), ("hu", ".hu"),
            ("gb", ".co.uk"), ("gb", ".uk"), ("ca", ".ca"),
            ("au", ".com.au"), ("au", ".au"), ("br", ".com.br"), ("br", ".br"),
            ("jp", ".jp"), ("in", ".in"), ("in", ".co.in"), ("za", ".co.za"),
            ("tr", ".com.tr"), ("tr", ".tr"), ("vn", ".vn"),
            ("mx", ".mx"), ("mx", ".com.mx"), ("ar", ".com.ar"), ("ar", ".ar"),
            ("cl", ".cl"), ("ae", ".ae"), ("il", ".co.il"), ("il", ".il"),
            ("id", ".id"), ("id", ".co.id"), ("pk", ".pk"), ("ir", ".ir"),
            ("us", ".us"), ("at", ".at"), ("ch", ".ch"),
        ],
    )  # fmt: skip
    def test_зона_належить_правильній_країні(self, code, zone):
        assert country_by_zone(zone) is country_by_code(code)

    def test_кожна_складена_зона_відома_парсеру(self):
        """Захист від розбіжності між словником і розбором доменів.

        Якщо у словник додати країну із зоною «.com.xx», а в MULTI_PART_SUFFIXES
        її не додати — бот мовчки не знайшов би жодного донора цієї країни.
        Тест ловить саме таку тиху помилку.
        """
        for zone in ALL_COUNTRY_ZONES:
            if zone.count(".") == 2:
                assert zone.lstrip(".") in MULTI_PART_SUFFIXES, (
                    f"Зона {zone} є у словнику країн, але парсер доменів про неї не знає"
                )

    def test_зона_витягується_з_домену_і_сходиться_зі_словником(self):
        """Наскрізна перевірка: домен → зона → країна."""
        assert country_by_zone(extract_zone("bbc.co.uk")) is country_by_code("gb")
        assert country_by_zone(extract_zone("shop.example.de")) is country_by_code("de")
        assert country_by_zone(extract_zone("loja.com.br")) is country_by_code("br")

    def test_основна_мова_вказана_правильно(self):
        assert country_by_code("de").primary_language == "de"
        assert country_by_code("at").primary_language == "de"
        assert country_by_code("ch").primary_language == "de"
        assert country_by_code("gb").primary_language == "en"
        assert country_by_code("br").primary_language == "pt"

    def test_суміжні_країни_зі_спільною_мовою(self):
        """Німеччина → Австрія і Швейцарія. Саме цього просив ТЗ."""
        neighbours = {c.code for c in countries_with_language("de", exclude="de")}
        assert {"at", "ch"} <= neighbours
        assert "de" not in neighbours

    def test_кожна_мова_країни_існує_у_словнику_мов(self):
        for country in COUNTRIES.values():
            assert country.language is not None, f"{country.name_uk}: невідома основна мова"


class TestГлобальніЗони:
    @pytest.mark.parametrize(
        "zone",
        [".com", ".net", ".org", ".online", ".site", ".info", ".eu", ".co", ".shop"],
    )
    def test_глобальна_зона_нікому_не_належить(self, zone):
        """Сайт на .com може бути звідки завгодно — приписувати його країні не можна."""
        assert is_global_zone(zone)
        assert country_by_zone(zone) is None

    def test_жодна_глобальна_зона_не_закріплена_за_країною(self):
        assert not (GLOBAL_ZONES & ALL_COUNTRY_ZONES)

    def test_co_глобальна_а_com_co_це_колумбія(self):
        """Тонке місце: .co продають усьому світу, а Колумбія — це .com.co."""
        assert country_by_zone(".co") is None
        assert country_by_zone(".com.co") is country_by_code("co")


class TestРозпізнаванняКраїни:
    @pytest.mark.parametrize(
        "text",
        ["Німеччина", "німеччини", "по Німеччині", "Німеччину", "Німеччиною",
         "Germany", "germany", "Deutschland", ".de", "донори в зоні .de"],
    )  # fmt: skip
    def test_німеччина_у_різних_формах(self, text):
        assert resolve_country(text) is country_by_code("de")

    @pytest.mark.parametrize(
        "text",
        ["Британія", "по Британії", "Britain", "UK", "United Kingdom",
         "great britain", ".co.uk", "Англія"],
    )  # fmt: skip
    def test_британія_у_різних_формах(self, text):
        assert resolve_country(text) is country_by_code("gb")

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("Франція", "fr"), ("по Франції", "fr"), ("Францію", "fr"),
            ("Туреччина", "tr"), ("по Туреччині", "tr"),
            ("Данія", "dk"), ("по Данії", "dk"),
            ("Китай", "cn"), ("по Китаю", "cn"),
            ("Корея", "kr"), ("Південна Корея", "kr"),
            ("США", "us"), ("usa", "us"), ("Америка", "us"),
            ("ПАР", "za"), ("Південна Африка", "za"),
            ("Вʼєтнам", "vn"), ("Вєтнам", "vn"),
            ("Нова Зеландія", "nz"),
        ],
    )  # fmt: skip
    def test_різні_країни(self, text, code):
        assert resolve_country(text) is country_by_code(code)

    def test_дані_не_плутаються_з_данією(self):
        """«дані» — звичайне слово. Воно не має ставати Данією."""
        assert resolve_country("покажи дані по донорах") is None
        assert resolve_country("скільки даних") is None


class TestКороткіКодиУСписку:
    """UK/US та інші короткі скорочення — безпечно, лише в контексті переліку."""

    def _codes(self, text: str) -> set[str]:
        return {c.code for c in find_all_countries(text)[0]}

    def test_список_з_uk_us_розпізнається_повністю(self):
        """6 із 6: раніше «us» губився (голий двобуквений код був вимкнений)."""
        codes = self._codes("uk, us, канада, австралія, ірландія, нова зеландія")
        assert codes == {"gb", "us", "ca", "au", "ie", "nz"}

    def test_us_у_переліку_через_кому(self):
        assert self._codes("us, франція") == {"us", "fr"}

    def test_uae_і_usa_теж_працюють(self):
        assert self._codes("франція, uae, usa") == {"fr", "ae", "us"}

    def test_us_у_суцільній_прозі_не_ловиться(self):
        """Без ознак переліку (кома/«та»/«країн»/сусідні країни) — «us» мовчить."""
        assert self._codes("we can trust us here today") == set()

    def test_правило_проти_in_is_ціле(self):
        """«in», «is» — звичайні слова, країнами не стають ні за яких умов."""
        assert self._codes("this is a report in progress") == set()
        # Навіть у контексті переліку «in/is» не є безпечними кодами.
        assert "in" not in [c.code for c in find_all_countries("франція, in, is")[0]]

    def test_scan_entities_не_ловить_us(self):
        """Одиночний скан прози короткі коди не вмикає взагалі."""
        assert scan_entities("trust us").country is None


class TestРозпізнаванняМови:
    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("німецькою", "de"), ("німецька", "de"), ("німецькомовні", "de"),
            ("German", "de"), ("deutsch", "de"),
            ("англійською", "en"), ("англомовні", "en"), ("English", "en"),
            ("французькою", "fr"), ("французькомовних", "fr"), ("French", "fr"),
            ("Turkish", "tr"), ("турецькою", "tr"),
            ("Chinese", "zh"), ("китайською", "zh"),
            ("Vietnamese", "vi"), ("вʼєтнамською", "vi"),
            ("Spanish", "es"), ("іспаномовні", "es"),
            ("Indonesian", "id"), ("Portuguese", "pt"),
        ],
    )  # fmt: skip
    def test_мова_розпізнається(self, text, code):
        assert resolve_language(text) is language_by_code(code)


class TestЗбігиПочатківСлів:
    """НАЙВАЖЛИВІШИЙ клас у цьому файлі.

    Назви країн і мов українською часто починаються однаково. Якщо
    розпізнавати країну першою, «англійською» перетвориться на країну Англія,
    і бот тихо дасть відповідь не на те питання.

    Тому у вільному тексті спершу шукається мова, а знайдене слово
    «затирається». Тести нижче перевіряють, що це справді працює.
    """

    @pytest.mark.parametrize(
        ("text", "language_code"),
        [
            ("англійською", "en"),  # ховається «Англія»
            ("англомовні донори", "en"),
            ("італійською", "it"),  # ховається «Італія»
            ("латвійською", "lv"),  # ховається «Латвія»
            ("українською", "uk"),  # ховається «Україна»
            ("індонезійською", "id"),  # ховається «Індонезія»
            ("нідерландською", "nl"),  # ховається «Нідерланди»
            ("філіппінською", "tl"),  # ховається «Філіппіни»
        ],
    )
    def test_це_мова_а_не_країна(self, text, language_code):
        scan = scan_entities(text)
        assert scan.language is language_by_code(language_code)
        assert scan.country is None, f"«{text}» — це мова, країни тут немає"

    @pytest.mark.parametrize(
        ("text", "country_code"),
        [
            ("Англія", "gb"),
            ("Італія", "it"),
            ("Латвія", "lv"),
            ("Україна", "ua"),
            ("Індонезія", "id"),
            ("Нідерланди", "nl"),
            ("Філіппіни", "ph"),
        ],
    )
    def test_а_це_країна_а_не_мова(self, text, country_code):
        scan = scan_entities(text)
        assert scan.country is country_by_code(country_code)
        assert scan.language is None, f"«{text}» — це країна, мови тут немає"

    def test_французька_і_франція_не_плутаються(self):
        assert scan_entities("французькою мовою").country is None
        assert scan_entities("по Франції").language is None

    def test_країна_і_мова_разом(self):
        """«німецькомовні донори в Німеччині» — це обидва фільтри одразу."""
        scan = scan_entities("німецькомовні донори в Німеччині")
        assert scan.language is language_by_code("de")
        assert scan.country is country_by_code("de")


class TestКороткіКоди:
    """У вільному тексті голі двобуквені коди не використовуються.

    «in», «is», «no», «it» — звичайні слова, а не Індія, Ісландія, Норвегія
    та Італія.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "how many donors in it",
            "is there no traffic",
            "donors in the base",
            "no dr limit",
        ],
    )
    def test_голі_двобуквені_коди_ігноруються(self, text):
        scan = scan_entities(text)
        assert scan.country is None
        assert scan.language is None

    def test_зона_з_крапкою_завжди_надійна(self):
        """«.it» з крапкою — однозначно Італія, на відміну від голого «it»."""
        assert scan_entities("донори в зоні .it").country is country_by_code("it")
        assert scan_entities("донори .in").country is country_by_code("in")

    def test_короткий_код_дозволено_коли_питали_прямо(self):
        """Якщо бот спитав «яка країна?», відповідь «fr» зрозуміла."""
        assert resolve_country("fr", allow_short=True) is country_by_code("fr")
        assert resolve_language("fr", allow_short=True) is language_by_code("fr")


class TestСканВільногоТексту:
    def test_реальний_запит_по_країні(self):
        scan = scan_entities("Скільки у нас донорів по Британії в Меджику з трафіком від 1?")
        assert scan.country is country_by_code("gb")
        assert scan.language is None

    def test_реальний_запит_по_мові(self):
        scan = scan_entities("Скільки французькомовних донорів з трафіком від 5?")
        assert scan.language is language_by_code("fr")
        assert scan.country is None

    def test_глобальні_зони_окремо(self):
        scan = scan_entities("донори .com і .net")
        assert scan.global_zones == (".com", ".net")
        assert scan.zones == ()
        assert scan.country is None

    def test_порожній_запит(self):
        assert scan_entities("").is_empty
        assert scan_entities("   ").is_empty
        assert scan_entities("абракадабра щось незрозуміле").is_empty


class TestПідказкаПереплутаногоРежиму:
    """У мовному режимі ввели країну/зону (або навпаки) — потрібна підказка,
    а не порожній результат."""

    def test_зона_в_мовному_режимі_дає_підказку(self):
        """«.ua» — доменна зона, а не мова. Пропонуємо Україну і українську."""
        hint = hint_for_language_mode(".ua")
        assert hint is not None
        assert hint.country is country_by_code("ua")
        assert hint.language is language_by_code("uk")
        assert hint.via_zone is True

    def test_назва_країни_в_мовному_режимі_дає_підказку(self):
        hint = hint_for_language_mode("Німеччина")
        assert hint is not None
        assert hint.country is country_by_code("de")
        assert hint.language is language_by_code("de")
        assert hint.via_zone is False, "назву ввели словом, не зоною"

    def test_глобальна_зона_не_дає_підказки(self):
        """«.com» нікому не належить — це не країна, підказки немає."""
        assert hint_for_language_mode(".com") is None

    def test_нерозпізнане_в_мовному_режимі_без_підказки(self):
        assert hint_for_language_mode("абракадабра") is None

    def test_мова_в_країновому_режимі_дає_дзеркальну_підказку(self):
        """«Ukrainian» / «українською» — мова, а не країна."""
        for text in ("Ukrainian", "українською"):
            hint = hint_for_country_mode(text)
            assert hint is not None, text
            assert hint.language is language_by_code("uk")
            assert hint.country is country_by_code("ua"), "українською володіє одна країна"

    def test_мова_кількох_країн_без_однозначної_країни(self):
        """Німецькою пишуть у трьох країнах — конкретної країни в підказці немає."""
        hint = hint_for_country_mode("німецькою")
        assert hint is not None
        assert hint.language is language_by_code("de")
        assert hint.country is None, "де/ат/ch — однозначної країни немає"

    def test_країна_в_країновому_режимі_без_підказки(self):
        """«Німеччина» — це справді країна, дзеркальна підказка не потрібна."""
        assert hint_for_country_mode("Німеччина") is None

    def test_нерозпізнане_в_країновому_режимі_без_підказки(self):
        assert hint_for_country_mode("абракадабра") is None
