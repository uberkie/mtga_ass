# MTGA Stage 6: Local Draft Pick Assistant

Date: 2026-10-14 (Africa/Johannesburg).
Status: In scope under the user's direction to continue through feature parity.

## Purpose

Compare a currently visible draft pack against the player's drafted pool, using local card metadata and optional historical 17Lands draft-selection statistics. The first usable slice is an offline command; it does not assume the current Arena log contains draft packs.

## Inputs and commands

- `draft stats import --source PATH --database PATH --set CODE --event-type TYPE` reads a user-obtained 17Lands public draft CSV or gzip CSV. It stores only per-card counts and average observed pick number, plus set/event/import metadata. It does not download, call an API, or retain source rows or paths.
- `draft rank --state PATH --cards-db PATH [--stats-db PATH] [--json]` reads one local state JSON object with set code, event type, pack number, pick number, offered Arena IDs and previously picked Arena IDs. A future verified log adapter can produce this same state object. The current saved Arena logs here contain no pack state, so live extraction is not claimed.
- Card IDs in the input are used only to resolve names and metadata through the local Arena-ID catalog. Output never includes those IDs or source paths.

## Ranking behavior

- The current pack is the candidate set; no card outside it can be recommended.
- Local 17Lands `pick` and `pick_number` rows produce exact observed selected-pick counts and average pick index for each selected card after filtering to the requested set and event type. Rows with a second simultaneous pick are skipped and counted rather than blended into single-pick estimates.
- The card's pick statistic is compared within the current pack and shown with its sample count. It is a historical selection tendency, not a win-rate estimate or proof of card strength.
- Local pool and card catalog facts add visible fit signals for represented colors, shared creature types, mana-value needs and narrow role cues. These do not model draft archetypes or establish an optimal pick.
- If statistics are absent or stale/mismatched, the command either returns a pool-only comparison or a safe controlled error for a requested mismatch; it never fabricates statistics. Unknown IDs remain unnamed and counted.
- JSON and text output are bounded; inputs are read-only.

## Data provenance and privacy

- The player obtains the public 17Lands CSV manually from its public datasets page. 17Lands says these datasets are usually compressed CSV/JSON and are CC BY 4.0 unless noted. The command performs no live API request and the project stores neither a downloaded copy nor derived data beyond the user's local stats database.
- Pack/pool JSON is local and explicit. It contains only pack/pool Arena IDs and set/event/position metadata; unknown fields such as account name are ignored and never echoed.
- No local model, network, hidden cards, Arena API or input automation is used.
- Statistics are observational, format-specific and potentially stale. They can reflect sample and player-population bias. No win-rate metric is implemented in this slice.
- Local logs available during design contained no pack/pick state. A read-only live log adapter remains a separate task after a real draft-log shape is available for validation.

## Acceptance checks

- Tests cover bounded streaming CSV import, gzip/plain inputs, invalid or mismatched headers, format/set filtering, second-pick skips, atomic preservation, read-only source handling and no source path retention.
- Tests cover state parsing, explicit candidate-only ranking, pool signals, sample counts, unknown card IDs, absent/mismatched statistics, JSON/text parity, privacy sentinels and unchanged inputs.
- Full prior suite remains green. Synthetic data does not establish draft strategy quality or live log compatibility.

## Sources

- [17Lands public datasets](https://www.17lands.com/public_datasets)
- [17Lands card-performance metric definitions](https://www.17lands.com/metrics_definitions)
- [Wizards detailed-log instructions](https://mtgarena-support.wizards.com/hc/en-us/articles/360000726823-Creating-Log-Files-on-PC-Mac-Steam)
