"""Фейковий датасет для тестів.

Тести НЕ ходять у справжню таблицю: вони мають працювати без інтернету,
без ключа Google і давати однаковий результат щоразу.

Датасет зібраний навмисно так, щоб на ньому можна було перевірити всю
ключову логіку. Числа нижче — це «правильні відповіді», на які спираються
тести, тому міняти рядки треба обережно.

    ЗОНА .de .................. 6 донорів
    мова German .............. 8 донорів (4 у зоні .de + 4 поза нею)
    → мовний додаток для Німеччини = 4   (6 і 4 НЕ сумуються!)

    ЗОНА .fr .................. 3 донори
    мова French .............. 3 донори (2 у зоні .fr + 1 поза нею)
    → мовний додаток для Франції = 1, попередження НЕ показується

    ЗОНА .co.uk/.uk ........... 3 донори
    мова English ............. 7 донорів (3 у зоні + 4 поза нею)
    → мовний додаток для Британії = 4, попередження показується

Також усередині є «брудні» значення: "n/a" у DR, трафік "4 800" і "1,200",
мова з хвостовим пробілом, рядок без домену і повністю порожній рядок.
"""

from __future__ import annotations

# Скільки рядків очікувано стане донорами, а скільки — ні.
EXPECTED_DONORS = 24
EXPECTED_SKIPPED = 1  # один рядок має дані, але не має домену

# Очікувані числа для перевірок моделі гео.
GERMANY_ZONE_COUNT = 6
GERMAN_LANGUAGE_TOTAL = 8
GERMAN_LANGUAGE_OUTSIDE_ZONE = 4

FRANCE_ZONE_COUNT = 3
FRENCH_LANGUAGE_TOTAL = 3
FRENCH_LANGUAGE_OUTSIDE_ZONE = 1

UK_ZONE_COUNT = 3
ENGLISH_LANGUAGE_TOTAL = 7
ENGLISH_LANGUAGE_OUTSIDE_ZONE = 4


def magic_rows() -> list[dict[str, str]]:
    """Сирі рядки «Меджика» — у тому вигляді, у якому їх віддає Google Sheets.

    Ключі — це РОЛІ колонок (так само, як їх повертає SheetsReader).
    """
    return [
        # --- Німеччина, зона .de (6 донорів) --------------------------------
        {"domain": "de1.de", "language": "German", "dr": "40", "traffic": "4 800"},
        {"domain": "de2.de", "language": "German", "dr": "n/a", "traffic": "1,200"},
        {"domain": "de3.de", "language": "English", "dr": "25", "traffic": "500"},
        {"domain": "de4.de", "language": "German", "dr": "55", "traffic": "12K"},
        {"domain": "shop.de5.de", "language": "german", "dr": "10", "traffic": "100"},
        # турецькомовний сайт у зоні .de — зона й мова це різні речі
        {"domain": "de6.de", "language": "Turkish", "dr": "30", "traffic": "200"},
        # --- німецька мова ПОЗА зоною .de (4 донори) ------------------------
        {"domain": "at1.at", "language": "German", "dr": "35", "traffic": "900"},
        {"domain": "ch1.ch", "language": "German", "dr": "20", "traffic": "700"},
        {"domain": "glob1.com", "language": "German", "dr": "45", "traffic": "3000"},
        {"domain": "glob2.net", "language": "German ", "dr": "15", "traffic": "50"},
        # --- Франція, зона .fr (3 донори) -----------------------------------
        {"domain": "fr1.fr", "language": "French", "dr": "30", "traffic": "400"},
        {"domain": "fr2.fr", "language": "French", "dr": "22", "traffic": "150"},
        {"domain": "fr3.fr", "language": "English", "dr": "18", "traffic": "90"},
        # французька мова поза зоною .fr (1 донор)
        {"domain": "be1.be", "language": "French", "dr": "28", "traffic": "300"},
        # --- Британія, зони .co.uk і .uk (3 донори) -------------------------
        {
            "domain": "https://www.uk1.co.uk/blog",
            "language": "English",
            "dr": "50",
            "traffic": "5000",
        },
        {"domain": "uk2.co.uk", "language": "English", "dr": "33", "traffic": "800"},
        {"domain": "uk3.uk", "language": "English", "dr": "12", "traffic": "60"},
        # --- англійська мова у глобальних зонах -----------------------------
        {"domain": "glob3.com", "language": "English", "dr": "60", "traffic": "10000"},
        {"domain": "glob4.org", "language": "english", "dr": "5", "traffic": "20"},
        # --- мови, які мають розпізнаватися окремо --------------------------
        {"domain": "tr1.com.tr", "language": "Turkish", "dr": "26", "traffic": "600"},
        {"domain": "cn1.cn", "language": "Chinese", "dr": "44", "traffic": "7000"},
        {"domain": "vn1.vn", "language": "Vietnamese", "dr": "19", "traffic": "250"},
        # --- іспанська: одна в зоні .es, одна в глобальній .online ----------
        {"domain": "es1.es", "language": "Spanish", "dr": "21", "traffic": "320"},
        {"domain": "glob5.online", "language": "Spanish", "dr": "8", "traffic": "40"},
        # --- «брудні» рядки -------------------------------------------------
        # має дані, але немає домену → рахується як пропущений
        {"domain": "", "language": "Polish", "dr": "10", "traffic": "10"},
        # повністю порожній рядок → просто хвіст таблиці, не помилка
        {"domain": "", "language": "", "dr": "", "traffic": ""},
    ]


def empty_rows() -> list[dict[str, str]]:
    """Порожній аркуш — на випадок тестів «база порожня, але не падає»."""
    return []


# ---------------------------------------------------------------------------
# Фейкові «Морди» — з аналізом заспамленості.
#
# Ключі outlinks і spam — це РОЛІ колонок «Вихідні» і «Заспамлені».
# Заспамленість = spam / outlinks × 100%. Підібрано так, щоб покрити всі
# випадки з ТЗ: звичайні числа, 0 вихідних, заспамлені всі, порожні.
#
#   домен      вихідні  заспамлені  заспамленість
#   m1.de        16        10         62.5%   ← приклад із ТЗ: 10 з 16
#   m2.fr        20         5         25%
#   glob.com     52        52         100%    ← заспамлені всі
#   m4.de         0         0         невизначена (0 вихідних)
#   uk1.co.uk    10         0         0%      ← жодного заспамленого
#   m6.pl        ""        ""         невизначена (порожні клітинки)
#   m7.de         8         4         50%     (DR = n/a — стійкість)
# ---------------------------------------------------------------------------

MORDY_DONORS = 7
MORDY_OUTLINKS_SAMPLE = 6  # усі, крім m6 з порожніми клітинками
MORDY_SPAM_SAMPLE = 5  # усі, крім m4 (0 вихідних) і m6 (порожні)
MORDY_AVG_OUTLINKS = 17.7  # (16+20+52+0+10+8) / 6
MORDY_AVG_SPAM_PERCENT = 47.5  # (62.5+25+100+0+50) / 5


def mordy_rows() -> list[dict[str, str]]:
    """Сирі рядки «Морд» разом з колонками вихідних лінків і заспамленості."""
    return [
        {"domain": "m1.de", "language": "German", "dr": "30", "traffic": "500",
         "outlinks": "16", "spam": "10"},
        {"domain": "m2.fr", "language": "French", "dr": "25", "traffic": "300",
         "outlinks": "20", "spam": "5"},
        {"domain": "glob.com", "language": "English", "dr": "40", "traffic": "1000",
         "outlinks": "52", "spam": "52"},
        # 0 вихідних — заспамленість невизначена; рядок лишається в кількості
        {"domain": "m4.de", "language": "German", "dr": "15", "traffic": "100",
         "outlinks": "0", "spam": "0"},
        {"domain": "uk1.co.uk", "language": "English", "dr": "50", "traffic": "2000",
         "outlinks": "10", "spam": "0"},
        # порожні клітинки (формула дала "") — не падаємо
        {"domain": "m6.pl", "language": "Polish", "dr": "20", "traffic": "200",
         "outlinks": "", "spam": ""},
        # DR "n/a" разом із заспамленістю — перевірка стійкості
        {"domain": "m7.de", "language": "German", "dr": "n/a", "traffic": "400",
         "outlinks": "8", "spam": "4"},
    ]  # fmt: skip


class FakeReader:
    """Підміна SheetsReader для тестів: віддає підготовлені рядки замість Google."""

    def __init__(
        self,
        rows_by_section: dict[str, list[dict[str, str]]] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self.rows_by_section = rows_by_section or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def read_section(self, section) -> list[dict[str, str]]:
        self.calls.append(section.key)
        if section.key in self.errors:
            raise RuntimeError(self.errors[section.key])
        return [dict(row) for row in self.rows_by_section.get(section.key, [])]
