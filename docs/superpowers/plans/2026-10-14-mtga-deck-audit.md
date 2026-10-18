# MTGA Stage 4 Offline Deck Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local-only deck audit that checks Arena-exported deck lists against an offline card catalog and optional collection/wildcard counts.

**Architecture:** Upgrade new card-catalog imports to schema v2 while retaining read-only compatibility with v1. Add a pure deck-list parser/auditor, then expose it as a finite CLI action. All format findings remain catalog-based and unverified.

**Tech Stack:** Python 3.10+, standard library, SQLite, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-deck-audit-design.md`.

## Global constraints

- Local inputs only; do not download card data or collection data.
- Resolve by Arena ID after exact catalog matching; never substitute another ID field.
- Keep existing schema-v1 catalog files usable for name lookup/replay/watch.
- Do not claim current legality beyond the catalog timestamp.
- Never modify deck, collection, catalog source or Arena files during audit.
- No cloud/local model calls and no Arena input automation.
- Keep the feature standard-library-only and bound all input rows/output.
- Do not create initial Git history or commits; retain work in the current checkout under the existing ruling.

## Tasks

### Task 1: Catalog schema-v2 metadata with v1 read compatibility

**Files:** `mtga_assistant/cards.py`, `tests/test_cards.py`

- [x] Add failing tests for v1 read compatibility, v2 import fields, malformed legalities/rarity/set/collector/CMC, lookup allowlisting, duplicate identity conflicts and atomic preservation.
- [x] Run the focused tests and verify failures against schema-v1-only behavior.
- [x] Add v2 columns and import validation for `cmc`, `rarity`, `set`, `collector_number`, and a restricted legalities map; create new imports as schema 2.
- [x] Allow `_open_read_only` to validate schema 1 and schema 2 and branch lookup queries safely; `load_card_names` works for both.
- [x] Run catalog tests and existing replay/watch catalog tests.

### Task 2: Pure Arena deck parser and audit model

**Files:** create `mtga_assistant/decks.py`, create `tests/test_decks.py`

- [x] Add failing tests for Arena text parsing, sections, comments/blank rows, malformed counts, ambiguous names/printings, and unknown cards.
- [x] Add failing tests for 60-card minimum, sideboard maximum, four-copy limit/basic-land exemption, legal/not-legal/unknown metadata, composition summaries, collection shortages and wildcard counts.
- [x] Implement a bounded parser that retains only normalized card name, set code, collector number, count and section; never retain input path or raw row text.
- [x] Implement a catalog-backed auditor with safe categorical issues and explicit `catalog_based_unverified` provenance.
- [x] Run pure parser/audit tests, including malformed catalog records and unknown legality.

### Task 3: Finite `deck audit` CLI

**Files:** modify `mtga_assistant/__main__.py`, `tests/test_cli.py`, `README.md`

- [x] Add CLI tests for required arguments, text/JSON parity, schema-v1 compatibility, collection/wildcard findings, source-byte preservation, safe missing/corrupt input errors, and no network/model use.
- [x] Run tests and verify the command is absent before wiring it.
- [x] Add `deck audit --list PATH --format ... --cards-db PATH [--collection-json PATH] [--json]` and controlled safe errors.
- [x] Document format scope, catalog-date caveat, collection JSON shape and explicit limitations.
- [x] Run CLI tests and relevant cards/replay regression tests.

### Task 4: Full validation and evidence

**Files:** `docs/validation.md`, `.superpowers/sdd/2026-10-14-mtga-deck-audit/progress.md`

- [x] Run the complete unit suite and synthetic text/JSON deck audits.
- [x] Verify source hashes, output cap, and absence of path/account/unknown-ID sentinels.
- [x] Record catalog-version compatibility and legality/data limitations separately from test success.
- [x] Inspect the final workspace state; retain changes uncommitted under the no-HEAD ruling.

## Execution authorization

The user's 2026-10-14 direction to continue until feature parity authorizes this offline deck-audit stage and the remaining approved assistant capabilities. This stage does not imply that strategic replacement recommendations are complete.
