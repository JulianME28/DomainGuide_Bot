"""Second offline audit: 300 new natural-language requests, no API calls."""

from __future__ import annotations

from scripts.audit_300_queries import Case

COUNTRIES = (
    ("Німеччині", "de"),
    ("Франції", "fr"),
    ("Канаді", "ca"),
    ("Болгарії", "bg"),
    ("Британії", "gb"),
)


def build_cases_round2() -> list[Case]:
    cases: list[Case] = []

    def add(category: str, text: str, **expected: object) -> None:
        cases.append(Case(category, text, expected))

    # 1. Природні питання: інші слова й порядок частин, ніж у першому аудиті.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        value = 11 + index
        mode = index % 5
        if mode == 0:
            add(
                "природні",
                f"Скільки в Меджику донорів по {country} з DR від {value}?",
                section="magic",
                countries={code},
                dr_min=value,
            )
        elif mode == 1:
            add(
                "природні",
                f"Чи є в Мордах по {country} трафік понад {value}?",
                section="mordy",
                countries={code},
                traffic_min=value,
            )
        elif mode == 2:
            add(
                "природні",
                f"Порахуй Меджик для {country}, DR не менше {value}",
                section="magic",
                countries={code},
                dr_min=value,
            )
        elif mode == 3:
            add(
                "природні",
                f"Морди по {country}: заспамленість не більше {value}",
                section="mordy",
                countries={code},
                spam_max=value,
            )
        else:
            add(
                "природні",
                f"Знайди по {country} трафік вище {value}, база Меджик",
                section="magic",
                countries={code},
                traffic_min=value,
            )

    # 2. Валідні діапазони та явне скасування окремих вимірів.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        low, high, mode = 10 + index, 100 + index, index % 5
        if mode == 0:
            add(
                "межі",
                f"Меджик {country}: DR від {low} до {high}",
                countries={code},
                dr_min=low,
                dr_max=high,
            )
        elif mode == 1:
            add(
                "межі",
                f"Морди {country}: трафік від {low} до {high}",
                countries={code},
                traffic_min=low,
                traffic_max=high,
            )
        elif mode == 2:
            add(
                "межі",
                f"Меджик {country}, будь-який DR, трафік від {high}",
                countries={code},
                dr_none=True,
                traffic_min=high,
            )
        elif mode == 3:
            add(
                "межі",
                f"Морди {country}, трафік не важливий, DR від {low}",
                countries={code},
                dr_min=low,
            )
        else:
            add(
                "межі",
                f"Морди {country}, заспамленість від {low} до {high}",
                countries={code},
                spam_min=low,
                spam_max=high,
            )

    # 3. Вибір баз: жодної, одна явна, або всі в різних формулюваннях.
    base_forms = ("серед всіх баз", "обидві бази", "Меджик та Морди")
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        value, mode = 15 + index, index % 5
        if mode == 0:
            add(
                "вибір баз",
                f"Покажи {country} з DR від {value}",
                section_named=False,
                countries={code},
                dr_min=value,
            )
        elif mode in {1, 2, 3}:
            expected_bases = {"section_named": False} if mode == 1 else {"both_bases": True}
            add(
                "вибір баз",
                f"{base_forms[mode - 1]}: {country}, DR від {value}",
                countries={code},
                dr_min=value,
                **expected_bases,
            )
        else:
            add(
                "вибір баз",
                f"Лише Морди, {country}, DR від {value}",
                section="mordy",
                countries={code},
                dr_min=value,
            )

    # 4. Кілька країн, розклади та мовні умови.
    lists = (
        ("Німеччина, Франція", {"de", "fr"}),
        ("Канада, Британія", {"ca", "gb"}),
        ("Болгарія, Німеччина, Канада", {"bg", "de", "ca"}),
    )
    for index in range(50):
        names, codes = lists[index % 3]
        value, mode = 30 + index, index % 5
        if mode == 0:
            add("мультикраїнні", f"Меджик: {names}; DR від {value}", countries=codes, dr_min=value)
        elif mode == 1:
            add(
                "мультикраїнні",
                f"Морди: {names}; трафік від {value}",
                countries=codes,
                traffic_min=value,
            )
        elif mode == 2:
            add(
                "мультикраїнні",
                f"Порівняй по країнах {names}, DR від {value}",
                countries=codes,
                dr_min=value,
            )
        elif mode == 3:
            add(
                "мультикраїнні",
                f"Меджик {names}, англійською, DR від {value}",
                countries=codes,
                languages={"en"},
                dr_min=value,
            )
        else:
            add(
                "мультикраїнні",
                f"Які країни є в Мордах з DR від {value}?",
                dr_min=value,
                wants_country_breakdown=True,
            )

    # 5. GEO, зони, альтернативи та coverage-сигнали.
    for index in range(50):
        value, mode = 20 + index, index % 5
        if mode == 0:
            add(
                "контекстні",
                f"Меджик Німеччина, GEO Франція, трафік від {value}",
                countries={"de"},
                geo="fr",
                traffic_min=value,
            )
        elif mode == 1:
            add(
                "контекстні",
                f"Морди у доменній зоні .org, DR від {value}",
                zones={".org"},
                dr_min=value,
            )
        elif mode == 2:
            add(
                "контекстні",
                f"Меджик Канада DR від {value}; якщо замало, запропонуй схожі варіанти",
                countries={"ca"},
                dr_min=value,
                request_marker=True,
            )
        elif mode == 3:
            add(
                "контекстні",
                f"Морди: чи вистачає по {value} донорів для Німеччини і Канади?",
                countries={"de", "ca"},
                wants_coverage=True,
            )
        else:
            add(
                "контекстні",
                f"Меджик без гео, Болгарія, DR від {value}",
                countries={"bg"},
                dr_min=value,
            )

    # 6. Навмисно помилкові/неоднозначні запити. Число робить кожен текст унікальним.
    for index in range(50):
        value, mode = 70 + index, index % 10
        if mode == 0:
            add("помилкові", f"Покажи щось дуже хороше, рівень {value}", clarify=True)
        elif mode == 1:
            add("помилкові", f"Меджик не Німеччина, DR від {value}", negated_country_safe=True)
        elif mode == 2:
            add("помилкові", f"Морди трафік від {value} до 10", inverted_traffic=True)
        elif mode == 3:
            add("помилкові", f"Болгарея трафік від {value}", unrecognized=True)
        elif mode == 4:
            add("помилкові", f"Меджик трафік більше багатьо, приклад {value}", metric_missing=True)
        elif mode == 5:
            add("помилкові", f"Морди мовою ельфійською, DR від {value}", unrecognized=True)
        elif mode == 6:
            add("помилкові", f"Меджик крім Франції, трафік від {value}", excluded_country_safe=True)
        elif mode == 7:
            add("помилкові", f"Морди не DR від {value}", negated_metric_safe=True)
        elif mode == 8:
            add("помилкові", f"Меджик гео Атлантида, DR від {value}", unrecognized=True)
        else:
            add(
                "помилкові",
                f"Меджик або Морди, але не обидві, DR від {value}",
                ambiguous_bases=True,
            )

    assert len(cases) == 300
    texts = [case.text for case in cases]
    duplicates = sorted({text for text in texts if texts.count(text) > 1})
    assert not duplicates, duplicates
    return cases
