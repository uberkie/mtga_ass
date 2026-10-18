# MTGA Public Opponent Estimator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local opponent reference matching based only on currently revealed public cards.

**Architecture:** A narrow `GameState` method returns opponent-owned visible Arena-ID counts from public zones. A new opponent module imports user decklists into a path-free JSON reference file, ranks matching references, and reports aggregate support without probability claims. `watch` and `replay` optionally invoke that deterministic report; the desktop window exposes both prediction and import workflows.

**Tech Stack:** Python 3.10+, standard library, existing SQLite card catalog and Arena log reducer; no new dependency.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-public-opponent-estimator-design.md`.

## Global Constraints

- Never inspect or emit opponent hand/library identities or hidden card identities.
- Use only reducer-approved, currently visible opponent-owned cards in Battlefield, Stack, Graveyard and Exile.
- Keep deck exports, manifest paths, raw log lines, match history and account identifiers out of generated output.
- Read only local inputs; make no network/model/client calls.
- Treat reference overlap as evidence, not calibrated probability, legality, or confirmed opponent deck identity.
- Write generated reference output exclusively; never overwrite an existing path.

## Review Focus

- A hidden card's Arena ID must never reach observations, report output, or logs; test hand/library/face-down sentinels.
- Visible stolen permanents must count according to card owner, not controller; require explicit opponent owner evidence.
- One ambiguous or missing printing must reject the whole import before creating output.
- Repeated copies and duplicated reference lists must not overstate support as a game probability.
- Uncertain state must suppress candidates; partial state must label its evidence incomplete.

---

### Task 1: Add the public opponent observation boundary

**Files:**
- Modify: `mtga_assistant/state.py`
- Test: `tests/test_visibility.py`

**Interfaces:**
- Produces `GameState.public_opponent_card_counts() -> dict[int, int]`, using the existing `_visible_cards` visibility gate and explicit owner-seat checks.

- [x] Write tests for visible opponent cards in all four supported zones, user-owned cards, missing owner data, hand/library/sideboard, face-down and unknown-visibility sentinels, and a card controlled by the wrong seat.
- [x] Run the focused visibility tests and confirm the new method is missing or returns the wrong public-only counts.
- [x] Implement the minimal allowlisted extraction.
- [x] Run the focused test module and verify hidden sentinels are absent.

### Task 2: Build and rank local reference data

**Files:**
- Create: `mtga_assistant/opponent.py`
- Test: `tests/test_opponent.py`
- Modify: `mtga_assistant/cards.py` only if exact catalog resolution requires a bounded helper.

**Interfaces:**
- Produces bounded `load_reference_manifest(path)`, `build_reference_data(manifest_path, cards_db)`, `load_reference_data(path)`, `estimate_opponent(public_counts, integrity, *, my_seat, reference_data, card_names)`, and `render_opponent_report(report, json_output)`.
- Resolves names/optional set and collector from the existing `parse_decklist` and catalog `search_card_printings`; requires exactly one Arena ID per main-deck row.

- [x] Write tests for manifest schema/bounds/date/labels, path-free generated data, unambiguous printing resolution, missing/ambiguous IDs, all-or-nothing import, candidate copy coverage/order, unseen-card support fractions, zero evidence, partial state and uncertainty suppression.
- [x] Run the focused tests and verify expected failures before implementation.
- [x] Implement import and matching with no network, model, or raw path output.
- [x] Verify text/JSON rendering stays bounded and privacy-safe.

### Task 3: Integrate optional replay and live watch estimates

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Adds `opponent import --manifest --cards-db --output`.
- Adds optional `--opponent-data` to `watch` and `replay`; it requires a local catalog and emits text or JSON estimate output alongside the existing state.
- `replay` renders one final estimate; `watch` renders an estimate on each changed supported state.

- [x] Write CLI tests for import/no-overwrite/source preservation, replay privacy and final-only output, live-watch update output, missing catalog/reference errors, and network/model call guards.
- [x] Run the focused CLI tests and verify missing command/flag behavior.
- [x] Implement argument parsing and guarded integration.
- [x] Verify synthetic replay and unchanged source hashes.

### Task 4: Expose prediction and import in the desktop window

**Files:**
- Modify: `mtga_assistant/desktop.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Match forms accept an optional local opponent reference JSON for watch/replay and require the local catalog when selected.
- Data tab exposes manifest, catalog and new reference output fields through `opponent import`.

- [x] Write tests for safe argv construction, missing catalog validation and form dispatch.
- [x] Run focused desktop tests and confirm failure before implementation.
- [x] Add optional fields and the import workflow without arbitrary command entry or shell interpolation.
- [x] Run the desktop suite.

### Task 5: Documentation and final verification

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Modify: `.superpowers/sdd/2026-10-14-mtga-opponent-estimator/progress.md`

- [x] Document a manifest example, import and watch/replay usage, reference-set limitations, and strict privacy boundary.
- [x] Run the complete `python3 -m unittest discover -s tests` suite and `python3 -m compileall -q mtga_assistant tests`.
- [x] Run synthetic CLI import/replay and desktop argv smoke checks; compare source hashes and inspect generated output for sentinels/paths.
- [x] Record actual validation scope and any deferred issue in the ledger.
- [x] Independent review follow-up: mark duplicate public-zone membership partial and omit the ambiguous instance; atomically publish output to avoid partial files on write failure.
