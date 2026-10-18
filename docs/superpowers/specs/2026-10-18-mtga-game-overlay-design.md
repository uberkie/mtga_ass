# MTGA Read-Only Game Overlay

Date: 2026-10-18 (Africa/Johannesburg)
Status: Active UI direction supplied by the user.

## Purpose and success criteria

Replace the standalone multi-tab desktop window as the primary live assistant with a compact overlay that appears over Magic: The Gathering Arena. During a supported live decision, the existing observer and model pipeline must deliver the current sanitized game state and display its bounded response in this overlay while the state is current and within the existing decision/display deadlines. The UI must remain explicit that strategic guidance is unverified and that unsupported or incomplete evidence limits what can be said.

The full project scope remains live match coaching, public-information-only opponent analysis, rules-grounded advice, deckbuilding, draft assistance, post-game review and voice interaction. The working-match state-to-model-to-overlay path remains the first milestone. After that milestone is established, migrate the other approved capabilities into the overlay rather than restoring a standalone desktop dashboard.

## User intent and constraints

- The primary UI is a game overlay, not a separate desktop application window.
- Arena integration is read-only. The application must not inject code or DLLs, hook graphics or input, send clicks or keys, or automate a game action.
- The existing local log parser and event pipeline remain authoritative. Do not capture screenshots or inspect pixels to infer card identities or choices.
- Keep opponent hidden cards, raw log text, protocol IDs, account names and filesystem paths out of model prompts and rendered advice.
- Keep local Ollama on loopback, display warming/ready/unavailable state, and discard stale or expired responses.
- The overlay is an external native window. Arena must run windowed or borderless-windowed; exclusive fullscreen is not promised to display an external overlay.
- Use no new third-party runtime dependency for this first overlay slice.

## Selected approach

Build a Windows-only external overlay window with the project's existing Tkinter runtime and the Windows API through Python's standard-library `ctypes`. This keeps the existing Python and no-dependency deployment path while supporting a borderless, topmost window that can track the Arena window bounds. Do not use DirectX injection, graphics hooks or a browser-hosted overlay.

Keep the existing CLI and its sanitized child-process events as the source of match data. Add a compact overlay view that consumes the existing `CommandRunner` messages and validated `match_summary`, `decision_progress`, `model_status` and `decision_result` events. The overlay does not parse raw log text itself and does not change the meaning of decision analysis.

## Overlay behavior

- Launch with `python -m mtga_assistant overlay`; `gui` remains a compatibility alias to the same overlay. The former standalone multi-tab window is not the primary launch target.
- Locate the top-level Arena process by its executable identity, track its window rectangle and foreground status, and position the overlay near the top-right of the Arena window. Make it draggable and collapsible so the player can move it away from important game areas.
- While Arena is foreground, show the match summary, life totals, phase/turn, supported Arena-presented choices, deterministic observations, model readiness and current model advice. Clear advice when a newer state supersedes it.
- When Arena loses foreground, replace game details and advice with a small privacy-safe status bar. Keep Stop available there so the watcher and resident model can be stopped without exposing match information over another application.
- If Arena is not found, show a compact waiting state. If it closes during Watch, keep the watcher state accurate and allow the user to stop and release the model.
- Provide a local model drop-down populated from Ollama's list endpoint, a seat selector with the existing exact identity auto-detection and manual seats, and Start/Stop controls. Never require retyping an installed model tag on each launch.
- The overlay never presents an unsupported choice as a legal action. Partial hand identities may be discussed only as unknown; the model cannot infer their names or effects.
- Closing the overlay stops and reaps its child process and requests the existing verified model unload path.

## Architecture and boundaries

Add `mtga_assistant.overlay` as the primary overlay entry point. A Windows window tracker uses `EnumWindows`, process-image lookup, foreground-window checks and window bounds through `ctypes`; it returns only the matched Arena window handle, bounds and active state. It does not read process memory or interact with the Arena process beyond querying its top-level window identity.

The overlay uses the existing `desktop.CommandRunner`, command builder, bounded event decoder/validator, Ollama model-list/readiness commands and saved model preference. Tk widgets are updated only on the Tk thread. The CLI child continues to own log reading, game state, identity checks, deterministic observations, privacy projection and inference. The overlay receives only validated bounded events.

`gui` and `overlay` dispatch lazily import the overlay module. Non-UI CLI commands remain usable headlessly on Windows and other supported Python platforms. Attempting to launch the overlay outside native Windows returns a short controlled message; it does not initialize Tk or change CLI behavior.

The former `desktop.py` multi-tab window is retained only as historical implementation and reusable runner utilities while the overlay milestone is built. It must not remain the default user-facing application. Later feature work must put the approved deck, draft, review, voice and data workflows in overlay-owned panels or flows.

## Failure behavior

- If Windows APIs, Arena window discovery or Tk are unavailable, show a bounded user-facing error without a traceback.
- If Ollama or the exact selected model is unavailable, keep deterministic state visible and label model help unavailable; never describe an installed but unloaded model as ready.
- If event parsing, identity confirmation, state continuity, response validation or deadlines fail, suppress the stale/unsafe advice and display the specific safe status.
- If Arena is out of focus, hide game-specific contents while retaining only the small status/Stop strip.
- If Watch cannot stop or model release cannot be verified, report that status and keep the controls available for recovery.

## Validation and evidence

No test, compile check, model inference or benchmark is authorized by this design unless the user's current confidence condition is met. Before any model-backed runtime action, recheck GPU memory/use, RAM, disk, Ollama reachability, exact selected model installation/residency and the configured 4,096 context.

Source review can establish that the UI routes through the existing bounded event contract and contains no game-input API calls; it cannot establish Windows window attachment, foreground hiding, overlay visibility over Arena, or model response time. Those require separate Windows graphical and live-match evidence. Do not claim the application works in-game until an actual Arena window shows current state and a current model response in the overlay within the active deadline. Report parser compatibility, synthetic behavior, model latency and native Windows overlay behavior separately.

## Supersession

This design supersedes the standalone Tkinter desktop-window target in `2026-10-14-mtga-desktop-interface-design.md` and the desktop-tab UI target in `2026-10-15-mtga-working-application-design.md`. It does not supersede the read-only, privacy, model preflight, evidence-grounding or complete feature-parity requirements in those documents.
