# MTGA Stage 4: Offline deck audit foundation

Date: 2026-10-14 (Africa/Johannesburg).
Status: In scope under the user's 2026-10-14 direction to continue through feature parity.

## Purpose

Add a local-only deck audit as the first deckbuilding slice. It reads an Arena-exported deck list, checks deck structure and catalog-backed format legality, summarizes its composition, and optionally compares card counts against a user-provided collection/wildcard file. This gives later recommendation work a deterministic input and honest legality boundary.

The project-wide objective includes deckbuilding suggestions from collection, format legality and wildcard budget. This stage establishes deck import, metadata, legality and collection accounting. It does not yet claim to optimize a deck or recommend strategic replacements; that is a separate follow-up once the audited data contract is stable.

## Scope and boundaries

- Extend the offline Scryfall Arena-ID catalog with only the additional metadata required for an audit: converted mana cost, rarity, set code, collector number and format legalities.
- Read both the existing schema-v1 catalog and the new schema-v2 catalog. Existing name-only lookups and replay/watch remain compatible. New imports create schema v2 atomically.
- Add `deck audit --list PATH --format {standard,alchemy,explorer,historic,timeless} --cards-db PATH [--collection-json PATH] [--json]`.
- Parse the common Arena exported text format with `Deck` and optional `Sideboard` headings and lines such as `4 Card Name (SET) 123`. Match exact Arena IDs only through an exact name/set/collector catalog row. If set/collector are absent, resolve only when the card name maps to one unique Arena ID; otherwise report it unresolved.
- Apply the conventional 60-card minimum main-deck size, 15-card maximum sideboard, and four-copy constructed limit, with basic lands exempt. Report rather than repair violations. The audit does not assume best-of-one sideboard rules.
- Use the selected format's Scryfall legality value. Report `legal`, `not_legal`, or `unknown` based on the supplied catalog. Record catalog import timestamp and label all format results `catalog_based_unverified`; stale or missing legality data never becomes a current ruling.
- Optional collection JSON uses a documented allowlist: positive Arena IDs and nonnegative owned counts, plus nonnegative common/uncommon/rare/mythic wildcard counts. Report owned shortages and the remaining wildcard count needed by rarity. Never craft, import a collection from Arena, or change a deck.
- Composition summaries include main/sideboard counts, color counts, type counts, and mana-value buckets only when those facts are present in the catalog. Unknown values remain explicit.
- Output is a bounded JSON object or a text rendering of that object. No source rows, account identifiers, collection names, raw deck path, or unknown card IDs are emitted.
- All input remains local and read-only. No online lookup, catalog download, or model request is made by `deck audit`.

## Format and rules limits

This is a deck-file audit, not a complete Magic rules engine. The supported constructed formats use one generic structural baseline. Format-specific exceptions, companion rules, card-specific copy-limit text, special sideboard conventions, and legality changes after the catalog import are not independently interpreted. If a card cannot be matched or its legality is absent, the report says `unknown` and does not claim the deck is legal.

The local file may be a Scryfall bulk export, but Scryfall metadata is reference data. The date is exposed from the catalog metadata; the user is responsible for importing a current source. No transfer is initiated by this feature.

## Acceptance checks

- Schema-v1 catalogs still support replay/watch and name lookup; v2 imports are atomic and include validated legalities/rarity/set metadata.
- Tests cover Arena text parsing, duplicate/ambiguous names, malformed rows, unresolved cards, main/sideboard counts, four-copy/basic-land handling, each supported format's legal/illegal/unknown status, collection shortages and wildcard counts.
- Both JSON and text modes include the same safe findings, catalog timestamp and `catalog_based_unverified` status.
- Deck list and collection files remain byte-for-byte unchanged. Hidden or unrecognized fields and input-path/account sentinels never appear in output.
- The audit does not call a network service, Ollama, or any Arena input API.
- Full existing tests continue to pass. Synthetic tests do not establish correctness against current tournament rules or a current catalog.

## Deferred

Strategic card replacement suggestions, synergy scoring, draft assistance, full format rule exceptions, automatic catalog refresh, collection scraping, wildcard crafting, and Arena client control remain out of scope for this slice.
