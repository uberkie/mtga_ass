# MTGA Read-Only Game Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the primary Windows MTGA assistant a compact read-only overlay attached to Arena and display current validated match state and model advice there.

**Architecture:** Add a Windows-only `ctypes` Arena window tracker and a compact Tkinter overlay that consumes the existing CLI child-process events and `CommandRunner`. Keep game parsing, seat resolution, privacy projection, inference and deadlines in the existing CLI; route the primary `overlay` and compatibility `gui` commands to the new overlay.

**Tech Stack:** Python 3.10+, standard-library Tkinter, `ctypes`, `subprocess`, `threading`, and `queue`; no new third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-10-18-mtga-game-overlay-design.md`.

## Global Constraints

- The primary UI is an external Windows game overlay, not a standalone desktop application window.
- Arena integration remains read-only: no DLL injection, graphics/input hooks, clicks, keys or game actions.
- The overlay consumes only bounded, validated child events and never parses raw log text.
- Hidden card identities, protocol IDs, account names and raw logs stay out of model prompts and overlay content.
- Full game details appear only while Arena is foreground; other foreground apps see only a privacy-safe status/Stop strip.
- Preserve current model readiness checks, 4,096-token context, and existing inference/display deadlines.
- Do not claim native overlay or live-model success without a real Windows graphical/live-match observation.
- Do not run a test unless 100% confident the code is correct; if that condition is not met, stop and report the confidence status.

## Review Focus

- Arena missing, minimized, or replaced by another process must not expose stale game details.
- Alt-tab and overlay foreground transitions must not hide the Stop control or reveal hand/advice outside Arena.
- Arena process/window query failures must degrade to a bounded waiting/unavailable status.
- Model list, preload, watcher start, watcher stop and release failures must preserve readable state and recoverable controls.
- Newer game revisions and expired model responses must clear or suppress older advice using the existing validator/deadline contract.

## File Responsibilities

- Create `mtga_assistant/overlay.py`: Win32 window discovery/tracking, compact overlay UI, focus/privacy behavior and live Watch lifecycle.
- Reuse `mtga_assistant/desktop.py`: safe command builder, bounded child runner, event decoder/validator and output helpers; do not launch its old multi-tab window.
- Modify `mtga_assistant/__main__.py`: add `overlay` command and route `gui` to the same overlay implementation.
- Modify `README.md` and `docs/validation.md`: make overlay instructions primary and state native/live validation separately.
- Keep deck, draft, review, voice and catalog features in the eventual scope; their overlay panels follow only after the working live overlay milestone.

## Tasks

### Task 1: Arena window tracking and overlay shell

**Files:**
- Create: `mtga_assistant/overlay.py`

**Interfaces:**
- `ArenaWindowState(hwnd: int, pid: int, bounds: tuple[int, int, int, int], foreground: bool)` is an immutable snapshot of the Arena top-level window.
- `ArenaWindowTracker.find() -> ArenaWindowState | None` identifies `MTGA.exe` by executable path, reads its top-level bounds, and reports whether Arena owns foreground focus.
- `OverlayWindow` creates a compact borderless topmost Tk window, follows the Arena bounds, is draggable/collapsible, and changes to a privacy-safe status/Stop strip when Arena is not foreground.
- `launch_overlay() -> int` returns a controlled unavailable status outside native Windows or when Tk/Win32 setup fails.

- [ ] Implement bounded Win32 process/window discovery with `EnumWindows`, `GetWindowThreadProcessId`, `OpenProcess`/`QueryFullProcessImageNameW`, `GetWindowRect`, `GetForegroundWindow` and guaranteed handle cleanup.
- [ ] Implement a compact Tk shell positioned near Arena's top-right bounds; set borderless/topmost styles without activating or sending input to Arena.
- [ ] Implement drag, collapse, focus-state transition, privacy-safe inactive strip, wait-for-Arena view, and orderly close behavior.
- [ ] Read the new module after editing; no compile or test command is permitted unless the stated confidence condition is met.

### Task 2: Live Watch controls and validated match display

**Files:**
- Modify: `mtga_assistant/overlay.py`

**Interfaces:**
- Reuse `CommandRunner`, `build_command`, `start_workflow`, `_validated_desktop_event`, and `bounded_output` from `mtga_assistant.desktop`.
- Reuse `list_installed_models`, `load_match_model`, `save_match_model`, and `default_arena_log` for model list, remembered selection and log path.
- The overlay's model list is populated from the local Ollama list endpoint. Starting Watch warms and verifies the selected model before starting `watch`; Watch remains read-only.

- [ ] Add local model refresh/selection, remembered model, automatic/manual seat selection, log-path display, Start Watch and Stop controls.
- [ ] Display the validated match summary, current Arena choices, deterministic observations, model readiness and current revision-bound AI result in the overlay.
- [ ] Clear advice on newer revisions, reject stale/expired result events, and show partial/unknown evidence without fabricating card identity or legal actions.
- [ ] Stop and reap the CLI child on Stop/close, then use the existing model unload command and report whether release was verified.
- [ ] Read the edited module and confirm every UI update is scheduled on the Tk thread; no model request or test run is part of source review.

### Task 3: Primary CLI routing and user instructions

**Files:**
- Modify: `mtga_assistant/__main__.py`
- Modify: `README.md`
- Modify: `docs/validation.md`
- Reference: `AGENTS.md`, `docs/superpowers/specs/2026-10-18-mtga-game-overlay-design.md`

- [ ] Add `overlay` as the primary UI action and retain `gui` only as an alias to the exact same overlay launcher.
- [ ] Replace standalone desktop-window launch instructions with Windows overlay launch, Arena borderless/windowed requirement, privacy/focus behavior, model readiness and Stop behavior.
- [ ] Record only source-level findings in validation; mark Windows graphical attachment, live match state and model timing unverified until observed.
- [ ] Perform a final source read-through of routing, launch failures, privacy state transitions and cleanup. Do not claim feature parity or runtime success from documentation or static inspection.

## Validation Boundary

The user's confidence condition controls test execution. This plan does not authorize model inference or benchmark runs. The full working-overlay gate requires native Windows evidence of (1) attachment to an actual Arena window, (2) correct focus/privacy transitions, (3) current real match state rendered, (4) a response from the selected resident model rendered before the existing deadline, and (5) stop/close cleanup. Until those are observed, the overlay is implemented but the application is not verified as working in-game.
