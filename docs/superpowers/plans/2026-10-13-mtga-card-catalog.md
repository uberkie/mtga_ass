# MTGA Local Card Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a local Scryfall card bulk file into a compact SQLite catalog and let the existing observer name only card identities already visible in its sanitized state.

**Architecture:** Add a standard-library catalog module that streams JSONL or gzip JSONL into a temporary SQLite database, validates the narrow metadata allowlist, and atomically replaces the requested destination only after a complete successful import. Replay/watch may load Arena ID-to-name mappings from that local database; card lookup is an explicit offline CLI action.

**Tech Stack:** Python 3.10+, `sqlite3`, `gzip`, `json`, `unittest`; no new dependencies.

**Spec:** [Stage 2 design](../specs/2026-10-13-mtga-card-catalog-design.md).

## Global Constraints

- Card reference data is local; replay and watch make no network requests.
- Map only a positive integer Scryfall `arena_id`; never substitute Oracle, MTGO, or other IDs.
- Enrich snapshots only after the existing reducer authorizes a visible identity.
- Opponent hands, both libraries, face-down identities, and unknown IDs remain undisclosed.
- No rules legality, deckbuilding, drafting, prices, images, or Arena input automation is added.
- Use the Python standard library and preserve the existing manual `--cards-json` flow.

## Review Focus

- A malformed or oversized JSONL record, truncated gzip trailer, or corrupt gzip DEFLATE stream must fail without replacing an existing catalog.
- Duplicate Arena IDs with conflicting card facts must fail clearly; equivalent duplicate rows may be counted and skipped.
- Python booleans must not pass integer validation for Arena IDs; optional card facts must have their declared string/list shapes.
- Gzip input, UTF-8 errors, truncated gzip streams, and invalid SQLite databases must fail without tracebacks or partial publication.
- Import source and destination collisions must be rejected; replay output must never reveal hidden identities via catalog lookup.

## Tasks

### Task 1: Streaming importer and atomic catalog build

**Files:**
- Create: `mtga_assistant/cards.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Produce `ImportSummary(imported: int, missing_arena_id: int, duplicate_records: int, source_bytes: int, imported_at_utc: str)`.
- Produce `import_catalog(source: Path, database: Path) -> ImportSummary`.

- [x] Add tests for JSONL and gzip import, Arena-ID-only mapping, multiface fields, truncated/corrupt gzip preservation, missing/invalid IDs, equivalent/conflicting duplicates, malformed/invalid/oversized records, source/destination collision, and unchanged source.
- [x] Run `python3 -m unittest discover -s tests -p 'test_cards.py' -v`; confirm failures are due to the absent importer/API.
- [x] Implement byte-oriented JSONL reading with gzip support and a fixed 1,000,000-byte record limit. Validate a positive integer `arena_id`, required nonempty `name`, optional string fields `mana_cost`, `type_line`, `oracle_text`, `power`, `toughness`, and `layout`, string-list `colors`, and card-face fields `name`, `mana_cost`, `type_line`, `oracle_text`, `power`, and `toughness`; ignore unrelated bulk fields.
- [x] Build schema version 1 in a sibling temporary SQLite file. Store only allowlisted facts and `schema_version`, `imported_at_utc`, `source_bytes`, `imported_count`, `missing_arena_id_count`, and `duplicate_count` metadata. Skip valid records with no `arena_id`; skip duplicates with equivalent canonical allowlisted facts and abort on conflicting facts. Close and atomically replace the destination only after import and metadata commit succeed; delete the temporary file on all failures.
- [x] Rerun `python3 -m unittest discover -s tests -p 'test_cards.py' -v` and confirm the existing destination survives every rejected input.

### Task 2: Read-only lookup and name map

**Files:**
- Modify: `mtga_assistant/cards.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Produce `load_card_names(database: Path) -> dict[int, str]` for the existing snapshot API.
- Produce `lookup_card(database: Path, arena_id: int) -> dict[str, object] | None` containing only the stored allowlist.

- [x] Add tests for Arena-ID name mapping, allowlisted top-level and face fields, unknown IDs, boolean lookup IDs, malformed SQLite, and read-only access.
- [x] Run `python3 -m unittest discover -s tests -p 'test_cards.py' -v` and confirm expected failures before implementation.
- [x] Open SQLite in read-only mode, validate schema version and positive IDs, parse stored JSON columns, and return safe dictionaries without source paths or raw source rows.
- [x] Rerun `python3 -m unittest discover -s tests -p 'test_cards.py' -v`.

### Task 3: Offline catalog CLI and replay/watch integration

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Add `cards import --source PATH --database PATH` and `cards lookup --database PATH --arena-id ID`; both commands emit JSON.
- Add optional `--cards-db PATH` to `demo`, `replay`, and `watch`; reject combining it with `--cards-json`.
- Keep import and lookup output as bounded JSON or fixed safe category errors; do not print source rows or host exception text.

- [x] Add tests `test_cards_import_cli_reports_counts`, `test_cards_lookup_cli_is_offline_and_allowlisted`, `test_replay_uses_catalog_for_visible_cards`, `test_catalog_does_not_reveal_hidden_cards`, `test_cards_json_and_database_conflict`, `test_demo_database_overrides_default_sample_mapping`, and `test_cards_cli_errors_are_safe`.
- [x] Run `python3 -m unittest discover -s tests -p 'test_cli.py' -v` and verify they fail for the missing commands/flags.
- [x] Implement explicit command dispatch, reuse the existing `GameState.public_snapshot` name-map contract, preserve the demo's sample mapping unless `--cards-db` is supplied, and reject source/destination path collisions before import.
- [x] Rerun `python3 -m unittest discover -s tests -p 'test_cli.py' -v` and verify the synthetic log remains unchanged.

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Modify: `.superpowers/sdd/2026-10-13-mtga-card-catalog/progress.md`

- [x] Document offline import, lookup, and `--cards-db` examples; label card text as reference data rather than verified rules.
- [x] Run `python3 -m unittest discover -s tests -v`, both synthetic demo modes, `cards import` and `cards lookup` using a small local fixture, and a replay with the generated catalog.
- [x] Confirm source files remain unchanged, the hidden-zone tests pass, and no live card lookup or bulk download occurred.
- [x] Record actual check results in the progress ledger and validation documentation. Keep real-match accuracy and actual model inference in their existing separate statuses.

## Execution Notes

- The workspace plan selected native execution in the previous stage; use `superpowers:executing-plans` after this plan is reviewed.
- The checkout has Git metadata but no commits, and all project files are untracked. Do not change Git history; use plan-local baseline copies and the ledger for rollback/evidence.
