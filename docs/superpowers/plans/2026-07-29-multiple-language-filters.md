# Multiple Language Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add canonical multi-language query support with Ukrainian aliases and OR filtering while preserving the legacy single-language API.

**Architecture:** Centralize aliases and repeated language matching in the dictionary/resolver layer. Normalize legacy and new model inputs into `DonorQuery.languages`, then migrate parser, analytics, state, rendering, handlers, wizard, and LLM interpretation to the canonical tuple.

**Tech Stack:** Python 3.11+, dataclasses, aiogram 3.x, pytest, pytest-asyncio.

## Global Constraints

- `languages: tuple[Language, ...]` is canonical; `language` is compatibility-only.
- Multiple languages use OR semantics; an empty tuple applies no language filter.
- Preserve Latin country-code behavior and existing full language names.
- Do not change GEO, countries/zones, Google Sheets, recommendations, DR/traffic formulas, production configuration, secrets, or dependencies.
- Do not start the bot, call external APIs, or create a commit.

---

### Task 1: Canonical query model

**Files:**
- Modify: `app/analytics/query.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: normalized `DonorQuery.languages`, compatibility `DonorQuery.language`, and canonical `core_languages`.

- [ ] Add failing tests for legacy input, tuple input, stable merge, and deduplication.
- [ ] Run the focused model tests and confirm failures are caused by missing canonical support.
- [ ] Add the canonical tuple and `__post_init__` normalization; migrate kind, describe, and core keys.
- [ ] Run the focused tests until green.

### Task 2: Central aliases and repeated resolver

**Files:**
- Modify: `app/dictionary/languages.py`
- Modify: `app/dictionary/resolver.py`
- Test: `tests/test_dictionary.py`

**Interfaces:**
- Produces: `find_all_languages(text) -> tuple[list[Language], str]`.

- [ ] Add failing tests for `англ[.]`, `нім[.]`, `фр[.]`, delimiter variants, deduplication, unknown aliases, and `UK+FR+DE`.
- [ ] Run dictionary tests and confirm expected failures.
- [ ] Add centralized aliases and repeated match/mask logic.
- [ ] Run dictionary tests until green.

### Task 3: Free-form parser and canonical rendering

**Files:**
- Modify: `app/text/freeform.py`
- Modify: `app/text/cards.py`
- Test: `tests/test_freeform.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: `find_all_languages` and `DonorQuery.languages`.
- Produces: multi-language parsed queries and visible multi-language descriptions/cards.

- [ ] Add failing tests for every required free-form spelling, the full original query, unknown aliases, and multi-language card text.
- [ ] Run focused parser/card tests and confirm expected failures.
- [ ] Parse all languages, keep them out of unresolved fragments, and render every selected language.
- [ ] Run focused parser/card tests until green.

### Task 4: OR filtering and state/handler paths

**Files:**
- Modify: `app/analytics/engine.py`
- Modify: `app/bot/states.py`
- Modify as required: `app/bot/handlers/sections.py`
- Modify as required: `app/bot/handlers/wizard.py`
- Modify as required: `app/bot/execution.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: canonical `DonorQuery.languages`.
- Produces: OR-filtered results and lossless state round trips.

- [ ] Add failing OR, empty, single-language, and state-round-trip tests.
- [ ] Run focused tests and confirm expected failures.
- [ ] Migrate runtime consumers from compatibility `language` to `languages`.
- [ ] Run focused analytics/bot tests until green.

### Task 5: LLM compatibility

**Files:**
- Modify: `app/llm/interpreter.py`
- Modify as required: `app/llm/service.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: legacy `language` or preferred `languages` JSON fields.
- Produces: canonical `DonorQuery.languages`.

- [ ] Add failing tests for legacy scalar, new list, precedence, invalid entries, and deduplication.
- [ ] Run focused LLM tests and confirm expected failures without network calls.
- [ ] Parse both formats with `languages` taking priority when present.
- [ ] Run focused LLM tests until green.

### Task 6: Verification

**Files:**
- Inspect: all modified production and test files.

**Interfaces:**
- Produces: evidence that the change is complete, offline, and scoped.

- [ ] Run targeted dictionary/parser/model/engine/cards/bot/LLM tests.
- [ ] Run the complete offline pytest suite.
- [ ] Run Ruff checks without formatting or automatic fixes.
- [ ] Inspect `git diff --check`, `git diff`, and `git status --short`.
- [ ] Confirm no secret/config changes, no API calls, no bot start, and no commit.
