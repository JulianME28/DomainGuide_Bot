"""Фейковий датасет для тестів.

Тести НЕ ходять у справжню таблицю: вони мають працювати без інтернету,
без ключа Google і давати однаковий результат щоразу.

Датасет зібраний навмисно так, щоб на ньому можна було перевірити всю
ключову логіку. Числа нижче — це «правильні відповіді», на які спираються
тести, тому міняти рядки треба обережно.

МОДЕЛЬ КРАЇНИ. Підсумок: зона + GEO + мова-на-нейтральних. АЛЕ для СПІЛЬНИХ
мов (en, es, pt, ar) мова-на-нейтральних у підсумок НЕ входить — вона окремим
рядком. Букети не перетинаються (пріоритет зона → GEO → мова).

    Франція (fr, однозначна): зона .fr=3 | мова-на-GLOBAL=0 | GEO=2 → підсумок 5
              «на зонах інших країн» = 1 (be1 на .be)
    Німеччина (de, однозначна): зона .de=6 | мова-на-GLOBAL=2 (glob1,glob2) | GEO=1 (es1) → 9
              «на зонах інших країн» = 2 (at1 на .at, ch1 на .ch)
    Британія (gb, СПІЛЬНА): зона .co.uk/.uk=3 | GEO=0 → підсумок 3 (мова НЕ в підсумку)
              «на нейтральних зонах» = 2 (glob3,glob4);  «на інших зонах» = 2 (de3,fr3)
    Іспанія (es, СПІЛЬНА): зона .es=1 | GEO=0 → підсумок 1
              «на нейтральних зонах» = 1 (glob5.online)

GEO-колонка (формат «(cc, N)») додана лише кільком рядкам:
    de6 «(fr,5000)» — приклад: .de-зона + GEO(fr) → Німеччина=зона, Франція=GEO
    cn1 «(fr,3000)» — France→GEO;  vn1 «(fr,0)» — НЕ рахується (N=0)
    es1 «(de,900)»  — Germany→GEO; de1 «(de,1000)» — зона важливіша за GEO

Також усередині є «брудні» значення: "n/a" у DR, трафік "4 800" і "1,200",
мова з хвостовим пробілом, рядок без домену і повністю порожній рядок.
"""

from __future__ import annotations

# Скільки рядків очікувано стане донорами, а скільки — ні.
EXPECTED_DONORS = 24
EXPECTED_SKIPPED = 1  # один рядок має дані, але не має домену

# «Сирі» мовні підсумки — для запитів ПРО МОВУ (не про країну).
GERMAN_LANGUAGE_TOTAL = 8  # усі німецькомовні донори
FRENCH_LANGUAGE_TOTAL = 3  # fr1, fr2, be1 (fr3 — англійською)
ENGLISH_LANGUAGE_TOTAL = 7

# Підсумок країни: (зона, мова-на-нейтральних, GEO, підсумок, «на інших зонах»).
# Однозначні мови (fr, de): мова входить у підсумок.
FRANCE_ZONE, FRANCE_LANG, FRANCE_GEO, FRANCE_TOTAL, FRANCE_ADDENDUM = 3, 0, 2, 5, 1
GERMANY_ZONE, GERMANY_LANG, GERMANY_GEO, GERMANY_TOTAL, GERMANY_ADDENDUM = 6, 2, 1, 9, 2

# Спільні мови (gb, es): підсумок = зона + GEO; мова — окремим нейтральним рядком.
UK_ZONE, UK_GEO, UK_TOTAL = 3, 0, 3
UK_NEUTRAL = 2  # англійською на нейтральних зонах (glob3, glob4) — НЕ в підсумку
UK_ADDENDUM = 2  # англійською на зонах інших країн (de3, fr3)
SPAIN_TOTAL, SPAIN_NEUTRAL = 1, 1  # es1 у підсумку; glob5.online — нейтральний рядок


def magic_rows() -> list[dict[str, str]]:
    """Сирі рядки «Меджика» — у тому вигляді, у якому їх віддає Google Sheets.

    Ключі — це РОЛІ колонок (так само, як їх повертає SheetsReader).
    """
    return [
        # --- Німеччина, зона .de (6 донорів) --------------------------------
        # de1: GEO(de) — але зона .de важливіша (крок зони, не GEO)
        {
            "domain": "de1.de",
            "language": "German",
            "dr": "40",
            "traffic": "4 800",
            "geo": "(de, 1000)",
        },
        {"domain": "de2.de", "language": "German", "dr": "n/a", "traffic": "1,200"},
        {"domain": "de3.de", "language": "English", "dr": "25", "traffic": "500"},
        {"domain": "de4.de", "language": "German", "dr": "55", "traffic": "12K"},
        {"domain": "shop.de5.de", "language": "german", "dr": "10", "traffic": "100"},
        # de6: .de-зона + GEO(fr) — Німеччина рахує його в зону, Франція в GEO
        {
            "domain": "de6.de",
            "language": "Turkish",
            "dr": "30",
            "traffic": "200",
            "geo": "(fr, 5000)",
        },
        # --- німецька мова ПОЗА зоною .de (4 донори) ------------------------
        {"domain": "at1.at", "language": "German", "dr": "35", "traffic": "900"},
        {"domain": "ch1.ch", "language": "German", "dr": "20", "traffic": "700"},
        {"domain": "glob1.com", "language": "German", "dr": "45", "traffic": "3000"},
        {"domain": "glob2.net", "language": "German ", "dr": "15", "traffic": "50"},
        # --- Франція, зона .fr (3 донори) -----------------------------------
        {"domain": "fr1.fr", "language": "French", "dr": "30", "traffic": "400"},
        {"domain": "fr2.fr", "language": "French", "dr": "22", "traffic": "150"},
        {"domain": "fr3.fr", "language": "English", "dr": "18", "traffic": "90"},
        # французька мова поза зоною .fr (1 донор) — додаток «на зонах інших країн»
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
        # cn1: GEO(fr) — France→GEO
        {
            "domain": "cn1.cn",
            "language": "Chinese",
            "dr": "44",
            "traffic": "7000",
            "geo": "(fr, 3000)",
        },
        # vn1: GEO(fr, 0) — N=0, у GEO-крок НЕ входить
        {
            "domain": "vn1.vn",
            "language": "Vietnamese",
            "dr": "19",
            "traffic": "250",
            "geo": "(fr, 0)",
        },
        # --- іспанська: одна в зоні .es (GEO de → Germany→GEO), одна глобальна
        {
            "domain": "es1.es",
            "language": "Spanish",
            "dr": "21",
            "traffic": "320",
            "geo": "(de, 900)",
        },
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
# Фейкові «Морди» — з аналізом заспамленості І колонкою GEO (той самий формат
# «(cc, N)», що й у Меджику).
#
# Заспамленість = spam / outlinks × 100%. GEO покриває: зону-важливішу-за-GEO,
# GEO як третій крок, N=0 (не рахується), порожнє й бите значення.
#
#   домен      вихідні  заспамл.  заспамл.%   GEO         роль для Німеччини
#   m1.de        16        10       62.5%     (de, 500)   зона .de (GEO не важить)
#   m2.fr        20         5       25%       (de, 900)   GEO (de) → крок GEO
#   glob.com     52        52       100%      —           не Німеччина
#   m4.de         0         0       невизн.   (fr, 0)     зона .de; GEO(fr,0) N=0
#   uk1.co.uk    10         0       0%        —           не Німеччина
#   m6.pl        ""        ""       невизн.   ""          порожня GEO
#   m7.de         8         4       50%       "битий"     зона .de; GEO биту ігнор.
#
# Німеччина трикроково: зона .de (m1,m4,m7)=3 | мова=0 | GEO (m2)=1 → підсумок 4.
# ---------------------------------------------------------------------------

MORDY_DONORS = 7
MORDY_OUTLINKS_SAMPLE = 6  # усі, крім m6 з порожніми клітинками
MORDY_SPAM_SAMPLE = 5  # усі, крім m4 (0 вихідних) і m6 (порожні)
MORDY_AVG_OUTLINKS = 17.7  # (16+20+52+0+10+8) / 6
MORDY_AVG_SPAM_PERCENT = 47.5  # (62.5+25+100+0+50) / 5

# Трикроковий підсумок країни в «Мордах» (Німеччина).
MORDY_DE_ZONE, MORDY_DE_GEO, MORDY_DE_TOTAL = 3, 1, 4


def mordy_rows() -> list[dict[str, str]]:
    """Сирі рядки «Морд»: вихідні лінки, заспамленість і GEO у форматі (cc, N)."""
    return [
        # m1: GEO(de) — але зона .de важливіша (зона важить більше за GEO)
        {"domain": "m1.de", "language": "German", "dr": "30", "traffic": "500",
         "outlinks": "16", "spam": "10", "geo": "(de, 500)"},
        # m2: зона .fr, але GEO(de, 900) → у запиті про Німеччину це крок GEO
        {"domain": "m2.fr", "language": "French", "dr": "25", "traffic": "300",
         "outlinks": "20", "spam": "5", "geo": "(de, 900)"},
        {"domain": "glob.com", "language": "English", "dr": "40", "traffic": "1000",
         "outlinks": "52", "spam": "52"},
        # 0 вихідних — заспамленість невизначена; GEO(fr, 0) — N=0, не рахується
        {"domain": "m4.de", "language": "German", "dr": "15", "traffic": "100",
         "outlinks": "0", "spam": "0", "geo": "(fr, 0)"},
        {"domain": "uk1.co.uk", "language": "English", "dr": "50", "traffic": "2000",
         "outlinks": "10", "spam": "0"},
        # порожні клітинки (формула дала "") — не падаємо; GEO теж порожня
        {"domain": "m6.pl", "language": "Polish", "dr": "20", "traffic": "200",
         "outlinks": "", "spam": "", "geo": ""},
        # DR "n/a" + бита GEO — перевірка стійкості (битий формат → немає GEO)
        {"domain": "m7.de", "language": "German", "dr": "n/a", "traffic": "400",
         "outlinks": "8", "spam": "4", "geo": "битий-формат"},
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
        # Розділи, читання яких зараз падає з мережевою помилкою — для
        # перевірки, що репозиторій віддає кеш замість «база недоступна».
        self.failing: dict[str, Exception] = {}

    def fail_with(self, section_key: str, exc: Exception) -> None:
        """Змусити наступні читання цього розділу падати з exc (імітація мережі)."""
        self.failing[section_key] = exc

    def recover(self, section_key: str) -> None:
        """Мережа відновилася — читання знову працює."""
        self.failing.pop(section_key, None)

    def read_section(self, section) -> list[dict[str, str]]:
        self.calls.append(section.key)
        if section.key in self.failing:
            raise self.failing[section.key]
        if section.key in self.errors:
            raise RuntimeError(self.errors[section.key])
        return [dict(row) for row in self.rows_by_section.get(section.key, [])]
