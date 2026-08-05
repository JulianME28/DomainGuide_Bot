"""Third offline audit: 400 longer, compound free-form requests, no API calls."""

from __future__ import annotations

from scripts.audit_300_queries import Case

COUNTRIES = (
    ("Німеччині", "de"),
    ("Франції", "fr"),
    ("Канаді", "ca"),
    ("Болгарії", "bg"),
    ("Британії", "gb"),
)


def build_cases_round3() -> list[Case]:
    cases: list[Case] = []

    def add(category: str, text: str, **expected: object) -> None:
        cases.append(Case(category, text, expected))

    # 1. Three simultaneous numeric filters, reordered and embedded in prose.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        dr_low, dr_high = 10 + index, 60 + index
        traffic_low, spam_high = 100 + index * 10, 5 + index
        if index % 2 == 0:
            add(
                "складені метрики",
                f"Порахуй, будь ласка, у Морд для {country} донорів: трафік понад {traffic_low}, "
                f"DR у межах від {dr_low} до {dr_high}, а заспамленість не вище {spam_high}",
                section="mordy",
                countries={code},
                dr_min=dr_low,
                dr_max=dr_high,
                traffic_min=traffic_low,
                spam_max=spam_high,
            )
        else:
            add(
                "складені метрики",
                f"У базі Меджик по {country} покажи DR не нижче {dr_low} і не вище {dr_high}; "
                f"трафік вище {traffic_low}",
                section="magic",
                countries={code},
                dr_min=dr_low,
                dr_max=dr_high,
                traffic_min=traffic_low,
            )

    # 2. Synonyms and direct ranges, including reversed bounds normalized as intervals.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        low, high = 20 + index, 120 + index
        mode = index % 5
        if mode == 0:
            add(
                "синоніми й інтервали",
                f"Меджик по {country}: трафік вище {high}, DR нижче {low}",
                countries={code},
                traffic_min=high,
                dr_max=low,
            )
        elif mode == 1:
            add(
                "синоніми й інтервали",
                f"Морди {country}: трафік не нижче {high}, DR не вище {low}",
                countries={code},
                traffic_min=high,
                dr_max=low,
            )
        elif mode == 2:
            add(
                "синоніми й інтервали",
                f"Меджик {country}: DR від {high} до {low}, трафік понад {high}",
                countries={code},
                dr_min=low,
                dr_max=high,
                traffic_min=high,
            )
        elif mode == 3:
            add(
                "синоніми й інтервали",
                f"Морди {country}: заспамленість від {high} до {low}, DR понад {low}",
                countries={code},
                spam_min=low,
                spam_max=high,
                dr_min=low,
            )
        else:
            add(
                "синоніми й інтервали",
                f"Меджик {country}: DR більше {low}, але менше {high}",
                countries={code},
                dr_min=low,
                dr_max=high,
            )

    # 3. Several included countries plus one explicit exclusion.
    positive_sets = (
        ("Німеччина, Канада й Британія", {"de", "ca", "gb"}, "Франції", "fr"),
        ("Франція, Болгарія та Канада", {"fr", "bg", "ca"}, "Німеччини", "de"),
        ("Британія, Німеччина і Франція", {"gb", "de", "fr"}, "Канади", "ca"),
        ("Канада, Франція й Болгарія", {"ca", "fr", "bg"}, "Британії", "gb"),
        ("Болгарія, Британія та Німеччина", {"bg", "gb", "de"}, "Франції", "fr"),
    )
    for index in range(50):
        names, codes, excluded_name, excluded_code = positive_sets[index % 5]
        value = 25 + index
        marker = ("крім", "окрім", "за винятком", "крім", "окрім")[index % 5]
        add(
            "країни та виключення",
            f"У Меджик перевір {names}, {marker} {excluded_name}; "
            f"DR від {value} і трафік від {value * 10}",
            countries=codes,
            excluded_countries={excluded_code},
            dr_min=value,
            traffic_min=value * 10,
        )

    # 4. Explicit both-base requests versus intentionally ambiguous exclusive choice.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        value = 30 + index
        if index % 2 == 0:
            form = ("Меджик і Морди", "Морди та Меджик", "в обох базах")[index % 3]
            add(
                "маршрутизація баз",
                f"{form}: по {country} DR від {value}, трафік до {value * 20}",
                both_bases=True,
                countries={code},
                dr_min=value,
                traffic_max=value * 20,
            )
        else:
            form = ("Меджик або Морди, але не обидві", "Морди або Меджик, проте не обидві")[
                index % 2
            ]
            add("маршрутизація баз", f"{form}: по {country} DR від {value}", ambiguous_bases=True)

    # 5. Country, GEO, language and zone must remain separate dimensions.
    for index in range(50):
        value, mode = 15 + index, index % 5
        if mode == 0:
            add(
                "суміжні виміри",
                f"Меджик Німеччина, GEO Франція, англійською, DR від {value}",
                countries={"de"},
                geo="fr",
                languages={"en"},
                dr_min=value,
            )
        elif mode == 1:
            add(
                "суміжні виміри",
                f"Морди Канада, трафік із Німеччини, французькою, DR до {value}",
                countries={"ca"},
                geo="de",
                languages={"fr"},
                dr_max=value,
            )
        elif mode == 2:
            add(
                "суміжні виміри",
                f"Меджик лише в зоні .org, німецькою, трафік понад {value * 10}",
                zones={".org"},
                languages={"de"},
                traffic_min=value * 10,
            )
        elif mode == 3:
            add(
                "суміжні виміри",
                f"Морди у доменній зоні .com, англійською, DR від {value}",
                zones={".com"},
                languages={"en"},
                dr_min=value,
            )
        else:
            add(
                "суміжні виміри",
                f"Меджик Болгарія, гео Канада, французькою, трафік до {value * 10}",
                countries={"bg"},
                geo="ca",
                languages={"fr"},
                traffic_max=value * 10,
            )

    # 6. Negation and cancellation must not leak a forbidden metric into the query.
    for index in range(50):
        country, code = COUNTRIES[index % 5]
        value, mode = 40 + index, index % 5
        if mode == 0:
            add(
                "заперечення",
                f"Меджик {country}: не DR від {value}, зате трафік від {value * 10}",
                countries={code},
                dr_none=True,
                traffic_min=value * 10,
            )
        elif mode == 1:
            add(
                "заперечення",
                f"Морди {country}: DR будь-який, трафік не нижче {value * 10}",
                countries={code},
                dr_none=True,
                traffic_min=value * 10,
            )
        elif mode == 2:
            add(
                "заперечення",
                f"Морди {country}: трафік не важливий, DR понад {value}",
                countries={code},
                traffic_none=True,
                dr_min=value,
            )
        elif mode == 3:
            add(
                "заперечення",
                f"Морди {country}: заспамленість будь-яка, DR до {value}",
                countries={code},
                spam_none=True,
                dr_max=value,
            )
        else:
            add(
                "заперечення",
                f"Меджик не {country}, трафік вище {value * 10}",
                excluded_countries={code},
                traffic_min=value * 10,
            )

    # 7. Requests that alter output/operation while retaining all filters.
    for index in range(50):
        value, mode = 50 + index, index % 5
        if mode == 0:
            add(
                "операції та рекомендації",
                f"Морди: чи вистачить по {value} донорів для Німеччини, Канади й Франції?",
                countries={"de", "ca", "fr"},
                wants_coverage=True,
            )
        elif mode == 1:
            add(
                "операції та рекомендації",
                f"Меджик Канада, DR від {value}; якщо замало — запропонуй схожі варіанти",
                countries={"ca"},
                dr_min=value,
                request_marker=True,
            )
        elif mode == 2:
            add(
                "операції та рекомендації",
                f"Зроби розбивку по країнах у Мордах з DR понад {value} "
                f"і трафіком від {value * 10}",
                wants_country_breakdown=True,
                dr_min=value,
                traffic_min=value * 10,
            )
        elif mode == 3:
            add(
                "операції та рекомендації",
                f"По обох базах Німеччина й Британія, DR від {value}; якщо мало — підбери схожі",
                both_bases=True,
                countries={"de", "gb"},
                dr_min=value,
                request_marker=True,
            )
        else:
            add(
                "операції та рекомендації",
                f"Чи закриваємо потребу по Канаді та Болгарії по {value} донорів у Меджик?",
                countries={"ca", "bg"},
                wants_coverage=True,
            )

    # 8. Ill-formed, unsupported and incomplete requests must fail safely.
    for index in range(50):
        value, mode = 70 + index, index % 5
        if mode == 0:
            add(
                "помилкові й неповні",
                f"Знайди мені найкращі донори для проєкту номер {value}",
                clarify=True,
            )
        elif mode == 1:
            add(
                "помилкові й неповні",
                f"Меджик Атлантида, DR від {value}, мовою ельфів",
                unrecognized=True,
            )
        elif mode == 2:
            add(
                "помилкові й неповні",
                f"Морди трафік приблизно багато, контрольне число {value}",
                metric_missing=True,
            )
        elif mode == 3:
            add(
                "помилкові й неповні",
                f"Меджик або Морди, але не обидві; трафік вище {value}",
                ambiguous_bases=True,
            )
        else:
            add(
                "помилкові й неповні",
                f"Покажи щось прийнятне за якістю, бюджет {value}",
                clarify=True,
            )

    assert len(cases) == 400
    texts = [case.text for case in cases]
    assert len(texts) == len(set(texts)), "Audit texts must be unique"
    return cases
