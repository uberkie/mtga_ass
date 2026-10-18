# MTGA Stage 9: Arena-Presented Decision Context

Date: 2026-10-14 (Africa/Johannesburg)
Status: Implemented and reviewed within the user's approved complete read-only assistant scope.

## Purpose

Ground live coaching in the decision options Arena actually presented to the selected player. A real local rotated log contained 577 `ActionsAvailableReq`, 115 `DeclareAttackersReq`, 19 `DeclareBlockersReq`, 46 `PayCostsReq`, 7 `SelectTargetsReq` and 37 `MulliganReq` messages. The existing reducer does not consume these message types. The current `Player.log` was lobby-only, so this stage is validated against the identified rotated historical log and synthetic protocol fixtures, not an active live match.

## Boundary and behavior

- Continue reading the log locally and read-only. Never send a response, action or input to Arena.
- Capture only decision requests whose `systemSeatIds` includes the user's explicitly selected seat and whose `gameStateId` exactly matches the current reduced state.
- Treat captured options as choices Arena reported to that seat. This is evidence of what the client presented, not an independent rules-engine verification.
- Support the observed play/action, attacker, blocker, payment, target and mulligan request types. Represent an unsupported choice request only as an uninterpreted decision; do not copy its payload.
- Keep one current decision window. Clear it on a matching player response, a state-ID advance, a match/game reset, an integrity invalidation, game end or match completion. Reject new requests after an integrity gap or terminal state, even when a later record repeats the old state ID. Match request `msgId` to client `respId` together with `gameStateId` and an allowlisted response type.
- Use visible card labels only when the reducer has already established that the selected player can see that object. Never emit object, group, message, response or account identifiers; raw prompts and unrecognized strings are omitted.
- Bound every choice array and text label. Malformed, mismatched, hidden or oversized data fails closed and is marked as unavailable/truncated.
- Add the sanitized decision context to snapshots and CLI output. An optional local Ollama explanation may compare only the listed options, must not invent additional legal actions, and remains labeled as unverified strategy. Deterministic state capture continues if the model is unavailable.

## Data shapes

The public snapshot adds either `decision: null` or a bounded object with `source: "arena_presented"`, a controlled `kind`, safe option labels, and limitations. It excludes internal correlation IDs. Action labels come from a fixed allowlist. Attack and block entries contain only visible card names, seat aliases and Arena-provided relationships. Costs preserve only bounded, recognized mana-color/count pairs. Targets without a visible identity receive generic labels.

## Validation

Use synthetic fixtures shaped from the observed protocol to prove selected-seat gating, exact state matching, safe labeling, no raw identifiers or hidden sentinels, matching-response clearing, stale-state clearing, gap/terminal-state rejection and clearing, and bounded malformed-input handling. Missing visible identities must be marked as unavailable; action option counts must remain capped. Mock only the local Ollama transport when verifying that the sanitized decision reaches the prompt and that the model is told not to invent options. Run the full unittest suite and compile check. Reprocess the identified local log without storing or printing card identities to measure request coverage and diagnostic totals; this is offline historical evidence, not live-watch or independent gameplay verification.

## Out of scope

This stage does not implement a complete Magic rules engine, choose or submit actions, infer hidden cards, infer the user's seat, claim an option is strategically optimal, or independently validate current game legality. Voice and richer deck analysis remain separate stages.
