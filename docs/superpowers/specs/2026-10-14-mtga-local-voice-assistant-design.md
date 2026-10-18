# MTGA Stage 10: Local Voice Assistant

Date: 2026-10-14 (Africa/Johannesburg)
Status: Implemented in the current workspace; native audio validation remains pending, and a local-model combat hallucination is documented with a deterministic fallback.

## Purpose

Let the player ask one question by voice during a read-only live match and hear a concise spoken answer grounded in the latest supported public game snapshot and, when available, Arena-presented choices. This fills the voice interaction capability in the original assistant scope without adding game-client input or cloud speech/model services.

## User intent and constraints

- The assistant follows the local Arena log and never sends actions or input to Arena.
- The player explicitly selects the log, system seat and local Ollama model.
- Hidden opponent hand and library identities, protocol identifiers, raw log records and unrecognized prompt text stay out of model requests.
- The app does not save audio, transcripts or voice-session history.
- The player needs a direct way to stop listening and to correct a misrecognized question.
- Typed questions work in the desktop tab on platforms with Tkinter and the local Ollama service. Microphone capture and speech playback target the project's primary native Windows workflow.

## Selected approach

Use the Windows `System.Speech` APIs through a small PowerShell subprocess adapter. The current Windows profile exposes `en-US` and `ja-JP` recognition engines and three installed voices; no additional Python dependency or model download is needed. The app lists installed recognizer cultures and voices; it selects `en-US` by default when present and fills the first installed voice, both of which the player can change.

The app will not call an online speech endpoint. Recognition and speech synthesis are performed through the configured Windows speech engines; actual audio-device and offline behavior must be verified on the target Windows installation before claiming that the full voice path works there. If speech is unavailable, the user can select **Typed questions only** and submit an edited question. The model request uses the existing bounded, loopback-only Ollama transport. A packaged local speech model was considered but would add a large model/runtime setup; an online speech service was rejected because it would transmit match questions off-device.

## User flow

Add a **Voice** tab to the desktop window. It asks for the Arena log, explicit seat, optional local card catalog, local Ollama model and, when speech is used, an installed recognition language. In typed mode, the user enters a question directly. In speech mode, the user clicks **Listen for one question**; the app captures one bounded utterance and shows the recognized text in an editable field. The user can correct it, listen again, or click **Ask** to send it to the selected local model; the app shows the concise answer and speaks it when speech is enabled. The listener stops after one utterance or a short hard timeout; it never remains active between button presses. A Stop control invalidates pending work immediately and performs subprocess teardown off the Tk thread. The current transcript and answer can be cleared and are discarded when the session ends.

If native speech is unavailable or times out, the app gives a short visible status and lets the user type a question instead. There is no automatic response to Arena events. A combat question without a current Arena-presented attacker or blocker choice receives a deterministic cautious response; the local model is not asked to infer unlogged combat assignments or outcomes.

## Architecture and data flow

- `mtga_assistant.voice` owns the Windows speech adapter and bounded voice-session controller. PowerShell is launched as an argument vector with `shell=False`, no profile, a fixed script body and no user text embedded in script arguments. Recognized transcript and speech text cross the process boundary through standard streams. Subprocesses have explicit wall-clock timeouts, are cancellable, and are reaped on stop or window close.
- A `LiveStateMonitor` follows the explicitly selected log with the existing stream reader and `GameState` reducer on a background thread. It keeps the reducer's current in-memory state plus only the latest sanitized public snapshot; it never stores raw log records or an event history and does not create a second parser. Tk widgets are updated only by the UI thread through a bounded queue.
- The monitor tracks file identity, size and supported state changes. A consistent or partial state becomes `stale` after 120 seconds without a supported game-state or decision update and is refused for a new question. A replacement, truncation or unrelated telemetry record does not make stale state current; only a later supported update restores eligibility.
- Before a model request, the controller rebuilds a compact allowlisted snapshot using the same privacy boundary as live coaching, removes protocol IDs, validates a question length of at most 500 printable characters, and caps the complete request at 64 KiB. The question-aware system prompt must treat transcript text as untrusted question content, answer only from supplied public state, say when information is missing, compare only a present Arena decision, and never claim a legal move or instruct Arena automation. The answer is capped at 60 words and 600 characters before display or speech. Stop invalidates the request's session token so any late result is discarded; an already-sent local request may finish within the existing 25-second timeout.
- No state with integrity `waiting`, `uncertain`, `stale` or `ended` is sent for live strategic guidance. The UI instead says that it has no reliable active-game context. `partial` snapshots may be sent with their limitations included; the answer must state material uncertainty.
- The model endpoint remains `http://127.0.0.1:11434`; redirects and environment proxies stay disabled. No API key, cloud fallback, external speech request, automatic model download or audio file is introduced.

## Failure behavior and privacy

Missing PowerShell, missing `System.Speech`, absent audio devices, unsupported language, no recognized speech, timeout, Ollama unavailability, malformed response, stale logs and window closure all produce controlled status messages and preserve the ordinary Match/Deck/Draft/Data workflows. Stop invalidates results on the UI thread and terminates/reaps the speech process in a background teardown. A bounded reader caps subprocess stdout at 64 KiB and kills an oversized producer without saving transcript text to disk. A recognized transcript is shown only in the current session; it is sent only to the explicitly selected local Ollama endpoint after the player presses **Ask**. Audio is never written to disk, and neither transcript nor answer is saved as history.

## Testing and validation

Synthetic tests must cover Windows-platform gating, installed-language and voice parsing, fixed subprocess arguments, no shell use, timeout and cancellation, process reaping, bounded speech output capture, transcript bounds, hidden-state and protocol-ID exclusion, question-aware prompt injection boundaries, request/answer size limits, refusal on waiting/uncertain/stale/ended state, partial-state uncertainty, typed-only operation, combat fallback without an Arena choice, one-request-at-a-time behavior, UI-thread queue bounds, stale answer suppression, stop/close behavior and unchanged source-log bytes. Mock only the PowerShell process and local Ollama transport; tests must exercise the production sanitizers and session state transitions.

Run the full unit suite and compile check. On Windows, perform a manual smoke using a real microphone and speaker: start a session against a temporary synthetic or user-approved log, ask one question, confirm visible transcript and answer, confirm speech output, cancel a second listen, and verify that no audio/transcript artifact or log change was created. Report mocked tests, Windows audio smoke, local-model generation and any real-match validation as separate results. Until the Windows audio smoke succeeds, voice remains implemented-but-unverified (if code is later completed) and must not be described as usable.

## Scope exclusions

This stage does not add always-on listening, wake-word activation, arbitrary microphone recording, audio-file import/export, cloud services, automatic seat detection, Arena input automation, a full Magic rules engine, complete legal-action enumeration, hidden-card inference, persistent transcript/history storage or speech input/output on Linux/macOS. Richer deck evaluation and live draft-log extraction remain separate capability stages because current local logs contain account collection fields but no live pack/pick state.
