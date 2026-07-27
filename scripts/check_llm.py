"""Мінімальна діагностика виклику Anthropic API — окремо від бота.

Робить ОДИН найпростіший запит тим самим ключем із .env і друкує або сиру
відповідь, або ПОВНУ помилку (тип, repr, причину-обгортку, трасування). Так
видно чисту причину — SSLError / ConnectTimeout / ConnectionError / ProxyError
тощо — без шуму бота.

Ключ НЕ друкується (лише факт наявності й довжина, для перевірки, що він не
порожній і не обрізаний).

Запуск (з кореня проєкту):

    .venv\\Scripts\\python.exe scripts\\check_llm.py
"""

from __future__ import annotations

import sys
import traceback
import urllib.error
from pathlib import Path

# Дозволяємо запуск як окремого файлу: додаємо корінь проєкту в шлях імпорту.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.llm.provider import (  # noqa: E402 — після правки sys.path
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    _default_http_post,
)
from app.settings import SettingsError, load_settings  # noqa: E402


def main() -> int:
    try:
        settings = load_settings(require_token=False)
    except SettingsError as exc:
        print(f"❌ Не вдалося прочитати .env: {exc}")
        return 2

    if not settings.llm_enabled:
        print("⚠️ ШІ вимкнено в налаштуваннях — викликати нема чого.")
        print(f"   LLM_PROVIDER = {settings.llm_provider!r} (потрібно 'anthropic')")
        print(f"   LLM_API_KEY заданий: {bool(settings.llm_api_key)}")
        print("   Впишіть у .env: LLM_PROVIDER=anthropic і LLM_API_KEY=<ключ>.")
        return 2

    key = settings.llm_api_key
    print("── Налаштування ─────────────────────────────")
    print(f"  Модель:    {settings.llm_model}")
    print(f"  Ендпойнт:  {ANTHROPIC_URL}")
    print(f"  Таймаут:   {settings.llm_timeout_seconds} с")
    print(f"  Ключ:      заданий, довжина {len(key)} (сам ключ не друкуємо)")
    print("─────────────────────────────────────────────\n")

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": settings.llm_model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    }

    print("Роблю один запит до Anthropic API...\n")
    try:
        data = _default_http_post(ANTHROPIC_URL, headers, body, settings.llm_timeout_seconds)
    except Exception as exc:
        print("❌ ПОМИЛКА виклику:")
        print(f"   тип:    {type(exc).__name__}")
        print(f"   repr:   {exc!r}")
        print(f"   reason: {getattr(exc, 'reason', None)!r}  (urllib кладе причину сюди)")
        print(f"   cause:  {exc.__cause__!r}  (httpx/обгортки — сюди)")

        # HTTPError означає, що запит ДІЙШОВ до API, а той відповів помилкою.
        # Тоді причина — в тілі відповіді (напр. «invalid x-api-key», «model not
        # found»), а не в мережі. Друкуємо статус і тіло (ключа там немає).
        if isinstance(exc, urllib.error.HTTPError):
            print(f"\n   HTTP статус: {exc.code} {exc.reason}")
            try:
                print(f"   тіло відповіді API: {exc.read().decode('utf-8', 'replace')}")
            except Exception as read_exc:
                print(f"   (не вдалося прочитати тіло: {read_exc!r})")
            print(
                "\n   ⇒ Це НЕ локальна мережа: запит дійшов до Anthropic. Дивіться "
                "статус/тіло:\n"
                "     401/403 → ключ недійсний або немає доступу;\n"
                "     404 → невідома модель (перевірте LLM_MODEL);\n"
                "     400 → проблема з форматом запиту; 429 → ліміт."
            )
        else:
            print(
                "\n   ⇒ Схоже на ЛОКАЛЬНУ мережу. Причини:\n"
                "     SSLError → сертифікати/антивірус/проксі;\n"
                "     ConnectTimeout/ConnectionError → фаєрвол/проксі/немає доступу\n"
                "       до api.anthropic.com; ProxyError → блокує системний проксі."
            )

        print("\n── повне трасування ─────────────────────────")
        traceback.print_exc(file=sys.stdout)
        return 1

    print("✅ УСПІХ. Сира відповідь від API:\n")
    print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
