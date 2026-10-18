# MTGA Stage 7: Local Desktop Interface

> **Superseded UI target (2026-10-18):** The user now requires a game overlay rather than a standalone desktop app. Keep this document as historical scope for the prior Tkinter window; use `2026-10-18-mtga-game-overlay-design.md` for the active UI direction.

## Goal

Make the implemented read-only assistant workflows usable from a local desktop window, without requiring users to remember CLI syntax. The window wraps the existing commands; it does not expand game-state or coaching authority.

## User intent and constraints

The project brief calls for a complete assistant with live match coaching, post-game review, deckbuilding, draft support, voice and a desktop interface. Current features are exposed through command-line commands. The requested boundary is local and read-only: follow Arena logs, never send game actions, and keep hidden card identities out of output and model prompts. The UI must carry forward the CLI's explicit-seat and unverified-advice limits.

## Selected approach

Use Python's standard `tkinter`/`ttk` desktop widgets and launch the existing CLI as a child process. This avoids adding a GUI dependency, keeps business logic in the already-tested CLI, and works with the Python installations already used by this project. A native `PySide6` frontend would offer more styling control but would add a large third-party dependency and duplicate little capability in this first desktop slice.

## Interface and workflows

The window has four workflow tabs plus one shared output panel:

- **Match:** watch a log, replay it once, or run post-game review. The user supplies the log path and chooses seat 1 or 2. A local card catalog and Ollama model are optional. Watch provides an explicit Stop button.
- **Deck:** audit a deck or request bounded catalog candidates. Inputs include deck export, format and catalog; collection is optional for audit and required for suggestions; suggestion goal is explicit.
- **Draft:** import a user-obtained 17Lands CSV into a local stats database, or rank a supplied pack/pool snapshot against the local card catalog and optional stats database.
- **Data:** import a user-obtained card JSONL/gzip file into a local SQLite catalog.

The shared output panel displays child-process text, offers Clear, and retains only the most recent 1,048,576 Unicode characters. One workflow may run at a time. File inputs are selected with native file dialogs or typed paths. No path, log content, model response, command history or account data is persisted by the UI.

## Architecture and data flow

Add `mtga_assistant.desktop` with a pure command builder and a small Tkinter window. The command builder accepts a workflow and validated form values and returns an argument list beginning with the current Python executable and `-m mtga_assistant`; paths with spaces or shell metacharacters remain single arguments. The runner launches with `shell=False`, merges stdout/stderr, sets Python child output to UTF-8, reads it with a matching incremental decoder on a worker thread, and passes bounded messages through a queue. Each UI poll combines queued text into one bounded widget update. Tk widgets are touched only on the UI thread. Closing the window stops and waits for its child process, then terminates it if needed.

Add a `gui` CLI action that lazily imports and launches the desktop module. Importing the package or running any other command must not initialize Tk. If Tk is missing or no display is available, show a concise controlled error and nonzero exit rather than a traceback.

All data interpretation and privacy rules remain in the CLI's existing handlers. The UI starts only supported local commands; it does not offer arbitrary command entry, network access, client injection, keyboard/mouse control, or log/report history. The local Ollama option remains opt-in and uses the existing loopback-only integration.

## Failure behavior

Reject missing required fields before process launch, especially an absent/invalid seat for real match workflows. Disable Run controls while a child is active. Display bounded process output and a short safe status for launch failures. If the child exits, show its exit status; if stopped by the user, report that it stopped. If output exceeds the display cap, discard oldest displayed text and retain a visible truncation marker. Closing the window reliably reaps its own child process.

## Testing and validation

Unit tests cover every workflow's argument construction, required fields, valid enumerations, paths containing spaces and shell metacharacters, and the guarantee that no shell string is used. Runner tests cover output capture, nonzero exit, UTF-8 output under legacy parent encoding, stop/termination and output bounds. UI tests cover one widget update for a full queued-output backlog. GUI launch tests patch Tk initialization so they run headlessly; where a display is available, perform a manual window smoke that starts and stops the synthetic demo/watch against a temporary fixture. Existing full test and compile checks remain required.

## Scope exclusions

This stage does not implement voice, opponent prediction, automatic seat detection, live draft extraction, richer strategy evaluation, new card/rules data, persistent match history, client control or verified legal moves. Those remain separate capability stages. The desktop window only exposes current features and their limits.
