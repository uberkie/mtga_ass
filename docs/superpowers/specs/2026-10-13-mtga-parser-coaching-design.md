# MTGA assistant: reliable read-only state and grounded coaching

Date: 2026-10-13 (Africa/Johannesburg)
Status: Approved by the user in chat on 2026-10-13; implementation plan approved; first-stage local deliverable implemented and reviewed. Real-log and actual-inference validation remain pending.

## Purpose and scope

Build a complete local MTGA assistant in stages. This first stage strengthens the existing Python log parser and coaching engine so later card lookup, deckbuilding, drafting, match review, and desktop presentation have a trustworthy foundation.

The user approved this first-stage scope in chat. That approval permits preparing this specification; implementation follows review of the written specification and implementation plan.

Success means reconstructing supported log events in order, exposing only information available to the selected player, reporting incomplete state explicitly, and producing evidence-based coaching without claiming to verify legal actions. Actual game-log compatibility is a separate validation result, never inferred from synthetic tests.

## Existing foundation

The workspace contains a standard-library Python prototype with JSON scanning, replay/watch commands, a GRE state reducer, sanitized snapshots, deterministic observations, and optional local Ollama explanations. The current 16 synthetic tests and demo passed on initial inspection. No real match fixture is supplied in the workspace. Ollama inference has not been tested.

Observed weaknesses to address include zone counts defaulting to zero when membership was never reported; stale object identities after movement or visibility changes; incomplete stream diagnostics; and file disappearance/reappearance potentially retaining prior game state. The watcher currently blocks while an Ollama request runs. Existing tests do not establish rotation behavior, outbound prompt sanitization, or real-log compatibility.

## Approach

Extend the existing Python modules rather than replace the prototype or introduce a full Magic simulator. This preserves working replay and CLI flows while making their evidence and limitations explicit.

A third-party parser integration is an alternative, but introduces protocol and dependency assumptions before a real local log has established the required schemas. Building a complete rules engine now would expand the stage well beyond reliable state capture. Neither alternative is part of this stage.

## Data flow and responsibilities

1. **Stream reader:** read Player.log without writing to it; extract complete JSON records in source order; report malformed, oversized, incomplete, and reset conditions without echoing raw payloads.
2. **Event adapter and reducer:** recognize explicitly supported envelopes; merge partial updates, replace authoritative full states, remove deleted references, and reset across match/game/file boundaries. Unsupported state-relevant records produce diagnostics rather than silently establishing compatibility.
3. **Visibility boundary:** produce an allowlisted snapshot for the selected seat. CLI output, observations, and model prompts all consume this snapshot, never raw records or reducer objects.
4. **Deterministic coach:** attach the observed facts and missing prerequisites to each suggestion. Suppress actionable coaching when state integrity is uncertain or the game has ended.
5. **Optional model explanation:** interpret sanitized facts and observations with bounded local requests. Model output is labeled unverified strategic interpretation.
6. **CLI:** preserve demo/replay/watch and JSON-lines output; expose useful diagnostics and validation status without requiring an overlay.

## State and visibility contract

- Keep explicit `--seat 1|2` for real logs. Seat autodetection is deferred until trustworthy account-to-seat evidence is available.
- Preserve absent fields across ordinary deltas; distinguish absence from explicit empty values. Unknown zone counts remain unknown, not zero.
- Preserve source order within batched records. An accepted full-state replacement must not retain prior objects, player data, or turn data. An older same-game full snapshot is not authoritative and must not regress current state. Match/game boundaries must not leak prior-game information.
- Apply deletion and movement consistently to objects and zone references. Conflicting references are diagnosed and excluded from coaching until resolved by authoritative updates.
- Instance-ID remapping is supported only for a documented, fixture-backed event shape. Unknown shapes mark affected state uncertain; do not guess identity continuity.
- Hide opponent hand and library identities even if present in raw input. Hide library identities for both players. Publicly revealed information may be shown in supported public zones; tracking opponent revealed-hand history is deferred.
- Face-down or explicitly hidden objects must not disclose names, card identifiers, rules text, types, or statistics derived from their concealed face. Showing the user's own face-down identity requires explicit evidence of visibility to that seat, not controller ownership alone.
- Do not carry a previously public card identity into a hidden position as currently known information. Ignore stale zone membership that contradicts an object's current zone.
- Unrecognized visibility values and zone types fail closed. Snapshot fields distinguish unknown visibility from verified public visibility where supported; ambiguous identities are withheld.
- Match/account identifiers, raw log snippets, and private card metadata are excluded from emitted snapshots and model prompts. Local diagnostics use categories and source positions instead.

## Integrity and coaching contract

Snapshots include integrity status and categorized limitations. A turn number alone does not establish a complete state. Unknown fields are represented as unknown; the UI must not imply an empty hand, no blockers, or zero mana from missing information.

Observations remain conditional, such as reviewing a possible land drop. Every observation includes evidence and relevant unknowns. This stage does not calculate verified lethal, mana availability, legal attacks/blocks, playable spells, exact win probabilities, or an opponent's hidden cards. Legal-option grounding is reserved for a later stage with appropriate decision records and rules data.

Malformed state-relevant input, missing update continuity, or inconsistent object references invalidate actionable coaching until an authoritative recovery state arrives. Benign unrelated log noise does not invalidate an otherwise supported state. Ordinary unknown fields limit only observations that depend on those fields. Game-over states stop live suggestions.

## Stream and model behavior

File truncation, replacement, disappearance, and reappearance clear prior state and partial scanner buffers before new records are accepted. A read race is retried without retaining misleading state. Replay reports unfinished JSON at EOF; watch waits for more input. Buffer limits bound memory use, and recovery must not treat nested fragments from a discarded oversized record as complete game events.

Ollama remains opt-in, local-only, and uses the existing finite generation budget. A single background worker handles at most one active request and one replaceable latest pending snapshot, so watching the log continues during inference. Each result is tagged with its originating state revision; results from an obsolete position are discarded. Timeout, malformed response, or unavailable model is shown as unavailable, while deterministic state capture continues. Interval settings must be finite and nonnegative. No cloud fallback, model download, or API credentials are introduced.

## Validation and acceptance

- Preserve existing tests and demo behavior where compatible with stricter unknown/visibility handling.
- Add regression fixtures for full-state replacement, empty versus absent membership, stale references, visibility transitions, concealed-face metadata, both seats, and match/game resets.
- Exercise actual file following through partial writes, truncation, replacement, and disappearance/reappearance using temporary files with bounded waits.
- Verify prompt bodies and every output path with secret sentinel values; concealed identities and account identifiers must not appear.
- Mock the local inference endpoint to verify timeout/failure handling, request bounds, continued event ingestion, and stale-response suppression. Mock tests do not count as actual model inference.
- Provide a replay validation report recording supported event coverage, integrity limitations, diagnostic counts, and validation provenance without raw personal data.
- For real-log validation, use a user-provided redacted log or a specifically identified readable local Player.log. Do not scan unrelated user directories broadly. Verify selected life totals, turns, visible cards, zones, and game boundaries against independent user/game evidence; successful parsing alone is insufficient.
- If no real log or independent evidence is available, deliver tested code with real-log validation explicitly pending. Actual Ollama inference is likewise reported separately from mocked tests.

## Deferred work

Automatic seat detection, Arena database/Scryfall mapping, collection ingestion, draft statistics, full rules validation, SQLite history, post-game analysis, voice, desktop overlay, autonomous play, and client input automation are outside this stage. Existing optional card-name JSON remains supported with validation of its schema. No changes to Arena or its configuration are made by the assistant.

## Delivery and review

Deliver parser/coaching improvements, meaningful regression tests, and updated usage/validation documentation. Report automated checks, real-log evidence, and actual model inference as separate outcomes. This document contains no claim about current third-party permissions or current Arena protocol beyond the repository's demonstrated fixtures.

After specification approval, prepare the implementation plan with ordered changes and checks. This workspace is not currently a Git repository; keep artifacts on disk without initializing Git or claiming a commit.
