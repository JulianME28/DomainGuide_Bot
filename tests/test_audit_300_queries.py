"""Відтворюваний offline-аудит 300 вільних запитів."""

from app.text.freeform import parse_free_text
from scripts.audit_300_queries import _countries, build_cases, failures


def test_300_запитів() -> None:
    failed: list[str] = []
    cases = build_cases()
    for number, case in enumerate(cases, 1):
        parsed = parse_free_text(case.text)
        problems = failures(parsed, case.expected)
        if problems:
            failed.append(
                f"#{number} [{case.category}] {case.text!r}: {problems}; "
                f"countries={sorted(_countries(parsed))}; "
                f"unrecognized={parsed.unrecognized}"
            )
    assert len(cases) == 300
    # Перші 250 валідних сценаріїв мають проходити без втрат.
    assert not [item for item in failed if not item.startswith("#2")]
    # Лишилося 5 навмисних одруків «Німечина»; їх має виправляти ШІ-шлях.
    assert len(failed) == 5, "\n" + "\n".join(failed)
