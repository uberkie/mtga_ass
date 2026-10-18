# MTGA Parser and Coaching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make the existing read-only observer reliable about known state, concealed information, and the limits of its coaching.

**Architecture:** Keep the Python CLI and reducer. Introduce explicit stream records and diagnostics, a conservative snapshot boundary, a replay report, and a bounded background explanation worker. All presentation and inference consume sanitized snapshots.

**Tech Stack:** Python 3.10+, standard library, unittest, optional local Ollama.

**Spec:** [Approved design](../specs/2026-10-13-mtga-parser-coaching-design.md).

## Global Constraints

- Read Player.log without writing to it.
- Keep explicit `--seat 1|2` for real logs.
- Hide opponent hand and library identities even if present in raw input.
- CLI output, observations, and model prompts all consume the allowlisted snapshot, never raw records or reducer objects.
- Unknown zone counts remain unknown, not zero.
- Ollama remains opt-in, local-only, and uses the existing finite generation budget: `num_predict=200` and request timeout `25.0` seconds.
- No cloud fallback, model download, or API credentials are introduced.
- No changes to Arena or its configuration are made by the assistant.
- Actual game-log compatibility is a separate validation result, never inferred from synthetic tests.
- No Git repository exists. Do not initialize Git or claim commits; inspect changed files at task checkpoints.

## Review Focus

- A malformed record may contain private data: diagnostics and exceptions must not print it; test in Tasks 1 and 6.
- Duplicate or older state IDs can regress a position: detect supported continuity markers and invalidate uncertain state; test in Task 2.
- A card mapping may be malformed or misleading: reject bad schema without interpreting IDs as another database's IDs; test in Task 6.
- An inference result can arrive after a match reset: suppress it using a monotonically increasing revision; test in Task 5.
- Windows file replacement and concurrent access can differ from Linux: test portable filesystem behavior and document actual platform verification; test in Tasks 1 and 6.

## File map

- Modify `mtga_assistant/stream.py`: scanning, bounded recovery, stream records, rotation/retry behavior.
- Modify `mtga_assistant/state.py`: continuity, reducer consistency, revision/integrity and safe snapshots.
- Modify `mtga_assistant/coach.py`: observations gated by evidence and integrity.
- Modify `mtga_assistant/ollama.py`: checked local requests and bounded background explanations.
- Create `mtga_assistant/report.py`: replay validation report, separate from human presentation.
- Modify `mtga_assistant/__main__.py`: wire records, worker, diagnostics, replay report and input validation.
- Modify `tests/test_assistant.py` and create `tests/test_stream_follow.py`, `tests/test_visibility.py`, `tests/test_ollama.py`, `tests/test_cli.py`: regressions and external boundaries.
- Modify `README.md`; create `docs/validation.md`: usage and precise verification evidence.

All signatures below use Python types; `dict[str, Any]` is a JSON-shaped dictionary. Tests use standard-library unittest, temporary directories and mocks. No new dependencies.

### Task 1: Observable stream recovery

**Interfaces:** Introduce `StreamRecord(kind: str, payload: dict[str, Any] | None = None, code: str | None = None, position: int | None = None)` with kinds `event`, `reset`, `diagnostic`, `idle`. Add `replay_records(path: Path) -> Iterator[StreamRecord]` and `follow_records(path: Path, poll_seconds: float = 0.25) -> Iterator[StreamRecord]`. Preserve `replay_file` and `follow_file` as compatibility wrappers. Idle records let the CLI poll inference results while no game events arrive.

- [x] Add failing scanner tests: malformed JSON followed by a valid event recovers; oversize discard does not emit nested fragments; unfinished replay emits `incomplete_json`; changing chunk boundaries does not change extracted events. Assert diagnostics contain a code and position, never source text or secret sentinel values.
- [x] Run `python3 -m unittest discover -s tests -p 'test_assistant.py' -v`; confirm failures name the missing behavior.
- [x] Extend `JsonObjectScanner` with bounded storage, discard-until-record-boundary recovery, `finish() -> list[StreamRecord]` for EOF diagnostics, and a record-producing method `feed_records(text: str) -> list[StreamRecord]`. Keep `feed(text)` for existing callers. Retain the `12_000_000` limit; explicitly document its unit as characters rather than incorrectly promising bytes. Reset clears incomplete buffers and discard mode.
- [x] Add temporary-file tests in `test_stream_follow.py` for partial writes, truncation, replacement, disappearance/reappearance, and a mocked read/stat race. Use a short configurable poll interval and a five-second deadline per scenario; always close generators and remove temporary files. Assert reset precedes events from a replacement source and no partial old JSON survives.
- [x] Run `python3 -m unittest discover -s tests -p 'test_stream_follow.py' -v`; confirm the tests fail before adding the new reader behavior.
- [x] Implement the record readers. Emit one reset per loss of source continuity, retry missing/temporarily unreadable files with bounded polling, and yield idle records while waiting. Diagnostics use safe categories. Preserve complete record order and replay EOF diagnostics.
- [x] Run both test files and inspect changes. Record Linux/WSL tests as such; do not claim Windows-native verification.

### Task 2: State integrity and continuity

**Interfaces:** Keep `GameState.consume(payload) -> int`, `apply(game) -> None`, and `public_snapshot(card_names=None) -> dict[str, Any]`. Add monotonically increasing `revision: int`, `invalidate(code: str) -> None`, and `consume_record(record: StreamRecord) -> int`. Snapshot adds `revision`, `integrity: {status: str, limitations: list[str]}`. Status values are `waiting`, `partial`, `consistent`, `uncertain`, `ended`; `consistent` means internally consistent supported data, never a complete Magic rules state.

- [x] Add reducer tests for authoritative full replacement, absent versus empty membership, conflicting object/zone references, unknown membership counts, match/game resets, duplicate state IDs, older state IDs and a supported previous-state mismatch. Assertions: stale state is cleared, unknown counts are `None`, uncertainty suppresses actionable coaching, and revisions never repeat across resets.
- [x] Run `python3 -m unittest discover -s tests -p 'test_assistant.py' -v` and confirm new assertions fail.
- [x] Implement integrity in `state.py`. Track continuity only when explicit `gameStateId`/`prevGameStateId` fields of the supported game-state shape exist; absent markers alone do not fabricate a gap. Duplicate state IDs are ignored, older or mismatched updates invalidate without regressing accepted state. A newer authoritative full update resets previous-link validation and integrity after duplicate/older same-game IDs are rejected. A confirmed match/game/source boundary can reset the ID sequence. Preserve missing delta fields, explicit empty lists, and revision monotonicity.
- [x] Connect reset/diagnostic records through `consume_record`. Malformed or discarded records whose relevance cannot be established mark continuity uncertain; benign non-JSON noise does not. Recognized unsupported GRE state-bearing messages produce a limitation; unrelated messages do not. Invalid field types are diagnosed rather than crashing.
- [x] Add an unsupported instance-remapping fixture asserting uncertain state and no guessed identity linkage. Do not implement a guessed `ObjectIdChanged` schema without a documented, fixture-backed shape. Record it as unsupported in the report.
- [x] Run the reducer and stream suites; inspect the checkpoint diff. Keep waiting/partial distinct from proven gaps, and allow recovery only through authoritative evidence.

### Task 3: Concealed-information boundary

**Interfaces:** `GameState.public_snapshot()` retains the existing top-level game fields and zone/card shape where compatible. Zone `count` becomes `int | None`. Card adds `identity_status` (`visible`, `concealed`, `unknown`); concealed placeholders contain only supported public object properties. Existing snapshot `id` refers to an instance, never an account identifier.

- [x] Add tests in `test_visibility.py` for both seats, hidden hand/library metadata, face-down ownership versus visibility, unknown visibility enums, public-to-private transitions, stale memberships and account identifiers. Put unique secret sentinels in names, identifiers, types, power/toughness and metadata. Assert none appear in serialized snapshots, including nested integrity fields.
- [x] Run `python3 -m unittest discover -s tests -p 'test_visibility.py' -v`; confirm the intended assertions fail.
- [x] Implement an explicit allowlist in `state.py`. Public identity requires explicit supported visibility evidence; an own-hand identity requires confirmed hand ownership and no contradictory hidden marker. Own face-down identity requires explicit seat-visible evidence; without a fixture-backed marker it remains concealed. Exclude object membership conflicting with current zone. Never infer a hidden face's statistics from cached metadata.
- [x] Adjust synthetic fixtures to include explicit public visibility where intended; retain tests proving unknown visibility fails closed. Preserve opponent counts without exposing member IDs or identities in hidden zones.
- [x] Run visibility and existing reducer tests. Inspect every public snapshot field for accidental raw metadata; do not expand the allowlist to make synthetic output more attractive.

### Task 4: Evidence-gated coaching and replay report

**Interfaces:** Keep `observations(snapshot) -> list[dict[str, Any]]`; each observation contains existing `code`, `level`, `message`, `evidence` plus `unknowns: list[str]` and `legal_status: str` fixed to `unverified`. Add `ValidationReport.record(record: StreamRecord, state: GameState, applied: int) -> None` and `as_dict() -> dict[str, Any]` in `report.py`.

- [x] Add tests showing waiting/uncertain/ended states emit only status notes; unknown counts and concealed creatures cannot justify pressure or lethal suggestions. Conditional land/combat observations must retain relevant unknowns and unverified legal status.
- [x] Run the coaching tests in `test_assistant.py` and confirm failures.
- [x] Gate `observations` on integrity and per-observation evidence. Avoid treating visible subset size as total creature count when zone/object coverage is incomplete. Do not introduce mana, summoning-sickness or lethal computations.
- [x] Add report tests asserting applied update counts, supported message types, unsupported categories, malformed counts, resets and final integrity. Report includes `fixture_provenance` (`synthetic`, `user_log_unverified`, `user_log_verified`) and `independent_validation` (`pending` or `verified`). Default every user log to unverified; CLI never promotes it automatically.
- [x] Implement the report with category-only evidence and no account/match identifiers, card identities or raw text. `record` uses stream categories and supported message type names; all serialized categories are controlled labels.
- [x] Run coaching/report tests and inspect the checkpoint diff.

### Task 5: Nonblocking bounded local explanations

**Interfaces:** Keep `explain(snapshot, notes, model, timeout=25.0) -> str`. Add `ExplanationResult(revision: int, text: str | None, error_code: str | None)` and `ExplanationWorker(model: str, interval: float, timeout: float = 25.0)` with `submit(snapshot, notes) -> None`, `poll(current_revision: int) -> ExplanationResult | None`, `close() -> None`.

- [x] Add mocked endpoint tests in `test_ollama.py`: request targets exactly `http://127.0.0.1:11434/api/generate`, `num_predict` is 200, timeout is finite, missing/non-string response is failure, request contains no secret sentinels and error output contains no response body.
- [x] Add worker tests using threading events: ingestion can advance while a request is blocked; one active and one latest pending request is the maximum; interval throttles starts; a result from before a reset or revision advance is discarded; shutdown does not wait indefinitely. Validate intervals reject negative, NaN and infinity.
- [x] Run `python3 -m unittest discover -s tests -p 'test_ollama.py' -v`; confirm new tests fail.
- [x] Implement worker with one daemon thread, synchronized latest-pending slot and bounded result slot. Deep-copy sanitized submitted data, use monotonic scheduling, replace obsolete pending work and match results by revision. Closing prevents new requests; any active urllib call is bounded by timeout and must not delay CLI shutdown.
- [x] Validate Ollama responses and return controlled error categories (`unavailable`, `timeout`, `invalid_response`) without printing arbitrary exception payloads. Label output as unverified strategic interpretation.
- [x] Run worker and visibility tests and inspect changes. Report this as mocked inference; actual model smoke remains separate.

### Task 6: CLI integration and verification evidence

**Interfaces:** Keep `main(argv: list[str] | None = None) -> int`, demo/replay/watch and existing flags. Add replay-only `--report-json PATH`; write a report only to an explicit destination, never the input log or an existing file. Keep stdout as JSON-lines under `--json`; diagnostics go to stderr. Worker polling occurs for events and idle records.

- [x] Add `test_cli.py` cases for demo/replay JSON lines, reset notifications, report destination collisions, invalid card mapping keys/values, missing files, invalid AI interval, interrupted watch, malformed payload containing a private sentinel, and unavailable Ollama. Assert controlled nonzero exits for invalid inputs and no tracebacks/raw secret payloads.
- [x] Run `python3 -m unittest discover -s tests -p 'test_cli.py' -v`; confirm missing behavior fails.
- [x] Wire record consumers, integrity notes, report collection and background explanations into `__main__.py`. Remove synchronous inference from `emit`; emit structured AI results with revision and unverified status separately. Validate card mappings as positive integer ID keys with nonempty string names; reject coercion of arbitrary values. Catch expected I/O/schema failures using fixed safe messages.
- [x] Run `python3 -m unittest discover -s tests -v`, `python3 -m mtga_assistant demo`, and `python3 -m mtga_assistant demo --json`. Inspect demo output for explicit unknowns and no concealed sentinel values. Check replay report output using a new temporary destination and verify input content was not modified.
- [x] Update README and `docs/validation.md` with CLI usage, stricter visibility/unknown behavior, supported schemas, report meaning, platform tested and automated results. Preserve real-log compatibility as pending unless independently validated. Record exact evidence for every status claim.
- [x] If a specifically identified accessible real log exists, replay it read-only and record categorized coverage. Request only missing independent observations needed to verify life totals, turn, cards, zones and boundaries; parsing alone remains unverified. If none exists, mark this check pending and finish the local deliverable.
- [x] If an existing local Ollama model is available and explicitly selected, run one bounded sanitized demo explanation and record its outcome separately. Do not download a model or substitute another provider. If unavailable, record actual inference as pending.
- [x] Review all changes against the spec, run any checks justified by review fixes, and summarize code/tests, real-log verification and actual inference separately. No commit or deployment is part of this delivery.

## Execution handoff

Recommended method: **Native** execution in this chat. The six tasks share the same stream/snapshot contracts and benefit from one implementer maintaining consistency. After implementation, use one independent review of the integrated changes as required by the selected execution workflow. Alternatively, use task-by-task subagent implementation and review if the user chooses it.

User approved this plan and the recommended Native approach in chat on 2026-10-13. Tasks implemented; independent review completed and all five findings fixed by failing-then-passing regression tests. Final suite: 74 passing tests. Conditional real-log and actual-inference checks were assessed and remain pending as recorded in docs/validation.md. No dependencies were added.
