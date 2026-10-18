# MTGA AI Assistant — experimental prototype

A local, read-only Python assistant for MTG Arena logs, live coaching, post-game review, offline deck analysis, draft picks and public-only opponent estimates from user-supplied local references. The primary Windows interface is an external, borderless, always-on-top game overlay that reads the existing log pipeline; it does not inject into Arena or send game input. A user-provided native Windows match screenshot and existing service logs confirm one opening-hand response displayed with a reported 1,250 ms response time. Broader live decision coverage and advice quality remain incomplete. Requires Python 3.10+ and Tk support; no third-party Python dependencies.

The parser, visibility boundary, file follower, CLI and inference worker have prior regression coverage. Earlier timed-model and Tk-window results describe the superseded desktop build; they do not verify the new overlay. The assistant does not control Arena or verify legal moves. See [validation evidence](docs/validation.md).

## Open the game overlay

On Windows, double-click `Run MTGA Overlay.cmd` in the project folder. It runs the overlay from this checkout, detects the installed native Python runtime, and does not open the superseded desktop window. No model name or Python command needs to be entered.

The compatibility command `gui` launches the same overlay. Run Arena windowed or borderless-windowed; an external window cannot promise visibility over exclusive fullscreen. The overlay follows Arena's window, shows match details only while Arena is foreground, and keeps a small privacy-safe Stop/status strip visible when Arena is in the background. Its current surface focuses on live Watch; the other approved workflows remain in the CLI or await migration into overlay panels. The previous multi-tab desktop window is no longer the primary launch target.

## Use the timed local Match assistant

The overlay fills the standard `Player.log` path when it can identify it and defaults to **Auto-detect** seat matching. If identity becomes missing or ambiguous, advice pauses. The model picker lists local Ollama tags from `/api/tags` and remembers the last verified choice. If no choice is saved and exactly one installed model is already loaded, the overlay selects that model automatically. Watch preloads and verifies the chosen model before following the log. The model stays resident while Watch is active and is released when Watch stops or the overlay closes. The overlay uses only the existing sanitized, revision-bound events; it does not display raw logs or send inputs to Arena.

The current code applies an eight-second inference deadline and a ten-second display deadline. These are application limits, not official MTGA phase timers. One native opening-hand response was observed with a 1,250 ms reported response time; timing across ordinary turns and other choice types remains unverified. The response must match the current game revision and arrive before its deadline or it is suppressed.

Cold loading counts if it occurs after a decision is recognized. The overlay preloads and verifies the selected model before starting Watch; unexpected unloading still consumes the inference deadline. Earlier desktop-build timings do not establish overlay timing. Resource readiness, cold load, warm inference and GUI display must be reported separately.

Historical tests measured the preloaded `hf.co/jica98/qwen3.5-4B-super-coder:Q4_0` and a Tk Match widget. The newer native Windows overlay evidence is the live opening-hand screenshot using `qwen3.5:2b`, with matching service timings recorded in the [validation report](docs/validation.md).

A smaller `qwen3.5:2b` candidate had a 328.5 ms median among nine successful warm calls, versus 422.5 ms for the 4B model, but one of ten calls timed out at 4,005 ms. Its strategic quality was not evaluated, so the tests do not establish that it is similarly capable; the 4B model is the tested choice for reliability. Model load time, inference time, response validity and GUI time are recorded separately in the [validation report](docs/validation.md).

The prior desktop build had a Voice tab for typed questions and optional native Windows speech. That interface has not yet been migrated into the game overlay, so voice parity in the primary UI is pending.

The previous voice implementation did not save audio, transcripts or answers. Its historical tests and construction smoke do not validate overlay voice operation. The app does not control Arena, verify legal moves or infer hidden opponent cards. See [validation evidence](docs/validation.md).

## Try the synthetic demo

```bash
python3 -m mtga_assistant demo
python3 -m mtga_assistant demo --json
python3 -m unittest discover -s tests -v
```

On Windows, use `py -3` instead of `python3` if needed. Run commands from this project folder. The demo contains intentionally concealed opponent identities; they are excluded from output and model prompts.

## Replay or watch your log

Enable Detailed Logs in Arena's account settings and restart Arena. A usual Windows location is:

```text
%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log
```

For `replay`, provide your **system seat** explicitly; it can be 1 or 2. For `watch`, automatic account-to-seat matching is the default and `--seat 1` or `--seat 2` remains available as an explicit override. A new match is re-matched from its own player records. If the signed-in account cannot be uniquely matched, automatic mode sends no state to the model.

```powershell
py -3 -m mtga_assistant replay --seat 1 --log 'C:\Users\YOUR_USER\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log'
py -3 -m mtga_assistant watch --log 'C:\Users\YOUR_USER\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log' --json
```

On WSL/Linux, the app checks the mounted Windows profile for the standard Arena log. If there is more than one possible Windows profile, choose the correct log explicitly.

`watch` follows appended records. It clears prior state after observed truncation, replacement, disappearance or a read race. Press Ctrl+C to stop. Rotation detection depends on observable file identity, size and a trailing-content anchor; it cannot detect every rewrite that occurs entirely between polls.

## Understand the output

Every snapshot has a monotonically increasing `revision` and an `integrity` object:

| Status | Meaning |
| --- | --- |
| `waiting` | No current supported game state yet. |
| `partial` | Some supported state is known; categorized fields are missing. |
| `consistent` | Supported fields are internally consistent. Magic's rules and legal actions remain unverified. |
| `uncertain` | A malformed/unsupported update, continuity problem or conflicting reference prevents reliable actionable coaching. |
| `ended` | Arena reported game over; live suggestions stop. |

Unknown counts are `null` in JSON and `unknown` in text. An empty zone is explicitly `0`. In a full snapshot, an explicitly public battlefield, stack, graveyard or exile zone with an omitted protobuf membership list is empty. An omitted list in a delta remains unchanged; hidden or private zones do not gain an inferred zero count. Missing data does not establish that there are no blockers or no mana.

The voice monitor adds a `stale` status to its private snapshot when the selected log has no supported game-state or decision update for two minutes. After that, only a later supported update restores eligibility; telemetry-only log growth does not. This freshness status is specific to the Voice tab and does not alter replay/watch output.

Opponent hands and both libraries show counts only. Public identities require supported explicit public visibility. Your own private hand identities are shown only when both the object and its hand zone explicitly list your selected seat as a viewer and agree on ownership. Face-down identities are withheld for both players until a seat-specific visibility schema is independently established. Unknown visibility values fail closed. Movement and face changes clear stale visibility/identity metadata; contradictory zone members are withheld.

Coaching includes `evidence`, `unknowns` and `legal_status: "unverified"`. A limited mana check matches listed fixed colored/generic costs against your observed untapped lands. It recognizes intrinsic basic-land mana and complete unconditional tap-for-mana clauses in visible local card text, including the alternative colors of lands such as Opulent Palace and Deathcap Glade. Each land supplies at most one unit in this lower-bound check; its colors are alternatives, not extra mana. It excludes hand lands, opponent-controlled or tapped lands, and animated lands. Floating mana, mana creatures, unrecognized/conditional land abilities, restrictions, alternate payments, X and hybrid costs remain unverified. A failed check withholds a spell recommendation without claiming the action is illegal. If passing is the only supported action while other mana costs remain unverified, the model must abstain instead of defaulting to Pass. Suggestions do not establish legal moves, exact win probabilities or verified lethal lines. Uncertain states produce only status guidance.

When Arena sends a supported decision request to your selected seat for the current state, the snapshot includes a `decision` object and text output lists the Arena-presented choices. It covers available actions, attackers, blockers, payment, targets and mulligans. Other observed selection, grouping, ordering and damage-assignment prompts are marked as unsupported without copying their payload. These choices reflect what Arena reported to the selected seat; the assistant does not independently verify the rules or claim the list is complete. Unknown or hidden card identities use generic labels, and the decision object omits protocol identifiers and raw prompt text. An optional local model may compare only the listed options; it must not add alternatives.

Replay and watch now include a deterministic `decision_analysis` in each state output. It reports bounded facts beside their validated source labels, marks choice provenance as `Arena-presented`, and keeps independent legality `unverified`. Actions and mulligan options can receive a local model ordering only when the flat option list is structurally complete and still matches the analyzed snapshot; the JSON result contains option numbers and citations to fact codes, not model-authored prose. Attackers, blockers, targets and payment are shown as candidate evidence because their combined choice space is not enumerated, so the model comparison is skipped for those decisions. Missing or partial state remains explicit, and the deterministic analysis remains available when Ollama is unavailable.

Supported fixture shapes include batched GRE state/queued-state messages, connect responses containing game state, full/initial replacement, incremental players/turns/zones/objects, deleted object references, game-number/match resets and explicit `gameStateId`/`prevGameStateId` continuity markers. Timer-only messages are ignored. The observed `ObjectIdChanged` singleton-int32 `orig_id`/`new_id` detail shape is supported without copying hidden identity across instances. If a replacement has no known zone, affected counts stay unknown and coaching stays cautious. Other remapping shapes remain unsupported. Internal Revealed/Suppressed/Pending/Limbo zones are recognized and withheld from snapshots; internal views may overlap physical zones. Diagnostic counters and provenance remain available even when later full states recover integrity.

## Review a completed match

Use `review` for a finite, read-only timeline from one explicitly selected log. Confirm your seat for that match; the command requires `--seat 1` or `--seat 2` and never guesses. It reads the supplied file to EOF and does not create a report file.

```bash
python3 -m mtga_assistant review --seat 1 --log /path/to/Player.log
python3 -m mtga_assistant review --seat 1 --log /path/to/Player.log --cards-db ./data/cards.sqlite3 --json
```

The timeline reports observed turn/life changes, known zone-count changes, and movement of cards whose identities are visible in both snapshots. Questions invite you to revisit evidence; they do not grade choices or claim a missed legal play because the log does not capture every option Arena presented. Match/game boundaries and gaps remain explicit, and no winner is inferred. Opponent-hand and library identities are omitted. Reports are always `user_log_unverified` with independent validation `pending`.

An optional `--model YOUR_MODEL` asks an already-running local Ollama model for a short reflection. The request contains only a compact allowlisted public-state summary, is capped at 64 KiB, and goes to the loopback endpoint. Model prose is labeled unverified; unavailable or invalid responses leave the deterministic review intact.

## Save a replay validation report

```bash
python3 -m mtga_assistant replay --seat 1 --log /path/to/Player.log --report-json /path/to/new-report.json
```

The report destination must be a **new file**, distinct from the log. Existing files are never overwritten. Reports contain update/message counts, final public life totals, safe diagnostic categories, observed limitations and final integrity; they exclude raw log text, account/match identifiers and card identities.

A successfully parsed user log is still `user_log_unverified` with independent validation `pending`. To validate a match, compare life totals, turns, visible cards, zones and match boundaries against independent game evidence. Parsing alone cannot perform that comparison. Original logs may contain personal data; share redacted examples when debugging.

## Optional local Ollama explanations

Start Watch before entering a match so model initialization finishes first. The overlay stays at **Warming** while loading weights and completing a bounded text-generation request. **Ready** requires that initialization reply and confirmation that the exact selected model remains loaded. Loading weights alone is not readiness. Startup uses a 90-second total load/text-initialization budget; it is separate from the live decision deadline when completed before the decision.

Select an already available local model explicitly:

```bash
python3 -m mtga_assistant watch --log /path/to/Player.log --model YOUR_MODEL --ai-interval 30
python3 -m mtga_assistant demo --model YOUR_MODEL
```

Requests go only to `http://127.0.0.1:11434/api/generate`; environment proxies and HTTP redirects are disabled. At an active decision, the comparator receives the bounded deterministic analysis and current sanitized public state. It compares all supplied options and can return one preferred Arena choice with one or two evidence citations, or abstain with an empty preference list. For actions, its output schema and validator exclude spells and abilities whose listed mana cost lacks observed support. A listed land play or priority pass needs no mana payment; those options still carry no independent legality claim. The 96-token generation bound remains in force; prose, invented ordinals and unsupported citations are rejected. Composite choices receive deterministic candidate evidence without a model ranking. Opening reviews receive the current visible hand, assessment and pregame task, avoiding irrelevant empty-battlefield totals; bottom-card reviews retain their separate count-focused instruction. The assistant does not download models or fall back to cloud providers.

Attack and block requests use a dedicated combat state review. The local model receives the current public battlefield and stack, your visible hand, life totals and Arena's candidate/assignment details. It can discuss a priority or conditional permitted pairing; it does not rank individual creatures as complete combat plans or verify outcomes. Target and payment requests use a separate selection review, including visible graveyard/exile context and Arena's groups, selection bounds or cost details. These reviews retain the source/revision checks and are labeled separately in the overlay. Defending players receive a blocking reminder rather than an offensive lethal reminder. Unsupported decision schemas still receive no invented choices.

One background worker allows one active request and one replaceable latest pending snapshot. State reading continues during inference. Obsolete responses are discarded. Ordinary explanations use a 25-second timeout; live choice comparisons, combat/selection reviews and opening-hand reviews have an eight-second inference cutoff and ten-second display deadline. These requests generate at most 96 tokens. Response bodies are bounded. `--ai-interval` must be finite and nonnegative; it throttles ordinary request starts. Shutdown does not wait for an active inference request.

An unfamiliar action in an otherwise supported actions list remains visible as `Other Arena option` and is excluded from model recommendations. It does not prevent comparison of the recognized options. Malformed, truncated or inconsistently numbered choices still block comparison.

Choice comparisons allow up to 64 KiB for Ollama's complete API reply, including its returned context tokens and timing metadata, while the extracted model answer remains limited to 16 KiB. A longer game-state prompt must not make an otherwise short answer fail the answer-size check.

Finite demo/replay commands wait up to 25 seconds for an explanation of their final position; a long interval or slow model can leave it explicitly pending at exit. Inference errors are reported as controlled categories and do not stop deterministic state capture. AI output is always labeled **unverified strategic interpretation**.

## Card names and supported scope

An optional Arena `grpId` mapping supplies names:

```json
{"1001": "Plains", "1002": "Example card"}
```

Use `--cards-json /path/to/cards.json`. Keys must be canonical positive integer IDs, values nonempty string names. Do not substitute Scryfall Oracle IDs for Arena group IDs. `examples/cards.json` is synthetic.

For a searchable local catalog, first obtain a [Scryfall bulk card file](https://scryfall.com/docs/api/bulk-data) yourself, then import its JSONL data (plain or gzip-compressed). Import and lookup are offline; this project does not download or refresh card data.

```bash
python3 -m mtga_assistant cards import --source /path/to/default-cards.jsonl.gz --database ./data/cards.sqlite3
python3 -m mtga_assistant cards lookup --database ./data/cards.sqlite3 --arena-id 12345
python3 -m mtga_assistant replay --seat 1 --log /path/to/Player.log --cards-db ./data/cards.sqlite3
```

The import reports counts as JSON. A lookup returns `{"card": null}` when the Arena ID is absent. The catalog stores only allowlisted card facts, including name and Oracle text; those facts describe the supplied file and are reference material, not verified current rules or legal-move advice. `--cards-db` and `--cards-json` cannot be combined. Snapshot enrichment uses the database only to name identities the state layer has already marked visible; unknown IDs remain unnamed.

During `replay`, `watch` and `demo`, a local `--cards-db` can also add facts to cards referenced by a current Arena-presented choice, after the selected-seat visibility and current-state checks pass. The lookup uses the exact effective Arena group ID, so same-name printings do not get mixed. Supported facts are mana cost, type line, colors, mana value, power, toughness, Oracle text and a short face summary. Only the selected candidate's sourced facts appear in decision analysis; card text is reference-only and may be stale or incomplete. It does not establish legality, interactions or the best move. Missing, mismatched, malformed or oversized fields are omitted and marked partial. Lookups are limited to 128 exact IDs and 24 KiB of attached catalog facts per request. `--cards-json` remains name-only, and no card database is bundled or downloaded by the app.

## Offline deckbuilding

New catalog imports include converted mana cost, rarity, set/collector number and format legalities. Catalog schema-v1 databases remain readable for replay/watch, but they lack the new deck-audit facts; reimport a local bulk file to create a v2 catalog.

```bash
python3 -m mtga_assistant deck audit --list ./deck.txt --format standard --cards-db ./data/cards.sqlite3 --json
python3 -m mtga_assistant deck audit --list ./deck.txt --format historic --cards-db ./data/cards.sqlite3 --collection-json ./collection.json
python3 -m mtga_assistant deck suggest --list ./deck.txt --format standard --cards-db ./data/cards.sqlite3 --collection-json ./collection.json --goal interaction
```

The deck list uses Arena's exported text format (`Deck`, card-count rows, and an optional `Sideboard` section). Supported catalog legality fields are Standard, Alchemy, Explorer, Historic and Timeless. The audit checks a 60-card minimum, a 15-card maximum sideboard, and the usual four-copy limit with basic lands exempt. It also summarizes colors/types/mana values and, when supplied, compares Arena-ID owned copies with common/uncommon/rare/mythic wildcard counts.

Collection input is a small local JSON file such as:

```json
{"cards":{"12345":4},"wildcards":{"common":12,"uncommon":5,"rare":2,"mythic":1}}
```

The audit output never exposes Arena IDs or input paths. It labels results `catalog_based_unverified` and shows the catalog import time; a stale catalog or missing legality field is reported as unknown. It checks composition, format metadata and collection counts.

`deck suggest` requires a schema-v2 catalog, collection export and one explicit goal: `curve`, `interaction`, `card_advantage`, `ramp`, `threats` or `synergy`. It returns a small list of legal-in-catalog additions that fit the deck's known card colors and reports exact-printing ownership plus wildcard budget. Scores rank narrow catalog/collection signals, not card strength or win rate. Text/type cues are incomplete, and results are not a complete strategy evaluation. Alternate printings are not combined in collection counts. Suggestions do not identify cards to cut or modify the deck; no local model, network service or Arena API is called.

## Offline draft picks

Obtain a 17Lands public draft CSV yourself, then import it locally. The import supports plain or gzip CSV and stores per-card selection counts and average selected pick index for one set and event type. Rows for a different set/event and rows with a second simultaneous pick are excluded. This is selection behavior, not win-rate or card-strength data. The official [17Lands public data page](https://www.17lands.com/public_datasets) describes its downloadable datasets and license; the app does not fetch them or call the 17Lands API.

```bash
python3 -m mtga_assistant draft stats import --source ./draft_data_public.SET.PremierDraft.csv.gz --database ./data/draft-stats.sqlite3 --set SET --event-type PremierDraft
python3 -m mtga_assistant draft rank --state ./current-draft.json --cards-db ./data/cards.sqlite3 --stats-db ./data/draft-stats.sqlite3 --json
```

`current-draft.json` is a local snapshot using Arena IDs from the local catalog:

```json
{"set_code":"HOB","event_type":"PremierDraft","pack_number":0,"pick_number":2,"pack":[12345,12346],"pool":[12340,12341]}
```

Pack and pick positions are zero-based in the file. The ranking only compares cards listed in `pack`; local pool colors, creature subtypes, curve and narrow role cues add visible fit reasons. Historical pick ranking is used only with 20 or more observed selections per candidate, and sample counts remain visible. In the current prototype, you supply the state file; the saved Arena logs available during development did not contain a draft pack snapshot, so live draft extraction is not implemented. The output does not claim the highest-ranked card is an optimal pick.

## Public-only opponent estimates

Opponent estimates compare revealed opponent-owned cards with decklists you provide locally. The app counts currently visible cards in Battlefield, Stack, Graveyard and Exile. If a log ambiguously lists one object in multiple public zones, that object is omitted and the state is marked partial. It never inspects opponent hand or library identities. It ranks matching reference lists by exact Arena printing overlap and shows cards present in those lists but not yet publicly observed. Reference support describes your supplied sample; it is not a probability or confirmation of the opponent's deck or hand. Deck legality, source representativeness and sideboards are not verified.

Create a manifest with a format, date for your reference set, and local Arena deck-export paths. Relative paths are resolved from the manifest file:

```json
{
  "format": "standard",
  "reference_date": "2026-10-14",
  "decks": [
    {"label": "Aggro sample", "path": "decks/aggro.txt"},
    {"label": "Midrange sample", "path": "decks/midrange.txt"}
  ]
}
```

Imports resolve each main-deck card to one exact Arena ID in the local catalog. A card with no catalog match or multiple possible printings rejects the entire import; include the set and collector number from Arena exports where available. Sideboards are ignored. The generated reference file contains labels and card counts, not source paths or raw decklists. It is published only after the complete write succeeds; choose a new output path for each import because existing files are never overwritten.

```bash
python3 -m mtga_assistant opponent import --manifest ./references/manifest.json --cards-db ./data/cards.sqlite3 --output ./data/opponent-references.json
python3 -m mtga_assistant watch --seat 1 --log ./Player.log --cards-db ./data/cards.sqlite3 --opponent-data ./data/opponent-references.json
python3 -m mtga_assistant replay --seat 1 --log ./Player.log --cards-db ./data/cards.sqlite3 --opponent-data ./data/opponent-references.json --json
```

`watch` refreshes the estimate when supported public state changes; `replay` emits one estimate for the final supported state. Estimates are deterministic, local and separate from the optional model prompt.

## Remaining stages

Feature parity is not reached. The new overlay implementation still needs native Windows and live-match verification. The current overlay focuses on Watch; deckbuilding, draft, review, opponent, voice and data workflows still need overlay integration. Richer AI deck evaluation, live draft-log extraction, a deterministic Magic rules/legal-action engine, managed card-data updates and persistent match history remain future work. The available Arena logs did not contain live pack/pick state, so draft ranking uses a supplied pack/pool snapshot. The previous desktop window was superseded and is not the primary app. The application never controls Arena.

The prior prototype's external reference links are retained for research, not as verification of the current protocol or third-party permissions:

- [Wizards logging documentation](https://mtgarena-support.wizards.com/hc/en-us/articles/360000726823-Creating-Log-Files-on-PC-Mac-Steam)
- [Manasight parser](https://github.com/manasight/manasight-parser)
- [Wizards terms](https://company.wizards.com/en/legal/terms)
