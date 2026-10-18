# MTGA Overlay Deck Workflow

Date: 2026-10-19 (Africa/Johannesburg)
Status: Approved scope continuation; derived from the user's feature-parity authorization and the active game-overlay design.

## Purpose

Expose the existing offline deck audit and catalog-based suggestion commands from the primary MTGA overlay, so deck workflows do not require memorizing CLI commands or restore the former standalone desktop window.

## User flow

- A `MATCH` / `DECK` page switcher lives inside the existing borderless Arena overlay.
- The Deck page accepts an Arena deck-export file, a local card catalog, and one of the existing supported formats.
- `AUDIT` runs the existing `deck audit` command. Collection JSON is optional for audit.
- `SUGGEST` runs the existing `deck suggest` command. It also requires collection JSON and one existing goal (`curve`, `interaction`, `card_advantage`, `ramp`, `threats`, or `synergy`).
- File pickers select local inputs. Values and output remain in memory for the current overlay session. No source file is modified by these commands; existing CLI read-only checks and output limits remain authoritative.
- The existing child runner executes at most one workflow at a time. Deck output is shown in a bounded text area with a clear status and can be stopped. Starting a deck workflow while Match Watch is active is refused with an instruction to stop Watch first.

## Privacy and safety

- The existing overlay foreground policy hides the entire body, including deck paths and output, while Arena is not foreground.
- All deck behavior stays local and uses existing command construction and validation. No model request, network request, Arena API call, card-data download, or Arena input is added.
- The Deck page reports the existing catalog-based/unverified provenance and limitations; suggestions remain heuristic candidates, not win-rate or strategy claims.

## Boundaries

- Reuse `build_command('deck-audit'|'deck-suggest')`, `start_workflow`, and CLI renderers.
- Do not modify deck parsing, catalog schema, legality rules, or collection semantics in this UI slice.
- Keep Match Watch controls and their lifecycle intact. Switching pages does not stop or alter Watch; deck commands remain disabled while another child workflow runs.
- Preserve the existing overlay command as the primary application and the no-Arena-automation boundary.

## Validation limits

Source review only unless a specific check can be guaranteed to succeed under the user's test-run condition. No model inference is needed. Native Windows rendering, file-picker interaction, child execution and foreground privacy behavior remain runtime evidence requirements and cannot be claimed from source inspection.
