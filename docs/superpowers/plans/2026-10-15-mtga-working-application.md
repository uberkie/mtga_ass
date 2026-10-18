# MTGA Stage 1: Timed Live Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Connect the current sanitized Arena state to a ready local model and show a valid decision response in the desktop Match tab within the five-second application target.

**Architecture:** Keep the Arena observer read-only and the existing CLI/GUI subprocess boundary. Add an explicit loopback Ollama preload/readiness operation, pass the compact current snapshot with validated decision analysis, prioritize current decisions over the ordinary explanation throttle, and report state-to-response latency with a hard bounded timeout.

**Tech Stack:** Python 3.10+, standard library `urllib`, `threading`, `tkinter`, current local Ollama API, `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-15-mtga-working-application-design.md`.

## Global Constraints

- Arena observation remains read-only; the application sends no input to the Arena client.
- Model calls use only `http://127.0.0.1:11434`; no cloud fallback, credentials, or global Ollama configuration changes.
- Model input is the validated decision analysis plus `compact_live_snapshot(snapshot)` and never raw logs, protocol IDs, paths, account data, or hidden card identities.
- Request size remains at most 64 KiB; context is bounded to 8,192 tokens; decision output is bounded to 96 generated tokens. The hard deadline is five seconds from CLI decision recognition through Match-widget insertion, the measurable GUI endpoint. The model inference cutoff is four seconds from that same timestamp, reserving the final second for validation, subprocess delivery, and GUI insertion. Late results are discarded.
- A response is usable only for the exact current snapshot revision/fingerprint and retains `legal_status: unverified`.
- Live `watch` submits model work only for a validated current Arena-presented decision. This prevents an ordinary free-form explanation from occupying the single worker when a timed decision arrives; deterministic state continues to be emitted on all updates. Other existing explanation workflows retain their current behavior.
- Do not start any inference/benchmark or model download until the current GPU, RAM, disk, Ollama, model, and concurrent-work preflight is recorded and passes.
- Stage 12 and all other feature work remain deferred until this milestone passes.
- The checkout has no commit history and all project files are untracked; do not initialize history, create commits, or stage unrelated user files.

## Review Focus

- A decision result can be delayed by the ordinary 30-second worker interval; pin immediate submission for current Arena decisions in Task 2.
- A snapshot could be stale or contain hidden protocol identifiers; pin exact revision binding and sanitizer behavior in Task 2.
- Ollama may be reachable while the selected model is not loaded; pin `/api/ps` readiness and a load-only request in Task 1.
- UI output could look actionable while warming, timed out, or unavailable; pin the three readiness states and clear stale output in Task 3.
- A candidate can be faster but fail structured responses or need CPU offload; benchmark only after preflight and record response validity and VRAM residency in Task 4.

---

### Task 1: Add local model readiness and preload

**Files:**
- Modify: `mtga_assistant/ollama.py`
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_ollama.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Add `ollama_status(model: str, timeout: float = 2.0) -> dict[str, Any]` for loopback `/api/version`, `/api/tags`, and `/api/ps`; expose only service version, whether the exact model is installed/loaded, and loaded VRAM/total sizes.
- Add `preload_model(model: str, timeout: float = 90.0, num_ctx: int = 8192) -> dict[str, Any]`; POST an empty prompt to `/api/generate` with `stream: false`, `keep_alive: -1`, and `options.num_ctx`, then verify the exact model appears in `/api/ps`. Never generate a warm-up answer.
- Add `unload_model(model: str, timeout: float = 2.0) -> None`; unload with an empty prompt and `keep_alive: 0`.
- Add CLI `model status --model NAME`, `model warm --model NAME`, and `model unload --model NAME`, returning bounded JSON for `--json` and content-safe text otherwise. `warm` prints `Warming` before the request and `Ready` only after `/api/ps` confirms residency.
- If `watch` receives `--model NAME`, verify the exact model is already loaded before following logs; fail with a preload instruction if it is not. Watching without a model remains available.

- [x] **Step 1: Add failing API tests** for loopback-only status endpoints, missing/uninstalled model, empty-prompt preload/unload payloads, explicit keep-alive/context, service unavailable, timeout, malformed responses, and readiness only when `/api/ps` includes the exact selected model. Add CLI tests that model-backed watch refuses an unready model before opening the stream.
- [x] **Step 2: Run the focused tests** and confirm the helpers/CLI action are absent.
- [x] **Step 3: Implement readiness and preload** with strict response allowlists and bounded timeouts.
- [x] **Step 4: Run `python3 -m unittest tests.test_ollama tests.test_cli -v`.** Expect all readiness/preload and existing inference tests to pass.

### Task 2: Send current state and prioritize timely decisions

**Files:**
- Modify: `mtga_assistant/ollama.py`
- Modify: `mtga_assistant/__main__.py`
- Test: `tests/test_ollama.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Change `compare_decision(analysis, model, snapshot, timeout, ...) -> dict[str, Any]` to include the validated analysis and a fresh `compact_live_snapshot(snapshot)` in its prompt. Keep the exact JSON response schema and 64 KiB limit; pass `options.num_ctx=8192`, `num_predict=96`, `keep_alive=-1`, and `think=false`.
- Extend `ExplanationWorker.submit` with an optional monotonic `observed_at` supplied by the CLI immediately when it recognizes a supported current decision, before analysis and submission work. Extend `ExplanationResult` with `elapsed_ms: int | None`; measure observed-decision to model completion, including analysis and queueing before the call starts. The end-to-widget-insertion deadline is `observed_at + 5s`; inference cutoff is `observed_at + 4s`. The model call receives only the remaining time before the inference cutoff (minus elapsed pre-inference work); if no inference budget remains, do not call the model. Discard results completing at or after four seconds. A result completed before four seconds may be validated, transported, and inserted during the reserved final second if its revision remains current; expire it if insertion would occur at or after five seconds. Decision submissions bypass `interval` only when a valid current Arena decision is submitted. Ordinary explanations keep the existing interval behavior.
- During live `watch`, submit to the worker only when a supported Arena-presented decision exists and validates; do not launch ordinary free-form explanations in this mode, so no slow non-decision call can occupy the single worker ahead of a timed choice. Preserve existing model behavior for demo/replay and other workflows.
- Add an optional desktop-event mode for Match watch. Emit bounded newline-delimited control events with state revision and, for a decision result, the absolute monotonic recognition/deadline values and rendered unverified text. The event's integer revision is exactly the snapshot `revision` used by the worker's analysis/fingerprint binding. The desktop reader consumes protocol records without displaying their encoding. On a newer state event, clear prior decision advice. Immediately before Tk inserts a result, check that its revision is the latest and `time.monotonic() < deadline_at`; otherwise drop it. The CLI and GUI share the host monotonic clock. Keep ordinary readable output for all other CLI users.
- Keep the existing revision/fingerprint binding. A stale, over-deadline, or invalid result must not be surfaced.

- [x] **Step 1: Add failing tests** proving the exact sanitized snapshot and analysis arrive together, hidden-card and Arena-ID sentinels are absent, request bounds/options are exact, a decision bypasses a long interval, ordinary explanation still throttles outside live watch, live watch skips free-form calls, elapsed time begins at the supplied decision-recognition timestamp, remaining inference budget subtracts pre-inference work, no call begins after the four-second cutoff, and stale/late results do not replace current advice.
- [x] **Step 2: Run `python3 -m unittest tests.test_ollama tests.test_cli -v`** and confirm the new assertions fail.
- [x] **Step 3: Implement the prompt/runtime changes** without loosening the response schema or privacy projection.
- [x] **Step 4: Run focused tests** and confirm all new and existing decision-worker tests pass.

### Task 3: Expose model readiness and latency in the desktop Match tab

**Files:**
- Modify: `mtga_assistant/desktop.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- Add Match-tab **Preload model** and **Unload model** actions wired to `python -m mtga_assistant model warm|unload --model NAME`; require a nonempty model name, and leave the existing watch/replay command interface intact. Start watch with desktop-event mode enabled.
- Track Match model status as `Warming`, `Ready`, or `Unavailable` from the bounded child output. Consume control events separately from readable output, clear prior advice on each newer state revision, and insert a result only when its revision matches the latest state and its transmitted monotonic deadline has not expired. Keep status and the last current result visible in the existing Match output area.
- Continue to run all Tk updates on the UI thread; the existing runner queue and output bounds remain in force.

- [x] **Step 1: Add failing desktop tests** for required model validation, exact warm command construction, status transitions, result latency text, unavailable/timeout display, protocol decoding across partial chunks, acceptance of a valid pre-four-second response inserted during the reserved second, rejection at/after the five-second deadline, and clearing/rejection after a newer revision.
- [x] **Step 2: Run `python3 -m unittest tests.test_desktop -v`** and confirm the new behavior is missing.
- [x] **Step 3: Implement the Match-tab warm/status flow** using the existing command runner and output area.
- [x] **Step 4: Run `python3 -m unittest tests.test_desktop -v`.** Expect all Match and unrelated desktop tests to pass.

### Task 4: Verify real model timing and finish the milestone evidence

**Files:**
- Modify: `docs/validation.md`
- Modify: `README.md`
- Test: `tests/test_ollama.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_desktop.py`

**Interfaces:**
- No new product interface. Use the model warm/status CLI and existing desktop Match watch command from Tasks 1–3.
- Use synthetic Arena-shaped public state with a visible hand/board and an Arena-presented choice. Do not print or persist raw model prompts or generated responses; record only model name/digest, preflight summary, cold-load seconds, warm latency samples, response-validity counts, GUI smoke result, and hashes/aggregate counts for any real-log parser check.

- [x] **Step 1: Run deterministic validation** with `python3 -m unittest discover -s tests -v` and `python3 -m compileall -q mtga_assistant tests`; 346 tests passed and compilation succeeded.
- [x] **Step 2: Re-run resource preflight immediately before inference**: exact 4B model was installed/loaded at context 8,192; 3,438 MiB GPU memory free, 20,932,419,584 bytes RAM available, 869,785,763,840 bytes disk free, Ollama 0.35.0, and no other GPU compute processes.
- [x] **Step 3: Benchmark one model at a time** on the identical synthetic decision. The 4B cold load took 12.56 seconds; ten warm calls were valid with median 422.5 ms and maximum 625 ms. The 2B candidate produced nine valid responses out of ten, with a median 328.5 ms among successes and one 4,005 ms timeout; see the validation report for details.
- [x] **Step 4: Run the timed desktop Match integration** under an available display. All ten event-supply-to-visible-widget samples passed; times were 712–1,015 ms, median 1,013 ms. A separate historical partial-state mulligan inference also appeared in the Tk widget in 870 ms; it is not counted as a live latency sample.
- [x] **Step 5: Check real-log compatibility without exposing contents** by replaying the existing local Arena log through deterministic parsing and recording only hashes, event/diagnostic counts, supported-state coverage, and whether a decision was present. The current log had no game-state update; the historical log remained byte-for-byte unchanged and yielded a supported two-choice decision for the model/UI smoke. Replay time was not used as live latency evidence.
- [x] **Step 6: Update README and `docs/validation.md`** with exact current evidence and limitations.
