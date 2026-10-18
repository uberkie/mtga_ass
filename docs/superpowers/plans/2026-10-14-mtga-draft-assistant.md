# MTGA Stage 6 Local Draft Assistant Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add offline draft statistics import and a pack/pool comparison command, without claiming live log ingestion.

**Architecture:** Stream local 17Lands public draft CSV rows into an atomic SQLite statistics file. Parse a bounded local pack/pool JSON file, resolve Arena IDs through the existing card catalog, and rank only the current pack using historical average selection pick plus transparent pool-fit signals.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-draft-assistant-design.md`.

## Tasks

### Task 1: Bounded local 17Lands draft-data importer

**Files:** create `mtga_assistant/draft.py`, create `tests/test_draft.py`

- [x] Add failing tests for plain/gzip CSV, data set/event filtering, second-pick skips, invalid headers/rows, source preservation, atomic replacement and metadata path privacy.
- [x] Implement a streaming, bounded importer for `pick`, `pick_2`, `pick_number`, `expansion`, `event_type` and dynamic `pack_card_*` columns.
- [x] Store only selected-pick counts and average selected pick index by card name, set and event type.
- [x] Add safe read-only stats lookup and validate set/event identity.

### Task 2: Explicit pack/pool state and ranker

**Files:** `mtga_assistant/draft.py`, `tests/test_draft.py`

- [x] Add failing tests for state schema, count bounds, unknown IDs, pool colors/subtypes/curve, card candidates restricted to current pack, and absent/mismatched stats.
- [x] Implement a bounded JSON state loader and transparent historical-stat plus pool-fit ranking.
- [x] Preserve explicit unknown status when card/stat data is unavailable; omit Arena IDs and paths.
- [x] Run draft tests.

### Task 3: CLI and documentation

**Files:** `mtga_assistant/__main__.py`, `tests/test_cli.py`, `README.md`

- [x] Add CLI tests for stats import/rank, JSON/text output, local-only behavior, source hash preservation, privacy sentinels and safe errors.
- [x] Add `draft stats import` and `draft rank` commands.
- [x] Document manual local 17Lands dataset acquisition, attribution, freshness and no-live-log limitation.
- [x] Run focused draft and CLI tests.

### Task 4: Verification record

**Files:** `docs/validation.md`, `.superpowers/sdd/2026-10-14-mtga-draft-assistant/progress.md`

- [x] Run full suite, compile check and synthetic plain/gzip import plus text/JSON ranking smoke.
- [x] Verify bounds, unchanged sources, no network and no source path/account/ID leakage.
- [x] Record that real saved logs lack pack state and live-draft compatibility remains unverified.
