# MTGA Local Voice Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one-question-at-a-time typed or spoken questions and concise local answers to the desktop assistant, grounded in the current sanitized Arena state; Windows speech remains optional.

**Architecture:** Add a fixed PowerShell/System.Speech adapter, a background read-only state monitor using the existing reducer, and a question-aware local Ollama request. A Voice tab coordinates these pieces through worker threads and bounded UI queues; no audio, transcripts or history are persisted.

**Tech Stack:** Python 3.10+, standard library, Tkinter, PowerShell `System.Speech`, existing local Ollama endpoint; no new Python dependencies or model downloads.

**Spec:** `docs/superpowers/specs/2026-10-14-mtga-local-voice-assistant-design.md`.

## Global Constraints

- Typed questions work wherever Tkinter and local Ollama are available. Microphone input and spoken answers require native Windows speech.
- Capture one utterance only after the user presses Listen; no continuous listening or automatic game-triggered response.
- Use the explicit log path and seat; never send actions or input to Arena.
- Do not save audio, transcripts, answers or session history.
- Send only allowlisted public snapshots and a user-submitted question to `http://127.0.0.1:11434`; no proxy, redirect, cloud fallback, API key or automatic download.
- Refuse model guidance when integrity is `waiting`, `uncertain`, `stale` or `ended`; carry `partial` limitations into the prompt and response. Voice state is stale after 120 seconds without a supported game-state or decision update; unrelated file growth does not restore stale state.
- Bound the question to 500 printable characters, request to 64 KiB, and spoken answer to 60 words / 600 characters.
- Keep Tk access on the UI thread; bound inter-thread queues; stop and reap speech processes on cancellation or close.
- Do not create an initial Git commit in this unborn checkout.

## Post-review hardening (2026-10-14)

- [x] Mark a consistent or partial snapshot stale after two minutes without a supported update; refuse new questions until a later supported update arrives.
- [x] Allow typed-only sessions without installed speech options and enable the Start control on platforms without native speech.
- [x] Carry the snapshot revision to the Tk thread and discard an answer if the current state changed before display.
- [x] Move speech-process and log-monitor teardown off the Tk thread while invalidating the session immediately.
- [x] Use a bounded speech-output reader that kills an oversized subprocess without writing transcript text to disk.
- [x] Return a deterministic cautious answer for combat questions without an Arena-presented attacker or blocker choice after local-model inference invented an unsupported result.
- [x] Re-run the full test suite, compile check and live Tk construction smoke; record actual local-model and Windows audio evidence separately.

## Files

- Create `mtga_assistant/voice.py` for the Windows speech adapter, live-state monitor and session controller.
- Modify `mtga_assistant/ollama.py` for question-aware voice answers using the existing snapshot sanitizer and transport.
- Modify `mtga_assistant/desktop.py` to add the Voice tab and bounded worker coordination.
- Modify `tests/test_voice.py`, `tests/test_ollama.py`, and `tests/test_desktop.py` for speech, state, privacy and UI behavior.
- Modify `README.md`, `docs/validation.md`, and `.superpowers/sdd/2026-10-14-mtga-local-voice-assistant/progress.md` for usage and evidence.

## Review Focus

- Transcript, hidden-card and protocol-ID sentinels never reach model prompts except the visible user-entered question itself; pin with sanitizer and question-payload tests.
- No question is accepted without an active session, a usable snapshot and an explicit user action; pin waiting/uncertain/ended and stopped-session tests.
- A long or silent recognition attempt always times out and reaps the PowerShell child; pin timeout/cancel lifecycle tests.
- A late model or speech result after Stop cannot update the UI or play; pin generation-token/session-cancellation tests.
- Large or rapid worker output cannot freeze Tk or exceed the transcript/answer/queue bounds; pin bounded-queue and one-update-per-poll tests.

## Tasks

### Task 1: Question-aware local answer function

**Files:**
- Modify: `mtga_assistant/ollama.py`
- Modify: `tests/test_ollama.py`

**Interfaces:**
- Add `answer_question(snapshot: dict[str, Any], question: str, model: str, timeout: float = 25.0) -> str`.
- Reuse `_compact_live_snapshot` and the existing loopback-only `urlopen` boundary.

- [x] Write failing tests for the 500-character question bound, controlled printable text, 64 KiB request cap, current snapshot sanitization, hidden/protocol ID sentinels, prompt instructions, answer word/character caps, and transport timeout/model validation.
- [x] Run `python3 -m unittest tests.test_ollama` and confirm the new API is absent or the new expectations fail.
- [x] Implement a separate question-aware system prompt that treats the transcript as untrusted user content, follows the current-choice-only rule, and returns only concise answer text.
- [x] Re-run `python3 -m unittest tests.test_ollama`; confirm all focused tests pass.

### Task 2: Bounded Windows speech adapter

**Files:**
- Create: `mtga_assistant/voice.py`
- Create: `tests/test_voice.py`

**Interfaces:**
- Add `SpeechAdapter.available() -> bool`, `SpeechAdapter.options() -> SpeechOptions`, `SpeechAdapter.listen_once(culture: str, timeout_seconds: float = 12.0) -> str | None`, and `SpeechAdapter.speak(text: str, voice_name: str | None = None, timeout_seconds: float = 15.0) -> None`.
- Add immutable `SpeechOptions` with allowlisted recognizer-culture IDs and installed voice names, plus `VoiceUnavailableError` for controlled failures.

- [x] Write failing tests for native-Windows gating, bounded parsing of recognizer/voice JSON, fixed PowerShell argv with `shell=False`, culture/voice allowlisting, transcript sanitization, hard timeout, stop/terminate/kill/reap, speaker output bounds, and no audio-file creation.
- [x] Run `python3 -m unittest tests.test_voice` and confirm the missing adapter behavior fails as expected.
- [x] Implement a fixed no-profile PowerShell bridge that exchanges JSON over standard streams; never interpolate transcript or answer text into PowerShell source/arguments.
- [x] Re-run `python3 -m unittest tests.test_voice`; confirm all focused tests pass.

### Task 3: Read-only live state monitor and cancellable answer controller

**Files:**
- Modify: `mtga_assistant/voice.py`
- Modify: `tests/test_voice.py`

**Interfaces:**
- Add `LiveStateMonitor(log_path: Path, seat: int, card_names: Mapping[int, str] | None = None).start() -> None`, `current_snapshot() -> dict[str, Any] | None`, and `stop(timeout_seconds: float = 2.0) -> None`.
- Add `VoiceSessionController.start(log_path: Path, seat: int, model: str, culture: str | None, voice_name: str | None = None, cards_db: Path | None = None) -> None`, `listen_once() -> str | None`, `answer(question: str) -> VoiceAnswer | None`, `speak(answer: VoiceAnswer) -> None`, and `stop(*, wait: bool = True) -> None`; `VoiceAnswer` carries the session generation and snapshot revision so late or stale results are discarded.

- [x] Write failing tests for current sanitized snapshot refresh, log immutability, selected-seat use, match/reset/integrity propagation, no raw-record retention, no requests for waiting/uncertain/ended state, partial-state warnings, single in-flight question, explicit question submission, and stale-result rejection after cancellation.
- [x] Run the focused monitor/controller tests and verify they fail before implementation.
- [x] Implement one background `follow_records` monitor using `GameState.consume_record`, retaining only the latest public snapshot under a lock. Use bounded queues and generation tokens for question work; discard late answers after stop.
- [x] Re-run `python3 -m unittest tests.test_voice`; confirm monitor/controller tests pass.

### Task 4: Voice tab and UI-thread coordination

**Files:**
- Modify: `mtga_assistant/desktop.py`
- Modify: `tests/test_desktop.py`

**Interfaces:**
- Add a Voice tab with log, seat, optional card catalog, local model and installed language/voice selectors; Start, Listen, editable Transcript, Ask, Stop and Clear controls; visible status and answer.
- Keep one active desktop workflow. Voice callbacks enqueue bounded status/transcript/answer messages; Tk widgets are touched only from the existing UI poll.

- [x] Write failing headless UI tests for Windows-unavailable status, voice form validation, button state transitions, transcript correction and explicit Ask dispatch, worker-thread-to-UI queueing, queue bounds, Stop/close cancellation and no persistent transcript fields.
- [x] Run the focused desktop voice tests and verify they fail before implementation.
- [x] Implement the Voice tab using the existing Tk patterns, controller and UI-thread poll; disable conflicting workflows during an active voice session.
- [x] Re-run `python3 -m unittest tests.test_desktop`; confirm all UI tests pass without requiring Tk display or audio hardware.

### Task 5: Documentation and validation

**Files:**
- Modify: `README.md`
- Modify: `docs/validation.md`
- Create: `.superpowers/sdd/2026-10-14-mtga-local-voice-assistant/progress.md`

- [x] Document native Windows launch, explicit log/seat/model selection, one-question flow, transcript correction, privacy, local-only model use, unsupported-platform behavior and unverified strategy limits.
- [x] Run `python3 -m unittest discover -s tests` and `python3 -m compileall -q mtga_assistant tests`.
- [x] If a Windows microphone and speaker test is available, smoke one question and one cancellation against a temporary/synthetic log; confirm unchanged log bytes and no saved audio/transcript. Otherwise record this runtime test as pending.
- [x] Record synthetic tests, local-model inference, System.Speech/audio-device smoke and real-match validation as separate results.
