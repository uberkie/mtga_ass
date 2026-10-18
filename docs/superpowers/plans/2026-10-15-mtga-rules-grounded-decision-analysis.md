# MTGA Stage 11: Rules-Grounded Decision Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, ID-free evidence for current Arena-presented decisions and a local-model comparison that can only order explicitly enumerated choices and cite their emitted facts.

**Architecture:** Move the existing bounded live-snapshot projection to a small shared module, then feed that projection to a pure analyzer. The analyzer emits the same versioned object for text output, JSON output, and the optional Ollama path. Ollama may compare flat action or mulligan options; composite attacker, blocker, target, and payment requests remain deterministic candidate evidence because the current log does not enumerate their complete combinations.

**Tech Stack:** Python 3.10+, standard library, existing GameState/DecisionTracker, CLI, unittest, and optional loopback-only Ollama.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-rules-grounded-decision-analysis-design.md`.

## Global Constraints

- No Arena input automation or response transmission is added.
- A choice is described as Arena-presented only while it matches the current selected seat and current state. This proves what Arena reported, not independent correctness of Arena or the complete rules.
- The analyzer never receives or emits raw object/group/message/response IDs, account names, match IDs, source lines, hidden hand/library identities or raw request payloads.
- Visible card facts come only through the existing visibility-filtered compact snapshot. Missing facts remain unknown. No name-only join to catalog printings is allowed.
- Unsupported rule interactions remain unknown. This stage does not calculate mana sufficiency, attack/block outcomes, lethal or win probability, and makes no optimal-choice claim.
- Model output remains optional, local to loopback Ollama, bounded by existing request/response limits, and subordinate to deterministic evidence.
- Do not initialize Git history or create a commit in this unborn checkout. Keep all validation local and read-only, and update `README.md` and `docs/validation.md` with the exact scope and evidence after implementation.

## Review Focus

- Protocol IDs and unknown top-level fields must not survive projection, and unknown nested choice fields must not enter analysis or the model prompt; test these boundaries in Tasks 1–3 and add both-seat visibility sentinels in Task 5.
- Missing, ended, uncertain, or stale decision state must not yield a recommendation; test each integrity status and absent decision in Task 2, then verify the worker skips comparison in Task 4.
- Duplicate, out-of-range, malformed, truncated, or unknown decision options must be omitted or marked partial without inventing a choice; test exact ordinals and safe limitations in Task 2.
- Candidate relationships for composite attacker/blocker/target/payment decisions must not be presented as complete combinations; test `comparison_mode == 'candidate_evidence'` and no comparison request in Tasks 2 and 3.
- A model response with prose, unknown keys, duplicate/nonexistent option numbers, or a fact code not emitted for that option must discard the whole comparison; pin these cases in Task 3.

---

### Task 1: Share the bounded ID-free live projection

**Files:**
- Create: `mtga_assistant/projection.py`
- Modify: `mtga_assistant/ollama.py`
- Create: `tests/test_projection.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Produce `compact_live_snapshot(snapshot: Any) -> dict[str, Any]` in `mtga_assistant/projection.py`. Move the existing `_remove_protocol_ids` and live-snapshot allowlist behavior into this module, preserving its depth, object, list, string, and protocol-ID bounds.
- Keep `ollama._compact_live_snapshot` as a compatibility alias to `compact_live_snapshot` so current callers and tests keep the same contract. `rules.py` imports only from `projection.py`; the projection module must not import Ollama or reducer code.

- [x] **Step 1: Write failing projection tests** in `tests/test_projection.py` for a normal reducer snapshot, top-level field allowlisting, nested protocol-ID removal, `Card #123` replacement, bounded recursion/list lengths, and non-dict input rejection. Add `test_ollama_projection_alias_is_compatible` to `tests/test_ollama.py`.
- [x] **Step 2: Run the focused tests and verify they fail** because the shared projection module does not exist.

Run: `python3 -m unittest tests.test_projection tests.test_ollama.RequestTests.test_ollama_projection_alias_is_compatible -v`
Expected: FAIL because `mtga_assistant.projection` and the shared function are missing.

- [x] **Step 3: Move the projection implementation** into `mtga_assistant/projection.py`; import it from `ollama.py` and retain `_compact_live_snapshot = compact_live_snapshot`.
- [x] **Step 4: Run projection and Ollama regression tests**.

Run: `python3 -m unittest tests.test_projection tests.test_ollama -v`
Expected: all projection and existing Ollama tests pass with unchanged request sanitization.

### Task 2: Analyze sanitized decisions deterministically

**Files:**
- Create: `mtga_assistant/rules.py`
- Create: `tests/test_rules.py`

**Interfaces:**
- Produce `analyze_decision(compact_snapshot: dict[str, Any]) -> dict[str, Any]`. The caller supplies the result of `compact_live_snapshot`; the analyzer remains pure and imports neither `GameState` nor Ollama.
- Return exactly these top-level fields: `schema_version` (`1`), `status` (`analyzed|partial|unavailable`), `choice_provenance` (`arena_presented|unknown`), `independent_legality` (`unverified`), `decision_kind` (controlled kind or `None`), `comparison_mode` (`flat_options|candidate_evidence|unavailable`), `choices`, `unknowns`, and `limitations`.
- Each choice has `number` (one-based ordinal), a bounded safe `label`, and `facts`. Each fact has an allowlisted `code`, bounded scalar/string-list `value`, and a fixed `source` label. Source labels are limited to `decision.options.action`, `decision.options.card`, `decision.options.mana_cost`, `decision.attackers.card`, `decision.attackers.legal_recipients`, `decision.can_submit`, `decision.blockers.card`, `decision.blockers.legal_attackers`, `decision.blockers.selected_attackers`, `decision.blockers.max_attackers`, `decision.groups.minimum`, `decision.groups.maximum`, `decision.groups.selected`, `decision.groups.candidates.action`, `decision.groups.candidates.card`, `decision.mana_cost`, `decision.cost_selection.visible_candidates`, `decision.cost_selection.minimum`, `decision.cost_selection.maximum`, `decision.mana_actions`, `decision.mulligan_type`, `decision.mulligans_taken`, `decision.options`, `snapshot.phase`, `snapshot.step`, `snapshot.turn_number`, `snapshot.life_totals.you`, and `snapshot.life_totals.opponent`. The `you` and `opponent` life-total labels are normalized from the selected seat and contain no raw seat ID.
- Fact codes are limited to `action_label`, `visible_card`, `mana_cost`, `legal_recipients`, `can_submit_attackers`, `legal_attackers`, `selected_attackers`, `max_attackers`, `target_action`, `selection_minimum`, `selection_maximum`, `selected_count`, `payment_action`, `cost_candidate`, `mulligan_option`, `mulligan_type`, `mulligans_taken`, `phase`, `step`, `turn_number`, and `life_total`. Facts contain only strings, integers, booleans, or bounded lists of strings; no raw nested option object is copied.
- `actions` and `mulligan` produce flat choices in Arena order and set `comparison_mode='flat_options'`. `attackers`, `blockers`, `targets`, and `payment` produce only bounded candidate evidence with `comparison_mode='candidate_evidence'`; they do not enumerate or imply complete move combinations. `other` and unknown kinds produce no choices and `comparison_mode='unavailable'`.
- Accept integrity `consistent` or `partial`; `partial` integrity, omitted or unknown facts, and known reducer limitations make analysis `partial`. Integrity `waiting`, `uncertain`, or `ended`, absent/mismatched provenance, unsupported kinds, and malformed top-level snapshots produce `unavailable` status-only output. Never infer selected-seat or state-ID matches in this module.
- Use fixed safe codes for unknowns and limitations, including `missing_decision`, `unsupported_decision_kind`, `partial_state`, `missing_phase`, `missing_life_total`, `missing_card_identity`, `missing_mana_cost`, `malformed_choice`, `choice_list_truncated`, and `candidate_space_not_enumerated`; do not echo arbitrary input strings into these fields. Ignore every input field outside the fixed decision/snapshot schema.

- [x] **Step 1: Write failing analyzer tests** in `tests/test_rules.py` for all six supported kinds; stable one-based order; exact `schema_version` and fields; fact-code/source allowlists; ignored unknown nested option fields and protocol-ID sentinels; missing phase, life, mana, and identity; partial, uncertain, ended, waiting, absent, unsupported, and malformed decisions; invalid bounds; and an invariant that every flat choice corresponds to a sanitized input option.
- [x] **Step 2: Run the focused tests and verify they fail** because `analyze_decision` is not defined.

Run: `python3 -m unittest tests.test_rules -v`
Expected: FAIL because `mtga_assistant.rules` does not exist.

- [x] **Step 3: Implement the pure analyzer** in `mtga_assistant/rules.py`. Preserve option order, cap output at 64 choices and 16 facts per choice, emit only fixed fact/source/unknown/limitation codes, and mark composite choice sets as candidate evidence rather than flat options.
- [x] **Step 4: Run analyzer and reducer/visibility regressions**.

Run: `python3 -m unittest tests.test_rules tests.test_decisions tests.test_visibility -v`
Expected: all analyzer, selected-seat/state binding, and hidden-identity tests pass.

### Task 3: Add a strict structured Ollama comparison

**Files:**
- Modify: `mtga_assistant/ollama.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Produce `compare_decision(analysis: dict[str, Any], model: str, timeout: float = 25.0) -> dict[str, Any]` in `mtga_assistant/ollama.py`. It accepts `status='analyzed'` or `status='partial'` only when `choice_provenance='arena_presented'`, `independent_legality='unverified'`, `comparison_mode='flat_options'`, and 1–64 structurally complete choices. A partial state remains explicit in the prompt and rendered result; malformed or truncated option sets must set comparison mode to unavailable.
- Prompt Ollama with only the bounded analysis object. Require the response string to be JSON with exactly `ordered_choices`, a list of 1–64 objects each containing exactly `number` and `fact_codes`. Numbers must be unique existing choice ordinals; each code must occur in that choice's emitted facts; each list must contain at least one code. Reject extra keys, prose, Markdown fences, invalid JSON, duplicates, unknown numbers/codes, overlong output, and non-finite/nonpositive timeouts as an invalid response.
- Preserve the existing loopback-only transport, 64 KiB request cap, `think: false`, and finite generation bound. Return only `{'ordered_choices': [...]}` after validation; never return model-authored prose.

- [x] **Step 1: Write failing comparator tests** in `tests/test_ollama.py` for a valid order with per-choice citations, wrong endpoint/proxy behavior, oversize request, malformed JSON, extra prose/key, duplicate and nonexistent ordinals, and citations absent from the cited choice.
- [x] **Step 2: Run the focused tests and verify they fail** because the comparator is absent.

Run: `python3 -m unittest tests.test_ollama.DecisionComparisonTests -v`
Expected: FAIL because `compare_decision` is not defined.

- [x] **Step 3: Implement the comparator and strict parser** in `mtga_assistant/ollama.py`; invalid responses raise the existing controlled `ValueError` path so the worker can discard them without changing deterministic analysis.
- [x] **Step 4: Run comparator and Ollama regression tests**.

Run: `python3 -m unittest tests.test_ollama -v`
Expected: all existing and new Ollama tests pass; captured prompts contain only the sanitized analysis and the fake model cannot add claims.

### Task 4: Use one analysis object in CLI, watch, replay, and model output

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Modify: `mtga_assistant/ollama.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_ollama.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Add `render_decision_analysis(analysis: dict[str, Any]) -> str` and `render_decision_comparison(analysis: dict[str, Any], comparison: dict[str, Any]) -> str` in `mtga_assistant/__main__.py`. Both render only safe codes/values already present in their arguments and label analysis as `Arena-presented` and legality as `unverified`.
- Extend `emit(snapshot, notes, args, decision_analysis=None)` to emit JSON with top-level `decision_analysis` alongside `state` and `coaching`, or render that same object in text. Compute the analysis once per reducer snapshot and pass the same object to rendering and inference.
- Extend `ExplanationResult` with optional `comparison: dict[str, Any] | None` and `ExplanationWorker.submit(snapshot, notes, decision_analysis=None)`. At an active Arena decision, use `compare_decision` only for a structurally complete `flat_options` analysis with status `analyzed` or `partial`; skip free-form decision advice for composite or unavailable decision analysis. When no Arena decision is present, preserve existing `explain` behavior. Keep revision-based stale-result suppression.
- JSON model output uses `decision_comparison` and the current revision; text output uses the deterministic comparison renderer and the label `unverified strategic interpretation`. Invalid or stale inference must leave `decision_analysis` unchanged.

- [x] **Step 1: Write failing CLI and worker tests** for replay and watch JSON/text parity; deterministic output when Ollama is absent; the exact analysis object reaching `compare_decision`; no comparison request for composite, stale, ended, or absent decisions; valid comparison rendering by sourced facts only; and stale result suppression. Add a desktop test that the existing text pipe displays the new analysis without breaking tab startup/close.
- [x] **Step 2: Run the focused tests and verify they fail** because output and worker routing do not expose structured decision analysis.

Run: `python3 -m unittest tests.test_cli tests.test_ollama tests.test_desktop -v`
Expected: new Stage 11 tests fail while existing behavior remains covered.

- [x] **Step 3: Integrate analysis once per snapshot** in the replay/watch loop, pass it to deterministic output and the worker, and render validated model order only by resolving its option numbers and fact codes against that same analysis object.
- [x] **Step 4: Run CLI, worker, and desktop regressions**.

Run: `python3 -m unittest tests.test_cli tests.test_ollama tests.test_desktop -v`
Expected: all tests pass, including JSON/text parity and unchanged deterministic output on model failure.

### Task 5: Validate the stage and record exact evidence

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Test: `tests/test_rules.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_ollama.py`

**Interfaces:**
- Document Stage 11's supported output, explicit unverified status, flat-option-only model comparisons, candidate-evidence limitation, and remaining full rules-engine work. Do not claim overall feature parity.
- Keep real-log validation aggregate-only: record counts, integrity categories, decision-kind counts, and before/after file hashes; never print or retain card identities or raw log payloads. Report current `Player.log`, previous-session replay, actual Ollama output, Windows speech, and independent real-match validation as separate evidence states.

- [x] **Step 1: Add regression assertions** that deterministic text/JSON and Ollama prompt/output contain no protocol IDs, hidden hand/library identities, account/match data, source path, or raw request strings, and that source log bytes are unchanged.
- [x] **Step 2: Run focused Stage 11 tests**.

Run: `python3 -m unittest tests.test_projection tests.test_rules tests.test_ollama tests.test_cli -v`
Expected: all focused tests pass.

- [x] **Step 3: Run full local verification**: `python3 -m unittest discover -s tests -v`, `python3 -m compileall -q mtga_assistant tests`, synthetic demo JSON/text smoke, and the existing Xvfb desktop smoke. Reprocess available logs through the read-only reducer using an aggregate-only harness and compare source hashes before and after.
- [x] **Step 4: Update `README.md` and `docs/validation.md`** with the exact test totals, aggregate historical-log coverage, current-log limitations, whether Ollama comparison actually ran, and unverified Windows/real-match states.
- [x] **Step 5: Inspect the final workspace and confirm no log, catalog, Arena setting, or Git history was modified.** The checkout has no Git `HEAD`, so the whole workspace remains untracked; no commits were created. No Arena log or card database is present in the workspace, and the external logs used for aggregate verification retained their recorded hashes.
