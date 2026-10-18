# MTGA Stage 5: Collection-Aware Deck Candidate Suggestions

Date: 2026-10-14 (Africa/Johannesburg).
Status: In scope under the user's direction to continue through feature parity.

## Purpose

Extend the offline deck audit with a bounded candidate finder that uses the current deck profile, local format metadata, the owned-card inventory and wildcard counts. This is the first recommendation slice; it must make the evidence and heuristic nature of its ranking visible.

## User flow

Run `deck suggest` with an Arena-exported deck, a selected supported format, a schema-v2 local card catalog, a local collection/wildcard JSON file and one explicit improvement goal. The command returns a short ordered list of cards to inspect. It never changes the deck or collection.

Supported goals are `curve`, `interaction`, `card_advantage`, `ramp`, `threats` and `synergy`. The goal labels select narrow metadata/text signals; they do not claim the card is powerful or that the deck will win more.

## Ranking evidence

- Only records with the selected format marked `legal` in the supplied schema-v2 catalog are eligible. Unknown, banned, not-legal and restricted records are handled conservatively; a restricted card is not suggested if already present.
- Candidate cards must fit within the deck's known color identity. If the deck has no known colors, only colorless candidates are eligible. Basic lands and cards already at their ordinary copy limit are excluded.
- A documented, fixed heuristic uses the requested goal and facts already in the catalog: mana-value gaps, type line, explicit common rules-text phrases, and shared creature subtypes. It returns reason codes and a fit score, not a probability or power rating.
- The suggested addition count defaults to one. Collection status compares exact Arena IDs; owned copies beyond the deck count cost no wildcard, while missing copies use the catalog rarity and user-supplied wildcard counts. Over-budget cards remain visible with an explicit status so the user can decide.
- Results are bounded to at most 20 suggestions and 4 MiB. Catalog enumeration is bounded and reports truncation.

## Privacy and limits

- Local-only reads; no network, Ollama, Arena API, hidden game state, deck write or crafting action.
- Output omits Arena IDs and file paths. It includes names, printing, type line, mana value, visible heuristic reasons and budget counts.
- Exact Arena-ID ownership is not reconciled across alternate printings.
- Catalog legality is `catalog_based_unverified` and as current as the import only. Card-specific construction rules and format exceptions are not implemented.
- Rule-text cues can miss effects or match text that does not work as expected. The ranking is a starting shortlist, not a strategic evaluation, win-rate estimate, or recommendation to cut any particular card.
- A later slice may add optional local-model explanation/reranking with output constrained to eligible candidates; this slice makes no model request.

## Acceptance checks

- CLI output is deterministic across JSON and text modes and preserves source inputs byte-for-byte.
- Tests cover each goal, color fit, curve gaps, legal/unknown/banned/restricted rows, copy-limit filtering, owned/affordable/over-budget counts, v1 rejection, enumeration bounds, privacy sentinels and no-network behavior.
- Candidate IDs are used only internally to resolve local collection counts and never appear in output.
- Full existing tests continue to pass; synthetic ranking does not claim external strategy accuracy.
