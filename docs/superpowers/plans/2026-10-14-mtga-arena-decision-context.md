# MTGA Arena-Presented Decision Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ground live coaching in sanitized choices Arena presented to the explicitly selected seat.

**Architecture:** Add a bounded decision-window tracker fed by GRE requests and the observed client-response envelope. Include only current-state, selected-seat requests in public snapshots; clear on responses, state changes, reset, invalidation, game end or match completion.

**Tech Stack:** Python 3.10+, standard library, existing GRE reducer, CLI and local Ollama integration.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-arena-decision-context-design.md`.

## Global Constraints

- No Arena input automation or response transmission.
- Use only the explicitly selected seat and exact current game-state ID.
- Do not expose raw request/response IDs, object IDs, group IDs, hidden identities, raw prompts or unrecognized strings.
- Treat options as Arena-presented evidence, not independent rules verification.
- Keep the local model optional, loopback-only, bounded and unverified.

## Review Focus

- A request for the other seat or another state must not leak into the user's choices.
- A hidden or unknown object referenced by an option must not reveal its identity or IDs.
- A matching response must clear only the matching current decision; stale responses must not clear another prompt.
- A state advance, gap, reset, game end or match completion must not leave old choices visible; a gap or terminal state must reject later requests that repeat the old state ID.
- Oversized or malformed option arrays must remain bounded and fail closed.

### Task 1: Track a sanitized current decision

**Files:**
- Create: `mtga_assistant/decisions.py`
- Modify: `mtga_assistant/state.py`
- Test: `tests/test_decisions.py`

**Interfaces:**
- Add a bounded tracker owned by `GameState` with `observe_request`, `observe_responses`, `expire_for_state`, `clear` and a public snapshot method.
- Public snapshots expose `decision` as `null` or a sanitized Arena-presented choice object; internal correlation IDs stay private.

- [x] Write failing tests for current-seat/current-state action options and hidden-ID suppression.
- [x] Run the focused test and verify the behavior is absent.
- [x] Implement `ActionsAvailableReq` with fixed action labels, safe mana costs, visible card names and a 64-option cap.
- [x] Add failing tests and implementation for attacker/blocker relationships, payment choices, targets and mulligan context.
- [x] Verify privacy, bounds and snapshot output.

### Task 2: Clear stale and answered decision windows

**Files:**
- Modify: `mtga_assistant/decisions.py`
- Modify: `mtga_assistant/state.py`
- Test: `tests/test_decisions.py`

- [x] Add failing tests for seat mismatch, state mismatch, state advance, integrity invalidation, reset, game end, match completion, matched response and stale response.
- [x] Implement response correlation by `(gameStateId, msgId/respId)` and an allowlisted response type.
- [x] Verify a response or state transition clears only the appropriate current decision.

### Task 3: Expose evidence to CLI and optional local explanation

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Modify: `mtga_assistant/ollama.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_ollama.py`

- [x] Add failing tests for text/JSON decision output and sanitized model prompt instructions.
- [x] Render current Arena-presented choices without raw IDs; explain only among supplied options and retain the unverified label.
- [x] Verify errors or unavailable Ollama leave deterministic replay/watch output intact.

### Task 4: Real-log evidence and final checks

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Create: `.superpowers/sdd/2026-10-14-mtga-arena-decision-context/progress.md`

- [x] Add privacy-safe real-log coverage results and limitations; do not copy raw logs or card identities.
- [x] Run focused tests, `python3 -m unittest discover -s tests` (249 tests after the match-completion regression), and `python3 -m compileall -q mtga_assistant tests`.
- [x] Reprocess the identified log and confirm only aggregates are emitted.
