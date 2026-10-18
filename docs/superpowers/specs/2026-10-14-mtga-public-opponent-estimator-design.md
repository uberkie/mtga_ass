# MTGA Stage 8: Public-Information Opponent Estimator

## Goal

Offer a cautious, local estimate of which imported reference decklists match the opponent's revealed cards, then summarize other cards those matching references contain.

## User intent and constraints

The project brief asks what the opponent is likely holding, while requiring hidden identities to stay hidden. No trusted constructed-metagame dataset is currently installed, and the card catalog alone cannot establish what an opponent registered. This stage therefore compares only against a reference set the user builds from local Arena deck exports. It reports overlap and reference support, never actual hidden cards or calibrated probabilities.

The assistant continues to read Arena logs only. It does not inspect or output opponent hand/library identities, use network services, call a model, control the client, or retain match history.

## User flow and data contract

1. The user supplies a small manifest JSON containing a supported constructed format, a reference date, and labeled paths to Arena-exported decklists. Relative deck paths resolve from the manifest directory.
2. `opponent import` resolves each main-deck entry to one exact Arena ID using the local catalog. If a card is missing or ambiguous, the entire import fails. Sideboards are ignored. The command writes a new reference JSON file and never overwrites an existing file.
3. The user chooses `--opponent-data PATH` on `watch` or `replay`. This requires `--cards-db`. The selected format is read from the reference file.
4. `watch` emits an updated estimate when the public game state changes. `replay` emits one estimate for the final supported state in the log.

The generated reference file stores only schema version, user-selected format/date, catalog import timestamp, deck labels, and main-deck Arena-ID counts. It contains no source paths, raw deck exports, account IDs or match information. Manifest and deck exports are read-only. Input size, deck count, labels, rows, IDs and quantities are bounded and validated before output creation.

## Evidence and ranking

The reducer exposes a dedicated allowlisted method for currently visible cards owned by the other seat in Battlefield, Stack, Graveyard and Exile. It requires a supported public zone, explicit opponent ownership, and the reducer's existing face/visibility checks. It never examines Hand, Library, Command, Sideboard, concealed objects, or cards owned by the user. Only positive public evidence is used; absence from public zones is not treated as absence from the opponent's deck.

Each reference deck is ranked by the number of observed public card copies it contains, then by observed-copy coverage and label. Only references with at least one exact Arena-ID match appear as candidates. Cards in those references that have not yet been observed are summarized with the count and fraction of matching reference lists that contain them. Fractions describe the supplied reference set; they are not chances that the opponent holds a card.

If there are no visible observations, no matching references, or uncertain game integrity, the result says so and does not present unsupported cards as predictions. Partial state may show candidates with a clear incompleteness warning. Every result names the reference date and carries limitations: source representativeness and legality are unverified, exact Arena printings are matched, and sideboards are not modeled.

## Output and privacy

Text and JSON reports contain a safe status, selected format/date, public observations, ranked matching labels, aggregate reference support, integrity limitations and explicit uncertainty. Raw log lines, object IDs, source paths, manifest paths, hidden-zone counts, account identifiers and raw input labels containing control characters are excluded. The estimate is deterministic and is not sent to Ollama.

## Interfaces

- `opponent import --manifest PATH --cards-db PATH --output NEW_PATH` creates a reference file.
- `watch|replay --opponent-data PATH --cards-db PATH` enables prediction alongside existing output.
- `GameState.public_opponent_card_counts() -> dict[int, int]` returns an allowlisted count map for public cards currently owned by the opponent.
- `mtga_assistant.opponent` owns manifest parsing, exact-printing resolution, reference-file validation, matching and safe report rendering.
- The desktop Match tab accepts an optional opponent reference file for watch/replay. The Data tab can import a reference manifest.

The implementation does not import third-party metagame data, download files, or verify the format legality of reference decks. Those remain the user's source and catalog decisions.

## Failure behavior

Malformed, oversized, ambiguous, unresolvable or cross-format reference data fails with a content-free error and no partial output file. A pre-existing output path is never overwritten. Invalid/missing catalog data produces a controlled error. A game-state integrity status of `uncertain` suppresses ranking; `partial` permits only explicitly qualified evidence.

## Validation

Synthetic tests cover public-zone filtering, hidden hand/library/face-down privacy sentinels, owner-seat requirements, exact Arena-ID resolution, malformed/ambiguous imports, atomic no-overwrite behavior, candidate ordering, aggregate reference support, incomplete/uncertain state, safe text/JSON rendering, watch/replay integration, desktop command wiring, unchanged source hashes, and network/model guards. No real reference dataset or probability-quality claim is part of this stage.
