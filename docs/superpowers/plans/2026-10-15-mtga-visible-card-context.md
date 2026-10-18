# MTGA Stage 12: Exact-ID Visible Card Context Implementation Plan

> **Execution status:** Authorized under the user's standing approval after the working-application milestone passed on 2026-10-15 11:38:27 SAST. Execute inline and sequentially because each task depends on the preceding exact-ID and privacy contracts.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task after user approval.

**Goal:** Add bounded local catalog facts to card references in the current Arena-presented decision, using only exact Arena IDs for references that passed the selected-seat visibility checks.

**Architecture:** Keep the existing optional `--cards-db` name map for display. At a current request, the reducer gives the decision tracker only the Arena IDs and labels for currently visible instances; the tracker extracts IDs from the request's supported candidate fields, makes one bounded exact-ID batch lookup, checks each record name against its current visible label, and attaches allowlisted card data to that same sanitized reference. The shared projection removes protocol identity, the deterministic analyzer emits fixed sourced facts, and the existing CLI/Ollama/desktop path renders the same analysis object. `--cards-json` stays name-only.

**Tech Stack:** Python 3.10+, standard library `sqlite3`, existing `GameState`/`DecisionTracker`, projection and rules analyzer, CLI, optional loopback-only Ollama, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-15-mtga-visible-card-context-design.md`.

## Global Constraints

- Arena logs and the card database remain read-only. Do not automate Arena, send responses, or add screen control.
- Catalog lookup is allowed only for request-referenced instances that have passed the existing visibility, zone, owner, viewer, face-down, selected-seat, and current-state checks. Hidden, unknown, stale, wrong-seat, and unreferenced objects never reach lookup.
- Use the effective positive Arena group ID (`overlayGrpId`, otherwise `grpId`) for exact `arena_id` lookup. Never join or fall back by name. The visible label is only an internal consistency check against the fetched record name.
- Keep Arena IDs and raw request data inside the reducer/lookup boundary. Never include IDs, paths, metadata, unrelated rows, or raw logs in analysis, text/JSON output, or model prompts.
- Preserve the current `schema_version: 1` analysis envelope and `{code, value, source}` fact shape. Add only the eight Stage 12 catalog codes and their specified sources.
- Catalog text is untrusted, potentially stale reference data. Label it as reference-only; do not execute Oracle text, infer unpresented options, verify legality, rank composite decisions, or claim optimal play.
- With no `--cards-db`, or with only `--cards-json`, preserve existing output and lookup behavior. Catalog absence, mismatch, partial fields, and runtime lookup errors do not invalidate game-state integrity or suppress otherwise usable deterministic comparison.
- Keep the model optional and loopback-only, preserving the existing 64 KiB request cap, finite generation bound, strict response schema, and exact-snapshot binding.
- Do not initialize Git history or create commits in this unborn checkout. Keep tests/data local and read-only; update `README.md` and `docs/validation.md` with exact implementation evidence.

## Review Focus

- A visible card can share its name with another Arena printing; prove queries use only the selected effective Arena ID and never a name predicate or fallback. Cover duplicate names and mismatched fetched names in Tasks 1–2.
- Request payloads can contain IDs for objects that are not currently visible or belong to another state/seat. Prove those IDs never reach the batch callback in Task 2, including hidden hand, face-down, unknown visibility, unreferenced, wrong-seat, and stale instance cases.
- Catalog fields and stored JSON are untrusted and may be malformed, huge, or non-finite. Prove per-field validation, omit-not-clip behavior, stable 128-ID/24 KiB limits, and read-only database hashes in Tasks 1–3.
- Projection and analyzer changes can accidentally leak IDs or truncate Oracle text through the existing shorter generic string bound. Prove catalog-only field bounds, protocol-ID removal, fixed source/code validation, partial-evidence behavior, and unchanged action ordinals in Task 3.
- Runtime SQLite failure or hostile Oracle text must not break state capture or grant the model free-form authority. Prove deterministic fallback, reference-only prompts, rejected model claims, and no-catalog/JSON-only compatibility in Tasks 4–5.

## Tasks

### Task 1: Add one read-only exact-ID batch catalog API

**Files:**
- Modify: `mtga_assistant/cards.py`
- Modify: `tests/test_cards.py`

**Interfaces:**
- Add `lookup_cards(database: Path, arena_ids: Iterable[int]) -> dict[int, dict[str, object]]`.
- Validate at most 128 unique positive integer IDs within `1..2**63-1` (`bool` is invalid); deduplicate in first-seen order, and return no entry for missing IDs. The reducer caps a longer request in stable request order before calling this API.
- Use the existing `_open_read_only` schema validation for readable versions 1 and 2 and one parameterized `SELECT ... WHERE arena_id IN (...)` query. Decode only columns needed by Stage 12. Invalid primary ID/name or database/query errors use the existing content-free `CatalogError`; malformed optional fields are represented internally as omitted-field markers so the caller can report a fixed partial-evidence limitation. Never query by name or fetch unrelated rows or catalog metadata.

- [x] **Step 1: Add failing batch lookup tests** in `tests/test_cards.py` for exact-ID results, duplicate-name printings, stable unique IDs, missing IDs, empty input, schema v1/v2, invalid/bool/out-of-range IDs, more than 128 IDs, malformed rows, no name query, and unchanged database hash.

Run: `python3 -m unittest tests.test_cards -v`
Expected: the new `lookup_cards` tests fail because the API is not implemented; existing catalog tests continue to pass.

- [x] **Step 2: Run the focused tests and confirm the new API is absent.**
- [x] **Step 3: Implement the bounded batch read** with one read-only connection and one exact `arena_id` query. Preserve first-seen deduplication in the returned mapping and wrap SQLite/row-validation errors as `CatalogError`.
- [x] **Step 4: Rerun catalog tests.**

Run: `python3 -m unittest tests.test_cards -v`
Expected: all catalog tests pass, including the new exact-ID, bounds, version, and read-only assertions.

### Task 2: Resolve only currently visible request references

**Files:**
- Modify: `mtga_assistant/state.py`
- Modify: `mtga_assistant/decisions.py`
- Modify: `tests/test_decisions.py`
- Modify: `tests/test_visibility.py`

**Interfaces:**
- Add an optional `card_lookup: Callable[[Sequence[int]], Mapping[int, Mapping[str, Any]]] | None` parameter to `GameState.consume_record`, `GameState.consume`, and `DecisionTracker.observe_request`; existing callers without it retain the current behavior.
- Add a reducer helper that produces an internal visible-card context keyed by object instance ID, containing only the already-authorized visible label and effective Arena group ID. It must reuse the exact existing `_visible_cards` visibility decision and accept only a positive integer Arena ID within `2**63-1`.
- In `DecisionTracker`, collect request references in stable order from the same typed request fields already consumed by the sanitizers: action `instanceId`; attacker and blocker instance IDs and referenced recipients; target candidate `targetInstanceId`; and payment cost-selection `ids`. Intersect with the visible-card context before mapping to Arena IDs. Deduplicate Arena IDs and stop at 128; call `card_lookup` once only when the intersection is nonempty.
- Attach a `catalog` object only to the matching sanitized option/candidate reference. Keep IDs out of that object. Verify the fetched record's `name` exactly matches that visible reference's label; absent rows and mismatches attach no facts and add a fixed safe limitation. Catch the catalog API's `CatalogError` at this decision boundary, attach no catalog facts, add `catalog_unavailable`, and continue deterministic capture.
- Enforce Stage 12's per-field bounds and stable-request-order 24 KiB aggregate serialized catalog-fact budget before attaching data. Count repeated per-reference facts in this serialized budget even though duplicate arena IDs are queried once. Omit oversized/malformed fields without clipping and mark catalog evidence partial. Keep the existing decision/options unchanged when no catalog provider is supplied.

- [x] **Step 1: Add failing reducer/tracker tests** for a callback spy proving exact `overlayGrpId`/`grpId` lookup, one batch call, deduplication, duplicate names, name mismatch, unknown/missing rows, candidate-specific attachment, no-catalog unchanged output, and safe runtime failure. Add no-call assertions for hidden-hand, face-down, unknown-visibility, wrong-owner/wrong-seat, unreferenced, stale-state, and stale-instance IDs.
- [x] **Step 2: Run focused tests and confirm the new lookup path is absent.**

Run: `python3 -m unittest tests.test_decisions tests.test_visibility -v`
Expected: the new callback/enrichment assertions fail; existing visibility and decision tests remain diagnostic.

- [x] **Step 3: Add visible context and request-reference collection**, then perform one exact batch lookup only after seat/state validation and visibility intersection. Attach bounded allowlisted facts to the corresponding sanitized decision references, using fixed limitation codes for absent, mismatched, malformed, capped, and unavailable evidence.
- [x] **Step 4: Rerun reducer and visibility tests.**

Run: `python3 -m unittest tests.test_decisions tests.test_visibility tests.test_cards -v`
Expected: all tests pass; hidden/stale IDs produce zero lookup calls, a runtime lookup error leaves the snapshot's integrity status unchanged, and no protocol ID appears in `public_snapshot()`.

### Task 3: Preserve bounded card context through projection and analysis

**Files:**
- Modify: `mtga_assistant/projection.py`
- Modify: `mtga_assistant/rules.py`
- Modify: `mtga_assistant/ollama.py` (fact/source validation constants only in this task)
- Modify: `tests/test_projection.py`
- Modify: `tests/test_rules.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Extend the projection allowlist only for the per-reference `catalog` object. Add a catalog-specific sanitizer that retains only `mana_cost`, `type_line`, `colors`, `cmc`, `power`, `toughness`, `oracle_text`, and `card_faces`; apply the spec's field bounds without using the generic 400-character string sanitizer. Strip all unknown keys and IDs and omit malformed/oversized fields rather than clipping them.
- Keep the decision fact envelope unchanged. Emit the exact mappings `catalog_mana_cost` / `catalog.card.mana_cost`, `catalog_type_line` / `catalog.card.type_line`, `catalog_colors` / `catalog.card.colors`, `catalog_mana_value` / `catalog.card.cmc`, `catalog_power` / `catalog.card.power`, `catalog_toughness` / `catalog.card.toughness`, `catalog_oracle_text` / `catalog.card.oracle_text`, and `catalog_face_summary` / `catalog.card_faces`.
- Convert color codes to the fixed full color names; preserve an empty list. Emit finite catalog mana value only within the existing validated `0..1000` range. Keep card text at its approved length. Convert at most four faces into summary strings of at most 2,048 characters, containing only face name and available cost/type/oracle/P-T data.
- Accept catalog facts only on the same option/candidate reference as its visible card. Catalog omissions add a fixed partial-evidence limitation without changing Arena option order or making a structurally usable flat comparison unavailable. Composite decisions remain `candidate_evidence`.
- Extend the fixed analyzer/Ollama allowlists for only the Stage 12 codes/sources and bounded scalar/string-list values. Allow finite numeric values only for mana value and catalog-specific text bounds for Oracle and face summaries; keep existing shorter bounds for ordinary decision text. Model validation remains citation-only; no new free-form response fields are accepted.

- [x] **Step 1: Add failing projection/analyzer tests** for all eight facts and exact sources, per-field max/over-max values, null/missing fields, non-finite mana value, color conversion including empty colors, up-to-four face summaries, duplicate-name reference association, 24 KiB aggregate limit, stable omission order, partial status, ID/privacy sentinels, fixed code/source rejection, and unchanged choice ordinals/comparison mode.
- [x] **Step 2: Run focused tests and verify the new catalog facts are rejected or removed.**

Run: `python3 -m unittest tests.test_projection tests.test_rules tests.test_ollama -v`
Expected: the new Stage 12 projection/analyzer assertions fail because catalog facts are not yet allowlisted.

- [x] **Step 3: Implement the catalog-specific projection and analyzer paths** with their fixed source/code maps and bounded face summaries. Keep generic snapshot and decision bounds intact; pass finite numeric values through only for `catalog_mana_value` and preserve deterministic evidence on all omissions.
- [x] **Step 4: Rerun projection, analyzer, and Ollama validation tests.**

Run: `python3 -m unittest tests.test_projection tests.test_rules tests.test_ollama -v`
Expected: all tests pass; each emitted catalog fact has one fixed source, values remain inside limits, IDs and unknown fields are absent, and no omission changes the Arena-provided choice list.

### Task 4: Wire replay/watch, rendering, and local model behavior

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Modify: `mtga_assistant/ollama.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_ollama.py`
- Test: `tests/test_desktop.py` (only if the existing pipe needs an explicit regression assertion)

**Interfaces:**
- When replay/watch/demo has `--cards-db`, keep `load_card_names()` for existing visible labels and pass a `lookup_cards` callback to `GameState.consume_record`. Do not create a callback for `--cards-json` or when no catalog is supplied. Keep startup validation errors on the existing controlled input-error path.
- Preserve the current JSON top-level analysis object and text flow. Render each catalog fact with its exact fixed source label and clearly mark local card text as reference-only and potentially stale. Never show internal lookup IDs or catalog paths. Remove the intermediate per-reference `catalog` payload from the serialized JSON `state` view; the user and model receive the sourced Stage 12 facts only through `decision_analysis`, never a duplicate unlabeled catalog record.
- Extend the fact renderer's value checks for the bounded catalog Oracle and face-summary lengths while preserving the existing bounds on ordinary facts and labels.
- Update the Ollama prompt to state that catalog facts are local reference material, may be stale/incomplete, and do not prove legality or interactions. Preserve the existing request/response size bounds and `think: false`; require comparisons to cite only fact codes emitted for the corresponding option. A too-large prompt skips the optional comparison while deterministic analysis remains available.
- Limit live-decision lookup wiring to replay/watch/demo. Do not add catalog facts to post-game review, deck, draft, or opponent-analysis workflows in this stage.

- [x] **Step 1: Add failing CLI/Ollama tests** for replay and watch with a synthetic local database; JSON/text fact/source parity; reference-only output and prompt wording; absence of IDs, paths, hidden identity sentinels, and unrelated records; no-db and `--cards-json` compatibility; unchanged source-log and database hashes; prompt-size fallback; invalid/stale model citation rejection; and the existing desktop pipe displaying Stage 12 output.
- [x] **Step 2: Run the focused CLI, model, and desktop tests and confirm the new integration is absent.**

Run: `python3 -m unittest tests.test_cli tests.test_ollama tests.test_desktop -v`
Expected: Stage 12 integration assertions fail while existing command and desktop behavior remains covered.

- [x] **Step 3: Wire the optional exact-ID callback and safe reference-only text/prompt labels** through the current shared analysis object. Keep comparison rendering subordinate to its deterministic citations and preserve state capture if inference is unavailable or rejected.
- [x] **Step 4: Rerun CLI, model, and desktop regressions.**

Run: `python3 -m unittest tests.test_cli tests.test_ollama tests.test_desktop -v`
Expected: all focused tests pass; JSON and text show the same sourced facts, and no-catalog/JSON-only behavior remains unchanged.

### Task 5: Record scope and complete verification

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Test: all existing tests and Stage 12 regressions

- [x] **Step 1: Run the full unit suite and compilation.**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass; record the exact count.

Run: `python3 -m compileall -q mtga_assistant tests`
Expected: exit successfully with no output.

- [x] **Step 2: Run synthetic replay/watch/demo smoke checks** with a generated local catalog containing duplicate card names, a visible exact-ID match, a name mismatch, and a hidden identity sentinel. Verify aggregate stdout/JSON safety and compare source log/catalog hashes before and after. If desktop smoke is available, run the existing Xvfb check; report unavailable prerequisites explicitly.
- [x] **Step 3: Update `README.md` and `docs/validation.md`** with the feature boundary, catalog field/code/source list, optional `--cards-db` behavior, bounds/fallback behavior, exact test totals, synthetic evidence, and separate states for user-provided catalog availability, actual Ollama inference, live-match compatibility, and independent rules/strategy validation. Do not claim overall feature parity.
- [x] **Step 4: Inspect the final workspace** to confirm no Arena input, card download, database write outside temporary tests, log mutation, Git initialization, or commit occurred. Record the checkout's existing no-`HEAD`/untracked state without changing Git history. Inspection found no catalog/database/log artifacts; `git status` remains the existing unborn `master` with project files untracked, and `HEAD` is absent as before.

## Self-Review Notes

- Every behavior and boundary in the approved Stage 12 spec is assigned to a task: exact-ID batch lookup (Task 1), visibility-gated enrichment and failure isolation (Task 2), projection/analyzer facts and all numerical/text bounds (Task 3), user-visible and model behavior (Task 4), and documentation plus evidence reporting (Task 5).
- Task boundaries follow the data flow and each implementation task starts with tests. The batch API, reducer attachment, projection/analyzer and CLI/model depend on each preceding contract; they should execute serially unless an approved subagent workflow keeps each handoff narrowly scoped.
- The public catalog representation is per-reference and ID-free. Protocol IDs are consumed only before the shared projection; the name is used for consistency checking but never as a key or additional emitted identity.
- The eight fact code/source pairs match the approved spec. `cmc` is finite numeric evidence, Oracle/face text receives a catalog-specific longer bound, and existing flat-option eligibility does not expand.
- No plan step writes to a real user catalog or Arena log. Synthetic temporary database changes are test-only, and any real-log reprocessing stays aggregate-only and hash-verified.

## Execution Handoff

The user approved the Stage 12 design and plan and stated that all future requests are approved. The prerequisite working-application milestone has now passed. Execute the tasks inline in order; do not begin any model inference or benchmark without refreshing the environment preflight immediately beforehand.
