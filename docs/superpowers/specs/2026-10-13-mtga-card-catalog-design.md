# MTGA card catalog — proposed Stage 2

Date: 2026-10-13 (Africa/Johannesburg)
Status: Approved by the user on 2026-10-13; implementation plan approved on 2026-10-13.

## Purpose

Replace hand-maintained card-name mappings with a local, searchable catalog keyed only by MTGA Arena `grpId`. The Stage 1 observer can already preserve and expose visible card IDs safely; this stage would give those visible identities useful card names and reference text.

Scryfall defines `arena_id` as the card's Arena ID when one exists, and notes that some cards have no Arena ID. The catalog must therefore keep unmatched IDs unknown and must never substitute another identifier field. See [Scryfall's card field definition](https://github.com/scryfall/api-types/blob/main/src/objects/Card/CardFields.ts#L1536-L1547) and the [Scryfall bulk-data endpoint](https://api.scryfall.com/bulk-data).

## Proposed scope

- Import a locally supplied Scryfall bulk card file into a compact SQLite catalog using Python's standard library. Support newline-delimited JSON, including gzip-compressed input, so import does not need to hold the full source file in memory.
- Index records by positive integer `arena_id` only. Store a small allowlist of card facts: name, mana cost, type line, oracle text, colors, power/toughness, layout, and face names/text when present.
- Keep catalog source/update metadata and report skipped records or conflicting Arena IDs by safe category. Build a replacement catalog atomically so a bad import cannot destroy the last usable catalog.
- Add explicit CLI commands to import a local bulk file and inspect one Arena ID. Allow replay/watch to use the local catalog with no network activity; retain the existing `--cards-json` mapping for lightweight/manual use.
- Resolve metadata only after the state layer marks that card identity visible. Continue hiding opponent hands, both libraries, face-down identities, and unknown IDs from snapshots and model prompts.

## Boundaries

- This stage supplies card reference facts; it does not verify legal moves, compute mana, infer hidden cards, or claim that Oracle text alone proves a ruling.
- It does not add price data, images, collection imports, deck recommendations, draft ratings, cloud services, or Arena input automation.
- It does not download a catalog automatically. A separate future command can be considered if the user wants managed updates and agrees to the transfer size; the current bulk metadata endpoint is dynamic.
- The catalog stays local and read-only during match replay. No network lookup occurs while watching a match.

## Acceptance criteria

- A small fixture containing Arena IDs, an absent `arena_id`, a multifaced card, malformed input, and a duplicate ID exercises the importer without any network access.
- Import failure preserves the previous catalog; successful import records its source and update metadata.
- Lookup by Arena ID returns only the allowlisted facts. Other ID fields are never treated as Arena IDs.
- A visible card can be named through the catalog while hidden-zone identities remain absent in both-seat tests.
- Existing parser, CLI, and privacy tests continue to pass, and documentation distinguishes catalog lookup from verified rules or strategy.

## Deferred after this stage

Actual local-model inference still needs a user-selected installed model. Full rules/legal-action verification, deckbuilding, drafting, match history, voice, and an overlay remain separate stages.
