# MTGA Stage 3 Post-game Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `review` CLI command that turns an explicitly selected Arena log into a privacy-safe per-match/per-game timeline with evidence-based review questions.

**Architecture:** Extend the existing reducer with an optional observer that emits a sanitized snapshot and safe boundary category for each applied game-state message. A new `ReviewBuilder` diffs those snapshots, produces bounded deterministic reports, and optionally sends only a compact sanitized summary to a review-specific local Ollama prompt.

**Tech Stack:** Python 3.10+, standard library, existing SQLite card catalog, Ollama loopback API, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-postgame-review-design.md`

## Global Constraints

- Read only the explicitly supplied log; never modify the log or Arena configuration.
- Require explicit `--seat 1|2` for a real log; never infer the user's seat.
- Derive review facts only from the existing visibility-filtered snapshot; opponent hand and both library identities stay hidden.
- Never emit raw log records, account names, match IDs, reducer objects, raw card IDs, or hidden identities.
- Label a CLI review `user_log_unverified` with independent validation `pending`.
- Do not grade choices, claim mistakes, assert missed legal lines, or implement rules validation, deck recommendations, winner prediction, or client control.
- Keep at most 250 detailed timeline entries per game, 100 detailed games per log, 64 KiB of serialized model-summary request content, and 8 MiB per rendered report; mark truncation and omitted counts.
- Ollama remains optional, loopback-only, bounded by the existing timeout and 200-token generation limit, with `think: false`; deterministic output survives model failure.
- Do not add SQLite history, automatic log discovery, network lookup, file watching, or Arena input automation.
- Do not initialize Git history or create commits; this checkout has Git metadata but no existing commit.

## Review Focus

- A batched log object can contain several state messages: the observer must emit each applied state in order without sharing later mutable values; test `test_observer_emits_each_batched_update_in_order`.
- Repeated game numbers and same-game full replacements can confuse grouping: test `test_match_and_game_boundaries_do_not_merge_segments` and `test_same_game_replacement_keeps_game_with_continuity_note`.
- Public-to-hidden transitions can leak stale card identity through diffing or catalog lookup: test `test_hidden_transition_suppresses_card_movement_identity` for both seats and the model prompt.
- Gaps, malformed records and recovery updates can create false causal timelines: test `test_uncertain_continuity_is_reported_without_merging_facts`.
- Very long logs can exceed detail and output caps: test `test_caps_mark_omitted_details_and_never_emit_partial_json` at each exact cap.

---

### Task 1: Expose sanitized applied updates and boundary categories

**Files:**
- Modify: `mtga_assistant/state.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Extend `GameState.consume_record(record, *, on_applied=None, card_names=None) -> int`; existing callers remain valid. When supplied, `on_applied` receives `snapshot: dict[str, Any]` and `boundary: str | None` after each successfully applied game-state message. The snapshot is `public_snapshot(card_names)` and may carry instance IDs for transient diffing; the boundary value contains only a safe category, and the builder must strip IDs before storing or emitting results.
- Boundary strings are limited to `match_changed`, `game_changed`, and `same_game_replaced`; the first observed state uses `None` and the review builder starts its first segment. Stream diagnostics and the reducer's integrity state carry uncertainty because a rejected gap is not an applied update.

- [x] **Step 1: Write failing observer tests** in `tests/test_review.py`: assert one callback for each of two messages in the demo's batched event, source order and independent snapshots, and boundary classification for changed match IDs, changed game number, and same-game full replacement. Assert callback arguments contain no match ID or hidden card identity.
- [x] **Step 2: Run the focused tests and verify they fail** because `consume_record` has no observer contract.

Run: `python3 -m unittest tests.test_review.ObserverTests -v`
Expected: FAIL on missing callback/boundary behavior.

- [x] **Step 3: Implement the optional callback** in `mtga_assistant/state.py`. Thread it through record consumption and the existing GRE message loop; take the sanitized snapshot immediately after each applied state update. Preserve the current return count and behavior when no callback is supplied.
- [x] **Step 4: Run observer and existing reducer tests**.

Run: `python3 -m unittest tests.test_review.ObserverTests tests.test_assistant tests.test_real_protocol tests.test_visibility -v`
Expected: all focused observer, reducer, protocol, and visibility tests pass.

### Task 2: Build the bounded deterministic review report

**Files:**
- Create: `mtga_assistant/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Produce `ReviewBuilder(seat: int)`, `observe(snapshot: dict[str, Any], boundary: str | None) -> None`, `record_stream(record: StreamRecord, state: GameState, applied: int) -> None`, and `finish() -> dict[str, Any]`.
- `observe` accepts only public snapshots. It uses instance IDs internally to compare visible-card movement but never stores them in output. `record_stream` stores only safe diagnostic categories and final integrity.
- `finish` returns schema version 1 with `fixture_provenance='user_log_unverified'`, `independent_validation='pending'`, ordered local match/game sequence numbers, evidence-based questions, diagnostics, and truncation counters.

- [x] **Step 1: Write failing builder tests** for chronological life changes, visible card movement, known hidden-zone count changes, game end, match/game grouping, same-game replacement, uncertain continuity, malformed/oversized/incomplete diagnostics without echoing sentinels, both-seat redaction, empty reviews, and unknown identities.
- [x] **Step 2: Run focused tests and verify they fail** because `ReviewBuilder` is absent.

Run: `python3 -m unittest tests.test_review.ReviewBuilderTests -v`
Expected: FAIL because the review API is missing.

- [x] **Step 3: Implement snapshot diffing and questions** in `mtga_assistant/review.py`. Remove `revision` before comparing snapshots; emit entries only for relevant public changes. Limit detailed output to 250 entries per game and 100 games per log, preserve fixed-category counts and turn ranges after detail truncation, and reserve room for summary fields within the 8 MiB report cap.
- [x] **Step 4: Run builder tests**.

Run: `python3 -m unittest tests.test_review.ReviewBuilderTests -v`
Expected: all builder tests pass, including exact cap and no-partial-report cases. Verified: `python3 -m unittest tests.test_review.ReviewBuilderTests tests.test_review.ObserverTests -v` → 22 passed.

### Task 3: Add the finite `review` CLI action

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Add `review --log PATH --seat {1,2} [--cards-json PATH | --cards-db PATH] [--model NAME] [--json]`.
- JSON mode emits one review object; text mode renders the same object. Review requires a log path and seat, reads to EOF, creates no files, and always reports unverified provenance.
- Add `render_review(report: dict[str, Any], json_output: bool) -> str` or an equivalently focused formatter that cannot see raw log records.

- [x] **Step 1: Write CLI tests** for JSON/text output, required flags, empty/no-supported-state logs, multiple matches, catalog-enriched visible names, hidden catalog sentinels, source bytes unchanged, and controlled missing-file/catalog/serialization errors.
- [x] **Step 2: Run the new CLI tests and verify they fail** because the `review` action is not registered.

Run: `python3 -m unittest tests.test_cli.ReviewCliTests -v`
Expected: FAIL because the review command is unknown.

- [x] **Step 3: Wire replay through the observer and builder** in `mtga_assistant/__main__.py`. Keep demo/replay/watch code paths unchanged unless a small shared helper is needed; require exclusive log and seat arguments for review; never include source positions or match IDs in output.
- [x] **Step 4: Run review CLI and regression tests**.

Run: `python3 -m unittest tests.test_cli.ReviewCliTests tests.test_cli.CliTests.test_report_is_explicit_unverified_and_input_unchanged tests.test_cli.CliTests.test_catalog_does_not_reveal_hidden_cards -v`
Expected: review tests and existing replay/report/privacy tests pass. Verified: review CLI cases cover empty/single/multiple-match logs, both output formats, explicit flags, a catalog-enriched visible move, hidden sentinel exclusion, unchanged source bytes, and model fallback; full suite passed 150 tests.

### Task 4: Add optional bounded local review explanation

**Files:**
- Modify: `mtga_assistant/ollama.py`
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_ollama.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Add `explain_review(review_summary: dict[str, Any], model: str, timeout: float = 25.0) -> str`.
- Serialize only an allowlisted compact summary, reject requests above 64 KiB, use `http://127.0.0.1:11434/api/generate`, `think: false`, temperature `0.2`, `num_predict: 200`, and a review-specific final-answer prompt.
- On unavailable, timeout, invalid, or oversized inference results, retain the deterministic report and set a safe `model_status`; model prose cannot modify report facts or provenance.

- [x] **Step 1: Write failing request and CLI tests** for the 64 KiB request bound, loopback URL, thinking disabled, prompt excludes raw IDs/hidden sentinels, model prose marked unverified, and deterministic report preserved on unavailable/malformed responses.
- [x] **Step 2: Run focused tests and verify they fail** because `explain_review` is missing.

Run: `python3 -m unittest tests.test_ollama.ReviewRequestTests tests.test_review.ReviewModelTests -v`
Expected: FAIL because review inference and fallback behavior are absent.

- [x] **Step 3: Implement review-specific prompt and bounded request** in `mtga_assistant/ollama.py`; wire the optional single finite inference into `review` without starting a background watcher or delaying deterministic report creation beyond the 25-second timeout.
- [x] **Step 4: Run model request and fallback tests**.

Run: `python3 -m unittest tests.test_ollama.ReviewRequestTests tests.test_review.ReviewModelTests -v`
Expected: all local request, sanitization, cap, and fallback tests pass. Verified by the 150-test full suite; review-generation tests use mocked responses, and no review inference was sent to Ollama.

### Task 5: Document and validate Stage 3

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Create: `.superpowers/sdd/2026-10-14-mtga-postgame-review/progress.md`
- Test: all files from Tasks 1–4

- [x] **Step 1: Document the command and its limits** in `README.md`: explicit seat/log requirements, JSON/text output, catalog integration, optional local inference, privacy boundary, and unverified status. Record actual implementation and test evidence in `docs/validation.md` and the SDD progress ledger.
- [x] **Step 2: Run the full unit suite**.

Run: `python3 -m unittest discover -s tests -v`
Expected: complete suite passes with no failures. Verified: `python3 -m unittest discover -s tests -v` → 150 passed.

- [x] **Step 3: Run synthetic text and JSON review smokes** and verify output remains within 8 MiB, source hash is unchanged, and hidden/card/account/match sentinels are absent.
- [x] **Step 4: Verify final diff and workspace state**; retain all work uncommitted in accordance with the no-existing-commit ruling.

## Execution authorization

The user explicitly asked to continue through feature parity on 2026-10-14. Execute this plan natively in the current workspace, then continue the remaining approved read-only capabilities as separate specs and plans. Do not treat successful synthetic checks as real-match accuracy or useful coaching validation.
