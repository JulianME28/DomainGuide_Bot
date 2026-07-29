# Multiple Language Filters Design

## Goal

Support Ukrainian language abbreviations and multiple language filters throughout
DomainGuide Bot. Multiple selected languages use OR semantics.

## Canonical model

`DonorQuery.languages: tuple[Language, ...]` is the canonical representation.
The existing `language` constructor argument and attribute remain temporarily for
compatibility. During `DonorQuery` initialization:

- only `language` becomes a one-item `languages` tuple;
- only `languages` remains unchanged;
- both inputs are merged in stable order and deduplicated by language code;
- `language` is synchronized to the first canonical language or `None`.

New business logic must read `languages`, never the compatibility field.

## Recognition

The central language dictionary owns aliases:

- `англ` → `en`;
- `нім` → `de`;
- `фр` → `fr`.

Tokenization already treats `.`, `/`, commas, `+`, colons, `і`, and `та` as
boundaries or separate tokens. The resolver repeatedly finds, records, and masks
all language mentions, preserving input order and removing duplicates. Unknown
abbreviations remain available to the free-form parser as unrecognized text.

Cyrillic aliases do not conflict with Latin country codes. `UK+FR+DE` remains a
country list, while `мови англ./нім./фр.` is a language list.

## Data flow

The free-form parser writes every recognized language to `DonorQuery.languages`.
The analytics engine accepts a donor when its normalized language is contained in
the selected canonical language keys. Empty `languages` means no language filter.
State, wizard, handlers, query descriptions, Telegram cards, and LLM
interpretation must preserve all selected languages.

The LLM interpreter accepts both legacy `language` and new `languages`. A valid
new list is primary; legacy `language` is used when the list is absent. LLM input
continues to contain only the user's query and static catalogs, never donor data.

## Testing

Tests cover model normalization, aliases with punctuation, multiple-language
free-form parsing, country-code separation, OR filtering, empty and single
language behavior, descriptions/cards, LLM legacy/new payloads, and unknown
abbreviations. Tests are written and observed failing before production changes.
Targeted tests run first, followed by the complete offline suite.

## Boundaries

Do not change GEO logic, country/zone rules, Google Sheets, recommendations,
DR/traffic formulas, production configuration, secrets, dependencies, or
external APIs. Do not start the bot or create a commit.
