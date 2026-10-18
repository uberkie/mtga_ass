# MTGA Stage 5 Collection-Aware Deck Suggestions Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local-only `deck suggest` command that returns a bounded, explainable candidate list grounded in the deck, catalog legality and collection/wildcard data.

**Architecture:** Add a schema-v2-only bounded card enumerator to the read-only catalog layer. Rank the eligible records using fixed role, curve, color and creature-subtype signals; report exact-ID collection affordability. Keep the audit path unchanged.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-deck-suggestions-design.md`.

## Tasks

### Task 1: Safe bounded catalog enumeration

**Files:** `mtga_assistant/cards.py`, `tests/test_cards.py`

- [x] Add failing tests for selected-format legal filtering, v1 catalogs, result limits/truncation, sanitized fields and read-only preservation.
- [x] Add a read-only schema-v2 enumerator with a hard row cap and validated output.
- [x] Run catalog-focused tests.

### Task 2: Heuristic suggestion model

**Files:** `mtga_assistant/decks.py`, `tests/test_decks.py`

- [x] Add failing tests for all goals, color fit, curve gaps, unknown current cards, restricted/copy limits and collection/wildcard affordability.
- [x] Implement deterministic bounded candidate ranking with explicit basis and caveats.
- [x] Ensure no Arena ID or input path appears in the report.
- [x] Run deck audit/suggestion tests.

### Task 3: CLI and user documentation

**Files:** `mtga_assistant/__main__.py`, `tests/test_cli.py`, `README.md`

- [x] Add CLI regression tests for required inputs, JSON/text parity, offline/no-model behavior, input preservation, safe failures and output cap.
- [x] Wire `deck suggest` with goal, copies and result-limit options.
- [x] Document exact-ID collection semantics and heuristic limitations.
- [x] Run focused CLI/catalog/deck tests.

### Task 4: Final verification and record

**Files:** `docs/validation.md`, `.superpowers/sdd/2026-10-14-mtga-deck-suggestions/progress.md`

- [x] Run full unit suite, compile check and synthetic text/JSON CLI smoke.
- [x] Verify source hashes, privacy sentinels, no network/model calls and bounded output.
- [x] Inspect workspace; retain uncommitted under the no-HEAD ruling.
