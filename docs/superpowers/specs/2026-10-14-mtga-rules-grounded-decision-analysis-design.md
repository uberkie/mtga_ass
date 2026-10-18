# MTGA Stage 11: Rules-Grounded Decision Analysis

Date: 2026-10-14 (Africa/Johannesburg)
Status: Approved for implementation on 2026-10-15; implementation and verification are complete. See `docs/validation.md` for evidence and remaining limitations.

## Purpose

Build the first deterministic rules-grounding layer for live coaching. It will bind advice to the current decision window and the choices Arena presented to the explicitly selected seat, attach machine-checkable evidence and provenance, and constrain any optional local-model recommendation to that exact choice set. This is a foundation toward the requested rules-grounded assistant; it does not complete the full Magic rules engine or the overall feature-parity goal.

The project already captures Arena's current action, attacker, blocker, payment, target and mulligan options. It does not independently validate Magic rules, and its historical log contains continuity gaps. The current `Player.log` has no game-state or decision records. Advice must therefore remain conditional on a current, usable state and make the source of every fact visible.

## Approaches considered

1. **Use Arena's current presented options as the source boundary (recommended).** The existing reducer validates the selected seat, current state and request/response references; deterministic analysis reports supported facts with explicit provenance. This uses the server-generated choice set without claiming an independent implementation of comprehensive rules. Its limit is that it cannot enumerate actions Arena did not log.
2. **Implement a broad rules engine from scratch.** This could eventually model interactions beyond logged choices, but it requires extensive card semantics, timing, replacement effects and continuous-effect handling. Partial coverage could produce confident false claims, and comprehensive validation would be a separate major effort.
3. **Integrate an existing third-party rules engine.** This could provide wider semantics, but compatibility with Arena's protocol, current card data, Python runtime and local-first packaging has not been established. It would also add a versioned external dependency and its own validation burden.

Stage 11 uses approach 1. The later goal still includes broader deterministic rules reasoning; the implementation and validation must not describe this first slice as a complete rules engine.

## Proposed behavior

Add a deterministic analyzer for one ID-free compact public snapshot. The projection must be produced before the analyzer runs; the analyzer never receives the transient IDs currently present in `GameState.public_snapshot()`. It accepts only snapshots with integrity `consistent` or `partial` and a decision whose source is `arena_presented`. The reducer's `DecisionTracker` remains responsible for proving exact selected-seat and state-ID matching before the projection. It emits a versioned `decision_analysis` object containing:

- `status`: `analyzed`, `partial`, or `unavailable`;
- `choice_provenance`: `arena_presented` or `unknown`;
- `independent_legality`: always `unverified` in this stage;
- `decision_kind`: one of the existing controlled decision kinds;
- per-choice evidence drawn from the exact sanitized Arena option and supported snapshot fields, with a fixed source label for each fact;
- safe `unknowns` and `limitations`, including missing identity, mana, phase, or incomplete state;
- a bounded allowlist of fact codes that a local model may cite when comparing choices.

`choice_provenance` is the single field recording whether the choice set came from Arena. It is separate from `independent_legality`, which remains `unverified` and never claims that Arena's options were independently checked.

Each emitted choice has a stable per-window ordinal and zero or more `{code, value, source}` facts. Ordinals are temporary display references, never Arena protocol identifiers. The `source` value identifies either a structured Arena option field or a named field in the compact public snapshot.

The analyzer checks only structural consistency that survives identifier removal: option numbering and bounds, required fields for the decision kind, target and attacker relationships as Arena reported them, and whether the sanitized public facts needed for a comparison are present. Exact instance-to-option matching and selected-seat/current-state binding remain the reducer's responsibility and are not recomputed from names. Card catalog enrichment is deferred until it can be joined to visible identities before identifier removal without ambiguous name matching. Oracle text remains reference material and is not parsed as executable rules.

For action options, preserve Arena-supplied action labels and mana-cost symbols. For attacker, blocker, target, payment and mulligan decisions, preserve only their already-sanitized candidate relationships and bounds. Unsupported selection/grouping/ordering/damage-assignment requests remain uninterpreted. Unknown or hidden card identities stay generic. No action is synthesized, submitted, or added to Arena's list.

Live deterministic output states what Arena presented and which facts are known. Optional Ollama comparison returns a bounded structured response containing an ordering of current option numbers and fact codes from the analyzer's allowlist; it cannot add prose claims or arbitrary option identifiers. A deterministic renderer expands those codes into sourced facts and labels the ordering `unverified strategic interpretation`. Invalid or unsupported response fields discard the recommendation while preserving deterministic evidence. A malformed, stale, uncertain, ended, mismatched or absent decision produces status-only guidance.

## Data flow and interfaces

The implementation should add a small module such as `mtga_assistant/rules.py` with a pure entry point equivalent to `analyze_decision(compact_snapshot) -> dict[str, Any]`. The exact interface may follow repository conventions after plan review. It consumes no raw log record, reducer object, Arena identifier, source path or account data.

The current reducer remains the owner of selected-seat visibility, state continuity and Arena request/response matching. A dedicated projection reuses the existing recursive protocol-ID removal and snapshot allowlist before calling the analyzer. CLI text and JSON, watch output and model prompts derive from the same analysis object so they cannot disagree about supported facts. The deterministic output remains available if Ollama or a local card catalog is absent.

## Invariants and privacy

- Arena and all supplied files remain read-only. No Arena client input, screen control, injection or automation is added.
- A choice is described as Arena-presented only while it matches the current selected seat and current state. This proves what Arena reported, not independent correctness of Arena or the complete rules.
- The analyzer never receives or emits raw object/group/message/response IDs, account names, match IDs, source lines, hidden hand/library identities or raw request payloads.
- Visible card facts come only through the existing visibility-filtered compact snapshot. Missing facts remain unknown. No name-only join to catalog printings is allowed.
- Unsupported rule interactions remain unknown. This stage does not calculate mana sufficiency, attack/block outcomes, lethal or win probability, and makes no optimal-choice claim.
- Model output remains optional, local to loopback Ollama, bounded by existing request/response limits, and subordinate to deterministic evidence.

## Validation and acceptance

- Synthetic tests cover each supported decision kind, exact current-state/seat gating in the reducer, stale decisions, partial and uncertain state, unknown identities, malformed or mismatched option structures and unsupported decision families.
- Property-style cases prove every emitted choice is drawn from the sanitized Arena option set, every fact code exists in the analysis allowlist, and no raw identifier reaches the analyzer.
- Model tests prove the structured response can order only extant option numbers and cite only emitted fact codes. Unknown option numbers, free-form claims, invalid JSON and stale results discard the model recommendation without changing deterministic evidence.
- Privacy sentinels cover raw IDs, hidden hand/library identities, account/match data, paths and raw option strings in JSON, text and model prompts.
- Source log, card database and catalog-source hashes remain unchanged after analysis. No network access or model request is required for deterministic analysis.
- Historical-log coverage is measured using the available previous-session log without printing or retaining card identities. The currently available `Player.log` has no match events, so it cannot validate live behavior. Historical replay remains compatibility evidence; independent in-match accuracy is still required.
- Run the full unit suite, compilation, synthetic CLI text/JSON paths, and a desktop smoke. Report synthetic checks, historical-log coverage, actual model output, Windows speech behavior and real-match validation separately.

## Explicitly outside this stage

This stage does not implement comprehensive Magic rules, infer options Arena did not present, parse Oracle text into rules, simulate card effects, grade past choices, rank a winner or claim optimal strategy. Safe catalog-to-visible-card enrichment, a broader deterministic engine, richer deck ratings, live draft-pack extraction, native Windows speech validation and independent live-match validation remain work required to reach the full goal.

## Workspace constraints

Do not initialize Git history or create a commit in this unborn checkout. Keep all validation local and read-only, and update `README.md` and `docs/validation.md` with the exact scope and evidence after implementation.
