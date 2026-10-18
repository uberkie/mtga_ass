# MTGA Stage 12: Exact-ID Visible Card Context

Date: 2026-10-15 (Africa/Johannesburg)
Status: Spec approved on 2026-10-15; implementation plan written and execution authorized under the user's standing approval. The working-application milestone passed on 2026-10-15 11:38:27 SAST; Stage 12 execution may proceed.

## Purpose

Improve live decision comparisons by attaching useful card facts from the user's local card catalog to the current Arena-presented choices. The lookup must use the exact Arena card ID while the reducer still has the transient object-to-card mapping and after the existing selected-seat visibility checks. It must never join by card name, expose hidden identities, or imply that card text is a complete or current rules engine.

The original assistant goal includes read-only live coaching, public-information opponent estimates, deckbuilding, draft help, post-game review, and voice. Stage 12 addresses one live-coaching gap only. It does not complete the overall feature-parity goal or independently verify game legality.

## Current state

The reducer stores Arena object IDs and group IDs internally, filters visible cards for the selected seat, and gives `DecisionTracker` only a visible instance-to-name map. The catalog is already an optional local SQLite database with Arena IDs, names, mana cost, type line, Oracle text, colors, power, toughness, layout, card faces, and import metadata. Replay/watch load catalog names before reduction, while the shared projection removes protocol IDs before deterministic decision analysis and local-model comparison.

This creates a safe extension point: enrich only request-referenced objects that have already passed the existing visibility rules, perform one exact Arena-ID lookup, attach bounded facts to the sanitized decision, then let the ID-free analyzer expose fixed fact codes and sources. Name-based joins would remain ambiguous across printings and are excluded.

## Approaches considered

1. **Batch exact-ID lookup at the reducer's visibility boundary (recommended).** For each current decision request, resolve only referenced instances already known visible to the selected seat, map them through the reducer's effective Arena group ID, and fetch those exact IDs from the optional local catalog in one bounded read-only query. This uses the existing privacy boundary and avoids loading every card record into memory.
2. **Load all catalog facts into memory at process startup.** This simplifies lookup but increases memory use for records the current decision never references, and still requires the reducer to enforce visibility before attaching facts.
3. **Keep name-only decision facts.** This requires no changes but leaves current choices without card characteristics or reference text, so it does not address the approved gap.

The design uses approach 1. The existing `--cards-db` remains optional. `--cards-json` continues to provide names only and does not become a catalog source.

## Proposed behavior

When replay/watch receives a supported Arena decision request and a local card database is configured:

1. The reducer first applies its current state, selected-seat, zone, owner, viewer, face-down, and visibility checks. Only request-referenced instances that appear in that visible set are eligible for catalog lookup.
2. For each eligible instance, use the effective group ID already used for visible card naming (`overlayGrpId`, otherwise `grpId`). Accept only a positive integer within the existing Arena ID bound. Query the local catalog by that exact `arena_id`; never search by name or infer an ID from a label.
3. Fetch unique IDs in a single read-only batch, with at most 128 distinct card references per decision. The database schema remains read-only and supports the existing readable schema versions. If the request exceeds the bound, omit excess references and record a controlled limitation.
4. Attach only allowlisted catalog fields to the matching sanitized decision reference. The visible label remains the reducer's existing label. If the exact record is absent or its catalog name conflicts with the visible label, withhold the catalog facts for that reference and record a limitation; do not try another printing.
5. The compact projection strips all Arena IDs and preserves only the bounded fact values needed by the analyzer. JSON and text analysis show a fixed source label for every catalog fact. The model receives only this analysis, within the existing 64 KiB request cap.

The supported catalog facts are mana cost, type line, colors, mana value, power, toughness, Oracle text, and a bounded summary of catalog card faces where present. Color letters are converted to the fixed names White, Blue, Black, Red, and Green; an empty color list stays empty and represents colorless. A face summary is a list of at most four bounded strings, each containing that face's catalog name and the available mana cost, type line, Oracle text, and power/toughness. The record name is used only for an internal consistency check against the existing visible label; it is not used to find a record and is not emitted as a second identity claim. Format legality, rarity, set/collector metadata, catalog paths, database IDs, raw protocol payloads, and unrelated catalog records are not included in decision analysis.

Each emitted catalog fact uses the existing `{code, value, source}` shape and a fixed code/source allowlist: `catalog_mana_cost` / `catalog.card.mana_cost`, `catalog_type_line` / `catalog.card.type_line`, `catalog_colors` / `catalog.card.colors`, `catalog_mana_value` / `catalog.card.cmc`, `catalog_power` / `catalog.card.power`, `catalog_toughness` / `catalog.card.toughness`, `catalog_oracle_text` / `catalog.card.oracle_text`, and `catalog_face_summary` / `catalog.card_faces`. The analysis remains explicitly `Arena-presented` and `independent_legality: unverified`. Catalog text is labeled as local reference data that may be stale or incomplete; neither the analyzer nor the model parses it as executable rules, verifies an interaction, calculates legality, or claims an optimal choice. Existing comparison output remains an ordinal ordering with citations to fact codes, not model-authored prose.

Catalog fact values are bounded: face names at most 120 characters, card text fields at most 1,024 characters per face, type lines at most 256 characters, mana-cost strings at most 128 characters, power/toughness at most 64 characters each, colors are drawn from the five-color allowlist, and mana value must be finite and within the catalog's validated range. A value exceeding its bound is omitted rather than clipped. The serialized catalog-fact payload is capped at 24 KiB per decision, and duplicate IDs are looked up once. If an individual field or aggregate bound is exceeded, that fact is omitted, the analysis marks catalog evidence partial, and no partial text is silently presented as complete. Catalog limitations do not remove or reorder Arena's options and do not by themselves disable a comparison that is otherwise structurally usable.

When a decision refers to more than 128 distinct visible cards, process the first 128 in stable request order and omit later references with a controlled limitation. Apply the 24 KiB budget in the same stable order. Missing or nullable catalog fields are omitted; malformed or oversized fields are omitted with a controlled limitation. Existing request/response size limits remain in force.

Only flat action and mulligan decisions remain eligible for model ordering. Adding card facts does not enable model ranking for attacker, blocker, target, payment, or unsupported composite decisions; they continue to show candidate evidence only.

Without `--cards-db`, or when only `--cards-json` is supplied, decision output retains current behavior and contains no catalog facts. A malformed catalog explicitly supplied at startup follows the existing controlled invalid-input behavior. If a catalog read fails during an already-running replay/watch lookup, state capture continues with deterministic facts and a safe catalog-unavailable limitation; it does not mark game-state integrity uncertain or send a request to a different service.

## Interfaces and data flow

- Extend the reducer/decision-tracker boundary to pass sanitized catalog facts alongside the current visible instance labels. Transient IDs remain internal to the reducer and lookup call.
- Add a bounded batch lookup in `mtga_assistant/cards.py` that reuses the existing read-only connection and schema validation. The result is keyed only for internal exact-ID association; returned decision facts omit identifiers.
- Extend `mtga_assistant/projection.py` and `mtga_assistant/rules.py` with the new safe decision fields, fact codes, source labels, and explicit catalog limitations. Keep the existing schema-version-1 top-level analysis shape and `{code, value, source}` fact envelope; add only allowlisted fact codes and decision fields.
- Extend `mtga_assistant/ollama.py` validation and prompt instructions only to accept the new allowlisted facts and state plainly that local card text is reference-only and may be stale. No free-form model claims are introduced.
- Existing CLI flags and desktop workflows remain the user entry points. No new history, database write, network access, Arena setting, or Arena client input is introduced.

## Privacy, safety, and failure behavior

- The Arena client, log, and card database are read-only. The feature sends no actions to Arena and performs no screen control, injection, or automation.
- Hidden or unknown cards are never looked up. A name, catalog match, nearby object, or prior visible identity cannot substitute for current selected-seat visibility.
- The catalog is optional and user-supplied. No automatic download or refresh occurs. Its contents are untrusted reference data, even after schema validation.
- Arena instance/group IDs are used only for the exact join and are removed before analysis, text/JSON output, and model requests. No source path, account data, or raw log text is added.
- Catalog absence, no exact match, name conflict, malformed field, or lookup-time read failure never marks otherwise usable game state uncertain. It withholds the affected catalog facts and reports a bounded limitation. A database that fails existing startup validation remains a controlled input error.
- The existing stale-snapshot binding remains in force: a comparison can run only against the exact decision snapshot that produced the analysis.

## Validation and acceptance

- Tests prove exact Arena-ID lookup with duplicate card names, correct use of `overlayGrpId`/`grpId`, and no name-based fallback.
- Visibility tests prove that hidden-hand, face-down, unknown-visibility, unreferenced, wrong-seat, and stale-instance identities do not trigger catalog lookup or produce catalog facts.
- Catalog tests cover readable schema versions, missing IDs, name conflicts, malformed/oversized fields, duplicate reference IDs, the 128-ID and 24 KiB bounds, safe partial limitations, and read-only source/database hashes.
- Analyzer, CLI, desktop, and Ollama tests prove fixed fact/source allowlists, text labels, JSON shape, exact snapshot binding, absence of protocol IDs and privacy sentinels, and bounded local prompts that label Oracle text reference-only.
- Tests verify that no-catalog and `--cards-json` workflows preserve current decision behavior, and that a catalog lookup failure does not stop deterministic capture.
- Run focused suites, the full unit suite, compilation, synthetic text/JSON replay/watch, and the existing desktop smoke. Reprocess available Arena logs only in aggregate and compare source hashes; do not print or save card identities or raw log events.
- Report synthetic correctness, historical protocol coverage, a real local catalog lookup (if a user-provided catalog is available), actual model inference, current-match validation, and native Windows speech as separate evidence states. A passing synthetic test is not independent rules or strategic validation.

## Explicitly outside this stage

This stage does not implement comprehensive Magic rules, execute Oracle text, infer legal options that Arena did not present, simulate card effects or combat, calculate lethal or win probability, grade past decisions, improve opponent-deck probabilities, modify deck/draft recommendations, add persistent match history, or automate Arena. Broader deterministic rules reasoning, richer deck ratings, live draft-pack extraction, native Windows speech validation, and independent live-match accuracy remain future work toward the complete assistant goal.

## Workspace constraints

Do not initialize Git history or create commits in this unborn checkout. Keep tests and data handling local and read-only. Update `README.md` and `docs/validation.md` with the implemented scope, exact test totals, and the evidence limitations after implementation.
