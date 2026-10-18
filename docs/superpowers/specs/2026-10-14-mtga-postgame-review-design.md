# MTGA Stage 3: Read-only post-game review

Date: 2026-10-14 (Africa/Johannesburg).
Status: Conversational design approved by the user on 2026-10-14; project-wide continuation through feature parity authorized by the user on 2026-10-14. This stage proceeds under that direction.

## Purpose

Add a finite, read-only `review` command that turns one explicitly selected Arena log into a per-game timeline of supported public state changes and evidence-based questions for the player to consider after a match. This is the next slice of the user's complete local MTGA assistant, following the parser/coaching foundation and offline card catalog.

The current parser reconstructs current state but does not capture every option Arena presented or every decision the player could legally make. The review must therefore report observed facts and uncertainty, not grade choices, claim mistakes, or assert missed legal lines.

## User and safety constraints

- The command reads only the explicitly supplied log and never modifies the log or Arena configuration.
- A real log requires an explicit `--seat 1|2`; the assistant does not infer the user's seat from names or card identities.
- Review facts come only from the existing visibility-filtered snapshot. Opponent hand and both library identities stay hidden; counts may be reported when known.
- Raw log records, account names, match identifiers, and unfiltered reducer objects never enter the report or model prompt. The reducer may compare match IDs transiently to identify a boundary category; the raw value is never emitted. Internal instance IDs may be used transiently to correlate visible card movement, but they are not emitted.
- A CLI review of an explicit log is always labeled `user_log_unverified` with independent validation `pending`. Synthetic fixtures are identified as synthetic in tests and validation notes; they do not gain verified provenance.
- The command does not implement rules validation, opponent-hand inference, deck recommendations, winner prediction, win probabilities, or client control.

## Proposed approach

Extend the existing Python replay path and reducer rather than introduce a second parser, database, or rules engine. Add a reducer update-observer boundary that lets the review collector see a sanitized snapshot and a safe boundary category after each successfully applied `GameStateMessage`, including each message inside a batched log event. Boundary categories may identify a new match, a new game, an authoritative same-game replacement, or uncertain continuity; they never expose the match ID. Existing `consume_record` behavior remains compatible for demo, replay, and watch.

A small review builder consumes those snapshots in order. It groups observations by supported match/game boundary categories and game-number changes, compares consecutive snapshots, and retains only allowlisted facts needed for a timeline: turn, phase and step; integrity and limitations; life-total changes; known public-zone counts; and visible card names/types moving between supported zones. It discards raw records after reduction. A same-game authoritative replacement remains in the same game timeline with a continuity note. If a match/game boundary cannot be established, it starts a separate uncertain segment and reports the limitation instead of merging unrelated state.

The deterministic report contains per-game provenance, observed turn range, timeline entries, end-state status when Arena explicitly reports it, and review questions tied to evidence. Examples include asking the player to revisit a turn where their life total changed or a visible card moved to the graveyard. Each question explains what the log establishes and what it cannot establish. The report does not call an event a mistake or imply a legal alternative.

The new `review` CLI action requires `--log` and `--seat`, supports existing `--cards-json` or `--cards-db` enrichment and `--json` output, and runs to EOF. A source file may contain multiple observed matches; reports use local sequence numbers and never emit match IDs. It does not persist history or write a report file in this stage. Text and JSON output use the same sanitized review object. Review output can be empty but successful when a log contains no supported game updates; its provenance and limitations must still be explicit.

An optional `--model` may request a local Ollama explanation based on a compact, allowlisted review summary and review questions whose serialized request content is capped at 64 KiB. The model receives no raw records or hidden identities. Use the existing loopback-only transport, timeout, generation cap, `think: false`, and safe error categories, with a review-specific prompt. Deterministic report generation completes independently if the model is unavailable or returns malformed output. Model prose remains labeled unverified and cannot add facts, grade choices, or upgrade report provenance or rule certainty.

## Review event semantics

- State transitions are emitted only after a supported reducer update changes state. Timer-only messages and unchanged snapshots do not create timeline entries.
- Life changes are shown as observed before/after totals for the selected seats; causal explanations remain questions, not conclusions.
- Card movement is shown only when the same instance is visible in both relevant snapshots and its identity is authorized by the current snapshot. If identity or destination is unknown, report only a safe count/status change or omit the movement.
- Hidden-zone membership changes may be represented as known counts, never as card identities or inferred card names.
- A match change or game-number transition creates an explicit per-log match/game boundary. Missing continuity or uncertain reducer status creates a limitation; no facts are carried into a later game.
- A completed match/game is reported as ended only when supported Arena state says so. No winner is inferred from life totals or visible cards.
- Keep at most 250 detailed timeline entries per game and at most 100 detailed games per log. Text and JSON output are each capped at 8 MiB. When a detail cap is reached, preserve fixed-category event counts and turn ranges, set explicit truncation/omitted-game counts, and store no additional details. Reserve space for these summaries; if the final report still exceeds its output cap, exit with a controlled error and emit no partial report.

## Output shape

The JSON form has a versioned top-level object with:

- `schema_version`, `fixture_provenance` fixed to `user_log_unverified`, and `independent_validation` fixed to `pending`;
- `matches`, an ordered list using local sequence numbers only, each containing observed games and any boundary uncertainty;
- each game with a local sequence number, observed game number when known, start/end turn when known, final integrity, explicit end status, and ordered timeline entries;
- each timeline entry's turn/phase/step when known, observed changes, evidence, limitations, and related review questions;
- top-level safe diagnostic counts, truncation status, and omitted-game/detail counts;
- optional `model_status` and `model_explanation` fields, where prose is labeled unverified and a failure uses a safe category.

The output excludes raw source positions, match/account identifiers, raw card IDs, hidden identities, and unfiltered input. The text view is a readable rendering of the same fields, not a separate analysis path.

## Error handling and resource bounds

Reuse the existing bounded JSON scanner and safe diagnostic categories. Malformed, oversized, incomplete, or unsupported state-relevant input must not be echoed. A recoverable authoritative state may begin a new timeline segment; uncertainty remains visible in the report. Missing files, invalid seats, invalid card catalogs, and output serialization failures exit with a controlled error and no traceback containing source data.

The builder retains only bounded sanitized timeline details and fixed-category summary counts, then emits the compact report. Enforce the 250-entry, 100-game, 64-KiB model-summary, and 8-MiB rendered-output limits above. If a detail limit is reached, mark the affected output truncated and continue safe summary counts without storing more detail. No SQLite history, automatic log discovery, network lookup, file-follow watcher, source timestamp checks, or background service is introduced.

## Validation and acceptance

- Add synthetic fixtures for one and multiple matches/games, multiple game-state messages inside one log event, life changes, visible card movement, hidden-zone count changes, game completion, match-ID change, same-game authoritative replacement, state gaps, malformed input, and incomplete logs.
- Verify grouping and chronological ordering, including resets and repeated game numbers; ambiguous boundaries must remain separate and explicitly uncertain.
- Verify both-seat output: opponent hand and both library identities, hidden catalog sentinels, raw IDs, account names, match IDs, and raw source snippets never appear in text, JSON, or the optional model prompt.
- Verify card names appear only when allowed by the existing visibility layer and catalog; unknown or newly concealed identities do not inherit a prior visible name.
- Verify source-log bytes are unchanged by the command. The command creates no files unless a later approved stage adds an explicit export feature.
- Mock Ollama success, timeout, unavailable service, and malformed response; confirm the deterministic report remains available and model failures do not change provenance.
- Run the full unit suite, text and JSON review on synthetic logs, and regression checks for replay/watch compatibility.
- Record synthetic and user-log validation separately. Real-log parsing alone does not establish match accuracy; independent comparison remains required.

## Explicitly deferred

Persistent SQLite match history, collection/deck building, draft advice, a full Magic rules engine, legal-action enumeration, evaluating missed lethal or misplays, voice, desktop overlay, online services, automatic seat detection, and any Arena input automation remain separate stages. A later decision-analysis stage can be designed only after the log demonstrably captures the relevant choices and legal options.

## Delivery and workspace constraints

Deliver the `review` command, sanitized review builder, meaningful synthetic tests, and usage/validation documentation after the user approves this written spec and the resulting implementation plan. Do not initialize Git history or claim a commit: the workspace has Git metadata but no existing commit, and the approved Stage 2 execution ruling keeps artifacts on disk.
